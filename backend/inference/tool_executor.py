from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from backend.inference.tool_calls import ToolCallRecord
from backend.tools.registry import ToolRegistry

# Plafond d'UN résultat, et plafond de TOUS les résultats d'un même tour.
#
# Le premier existait déjà ; le second manquait, et c'est lui qui fait la
# différence. Un plafond par outil est une promesse locale : dix outils à
# 16 000 caractères donnent 160 000 caractères dans un seul tour, et rien ne
# l'interdit. Le tour, lui, part dans un contexte que `_estimated_tokens`
# (`backend/inference/runtime.py`) a dimensionné AVANT les résultats : le
# débordement ne se voit donc qu'à l'exécution, chez llama.cpp, sous la forme
# d'un `could not broadcast input array` — le défaut mesuré le 28/09.
#
# Trois fois le plafond unitaire : deux appels pleins plus une marge, assez
# pour le trio typique d'un tour (deux outils de découverte + une relecture),
# et trop court pour qu'un abus de recherche web remplisse la fenêtre.
TOOL_RESULT_MAX_CHARS = 16_000
TOOL_TURN_MAX_CHARS = 48_000

def _marqueur_troncature(kept: int, room: int) -> str:
    """La marqueur de coupure, qui porte les deux nombres réels.

    Une troncature muette ferait croire au modèle — et à l'utilisateur — que la
    page a été lue en entier, alors qu'elle ne l'a pas été. Elle est donc
    rendue, et elle est comptée dans le budget qu'elle signale.
    """
    return f"\n\n[Tronqué : {kept}→{room} caractères, plafond de tour atteint]"


@dataclass(frozen=True)
class ToolExecution:
    call: ToolCallRecord
    tool: str
    arguments: dict[str, Any]
    ok: bool
    result: str
    latency_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        execute: Callable[[str, dict[str, Any], ToolCallRecord], dict[str, Any]],
        allowed_names: frozenset[str],
        timeout_s: float | None = None,
        turn_max_chars: int = TOOL_TURN_MAX_CHARS,
    ) -> None:
        self.registry = registry
        self.execute = execute
        self.allowed_names = allowed_names
        self.timeout_s = timeout_s
        self.turn_max_chars = turn_max_chars
        # Dépense du tour en cours. Un exécuteur est construit par tour dans
        # `run_agent_loop`, donc ce compteur EST la mémoire du tour : le
        # remettre à zéro ici annulerait le plafond, et le partager entre
        # tours le rendrait invisible.
        self._spent_chars = 0

    def execute_call(self, call: ToolCallRecord) -> ToolExecution:
        """Valide puis exécute un appel ; un timeout d'attente attend la fin native.

        Une fonction synchrone arbitraire ne peut pas être interrompue sans
        risque.  On signale donc le dépassement, mais on ne rend jamais la
        ressource à la boucle avant que le worker ait réellement terminé.
        """
        if call.name not in self.allowed_names:
            return ToolExecution(call, call.name, dict(call.arguments), False, "[UNKNOWN_TOOL]", 0)
        tool = self.registry.try_get(call.name)
        if tool is None:
            return ToolExecution(call, call.name, dict(call.arguments), False, "[UNKNOWN_TOOL]", 0)
        try:
            arguments = tool.input_model.model_validate(call.arguments).model_dump()
        except Exception as exc:
            # Un code métier écrit par un validateur (« [I2I_UNAVAILABLE] … »)
            # doit survivre : noyer le code sous `[BAD_ARGS]` et l'URL pydantic
            # empêche le modèle de comprendre ce qu'il doit corriger. C'est ce
            # qui a fait qu'un incident de q-r.md a consommé un tour de retry
            # « au hasard » au lieu de se corriger.
            if isinstance(exc, ValidationError):
                # `_extract_spec_error` renvoie déjà le texte PRÉFIXÉ par son
                # code (`[EMPTY_QUERY] …`) : lerajouter le doublerait.
                message = tool._extract_spec_error(exc)[0]
            else:
                message = f"{exc}"
                if "[BAD_ARGS]" not in message:
                    message = f"[BAD_ARGS] {message}"
            return ToolExecution(call, call.name, dict(call.arguments), False, message[:500], 0)
        timeout_s = self.timeout_s if self.timeout_s is not None else tool.timeout_s
        if not math.isfinite(timeout_s):
            return ToolExecution(call, call.name, dict(call.arguments), False, "[INTERNAL_ERROR] timeout invalide", 0)
        try:
            if call.name == "generate_image" or timeout_s <= 0:
                result = self.execute(call.name, arguments, call)
            else:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thot-tool")
                timed_out = False
                try:
                    future = executor.submit(self.execute, call.name, arguments, call)
                    try:
                        result = future.result(timeout=timeout_s)
                    except FutureTimeoutError:
                        # Un timeout d'attente n'est pas une preuve d'arrêt.
                        # Attendre la fin native avant de relâcher la ressource
                        # évite de lancer un modèle suivant pendant l'ancien.
                        timed_out = True
                        try:
                            future.result()
                        except Exception:
                            pass
                        result = None
                finally:
                    # wait=True est volontaire après un dépassement : le worker
                    # doit avoir rendu ses ressources avant le retour à la boucle.
                    executor.shutdown(wait=True, cancel_futures=True)
                if timed_out:
                    return ToolExecution(call, call.name, arguments, False, "[TIMEOUT] tool timeout", int(timeout_s * 1000))
        except Exception as exc:
            return ToolExecution(call, call.name, arguments, False, f"[INTERNAL_ERROR] {type(exc).__name__}: {exc}"[:2000], 0)
        if not isinstance(result, Mapping):
            return ToolExecution(call, call.name, arguments, False, "[INTERNAL_ERROR] résultat invalide", 0)
        return ToolExecution(
            call=call,
            tool=call.name,
            arguments=arguments,
            ok=bool(result.get("ok")),
            result=self._coupe_turn(str(result.get("result") or "")),
            latency_ms=int(result.get("latency_ms", 0) or 0),
            metadata={str(key): value for key, value in result.items() if key not in {"ok", "result", "latency_ms"}},
        )

    def _coupe_turn(self, texte: str) -> str:
        """Applique les DEUX plafons et annonce la coupure.

        L'unitaire d'abord : il borne un outil qui rend 200 ko. L'agrégé
        ensuite, parce qu'il est le seul qui voie les dix autres. Le rendu
        final est la RENCONTRE des deux — `min` des places restantes — donc
        aucune des deux bornes ne peut être dépassée par l'autre.

        Ce qui est coupé reste visible : sans marqueur, la fin du résultat
        disparaît et le modèle conclut à partir d'un texte qu'il a cru entier.
        """
        if len(texte) > TOOL_RESULT_MAX_CHARS:
            texte = texte[:TOOL_RESULT_MAX_CHARS]
        place = self.turn_max_chars - self._spent_chars
        if place <= 0:
            # Tour épuisé. On rend un refus, pas un résultat amputé : un
            # fragment sans sa fin ferait croire à une recherche qui s'est
            # arrêtée, alors qu'elle a été interrompue par le plafond.
            return "[BUDGET_EXHAUSTED] plafond de tour atteint — résultat non rendu"
        if len(texte) > place:
            # La marqueur est elle-même rendue : elle doit tenir DANS le
            # budget, sinon elle mange la place qu'elle annonce et le plafond
            # devient faux du premier caractère coupé. On réserve sa longueur
            # avant de couper, et les deux nombres qu'elle porte sont donc les
            # nombres réels du texte livré.
            keep = place - len(_marqueur_troncature(0, 0))
            marqueur = _marqueur_troncature(len(texte), keep)
            keep = place - len(marqueur)
            rendu = texte[:keep] + marqueur
            # La DÉPENSE est celle du texte livré, marqueur comprise : c'est
            # lui qui occupe la fenêtre. Ne pas la compter laisserait le
            # plafond intact après une coupure, donc inépuisable — le défaut
            # qu'il est censé corriger.
            self._spent_chars += len(rendu)
            return rendu
        self._spent_chars += len(texte)
        return texte

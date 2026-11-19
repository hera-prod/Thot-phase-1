# tools/base.py — socle minimal Phase 1 (fusion v0.8 tools/base.py simplifié)
# Notes FR : validation stricte à la frontière, pas de sanitization complexe (Phase 2)
# Supprimé v0.8 : permissions, sanitization, toolsets, sourceRef, attachedFiles — garde 2 tools chaleur
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError, model_validator


class ToolContext(BaseModel):
    """Contexte d'exécution injecté — run_id + chemins temporaires + scope annexe 11."""

    run_id: str
    request_id: str
    image_paths: list[str] = []  # /tmp/thot/{request_id}/{hash}.ext
    conv_id: str | None = None  # scope effectif (search_documents, Lot A annexe 11)
    tool_call_id: str | None = None


class ToolStatus(StrEnum):
    """Les cinq statuts terminaux de l'enveloppe (contrat commun §3.3).

    — ``success``  : opération aboutie, éventuellement sans correspondance ;
    — ``partial``  : éléments utiles obtenus, mais une partie de l'opération n'a pas abouti ;
    — ``rejected`` : opération refusée avant exécution par validation ou permissions ;
    — ``failed``   : traitement lancé ou requis mais non abouti ;
    — ``cancelled``: opération interrompue ou non poursuivie à la suite d'une annulation.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolResult(BaseModel):
    status: ToolStatus | None = None
    ok: bool | None = None
    content: str = ""
    display_summary: str = ""
    error: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    tool_name: str | None = None
    tool_call_id: str | None = None
    # Ce que la BOUCLE doit relire — preuves citables par le brief, `doc_N` nommés
    # au tour — et que le MODÈLE n'a pas besoin de lire.
    #
    # Pourquoi un canal à part alors que `content` porte déjà ces identifiants :
    # `call()` tronque `content` à `max_output_chars` (6000 pour `search_info`), et
    # couper un JSON en plein milieu le rend ILLISIBLE. Un `search_info` à trois
    # requêtes produit ~6 ko de payload — la troncature n'est pas un cas rare, c'est
    # le cas ordinaire. Un identifiant logé dans `content` disparaît donc au premier
    # résultat long, alors que la boucle a besoin des preuves même quand le texte a
    # été coupé : elle doit pouvoir citer une réponse tronquée.
    evidence_ids: list[str] = []
    public_ids: list[str] = []

    @model_validator(mode="after")
    def _concilie_ok_et_status(self):
        """Le contrat commun §3.2 : « Un adaptateur peut préserver des sorties
        existantes, par exemple ``ok``, sans introduire plusieurs sémantiques
        contradictoires. Il ne doit pas utiliser ce seul booléen pour cacher un
        résultat partiel. »

        ``ok`` est donc DÉRIVÉ de ``status`` (``ok`` ⇔ ``success``) — un statut
        ``partial`` rend ainsi un ``ok=False`` bruyant plutôt qu'un faux succès.
        Dans l'autre sens, un appel qui ne porte que ``ok`` dérive ce statut
        minimal honnête : un échec non qualifié est ``failed``, jamais ``rejected``
        ni ``cancelled`` par défaut (ces deux-là réclament une cause nommée).
        """
        if self.status is not None and self.ok is not None:
            attendu = self.status == ToolStatus.SUCCESS
            if self.ok is not attendu:
                raise ValueError(
                    f"ToolResult contradictoire : status={self.status} impose "
                    f"ok={str(attendu).lower()}, ok={str(self.ok).lower()}."
                )
        elif self.status is not None:
            self.ok = self.status == ToolStatus.SUCCESS
        elif self.ok is not None:
            self.status = ToolStatus.SUCCESS if self.ok else ToolStatus.FAILED
        else:
            raise ValueError("ToolResult exige ok ou status : un résultat sans verdict n'est pas un résultat.")
        return self


TArgs = TypeVar("TArgs", bound=BaseModel)

# Bornes hautes de longueur/quantité : dépliées en UNE imbrication GBNF par unité.
_UPPER_BOUNDS = {"maxLength": "caractères", "maxItems": "éléments"}


def _deplie_defs(schema: dict[str, Any]) -> Any:
    """Résout en place les `$ref` internes (`$defs`) d'un schéma publié.

    Un champ optionnel modélisé (`AskFilesScope`) publie sinon
    `{"$ref": "#/$defs/…"}` + un bloc `$defs` : le lecteur du contrat compare
    le type au mot de la fiche (« object »), et le convertisseur JSON-schema →
    GBNF ne suit pas les références locales. Le dépliage se fait au point de
    PUBLICATION — la validation, elle, reste portée par le modèle parseur.
    Pur : renvoie une structure neuve ; une référence inconnue ou cyclique
    lève, jamais un schéma à moitié résolu ne sort d'ici.
    """
    defs = schema.get("$defs") or {}
    if not defs:
        return schema
    racine = {key: value for key, value in schema.items() if key != "$defs"}

    def resolue(noeud: Any, vus: frozenset[str] = frozenset()) -> Any:
        if isinstance(noeud, list):
            return [resolue(item, vus) for item in noeud]
        if not isinstance(noeud, dict):
            return noeud
        reference = noeud.get("$ref")
        if not reference:
            return {key: resolue(value, vus) for key, value in noeud.items()}
        nom = str(reference).rsplit("/", 1)[-1]
        if nom not in defs:
            raise ValueError(f"[BAD_ARGS] Référence de schéma inconnue : {reference}.")
        if nom in vus:
            raise ValueError(
                f"[INTERNAL_ERROR] Schéma de modèle récursif non publiable : {nom}.")
        return resolue(defs[nom], vus | {nom})

    return resolue(racine)


def _publishable_schema(node: Any) -> Any:
    """Copie du schéma sans borne haute de longueur/quantité, limite énoncée en prose.

    Le convertisseur JSON-schema → GBNF de llama-cpp déplie une ``maxLength`` en
    une imbrication par unité : ``maxLength: 2000`` devient une règle
    ``char (char (char …))`` de deux mille niveaux que llama.cpp refuse de parser
    ("number of rules that are going to be repeated … exceeds sane defaults").
    ``LlamaSampler.add_grammar`` ajoute alors le sampler NULL renvoyé par
    ``llama_sampler_init_grammar`` à la chaîne d'échantillonnage sans vérifier le
    retour, et le premier token échantillonné déréférence NULL : SIGSEGV, que
    Python ne peut pas rattraper. La borne disparaît donc du schéma publié et
    reste une phrase que le modèle lit ; ``input_model`` continue de l'appliquer.

    Pur : ne lit ni ne modifie le modèle, renvoie une structure neuve.
    """
    if isinstance(node, list):
        return [_publishable_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    published = {
        key: _publishable_schema(value)
        for key, value in node.items()
        if key not in _UPPER_BOUNDS
    }
    stated = [f"Maximum {node[key]} {_UPPER_BOUNDS[key]}." for key in _UPPER_BOUNDS if key in node]
    if stated:
        description = str(published.get("description") or "").strip()
        if description and description[-1] not in ".!?:;":
            description += "."
        published["description"] = f"{description} {' '.join(stated)}".strip()
    return published


class BaseTool(ABC, Generic[TArgs]):
    name: str
    description: str
    input_model: type[TArgs]
    timeout_s: float = 15.0  # Phase 1 : 15s max
    max_output_chars: int = 8_000

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _publishable_schema(_deplie_defs(self.input_model.model_json_schema())),
            },
        }

    async def call(self, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.monotonic()
        # Spec 01/02 : appel vide → EMPTY_CALL avant toute validation (aucun argument exploitable).
        if not isinstance(raw_args, dict) or not raw_args:
            from backend.tools.errors import error_text, ToolErrorCode
            _allowed = self._allowed_args_text()
            return ToolResult(
                status=ToolStatus.REJECTED,
                display_summary=f"{self.name}: appel vide",
                error=error_text(ToolErrorCode.EMPTY_CALL, f"Ton appel de {self.name} est vide. Utilise les arguments autorisés : {_allowed}."),
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        # Spec 01/02 : clé inconnue → UNKNOWN_ARG (jamais BAD_ARGS générique).
        # Aucun filet ici : `model_fields` est un attribut de classe Pydantic, il
        # n'est pas censé lever — si un artefact mal câblé le fait, la panne doit
        # être VUE (un `{__class__} AttributeError` remonte) et non absorbée, car
        # cette porte silencieuse ferait de tout appel bruité un appel accepté.
        _allowed_keys = set((self.input_model.model_fields or {}).keys())
        _unknown = [k for k in raw_args.keys() if k not in _allowed_keys]
        if _unknown:
            from backend.tools.errors import error_text, ToolErrorCode
            return ToolResult(
                status=ToolStatus.REJECTED,
                display_summary=f"{self.name}: argument inconnu",
                error=error_text(ToolErrorCode.UNKNOWN_ARG, f"Mauvais argument. Utilise uniquement les arguments autorisés : {self._allowed_args_text()}."),
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        try:
            args = self.input_model.model_validate(raw_args)
        except ValidationError as exc:
            # Spec 02 : les validateurs lèvent ValueError préfixé [CODE] message métier.
            # On préserve le code exact au lieu d'un BAD_ARGS générique.
            _msg, _code = self._extract_spec_error(exc)
            return ToolResult(
                status=ToolStatus.REJECTED,
                display_summary=f"{self.name}: {_code.lower() if _code else 'arguments invalides'}",
                error=_msg,
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        try:
            async with asyncio.timeout(self.timeout_s):
                result = await self.execute(args, ctx)
        except TimeoutError:
            return ToolResult(
                status=ToolStatus.FAILED,
                display_summary=f"{self.name}: timeout",
                error=f"[TIMEOUT] {self.name} timeout après {self.timeout_s}s. Retente avec une portée réduite, ou conclus avec les résultats déjà obtenus.",
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        except Exception as exc:
            return ToolResult(
                status=ToolStatus.FAILED,
                display_summary=f"{self.name}: erreur",
                error=f"[INTERNAL_ERROR] {self.name} erreur: {type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        # Troncature Phase 1 (8K chars)
        content = result.content
        truncated = False
        if len(content) > self.max_output_chars:
            content = content[: self.max_output_chars] + f"\n\n[Tronqué : {len(content)}→{self.max_output_chars} chars]"
            truncated = True
        return result.model_copy(update={"content": content, "truncated": truncated, "duration_ms": int((time.monotonic() - started) * 1000), "tool_name": self.name})

    def _allowed_args_text(self) -> str:
        try:
            return ", ".join(sorted((self.input_model.model_fields or {}).keys()))
        except Exception:
            return ""

    @staticmethod
    def _extract_spec_error(exc: ValidationError) -> tuple[str, str | None]:
        """Extrait le premier [CODE] message métier des erreurs pydantic. Pur."""
        import re as _re
        try:
            for err in exc.errors():
                ctx = err.get("ctx") or {}
                raw = str(ctx.get("error", err.get("msg", "")))
                m = _re.search(r"\[([A-Z_]+)\]\s*(.*)", raw)
                if m:
                    return raw.strip(), m.group(1)
            # Enum Literal / type pur sans préfixe → mapping résiduel spec 01
            blob = str(exc.errors())
            if "extra" in blob.lower() or "unexpected" in blob.lower():
                from backend.tools.errors import error_text, ToolErrorCode
                return error_text(ToolErrorCode.UNKNOWN_ARG), "UNKNOWN_ARG"
            if "enum" in blob.lower() or "literal" in blob.lower():
                from backend.tools.errors import error_text, ToolErrorCode
                return error_text(ToolErrorCode.ENUM_INVALID), "ENUM_INVALID"
            from backend.tools.errors import error_text, ToolErrorCode
            return error_text(ToolErrorCode.BAD_ARGS, str(exc.errors())[:500]), "BAD_ARGS"
        except Exception:
            return "Arguments invalides.", None

    @abstractmethod
    async def execute(self, args: TArgs, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

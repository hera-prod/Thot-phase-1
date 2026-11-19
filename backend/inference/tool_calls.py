from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# Ce set sert à reconnaitre la syntaxe d'appel que le modèle RECOPIE, pas à
# déclarer ce que l'application sait faire. Il reste donc plus large que le
# registre et conserve des noms retirés comme `get_source` : un modèle qui a
# vu `[get_source()]` ailleurs l'écrira quand même, et le role du filtre est
# justement de l'effacer avant qu'il n'atteigne l'utilisateur. Le vider rendrait
# la sortie du modèle encore plus facile a copier.
_PROVIDER_TOOL_NAMES = frozenset({
    "vision",
    "doc_ocr",
    "get_source",
    "read_text",
    "search_documents",
    "get_document_card",
    "read_document",
    "search_info",
    "find_files",
    "ask_files",
    "generate_image",
    "web_research",
    "submit_brief",
})
_LFM_BLOCK_RE = re.compile(
    r"<\|tool_call_start\|>.*?(?:<\|tool_call_end\|>|(?=<\||$))",
    re.IGNORECASE | re.DOTALL,
)
_TEXTUAL_TOOL_START_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:\[)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(",
)
_MALFORMED_TOOL_JSON_RE = re.compile(
    r"\{\s*['\"]name['\"]\s*:\s*['\"](?:"
    + "|".join(sorted(_PROVIDER_TOOL_NAMES))
    + r")['\"]\s*,\s*['\"]arguments['\"]\s*:",
    re.IGNORECASE,
)
_INTERNAL_ID_RE = re.compile(r"\b(?:art|sti)_[A-Za-z0-9_-]+\b", re.IGNORECASE)


def _raw_tool_json_spans(text: str) -> list[tuple[int, int]]:
    """Retourne les spans JSON qui envelope un pseudo-appel d'outil.

    Le décodage JSON sert seulement à reconnaître le contenu à supprimer.  Les
    arguments ne sont jamais exécutés ici : cette fonction est un filtre de
    contenu provider, pas un nouveau parseur d'appels.
    """
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int]] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[start:])
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(payload, Mapping):
            continue
        keys = set(payload)
        name = payload.get("name")
        if "tool" in keys and name is None:
            name = payload.get("tool")
        function = payload.get("function")
        if name is None and isinstance(function, Mapping):
            name = function.get("name")
        if not isinstance(name, str) or name.strip().lower() not in _PROVIDER_TOOL_NAMES:
            continue
        has_arguments = "arguments" in keys or "args" in keys
        if isinstance(function, Mapping) and "arguments" in function:
            has_arguments = True
        if not has_arguments:
            continue
        spans.append((start, start + end))
    return spans


def _matching_parenthesis(text: str, opening: int) -> int | None:
    """Trouve la parenthèse fermante en ignorant les chaînes et imbrications."""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _strip_textual_tool_spans(text: str) -> str:
    """Retire les appels textuels connus, même imbriqués, sans les exécuter."""
    spans: list[tuple[int, int]] = []
    for match in _TEXTUAL_TOOL_START_RE.finditer(text):
        name = match.group(1).lower()
        if name not in _PROVIDER_TOOL_NAMES:
            continue
        opening = text.find("(", match.start(), match.end())
        if opening < 0:
            continue
        closing = _matching_parenthesis(text, opening)
        if closing is None:
            # Un appel pseudo incomplet est non fiable : discarder le reste
            # plutôt que de conserver un fragment qui pourrait être réémis.
            spans.append((match.start(), len(text)))
            continue
        end = closing + 1
        bracket_index = end
        while bracket_index < len(text) and text[bracket_index].isspace():
            bracket_index += 1
        if bracket_index < len(text) and text[bracket_index] == "]":
            end = bracket_index + 1
        spans.append((match.start(), end))
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return text


def sanitize_provider_content(content: str) -> str:
    """Retire les artefacts provider non exécutables avant toute diffusion.

    Les wrappers LFM, les pseudo-appels ``fn(...)`` et les enveloppes JSON
    ``{"name": ..., "arguments": ...}`` ne sont pas des messages assistant.
    Cette fonction est volontairement conservatrice : elle ne touche pas aux
    résultats JSON d'outils, seulement au texte libre d'un provider.
    """
    text = str(content or "")
    if not text:
        return ""
    text = _LFM_BLOCK_RE.sub(" ", text)
    raw_spans = _raw_tool_json_spans(text)
    for match in _MALFORMED_TOOL_JSON_RE.finditer(text):
        if not any(start <= match.start() < end for start, end in raw_spans):
            # Un envelope malformé est non fiable : fail closed jusqu'à la fin.
            raw_spans.append((match.start(), len(text)))
    for start, end in reversed(raw_spans):
        text = text[:start] + text[end:]
    text = _strip_textual_tool_spans(text)
    text = re.sub(r"<\|[^|\r\n]{1,80}\|>", "[control-token]", text)
    text = _INTERNAL_ID_RE.sub("[identifiant interne retiré]", text)
    return text.strip()


class ToolCallParseError(ValueError):
    pass


@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    name: str
    arguments: dict[str, Any]
    ordinal: int
    provenance: str = "native"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_call_id(run_id: str, round_index: int, ordinal: int, name: str, arguments: Mapping[str, Any]) -> str:
    payload = f"{run_id}:{round_index}:{ordinal}:{name}:{canonical_json(arguments)}"
    return "call_stable_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def normalize_native_tool_calls(
    native: Any,
    *,
    run_id: str,
    round_index: int,
    max_calls: int = 8,
    provenance: str = "native",
    allowed_names: frozenset[str] | None = None,
) -> tuple[ToolCallRecord, ...]:
    if native is None:
        return ()
    if not isinstance(native, list):
        raise ToolCallParseError("tool_calls doit être une liste")
    if len(native) > max_calls:
        raise ToolCallParseError("TOO_MANY_TOOL_CALLS")
    records: list[ToolCallRecord] = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(native):
        if not isinstance(raw, Mapping):
            raise ToolCallParseError(f"tool_calls[{ordinal}] invalide")
        function = raw.get("function")
        if not isinstance(function, Mapping):
            raise ToolCallParseError(f"tool_calls[{ordinal}].function manquant")
        name = str(function.get("name") or "").strip()
        if not name:
            raise ToolCallParseError(f"tool_calls[{ordinal}].name manquant")
        if allowed_names is not None and name not in allowed_names:
            raise ToolCallParseError(f"outil non autorisé dans ce canal: {name}")
        raw_arguments = function.get("arguments", "{}")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise ToolCallParseError(f"arguments invalides pour {name}") from exc
        elif isinstance(raw_arguments, Mapping):
            arguments = dict(raw_arguments)
        else:
            raise ToolCallParseError(f"arguments invalides pour {name}")
        if not isinstance(arguments, dict):
            raise ToolCallParseError(f"arguments invalides pour {name}")
        call_id = str(raw.get("id") or "").strip()
        if not call_id:
            call_id = stable_call_id(run_id, round_index, ordinal, name, arguments)
        if call_id in seen:
            raise ToolCallParseError(f"tool_call_id dupliqué: {call_id}")
        seen.add(call_id)
        records.append(ToolCallRecord(call_id, name, arguments, ordinal, provenance))
    return tuple(records)


_LFM_START = "<|tool_call_start|>"
_LFM_END = "<|tool_call_end|>"
_LFM_WRAPPED_RE = re.compile(re.escape(_LFM_START) + r"(.*?)" + re.escape(_LFM_END), re.DOTALL)
_LFM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CONTROL_ARG_RE = re.compile(r"<\|[^|\r\n]{1,80}\|>", re.IGNORECASE)


def _literal_value(node: ast.AST, depth: int = 0) -> Any:
    if depth > 12:
        raise ToolCallParseError("imbrication d’arguments trop profonde")
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        if isinstance(node.value, str) and _CONTROL_ARG_RE.search(node.value):
            raise ToolCallParseError("token de contrôle dans un argument")
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal_value(item, depth + 1) for item in node.elts]
    if isinstance(node, ast.Dict):
        result: dict[str, Any] = {}
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise ToolCallParseError("clé d’argument invalide")
            if key.value in result:
                raise ToolCallParseError("clé d’argument dupliquée")
            result[key.value] = _literal_value(value, depth + 1)
        return result
    raise ToolCallParseError("argument non littéral")


def normalize_lfm25_content(
    content: str,
    *,
    run_id: str,
    round_index: int,
    max_calls: int = 8,
    provenance: str = "native",
    allowed_names: frozenset[str] | None = None,
) -> tuple[ToolCallRecord, ...]:
    text = content or ""
    if len(text) > 65536:
        raise ToolCallParseError("sortie provider trop longue")
    starts = text.count(_LFM_START)
    ends = text.count(_LFM_END)
    if starts == 0 and ends == 0:
        return ()
    if starts != ends:
        raise ToolCallParseError("wrapper LFM incomplet")
    if starts > max_calls:
        raise ToolCallParseError("trop d’appels LFM")
    records: list[ToolCallRecord] = []
    seen: set[str] = set()
    for match in _LFM_WRAPPED_RE.finditer(text):
        body = match.group(1).strip()
        if not (body.startswith("[") and body.endswith("]")):
            raise ToolCallParseError("corps LFM non bracket")
        try:
            tree = ast.parse(body, mode="eval")
        except SyntaxError as exc:
            raise ToolCallParseError("appel LFM syntaxiquement invalide") from exc
        if not isinstance(tree.body, ast.List):
            raise ToolCallParseError("liste d’appels LFM absente")
        if len(tree.body.elts) > max_calls or len(records) + len(tree.body.elts) > max_calls:
            raise ToolCallParseError("trop d’appels LFM")
        for element in tree.body.elts:
            if not isinstance(element, ast.Call) or element.args or element.keywords is None:
                raise ToolCallParseError("appel LFM invalide")
            if isinstance(element.func, ast.Name):
                name = element.func.id
            elif isinstance(element.func, ast.Attribute):
                parts: list[str] = []
                current: ast.AST = element.func
                while isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                if not isinstance(current, ast.Name):
                    raise ToolCallParseError("nom d’outil LFM invalide")
                parts.append(current.id)
                name = ".".join(reversed(parts))
            else:
                raise ToolCallParseError("nom d’outil LFM invalide")
            if not _LFM_NAME_RE.fullmatch(name):
                raise ToolCallParseError("nom d'outil LFM invalide")
            if allowed_names is not None and name not in allowed_names:
                raise ToolCallParseError(f"outil non autorisé dans ce canal: {name}")
            arguments: dict[str, Any] = {}
            for keyword in element.keywords:
                if keyword.arg is None or keyword.arg in arguments:
                    raise ToolCallParseError("argument LFM invalide")
                arguments[keyword.arg] = _literal_value(keyword.value)
            call_id = stable_call_id(run_id, round_index, len(records), name, arguments)
            if call_id in seen:
                raise ToolCallParseError("tool_call_id LFM dupliqué")
            seen.add(call_id)
            records.append(ToolCallRecord(call_id, name, arguments, len(records), provenance))
    return tuple(records)


def normalize_named_tool_response(
    message: Any,
    *,
    expected_name: str,
    run_id: str,
    round_index: int,
    allow_wrapper: bool = False,
) -> tuple[ToolCallRecord, ...]:
    """Normalise une réponse à choix nommé sans accepter un autre outil.

    Le canal natif est prioritaire.  Le wrapper LFM est optionnel et ne peut
    être activé que par un appelant qui autorise explicitement ce canal.  Un
    JSON libre ne produit jamais de record : il est nettoyé par
    ``sanitize_provider_content`` avant d'atteindre l'historique ou la trace.
    """
    if not isinstance(message, Mapping):
        raise ToolCallParseError("message chat invalide")
    records = normalize_native_tool_calls(
        message.get("tool_calls"),
        run_id=run_id,
        round_index=round_index,
        provenance="native",
    )
    if not records and allow_wrapper:
        records = normalize_lfm25_content(
            str(message.get("content") or ""),
            run_id=run_id,
            round_index=round_index,
            provenance="lfm25_wrapper",
        )
    if len(records) != 1 or records[0].name != expected_name:
        return ()
    return (records[0],)


# Les noms qu'un pseudo-appel textuel peut nommer aujourd'hui : exactement
# l'allowlist de la phase de DÉCOUVERTE (DISCOVERY_TOOL_NAMES côté boucle). «
# legacy_ » serait un mensonge — rien ici ne parle d'un format historique, mais
# du re-ask natif forcé que la boucle déclenche AUJOURD'HUI sur un pseudo-appel.
# Ce n'est pas une seconde source de vérité : l'égalité est épinglée par un test
# (test_toolcall_integration) contre DISCOVERY_TOOL_NAMES.
_REASK_NAMED_TOOL_NAMES = frozenset({"search_info", "find_files", "ask_files", "generate_image", "web_research"})
_TEXTUAL_INTENT_MAX_CHARS = 65536
_TEXTUAL_INTENT_MAX_SCANS = 64


def _reask_json_has_control_token(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_CONTROL_ARG_RE.search(value))
    if isinstance(value, Mapping):
        return any(_reask_json_has_control_token(key) or _reask_json_has_control_token(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_reask_json_has_control_token(item) for item in value)
    return False


def _textual_intent_name(payload: Any) -> str | None:
    if not isinstance(payload, Mapping) or set(payload) != {"name", "arguments"}:
        return None
    name = payload.get("name")
    arguments = payload.get("arguments")
    if not isinstance(name, str) or name not in _REASK_NAMED_TOOL_NAMES:
        return None
    if not isinstance(arguments, Mapping) or _reask_json_has_control_token(arguments):
        return None
    return name


def _textual_call_names(text: str) -> set[str]:
    """Noms d'outils nommés par un pseudo-appel ``fn(...)`` écrit en prose.

    Mesuré sur le 8B : le même tour produit DEUX dialectes de pseudo-appel —
    l'enveloppe JSON ``{"name": …}`` et l'appel Python
    ``[generate_image(prompt='…')]`` sans marqueur de wrapper. Seul le premier
    était lu ; le second passait donc inaperçu, sans re-ask, sans image.

    Seul l'identifiant qui précède la parenthèse est lu : le corps n'est jamais
    interprété (C04), et c'est le re-ask forcé qui redemande les arguments au
    modèle sur son canal natif. Un balayage exhaustif, donc l'unicité du nom
    est PROUVÉE ici, pas supposée.
    """
    return {
        name
        for match in _TEXTUAL_TOOL_START_RE.finditer(text)
        if (name := match.group(1).lower()) in _REASK_NAMED_TOOL_NAMES
    }


def textual_call_args(content: str, tool_name: str) -> dict | None:
    """Les ARGUMENTS d'un pseudo-appel textuel, ou None.

    Le pseudo-appel reste, partout ailleurs, une décision invalide : son JSON
    n'est ni exécuté ni diffusé. Cette fonction est le seul endroit du projet
    où on en tire quelque chose, et c'est un **filet de dernier recours**, après
    que le re-ask nommé a échoué.

    Pourquoi ce filet existe, et pourquoi il est borné. Mesuré le 28/09 sur
    LFM2.5-8B : le modèle écrit `[web_research(queries=[...], type='general')]`
    — un appel **correct**, avec les bons arguments — et le re-ask natif forcé
    lui fait rendre une réponse **vide**. Les arguments étaient donc là, et on
    les perdait. Les jeter deux fois n'est pas de la sévérité, c'est une perte
    sèche.

    Le découpage se fait avec `ast.literal_eval` sur les MOTS-CLÉS de l'appel :
    aucun nom, aucune fonction, aucun appel n'est évalué — seuls des littéraux
    (chaînes, nombres, listes, booléens, `None`). Le modèle ne peut donc pas
    faire exécuter du code, y compris en écrivant `__import__('os').system(...)`
    dans un argument : la valeur serait simplement refusée.

    On rend `None` — donc le filet ne s'enclenche pas — dès que le contenu est
    trop long, contient un second nom d'outil, ou n'est pas analysable.
    """
    text = content or ""
    if not text or len(text) > _TEXTUAL_INTENT_MAX_CHARS:
        return None
    if textual_tool_intent(text) != tool_name:
        return None   # nom différent, ou ambigu : pas de décision de notre part
    motif = re.compile(
        r"(?<![A-Za-z0-9_])(?:\[)?\s*" + re.escape(tool_name) + r"\s*\((?P<corps>.*?)\)\s*\]?",
        re.DOTALL,
    )
    for found in motif.finditer(text):
        corps = found.group("corps").strip()
        if not corps:
            continue
        try:
            arbre = ast.parse(f"_inconnu({corps})", mode="eval")
        except SyntaxError:
            continue
        if not isinstance(arbre.body, ast.Call):
            continue
        # On refuse tout argument POSITIONNEL : tous nos outils sont à mots-clés,
        # et un positionnel serait une autre grammaire que celle du schéma.
        if arbre.body.args:
            continue
        arguments: dict[str, object] = {}
        for mot in arbre.body.keywords:
            if mot.arg is None:
                continue
            try:
                arguments[mot.arg] = ast.literal_eval(mot.value)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                return None   # un seul argument illisible : on ne devine pas le reste
        if arguments:
            return arguments
    # Deuxième forme, observée le même jour : l'enveloppe JSON explicite, où le
    # 8B écrit des `null` pour tout ce qu'il ne fournit pas —
    # `{"name": "web_research", "arguments": {"type": [], "purpose": null}}`.
    # Les `null` sont retirés, sinon ils.countent comme des arguments donnés et
    # le validateur les refuse à tort ; un `[]` ou un `""` reste, car c'est une
    # valeur — et c'est au validateur de dire qu'elle est vide.
    decodeur = json.JSONDecoder()
    position = 0
    balayages = 0
    while position < len(text) and balayages < _TEXTUAL_INTENT_MAX_SCANS:
        ouverture = text.find("{", position)
        if ouverture < 0:
            break
        balayages += 1
        try:
            charge, fin = decodeur.raw_decode(text, ouverture)
        except (json.JSONDecodeError, RecursionError):
            position = ouverture + 1
            continue
        position = fin
        if not isinstance(charge, Mapping) or charge.get("name") != tool_name:
            continue
        enveloppe = charge.get("arguments")
        if not isinstance(enveloppe, Mapping):
            continue
        retenus = {str(k): v for k, v in enveloppe.items() if v is not None}
        if retenus:
            return retenus
    return None


def textual_tool_intent(content: str) -> str | None:
    """Nom d'outil d'un pseudo-appel textuel, ou None. Ne retourne JAMAIS les arguments.

    Le pseudo-appel reste une décision invalide (C04) : son JSON n'est ni exécuté
    ni diffusé.  Seul le nom est lu dans le texte, pour contraindre le modèle à
    réémettre un vrai ``tool_calls`` natif.  Deux dialectes portent ce nom — la
    forme ``fn(...)`` et l'enveloppe JSON ``{"name": …}`` — et un seul nom ne
    fait pas autorité sur l'autre. Deux noms distincts dans un même contenu sont
    ambigus, et un balayage arrêté par la limite de tentatives n'a pas
    davantage prouvé l'unicité : les deux cas sont refusés.
    """
    text = content or ""
    if not text or len(text) > _TEXTUAL_INTENT_MAX_CHARS:
        return None
    decoder = json.JSONDecoder()
    names: set[str] = _textual_call_names(text)
    scans = 0
    index = 0
    while index < len(text) and scans < _TEXTUAL_INTENT_MAX_SCANS:
        opening = text.find("{", index)
        if opening < 0:
            break
        scans += 1
        try:
            payload, end = decoder.raw_decode(text, opening)
        except (json.JSONDecodeError, RecursionError):
            index = opening + 1
            continue
        name = _textual_intent_name(payload)
        if name is not None:
            names.add(name)
        # `raw_decode` renvoie une position ABSOLUE dans `text` : ajouter encore
        # `opening` sautait par-dessus la suite du contenu, et un second
        # pseudo-appel hitché au premier devenait invisible — l'ambiguïté que la
        # docstring promet de refuser n'était jamais vue.
        index = end
    # Un nom trouvé NE PROUVE PAS qu'il est le seul : le texte restant peut
    # contenir un second pseudo-appel que la limite a empêché de lire. Refuser
    # est la seule lecture honnête, et c'est celle que la docstring annonce.
    if index < len(text) and scans >= _TEXTUAL_INTENT_MAX_SCANS:
        return None
    if len(names) != 1:
        return None
    return next(iter(names))


# --- Dialecte 3 : l'ENVELOPPE D'ARGUMENTS SANS NOM ------------------------------
# Le 8B écrit parfois l'appel CORRECT et COMPLET, mais omet la clé `name` :
#   {"prompt": "A vibrant fruit market …", "aspect_ratio": "16:9"}
# Aucun nom n'est lisible, donc `textual_tool_intent` renvoie None, le re-ask
# nommé ne part jamais, et la boucle tombe sur `if not records: break` : la
# décision — la plus explicite des cinq formulations — disparaît.
#
# CE QUE CETTE RECONNAISSANCE A DROIT FAIRE : constater le MOMENT du re-ask.
# CE QU'ELLE N'A PAS LE DROIT DE FAIRE, et ne le fera jamais :
#   * choisir l'outil à partir de la FORME des arguments (une table
#     `prompt`+`aspect_ratio` ⇒ generate_image est un choix backend, pas un choix
#     du modèle — le propriétaire l'a explicitement refusé) ;
#   * lire, transmettre ou exécuter une seule valeur de l'enveloppe.
# L'enveloppe sert de DÉCLENCHEUR, jamais de_SOURCE. L'outil et les arguments
# exécutés viennent du modèle, sur son canal natif, au re-ask.


def _published_property_keys(tool_defs: Sequence[Any]) -> tuple[frozenset[str], ...]:
    """Clés de propriétés publiées, une par outil — lues dans les DÉFINITIONS OFFERTES.

    Source UNIQUE : les `tool_defs` que la boucle construit déjà de
    ``registry.definitions()`` filtré par ``DISCOVERY_TOOL_NAMES``. Aucune table
    écrite à la main ici : publier un outil avec un nouvel argument le rend
    détectable sans que ce fichier change.
    """
    published: list[frozenset[str]] = []
    for definition in tool_defs or ():
        function = definition.get("function") if isinstance(definition, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        parameters = function.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, Mapping) else None
        if not isinstance(properties, Mapping) or not properties:
            continue
        published.append(frozenset(str(key) for key in properties))
    return tuple(published)


def _nameless_envelope_keys(payload: Any) -> frozenset[str] | None:
    """Clés d'une enveloppe d'arguments sans nom, ou None si ce n'en est pas une.

    Ne lit AUCUNE valeur : seul le jeu de CLÉS est rendu, et c'est lui seul qui
    decide. Le premier dialecte est écarté par sa clé `name` — il a un nom, donc
    le canal nommé le traite, et ce prédicat n'a rien à y dire.
    """
    if not isinstance(payload, Mapping):
        return None
    keys = frozenset(str(key) for key in payload)
    if not keys or "name" in keys:
        return None
    return keys


def _tools_accepting(envelope_keys: frozenset[str], published: tuple[frozenset[str], ...]) -> set[int]:
    """Emplacements des outils dont le schéma publié accepte ce jeu de clés."""
    return {index for index, properties in enumerate(published) if envelope_keys <= properties}


def carries_nameless_envelope(content: str, tool_defs: Sequence[Any]) -> bool:
    """Le texte porte-t-il une enveloppe d'arguments d'outil SANS nom d'outil ?

    Prédicat PUR, adossé aux TROIS dialectes de pseudo-appel du 8B. Il répond
    OUI ou NON — jamais « quel outil » : un nom n'est pas disponible, et le
    backend n'a pas le droit d'en fabriquer un à partir de la forme des
    arguments. La seule question posée est « le modèle a-t-il essayé
    d'appeler un outil ? », et la réponse déclenche un re-ask où c'est le
    MODÈLE qui choisit l'outil et remplit les arguments.

    Conditions, toutes exigées (une seule manque, le signal est refusé) :
      * un objet JSON, sans clé `name`, au moins une clé ;
      * toutes les clés sont des propriétés PUBLIÉES d'un outil de découverte
        (les `tool_defs` reçus, source unique) ;
      * ce jeu de clés est compatible avec EXACTEMENT UN outil. Zéro outil :
        la prose n'est pas un appel. Deux outils ou plus : l'ambiguïté est
        refusée, parce que trancher serait choisir à la place du modèle.

    Mêmes bornes que `textual_tool_intent` (taille, nombre de `{` essayés) :
    un balayage arrêté par la limite n'a pas PROUVÉ l'unicité de ce qu'il
    cherche, donc il ne prouve rien. Les dialectes 1 et 2 sont hors sujet —
    ils portent un nom et sont traités par le canal nommé, qui garde son
    comportement et ses tests.
    """
    text = content or ""
    if not text or len(text) > _TEXTUAL_INTENT_MAX_CHARS:
        return False
    published = _published_property_keys(tool_defs)
    if not published:
        return False
    decoder = json.JSONDecoder()
    undecided: set[int] = set()
    saw_envelope = False
    scans = 0
    index = 0
    while index < len(text) and scans < _TEXTUAL_INTENT_MAX_SCANS:
        opening = text.find("{", index)
        if opening < 0:
            break
        scans += 1
        try:
            payload, end = decoder.raw_decode(text, opening)
        except (json.JSONDecodeError, RecursionError):
            index = opening + 1
            continue
        keys = _nameless_envelope_keys(payload)
        if keys is not None:
            saw_envelope = True
            undecided |= _tools_accepting(keys, published)
        index = end
    if index < len(text) and scans >= _TEXTUAL_INTENT_MAX_SCANS:
        return False
    if not saw_envelope:
        return False
    # Le même ensemble d'outils proposé par deux enveloppes n'est pas une
    # ambiguïté : c'est un seul et même signal, comme deux fois le même nom.
    return len(undecided) == 1


def sanitize_tool_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Copie les arguments en retirant les artefacts provider des chaînes."""
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return sanitize_provider_content(value)
        if isinstance(value, Mapping):
            return {str(key): clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, tuple):
            return [clean(item) for item in value]
        return value

    return {str(key): clean(value) for key, value in arguments.items()}


def as_openai_tool_call(record: ToolCallRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "type": "function",
        "function": {
            "name": record.name,
            "arguments": canonical_json(sanitize_tool_arguments(record.arguments)),
        },
    }


def append_canonical_tool_round(
    messages: list[dict[str, Any]],
    assistant_content: str,
    records: tuple[ToolCallRecord, ...],
    results: Mapping[str, str],
) -> None:
    messages.append({
        "role": "assistant",
        "content": sanitize_provider_content(assistant_content),
        "tool_calls": [as_openai_tool_call(record) for record in records],
    })
    for record in records:
        if record.id not in results:
            raise ToolCallParseError(f"résultat manquant pour {record.id}")
        messages.append({
            "role": "tool",
            "tool_call_id": record.id,
            "content": sanitize_provider_content(results[record.id]),
        })

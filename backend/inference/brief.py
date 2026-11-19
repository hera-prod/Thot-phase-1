# backend/inference/brief.py — Brief structurel submit_brief (annexe 12 + diagnostic tool_calls)
#
# D'OÙ VIENT CE MODULE : le diagnostic sur notre stack exacte a prouvé que LFM2.5-8B émet
# des appels pythoniques `[fn(k='v')]` SANS les tokens <|tool_call_start|> en mode auto
# (runtime 0.3.34 ne les parse pas → tool_calls null), tandis que le forçage NOMMÉ remonte
# des tool_calls structurés propres. Donc : parser strict côté backend (découverte) +
# submit_brief forcé nommé (handoff) + validation schéma. Le prompt ne porte plus la
# contrainte « texte ou appel » — elle vit dans la structure.
# OÙ VA QUOI : parse_pythonic_calls() récupère les appels fuyés en texte ; validate_
# submit_brief() contrôle le handoff (fiche v1.2 : un seul argument `brief`, chaîne
# non vide) ; render_brief() sérialise pour le générateur (bulle UI, colonne brief
# et list_messages inchangés — le rendu EST le texte soumis).
# POURQUOI STRICT : noms allowlist + Pydantic par outil + scope déjà en aval. Un appel
# malformé ou inconnu est ignoré (pas d'exécution), jamais deviné.
from __future__ import annotations

import re

# Noms d'outils exécutables + terminal. submit_brief n'a PAS d'exécuteur : c'est le handoff.
# `get_source` et `get_document_card` en sont absents : retirés faute d'appelant.
# Les garder ici ferait valider une citation d'outil qui n'a jamais pu être
# exécutée — une preuve irrecevable présentée comme recevable.
ALLOWED_TOOLS = frozenset(
    {
        "vision",
        "doc_ocr",
        "read_text",
        "search_documents",
        "read_document",
        "search_info",
        "find_files",
        "ask_files",
        "submit_brief",
    }
)

_CALL_OPEN_RE = re.compile(r"\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_call_spans(content: str) -> list[tuple[str, str]]:
    """Spans [nom(args…)] avec équilibrage de parenthèses hors quotes. Pur."""
    spans: list[tuple[str, str]] = []
    try:
        text = content or ""
        for m in _CALL_OPEN_RE.finditer(text):
            name = m.group(1)
            i = m.end()
            depth = 1
            quote: str | None = None
            buf: list[str] = []
            while i < len(text) and depth > 0:
                ch = text[i]
                if quote is not None:
                    buf.append(ch)
                    if ch == quote and (not buf or True):
                        # fin de quote sauf échappée \' ou \"
                        backslashes = 0
                        j = len(buf) - 2
                        while j >= 0 and buf[j] == "\\":
                            backslashes += 1
                            j -= 1
                        if backslashes % 2 == 0:
                            quote = None
                elif ch in ("'", '"'):
                    quote = ch
                    buf.append(ch)
                elif ch == "(":
                    depth += 1
                    buf.append(ch)
                elif ch == ")":
                    depth -= 1
                    if depth > 0:
                        buf.append(ch)
                else:
                    buf.append(ch)
                i += 1
            if depth != 0:
                continue  # parenthèse jamais fermée → ignoré
            # consomme le ] fermant optionnel
            j = i
            while j < len(text) and text[j] in (" ", "\t", "\n"):
                j += 1
            if j < len(text) and text[j] == "]":
                j += 1
            spans.append((name, "".join(buf)))
    except Exception:
        pass
    return spans


def _split_top_level(s: str) -> list[str]:
    """Découpe sur virgules de niveau 0 (hors quotes et crochets). Pur."""
    parts, cur, quote, depth = [], [], None, 0
    try:
        i = 0
        while i < len(s):
            ch = s[i]
            if quote is not None:
                cur.append(ch)
                if ch == quote and (i == 0 or s[i - 1] != "\\"):
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                cur.append(ch)
            elif ch == "[":
                depth += 1
                cur.append(ch)
            elif ch == "]":
                depth = max(0, depth - 1)
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
            i += 1
        if "".join(cur).strip():
            parts.append("".join(cur))
    except Exception:
        pass
    return parts


def _parse_value(raw: str):
    """Valeur pythonique stricte : 'str' → str, nombres, booléens, null, listes simples."""
    try:
        t = (raw or "").strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
            inner = t[1:-1]
            return inner.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
        if t.startswith("[") and t.endswith("]"):
            return [_parse_value(p) for p in _split_top_level(t[1:-1])]
        low = t.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("none", "null"):
            return None
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        if re.fullmatch(r"-?\d+\.\d+", t):
            return float(t)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-\.]*", t):
            return t  # identifiant nu (ex: purpose=lire) → chaîne tolérée
        raise ValueError(f"valeur non parsable: {t[:60]}")
    except Exception:
        raise


def parse_pythonic_calls(content: str) -> list[dict]:
    """Extrait [{name, args}] depuis le texte. Noms allowlist, args stricts, le reste ignoré."""
    out: list[dict] = []
    try:
        for name, argstr in _extract_call_spans(content):
            if name not in ALLOWED_TOOLS:
                continue
            argstr = (argstr or "").strip()
            if not argstr:
                out.append({"name": name, "args": {}})
                continue
            args: dict = {}
            ok = True
            for part in _split_top_level(argstr):
                if "=" not in part:
                    ok = False
                    break
                k, v = part.split("=", 1)
                k = k.strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                    ok = False
                    break
                try:
                    args[k] = _parse_value(v)
                except Exception:
                    ok = False
                    break
            if ok:
                out.append({"name": name, "args": args})
    except Exception:
        pass
    return out


def _spec_error(code: str, message: str) -> str:
    return f"[{code}] {message}"


_BRIEF_REPLIE_VIDE = (
    "Le brief n'a pas pu être préparé : la soumission de l'orchestrateur a échoué "
    "et aucun résultat exploitable n'est disponible pour ce tour. "
    "Réponds à l'utilisateur à partir de sa demande et du contexte déjà présent "
    "dans la conversation. Ne prétends pas avoir consulté autre chose. Dire "
    "explicitement la limite : si un travail d'enquête était attendu, explique "
    "qu'il n'a pas pu être mené à bien et propose à l'utilisateur de reformuler."
)


def _valeurs(texte: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Agrège un champ du tour — chaîne ou liste — en éléments exploitables.

    Une absence reste une absence : on n'invente ni zéro ni contenu.
    """
    if texte is None:
        return []
    items = list(texte) if isinstance(texte, (list, tuple)) else [texte]
    return [str(item).strip() for item in items if str(item).strip()]


def _section(label: str, elements: list[str], absent: str) -> str:
    """Une ligne de brief : les éléments du tour, ou l'absence assumée. Pure."""
    if not elements:
        return f"{label} :\n- {absent}"
    return f"{label} :\n" + "\n".join(f"- {element}" for element in elements)


def _brief_replie(tour: dict | None) -> str:
    """Brief de repli : les cinq éléments de la fiche, rendus en texte (mono-champ).

    Construit depuis l'état réellement connu du tour : la synthèse est un gabarit
    déterministe, elle ne fabrique aucune conclusion et n'appelle aucun modèle
    (fiche §4.3 : « sans nouvelle inférence »). L'objectif initial, les éléments
    déjà obtenus, les sources disponibles, les opérations en échec et ce qui reste
    indéterminé portent le tour ; ce qui manque est dit, jamais deviné.
    """
    if not tour:
        return _BRIEF_REPLIE_VIDE
    objectif = str(tour.get("objective") or "").strip()
    sections = [
        f"Objectif : {objectif}" if objectif else "Objectif : la demande initiale",
        _section(
            "Éléments déjà obtenus",
            _valeurs(tour.get("obtained")),
            "aucun résultat exploitable n'a été obtenu pour ce tour",
        ),
        _section(
            "Sources disponibles",
            _valeurs(tour.get("sources")),
            "aucune source documentée pour ce tour",
        ),
        _section(
            "Opérations en échec",
            _valeurs(tour.get("failed_operations")),
            "aucune opération échouée n'a été observée pour ce tour",
        ),
        _section(
            "Reste indéterminé",
            _valeurs(tour.get("indeterminate")),
            "l'information nécessaire à la réponse n'a pas encore été établie",
        ),
    ]
    texte = "\n\n".join(sections)
    texte += (
        "\n\nPour la réponse : restituer ce qui a été obtenu et expliquer ce qui manque. "
        "Ne prétends pas avoir consulté autre chose."
    )
    return texte.strip()


def build_fallback_brief(tour: dict | None = None) -> dict:
    """Brief de repli déterministe quand le relais échoue (fiche submit_brief v1.2 §4.1, §4.2, §4.3).

    « Cette construction ne requiert pas que le modèle réussisse un ultime appel
    d'outil. Elle doit permettre de sortir même lorsque l'orchestrateur ou la
    validation ne répond plus correctement. » (§4.1) — « Le backend ne doit pas
    générer librement une nouvelle conclusion documentaire. » (§4.2) : la synthèse
    part de l'état réellement connu du tour, sans nouvelle inférence (§4.3), et le
    texte la porte en entier.

    Le tour (optionnel) porte les cinq éléments : l'objectif initial, les éléments
    déjà obtenus, les sources disponibles, les opérations en échec et ce qui reste
    indéterminé. Sans état fiable, le texte le dit : l'absence, jamais le silence.

    Retourne la forme mono-champ ``{"brief": texte}`` (§2.2) : les cinq éléments
    sont rendus DANS le texte, jamais en dict ``facts`` (§2.3, forme révoquée).
    """
    return {"brief": _brief_replie(tour)}


# --- Synthèse d'espace : le contexte documentaire de l'orchestrateur (tâche 5.4) ---
# D'OÙ VIENT CE MODULE : orchestration.md §2.1 — « Au début d'une discussion d'espace,
# l'orchestrateur reçoit une synthèse courte de l'espace pour comprendre le projet et
# ses orientations ». Le texte n'est PAS injecté en bloc dans le prompt du générateur :
# les éléments de synthèse utiles sont sélectionnés par l'orchestrateur et transmis
# dans le brief.
# OÙ VA QUOI : `_synthese` / `build_space_synthesis` condensent les orientations
# connues du registre d'espace en un court texte. La synthèse est un gabarit
# déterministe : elle ne fabrique aucune conclusion, n'appelle aucun modèle et ne
# traite pas un fait comme une instruction — elle le rend, avec son statut, son
# origine et sa fraîcheur, et dit l'absence quand le registre est vide.
# POURQUOI DÉTERMINISTE : la synthèse d'espace est un CONTEXTE documentaire, le même
# caractère que le brief de secours — son texte se construit sans inférence, se rejoue
# identiquement, et garde l'absence comme une valeur lisible plutôt qu'un silence.

_SYNTHESE_VIDE = (
    "Synthèse d'espace : aucune orientation n'a encore été établie pour cet espace."
)


def _lignes_de_la_fait(fact: dict) -> list[str]:
    """Les lignes d'une orientation : le contenu, puis le statut, l'origine et la
    fraîcheur lorsqu'elles sont connues. Une fiche sans contenu ne porte pas de fait :
    elle ne se synthétise pas, elle reste une absence.
    """
    contenu = str(fact.get("content") or "").strip()
    if not contenu:
        return []
    lignes = [f"- {contenu}"]
    for label, key in (
        ("Statut", "status"),
        ("Origine", "origin"),
        ("Fraîcheur", "freshness"),
    ):
        valeur = str(fact.get(key) or "").strip()
        if valeur:
            lignes.append(f"  {label} : {valeur}.")
    return lignes


def _synthese(facts: list[dict] | dict | None) -> str:
    """Texte de synthèse d'espace : les orientations connues, ou l'absence assumée.

    Gabarit déterministe — les faits transmis sont rendus tels quels, jamais réinterprétés.
    L'absence est une valeur : un registre vide produit un texte, jamais un silence.
    """
    if not facts:
        return _SYNTHESE_VIDE
    items = (
        [facts]
        if isinstance(facts, dict)
        else [fact for fact in (facts or []) if isinstance(fact, dict)]
    )
    blocs: list[str] = []
    for fact in items:
        lignes = _lignes_de_la_fait(fact)
        if lignes:
            blocs.append("\n".join(lignes))
    if not blocs:
        return _SYNTHESE_VIDE
    return "Synthèse d'espace :\n" + "\n\n".join(blocs)


def build_space_synthesis(facts: list[dict] | dict | None = None) -> dict:
    """Synthèse courte de l'espace, pour l'orchestrateur, au début d'un espace.

    « Au début d'une discussion d'espace, l'orchestrateur reçoit une synthèse courte
    de l'espace pour comprendre le projet et ses orientations. » (orchestration.md §2.1)

    La construction est déterministe et sans nouvelle inférence : elle ne fait que
    condenser les orientations transmises (contenu, statut, origine, fraîcheur), dans
    le respect de leur état. Elle ne fabrique pas une conclusion pour remplacer une
    information absente, et la synthèse n'est pas une instruction : les corrections et
    changements explicites du tour courant priment, l'orchestrateur sélectionne les
    éléments utiles et les transmet dans le brief.

    Retourne la forme mono-champ ``{"synthese": texte}`` : le texte porte la
    synthèse, jamais un dict de faits — le brief v1.2 (§2.2) impose déjà cette forme
    mono-champ pour le relais, et la synthèse suit la même règle de forme.
    """
    return {"synthese": _synthese(facts)}


# --- Un verdict ne se retente pas : reclasser l'échec, pas la question --------
# D'OÙ CETTE CLASSIFICATION : l'orchestrateur émettait `evidence_ids: ["generate_image"]`
# — un NOM D'OUTIL là où une preuve revient — et le retry reposait la question
# EXACTEMENT identique : même réponse, trois fois, pour un coût certain.
#
# LA FORME ACTUELLE DE LA RÈGLE (fiche v1.2) : la signature de submit_brief est
# `{"brief": une chaîne non vide}`. Toute erreur de FORME (argument manquant,
# contenu vide, appel vide) se corrige en réécrivant l'appel : retentable.
# Une panne du BACKEND (`SCHEMA_UNAVAILABLE`) : ne dépend pas du modèle, une
# seconde tentative peut passer. Verdict figé : plus aucun — une signature
# mono-argument n'a pas d'inventaire contre lequel figer un verdict.
# Une nouvelle famille de verdicts doit être déclarée ici : c'est le seul endroit
# qui décide du coût d'un échec.
NON_RETRYABLE_BRIEF_ERRORS = frozenset()


def brief_error_is_retryable(code: str) -> bool:
    """Ce code mérite-t-il qu'on repose la question au modèle ?

    Vrai par défaut (une panne passe, une forme se corrige) ; faux uniquement pour
    un verdict rendu contre un état que la phase ne peut pas faire grandir.
    """
    return (code or "").strip().upper() not in NON_RETRYABLE_BRIEF_ERRORS


_ALLOWED_BRIEF_ARGS = ("brief",)


def _message_de(code: str, detail: str) -> str:
    """Texte rendu au modèle : [CODE] détail. Pur."""
    return f"[{code}] {detail}"


def validate_submit_brief(args: dict) -> tuple[bool, str, dict]:
    """Valide le handoff submit_brief — fiche v1.2 §2.2 : signature minimale ``{"brief"}``.

    La fiche ne l'admet pas « en plus » de l'ancienne : §2.3, « Cette proposition
    supprime les obligations anciennes suivantes : — Une liste de un à vingt faits
    pour chaque demande. » La forme ``facts`` est donc RÉVOQUÉE au schéma
    (``UNKNOWN_ARG``, la table commune : « argument non autorisé par la
    signature ») — les 7 rubriques structurelles sortent avec elle.

    Ordre de contrôle (contrat commun §2.3) : parsing → signature → types →
    contenu. Retourne (ok, code, normalisé) ; codes : ok | EMPTY_CALL |
    UNKNOWN_ARG | SCHEMA_UNAVAILABLE | MISSING_REQUIRED | BAD_ARGS | EMPTY_BRIEF.
    Le coût d'un échec — retentable ou non — est décidé par
    `brief_error_is_retryable`.
    """
    # 0. Appel vide (contrat commun : aucun argument exploitable)
    if not isinstance(args, dict) or not args:
        return (
            False,
            "EMPTY_CALL",
            {
                "error": _message_de(
                    "EMPTY_CALL",
                    "Ton appel de submit_brief est vide. Renseigne brief : une chaîne non vide.",
                )
            },
        )
    # 0b. Clé inconnue, y compris une rubrique de l'ancienne forme (facts, limits…)
    _unknown = [k for k in args.keys() if k not in _ALLOWED_BRIEF_ARGS]
    if _unknown:
        return (
            False,
            "UNKNOWN_ARG",
            {
                "error": _message_de(
                    "UNKNOWN_ARG",
                    "La signature de submit_brief est un seul argument : brief, une chaîne non vide. "
                    "Retire les autres champs ("
                    + ", ".join(sorted(_unknown))
                    + ") et rédige tout dans brief.",
                )
            },
        )
    # 1. Schéma (pydantic : type de brief, contenu non vide)
    try:
        from backend.prompts.orchestrator import SubmitBriefArgs
    except Exception as e:
        # Panne du BACKEND, pas faute du modèle : un code à part, donc retentable
        # (voir brief_error_is_retryable). BAD_ARGS dirait « le modèle a mal répondu ».
        return (
            False,
            "SCHEMA_UNAVAILABLE",
            {
                "error": _message_de("SCHEMA_UNAVAILABLE", f"schéma indisponible: {e}")[
                    :500
                ]
            },
        )
    try:
        parsed = SubmitBriefArgs(**args)
    except Exception as e:
        blob = str(e)
        m = re.search(
            r"\[(EMPTY_BRIEF|EMPTY_CALL|UNKNOWN_ARG|MISSING_REQUIRED|ENUM_INVALID|"
            r"TOO_LONG|TOO_MANY|ID_UNKNOWN|ID_OUT_OF_SCOPE|EVIDENCE_UNKNOWN|"
            r"TIMEOUT|TOO_LARGE|BAD_ARGS|EMPTY_QUERY)\]",
            blob,
        )
        if m:
            code = m.group(1)
            msg = blob[blob.find(f"[{code}]") : blob.find(f"[{code}]") + 300]
            return False, code, {"error": msg}
        if "extra" in blob.lower() or "unexpected" in blob.lower():
            return (
                False,
                "UNKNOWN_ARG",
                {
                    "error": _message_de(
                        "UNKNOWN_ARG",
                        "La signature de submit_brief est un seul argument : brief, une chaîne non vide.",
                    )
                },
            )
        return False, "BAD_ARGS", {"error": _message_de("BAD_ARGS", blob[:500])}
    return True, "ok", parsed.model_dump()


def render_brief(normalized: dict) -> str:
    """Sérialise le brief validé en texte (bulle UI + colonne brief + DB inchangées).

    La fiche §2.1 : « Le brief est une préparation structurée en texte ou
    Markdown » — le rendu EST le contenu, borné au plafond d'affichage.
    """
    try:
        return str((normalized or {}).get("brief") or "").strip()[:6000]
    except Exception:
        return ""


def render_brief_json(normalized: dict) -> str:
    """Sérialise le brief validé en JSON compact pour le bloc <BRIEF_DONNEES> du générateur."""
    import json as _json

    try:
        return _json.dumps(
            {"brief": str((normalized or {}).get("brief") or "")}, ensure_ascii=False
        )
    except Exception:
        return '{"brief": ""}'

# backend/inference/media_policy.py — Stratégie toolcalls média (méthode officielle Phase 1)
#
# D'OÙ VIENT CE FICHIER : constat des logs q-r.md — le function-calling natif du GGUF
# LFM2.5-8B est défaillant (template non reconnu → `[vision(...)]` textuel au lieu de
# tool_call, stripé par le sanitizer). La décision d'observer un fichier ne doit donc
# JAMAIS dépendre du LLM : elle est prise ici, en pur déterministe, SANS modèle.
# OÙ VA QUOI : loop.py appelle needs_reinspection()/decide_reinspection()/plan_media_tools()
# (via wrappers compat) dans sa Phase 0, AVANT tout appel LLM ; clean_media_query() est
# appliqué aux requêtes VLM/OCR dans _execute_vision/_execute_doc_ocr.
# POURQUOI PAS DE MOTS-CLÉS ÉPARPILLÉS : avant, chaque fonction avait son tuple de termes
# (plan vs réinspection divergeaient → "lire ... en japonais" partait en vision). Ici une
# seule source de vérité, testée par test_media_policy.py (cas réels q-r.md en régression).
#
# Méthode en 3 règles :
#   R1. Nouveau fichier au tour courant → observer systématiquement (plan_media_tools).
#   R2. Pas de nouveau fichier + média dispo + question visuellement ancrée → réobserver
#       (needs_reinspection, 3 couches OR ci-dessous), avec la question comme query ciblée.
#   R3. Le registre SOURCES DISPONIBLES est la source de vérité ; get_source est l'outil
#       qui liste/montre TOUS les fichiers de la conversation (list-all sans source_id).
#
# Couches de détection R2 (OR — l'une suffit) :
#   A. Référence explicite : "cette image", "dans/sur l'image", "première/deuxième", ...
#   B. Verbe de réexamen : "revois", "regarde", "montre", "vérifie sur", ...
#   C. Question de détail visuel : "couleur", "cheveux", "porte", "tient", "fond",
#      "premier plan", "gauche/droite", "combien", ... (cas q-r.md T2 qui manquait)
from __future__ import annotations

# --- Couche A : référence explicite au média ---
EXPLICIT_REFERENCE_TERMS = (
    "cette image", "cette photo", "ce pdf", "ce document",
    "la première image", "la seconde image", "la deuxième image",
    "l'image précédente", "l'image d'avant",
    "celle-ci", "celle-là",
    "dans l'image", "sur l'image", "dans la photo", "dans le document",
    "première image", "deuxième image",
)

# --- Couche B : verbes de réexamen ---
REVISIT_VERBS = (
    "revois", "revoit", "revoir", "revoies",
    "relis", "réanalyse", "reanalyse",
    "regarde à nouveau", "regarde encore", "regarde de nouveau",
    "montre-moi", "montre moi",
    "vérifie sur", "verifie sur",
    "zoom",
)

# --- Couche C : lexique de détail visuel ---
# Une question portant sur ces détails exige un regard frais : la description
# stockée en historique ne contient presque jamais la réponse (cf. q-r.md :
# "quelle est la couleur de cheveux de la personne au fond ?").
VISUAL_DETAIL_TERMS = (
    "couleur", "cheveux", "yeux",
    "porte", "portent", "tient", "tiennent",
    "fond", "premier plan", "arrière-plan", "arriere-plan",
    "à gauche", "à droite", "au milieu", "au centre",
    "combien", "nombre de",
    "où est", "où sont", "ou est", "ou sont",
    "décris le", "décris la", "décris-moi le", "décris-moi la",
    "que porte", "qu'est-ce qu'on voit", "qu est-ce qu on voit",
    "on voit quoi",
)


def _low(message: str) -> str:
    return (message or "").lower()


def has_explicit_reference(message: str) -> bool:
    return any(t in _low(message) for t in EXPLICIT_REFERENCE_TERMS)


def has_revisit_verb(message: str) -> bool:
    return any(t in _low(message) for t in REVISIT_VERBS)


def asks_visual_detail(message: str) -> bool:
    return any(t in _low(message) for t in VISUAL_DETAIL_TERMS)


def needs_reinspection(message: str) -> bool:
    """R2 : True si la question est visuellement ancrée (l'une des 3 couches suffit).
    D'où vient l'appel : Phase 0 de run_agent_loop (loop.py), uniquement quand le tour
    n'apporte AUCUN nouveau fichier. Pourquoi OR et pas un score : un seul indice suffit
    (ex. "Revoit l'image." n'a que le verbe) ; un score complexifierait sans gain mesuré.
    """
    return has_explicit_reference(message) or has_revisit_verb(message) or asks_visual_detail(message)


def resolve_tool_for(kind: str, message: str, asks_text_fn=None) -> str:
    """R1/R2 : pdf→doc_ocr, image+demande de texte→doc_ocr, sinon vision."""
    if (kind or "").lower() == "pdf":
        return "doc_ocr"
    if asks_text_fn is not None:
        try:
            if asks_text_fn(message):
                return "doc_ocr"
        except Exception:
            pass
    return "vision"


def pick_target(message: str, entries: list[dict]) -> dict | None:
    """Choisit l'entrée à (ré)observer : ordinal explicite sinon dernière dispo."""
    avail = [e for e in (entries or []) if e.get("available", True) and e.get("id")]
    if not avail:
        return None
    low = _low(message)
    if ("première image" in low or "premiere image" in low or "premier pdf" in low or "premier document" in low) and len(avail) >= 1:
        return avail[0]
    if ("deuxième image" in low or "deuxieme image" in low or "seconde image" in low or "second pdf" in low) and len(avail) >= 2:
        return avail[1]
    return avail[-1]


# --- Demande d'extraction de texte (routage OCR) ---
# Unifie et étend les deux anciennes listes (plan vs réinspection) : toute formulation
# de lecture/OCR y compris "lire ce qu'il y a d'écrit en japonais" (q-r.md).
OCR_REQUEST_TERMS = (
    "ocr",
    "extrais le texte", "extraire le texte",
    "lis le texte", "lire le texte",
    "transcris", "transcrire",
    "texte sur l'image", "texte dans l'image",
    "quel est le texte", "que dit ce document",
    "qu'est-ce qui est écrit", "ce qu'il y a d'écrit",
    "écrit en japonais", "écrit en chinois",
    "japonais", "chinois",
    "tableau", "facture", "scan",
    "panneau", "étiquette", "inscription", "lisible",
)


def asks_for_text(message: str) -> bool:
    return any(t in _low(message) for t in OCR_REQUEST_TERMS)


# --- Cadrage requête VLM (l'utilisateur a raison : le VLM n'a pas le contexte LLM) ---
# Le VLM reçoit : prompt système + UNE question + l'image. Ni historique, ni déixis
# ("la précédente image", "et maintenant", "revoit l'image") qui le fait dérailler
# (ex. q-r.md : le VL se décrit lui-même au lieu de lire). On nettoie en déterministe.
_DEICTIC_STRIP = (
    "et sur la première image", "sur la première image",
    "et sur la deuxième image", "sur la deuxième image",
    "et sur la seconde image", "sur la seconde image",
    "la précédente image", "l'image précédente",
    "le premier document", "le deuxième document",
    "la première image", "la deuxième image", "la seconde image",
    "est-ce que tu arriverais à", "est-ce que tu pourrais",
    "peux-tu me dire", "peux tu me dire",
    "peux-tu", "peux tu",
    "et me dire", "me dire",
    "revoit l'image", "revois l'image", "revoir l'image", "revoir",
    "sur cette image", "dans cette image", "de cette image",
    "cette image", "cet image", "l'image",
    "et maintenant", "maintenant",
    "s'il te plaît", "s'il vous plaît", "stp",
)

_FILLER_ONLY = {"et", "alors", "donc", "et alors", "alors là", "oui", "non"}

DEFAULT_MEDIA_QUERY = "Décris cette image en détail."


def clean_media_query(message: str, default: str = DEFAULT_MEDIA_QUERY) -> str:
    """Retourne une question VLM autonome : sans déixis conversationnelle, sans verbes creux.
    D'où vient le besoin : le VLM (VL-3B/GLM-OCR) reçoit système + UNE question + l'image,
    SANS historique LLM. "La précédente image" ou "et maintenant" le font dérailler (q-r.md :
    le VL s'est décrit lui-même au lieu de lire). Le choix de l'image, lui, est déjà fait
    en amont (routage déterministe sur artifact_id), donc la requête n'a plus besoin de
    désigner le fichier — seulement de poser la question visuelle.
    Pourquoi des motifs statiques et pas le LLM pour reformuler : le LLM 8B est justement
    la pièce non fiable (tour supplémentaire, latence, hallucinations) ; une liste de
    motifs + tests de régression est prévisible et instantanée.
    """
    import re
    text = (message or "").strip()
    if not text:
        return default
    had_question = "?" in text
    # Plus longs d'abord (évite qu'un motif court ne casse un motif long)
    for pat in sorted(_DEICTIC_STRIP, key=len, reverse=True):
        text = re.sub(re.escape(pat), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.;:!?\u2026").strip()
    if not text or text.lower() in _FILLER_ONLY:
        return default
    text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
    return text + (" ?" if had_question else ".")


def decide_reinspection(message: str, current_entries: list[dict], candidate_entries: list[dict], asks_text_fn=None) -> dict | None:
    """Décision R2 pure et testable : None ou {"tool", "artifact_id", "name"}.
    D'où viennent les entrées : current_entries = artefacts résolus du tour courant
    (attachment_ids du frontend) ; candidate_entries = historique MessageAttachment
    de la conversation (construit par loop.build_history_artifact_entries).
    Où va le résultat : Phase 0 de loop.py l'exécute aussitôt (vision/doc_ocr) et verse
    l'observation au générateur — sans attendre l'orchestrateur LLM.
    Pourquoi current_entries court-circuite : R1 (plan_media_tools) observe déjà les
    nouveaux fichiers ; réinspecter en plus serait un double chargement modèle inutile.
    """
    if current_entries:
        return None
    if not needs_reinspection(message):
        return None
    target = pick_target(message, candidate_entries)
    if target is None:
        return None
    return {
        "tool": resolve_tool_for(target.get("kind") or "", message, asks_text_fn),
        "artifact_id": target.get("id"),
        "name": target.get("name") or target.get("id"),
    }

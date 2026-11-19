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

import re
from dataclasses import dataclass

# --- Couche A : référence explicite au média ---
# Les PHRASES pointent un média sans dire lequel. Les ORDINAUX (« la dernière
# image ») sont lus par `parse_media_reference` plus bas, sur la MÊME table
# d'ordinalité que `pick_target` et `find_files` : les lister ici en dur créerait
# une quatrième liste qui divergerait, et c'est déjà arrivé ici (voir l'en-tête).
_PHRASES_REFERENCE = (
    "cette image",
    "cette photo",
    "ce pdf",
    "ce document",
    "celle-ci",
    "celle-là",
    "dans l'image",
    "sur l'image",
    "dans la photo",
    "dans le document",
)

# --- Couche B : verbes de réexamen ---
REVISIT_VERBS = (
    "revois",
    "revoit",
    "revoir",
    "revoies",
    "relis",
    "réanalyse",
    "reanalyse",
    "regarde à nouveau",
    "regarde encore",
    "regarde de nouveau",
    "montre-moi",
    "montre moi",
    "vérifie sur",
    "verifie sur",
    "zoom",
)

# --- Couche C : lexique de détail visuel ---
# Une question portant sur ces détails exige un regard frais : la description
# stockée en historique ne contient presque jamais la réponse (cf. q-r.md :
# "quelle est la couleur de cheveux de la personne au fond ?").
VISUAL_DETAIL_TERMS = (
    "couleur",
    "cheveux",
    "yeux",
    "porte",
    "portent",
    "tient",
    "tiennent",
    "fond",
    "premier plan",
    "arrière-plan",
    "arriere-plan",
    "à gauche",
    "à droite",
    "au milieu",
    "au centre",
    "combien",
    "nombre de",
    "où est",
    "où sont",
    "ou est",
    "ou sont",
    "décris le",
    "décris la",
    "décris-moi le",
    "décris-moi la",
    "que porte",
    "qu'est-ce qu'on voit",
    "qu est-ce qu on voit",
    "on voit quoi",
)


def _low(message: str) -> str:
    return (message or "").lower()


# --- L'ORDINALITÉ d'une référence : le vocabulaire, puis le parseur ---
# POURQUOI ICI ET PAS DANS `find_files` : « la dernière image » est la MÊME question
# posée à deux étages — « quel fichier re-regarder ? » (R2, ce module) et « quel
# fichier nommer ? » (`find_files`). Deux listes, donc deux réponses possibles dans un
# même tour : c'est ce qu'on corrige. Le vocabulaire et le calcul de position sont donc
# écrits UNE fois ici, en pur, et les deux appelants les partagent.
#
# LE VOCABULAIRE est celui de la Bible §2.5 (« la dernière image que j'ai envoyée »,
# l. 153 et l. 167) et de la couche A : premier, deuxième/seconde, dernière,
# précédente/d'avant. Les mots sont lus SANS ACCENT et sans ponctuation, parce que
# « l'image d'avant » et « la 2ème image » doivent tomber dans la même règle que
# « la dernière image ».

_RANG_PAR_MOT: dict[str, int] = {
    "premier": 1,
    "premiers": 1,
    "premiere": 1,
    "premieres": 1,
    "second": 2,
    "seconds": 2,
    "seconde": 2,
    "secondes": 2,
    "deuxieme": 2,
    "deuxiemes": 2,
    "troisieme": 3,
    "troisiemes": 3,
    "quatrieme": 4,
    "quatriemes": 4,
    "cinquieme": 5,
    "cinquiemes": 5,
    "sixieme": 6,
    "sixiemes": 6,
    "septieme": 7,
    "septiemes": 7,
    "huitieme": 8,
    "huitiemes": 8,
    "neuvieme": 9,
    "neuviemes": 9,
    "dixieme": 10,
    "dixiemes": 10,
}
# « dernier » / « premier » / « précédent » sont des EXTRÊMES, pas des rangs : ils se
# calculent sur le nombre de pièces. Toutes leurs flexions sont acceptées, parce que
# « la dernière », « le dernier » et « la dernières » sont la même règle.
_POSITION_DERNIER = "dernier"
_POSITION_PREMIER = "premier"
_POSITION_PRECEDENT = "precedent"
_MOTS_DERNIER = frozenset({"dernier", "derniers", "derniere", "dernieres"})
_MOTS_PREMIER = frozenset({"premier", "premiers", "premiere", "premieres"})
_MOTS_PRECEDENT = frozenset({"precedent", "precedente", "precedents", "precedentes"})
# Suffixes d'ordinal chiffré : « 2ème », « 3e », « 1ère ». Un chiffre NU n'en est pas
# un — « la dernière image envoyée en 2 minutes » ne parle pas d'un deuxième fichier,
# et l'accepter ferait de « dernier » un « 2ème » sans que l'utilisateur l'ait dit.
_SUFFIXES_RANG = frozenset({"er", "ere", "eme", "e", "ieme"})

# Type nommé par un mot. Un mot qui ne nomme PAS un type n'en restreint aucun :
# « document » pourrait être un pdf comme un tableur, et choisir à la place de
# l'utilisateur serait inventer. `Artifact.kind` ne connaît que trois valeurs
# (image | pdf | sheet), donc « texte » n'en est pas une.
_TYPE_PAR_MOT: dict[str, str] = {
    "image": "image",
    "images": "image",
    "photo": "image",
    "photos": "image",
    "visuel": "image",
    "visuels": "image",
    "illustration": "image",
    "illustrations": "image",
    "pdf": "pdf",
    "tableur": "sheet",
    "tableurs": "sheet",
    "feuille": "sheet",
    "feuilles": "sheet",
    "classeur": "sheet",
    "classeurs": "sheet",
    "ods": "sheet",
    "csv": "sheet",
    "xlsx": "sheet",
    "xls": "sheet",
}
# La DIRECTION : « que je t'ai envoyée » dit QUI a envoyé le fichier. Son absence
# n'est pas un blanc : « la dernière image » sans précision reste une référence à
# l'ordre de la discussion, dépôt compris en explication mais pas en réponse.
_MOTS_ENVOYE = frozenset(
    {
        "envoye",
        "envoyee",
        "envoyes",
        "envoyees",
        "depose",
        "deposes",
        "deposee",
        "deposees",
        "partage",
        "partages",
        "partagee",
        "partagees",
    }
)
# Nom de média SANS type déductible : il désigne bien un fichier sans dire lequel. « Le
# dernier document » doit donc déclencher une ré-inspection, mais ne restreindre aucun
# type — ce sont deux décisions distinctes, et les confondre en choisirait un pdf pour un
# tableur.
_MOTS_MEDIA_SANS_TYPE = frozenset(
    {
        "fichier",
        "fichiers",
        "document",
        "documents",
        "page",
        "pages",
        "capture",
        "captures",
    }
)


@dataclass(frozen=True)
class ReferenceMedia:
    """Ce qu'une demande désigne par RÉFÉRENCE : un rang, pas un nom. Pur.

    `types` ne porte que les restrictions que l'utilisateur a ÉNONCÉES. `sent_by_user`
    ne vaut vrai que si la demande dit explicitement que le fichier vient de l'utilisateur.
    """

    position: (
        str  # _POSITION_DERNIER | _POSITION_PREMIER | _POSITION_PRECEDENT | "rang"
    )
    rang: int = 0  # 1-based ; 0 quand `position` est un extrême
    types: tuple[str, ...] = ()
    sent_by_user: bool = False


def _mots(message: str) -> list[str]:
    """Mots normalisés : minuscules, sans accent, ponctuation réduite à une espace. Pur."""
    import unicodedata as _u

    texte = _u.normalize("NFD", _low(message))
    texte = "".join(c for c in texte if not _u.combining(c))
    return re.sub(r"[^0-9a-z]+", " ", texte).split()


def _rang_du_mot(mot: str) -> int | None:
    """Rang 1-based d'un mot, ou `None` si le mot n'en est pas un. Pur."""
    rang = _RANG_PAR_MOT.get(mot)
    if rang is not None:
        return rang
    chiffres = 0
    while chiffres < len(mot) and mot[chiffres].isdigit():
        chiffres += 1
    if chiffres == 0 or mot[chiffres:] not in _SUFFIXES_RANG:
        return None
    valeur = int(mot[:chiffres])
    return valeur if 1 <= valeur <= 10 else None


def parse_media_reference(message: str) -> ReferenceMedia | None:
    """Lit une RÉFÉRENCE d'ordre dans une demande, ou `None` s'il n'y en a pas.

    PUR : aucune base, aucun modèle. C'est le seul endroit qui décide ce que
    « la dernière image que je t'ai envoyée » désigne — rang, type et direction — et
    les deux appelants (`pick_target`, `find_files`) lisent ce même verdict.

    Plusieurs ordinaux dans une même demande (« la première et la dernière ») : le
    PREMIER mentionné l'emporte, comme en français. Le rang retenu est décidé ici,
    jamais deviné plus loin.
    """
    mots = _mots(message)
    if not mots:
        return None
    rang, position = 0, ""
    for i, mot in enumerate(mots):
        if mot in _MOTS_DERNIER:
            position = _POSITION_DERNIER
            break
        if mot in _MOTS_PREMIER:
            position = _POSITION_PREMIER
            break
        if mot in _MOTS_PRECEDENT or mots[i : i + 2] == ["d", "avant"]:
            position = _POSITION_PRECEDENT
            break
        trouve = _rang_du_mot(mot)
        if trouve is not None:
            position, rang = "rang", trouve
            break
    if not position:
        return None
    types: list[str] = []
    for mot in mots:
        type_du_mot = _TYPE_PAR_MOT.get(mot)
        if type_du_mot and type_du_mot not in types:
            types.append(type_du_mot)
    return ReferenceMedia(
        position=position,
        rang=rang,
        types=tuple(types),
        sent_by_user=any(m in _MOTS_ENVOYE for m in mots),
    )


def reference_fichier(message: str) -> ReferenceMedia | None:
    """La référence si la demande désigne un FICHIER par son rang d'ajout, sinon `None`.

    Un ordinal seul ne suffit pas : « la dernière fois », « le dernier résultat » ne
    désignent aucun fichier. Il faut que la demande NOMME un média — et c'est la MÊME
    condition pour la ré-inspection et pour `find_files`, sinon les deux ouvrent sur des
    fichiers différents dès que la phrase ne nomme rien.

    Le type nommé est reporté tel quel par `parse_media_reference` : « document » y
    laisse `types` vide, parce qu'il ne distingue pas un pdf d'un tableur.
    """
    reference = parse_media_reference(message)
    if reference is None:
        return None
    if not any(
        mot in _TYPE_PAR_MOT or mot in _MOTS_MEDIA_SANS_TYPE for mot in _mots(message)
    ):
        return None
    return reference


def intitule_reference(reference: ReferenceMedia | None) -> str:
    """Comment NOMMER la position demandée, en français. Pur.

    L'intitulé vit ici et pas chez l'appelant : « le 2e » et « rang » sont deux
    formes du même fait, et les écrire deux fois les ferait diverger.
    """
    if reference is None:
        return "cette référence"
    if reference.position == "rang":
        return f"le {reference.rang}e"
    return {
        "dernier": "le dernier",
        "premier": "le premier",
        "precedent": "le précédent",
    }.get(reference.position, "cette référence")


def index_reference(reference: ReferenceMedia | None, total: int) -> int | None:
    """Position 0-based visée dans une liste de `total` pièces, ou `None`.

    `None` veut dire une chose précise : CETTE position N'EXISTE PAS. « la deuxième
    image » sur une seule image ne doit pas rendre la première — ce serait une
    réponse inventée. Le calcul est TOTALLEMENT défini pour tout `total >= 0`.

    WHY « précédent » = `total - 2` : « l'image précédente » se lit par rapport à la
    dernière, donc c'est celle d'avant elle. À une seule pièce, elle n'existe pas.
    """
    if reference is None or total <= 0:
        return None
    if reference.position == _POSITION_DERNIER:
        return total - 1
    if reference.position == _POSITION_PREMIER:
        return 0
    if reference.position == _POSITION_PRECEDENT:
        return total - 2 if total >= 2 else None
    rang = int(reference.rang or 0)
    return rang - 1 if 1 <= rang <= total else None


# `EXPLICIT_REFERENCE_TERMS` reste exporté : c'est le nom que le reste du dépôt
# connaît, et le repli de `loop.requires_media_reinspection` s'appuie sur la même
# idée. Les collocations ordinaux sont DÉRIVÉES de la table ci-dessus, donc un mot
# d'ordinalité ajouté ici déclenche la ré-inspection sans qu'on oublie de le lister.
EXPLICIT_REFERENCE_TERMS = (
    _PHRASES_REFERENCE
    + tuple(
        f"{ordinal} {nom}"
        for ordinal in ("première", "deuxième", "dernière")
        for nom in ("image", "photo", "pdf", "document", "tableur")
    )
    + ("l'image précédente", "l'image d'avant", "la photo précédente")
)


def has_explicit_reference(message: str) -> bool:
    """Couche A : la demande POINTE un média, par deixis ou par ordinalité.

    L'ordinalité passe par le parseur et non par une liste de collocations : « la
    dernière image » et « le dernier pdf que je t'ai envoyé » doivent compter, et
    aucune des deux ne se déduit d'un couple de mots figé.
    """
    if any(t in _low(message) for t in _PHRASES_REFERENCE):
        return True
    return reference_fichier(message) is not None


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
    return (
        has_explicit_reference(message)
        or has_revisit_verb(message)
        or asks_visual_detail(message)
    )


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


def _dans_le_type(entrees: list[dict], types: tuple[str, ...]) -> list[dict]:
    """Ne garde que les entrées dont le type CONVIENT, et celles qui n'en déclarent pas.

    Une entrée sans `kind` ne peut pas être écartée : on ne refuse pas ce qu'on ignore.
    Une entrée qui déclare un type AUTRE est écartée : « la dernière image » ne peut
    pas rendre un pdf, même quand un pdf est le dernier fichier de la discussion.

    Aucune entrée ne déclarant de type, la restriction est INAPPLICABLE et la liste
    reste entière. C'est le cas des listes construites à la main ; en production
    `loop._registry_entry` renseigne toujours `kind`, donc c'est un vrai filtre.
    """
    if not types or not any(str(e.get("kind") or "") for e in entrees):
        return list(entrees)
    connus = set(types)
    return [
        e
        for e in entrees
        if not str(e.get("kind") or "") or str(e.get("kind") or "").lower() in connus
    ]


def pick_target(message: str, entries: list[dict]) -> dict | None:
    """Choisit l'entrée à (ré)observer : la référence si la demande en porte une,
    sinon la dernière disponible.

    La restriction de TYPE est appliquée AVANT le calcul du rang : « la dernière
    image » portant sur [pdf, image] doit rendre l'image, pas le dernier fichier.
    C'est le même `reference_fichier` que `find_files` : les deux désignent donc
    le même fichier dans un même tour.

    Une référence dont la position N'EXISTE pas retombe ici sur la dernière
    disponible : R2 est un ré-examen au mieux, il vaut mieux regarder quelque chose
    que rien. `find_files`, lui, répond sur l'IDENTITÉ d'un fichier et signale donc
    l'absence au lieu de choisir à la place de l'utilisateur.
    """
    avail = [e for e in (entries or []) if e.get("available", True) and e.get("id")]
    if not avail:
        return None
    reference = reference_fichier(message)
    cible = _dans_le_type(avail, reference.types if reference else ())
    position = index_reference(reference, len(cible))
    return cible[position] if position is not None else cible[-1]


# --- Demande d'extraction de texte (routage OCR) ---
# Unifie et étend les deux anciennes listes (plan vs réinspection) : toute formulation
# de lecture/OCR y compris "lire ce qu'il y a d'écrit en japonais" (q-r.md).
OCR_REQUEST_TERMS = (
    "ocr",
    "extrais le texte",
    "extraire le texte",
    "lis le texte",
    "lire le texte",
    "transcris",
    "transcrire",
    "texte sur l'image",
    "texte dans l'image",
    "quel est le texte",
    "que dit ce document",
    "qu'est-ce qui est écrit",
    "ce qu'il y a d'écrit",
    "écrit en japonais",
    "écrit en chinois",
    "japonais",
    "chinois",
    "tableau",
    "facture",
    "scan",
    "panneau",
    "étiquette",
    "inscription",
    "lisible",
)


def asks_for_text(message: str) -> bool:
    return any(t in _low(message) for t in OCR_REQUEST_TERMS)


# --- Cadrage requête VLM (l'utilisateur a raison : le VLM n'a pas le contexte LLM) ---
# Le VLM reçoit : prompt système + UNE question + l'image. Ni historique, ni déixis
# ("la précédente image", "et maintenant", "revoit l'image") qui le fait dérailler
# (ex. q-r.md : le VL se décrit lui-même au lieu de lire). On nettoie en déterministe.
_DEICTIC_STRIP = (
    "et sur la première image",
    "sur la première image",
    "et sur la deuxième image",
    "sur la deuxième image",
    "et sur la seconde image",
    "sur la seconde image",
    "la précédente image",
    "l'image précédente",
    "le premier document",
    "le deuxième document",
    "la première image",
    "la deuxième image",
    "la seconde image",
    "est-ce que tu arriverais à",
    "est-ce que tu pourrais",
    "peux-tu me dire",
    "peux tu me dire",
    "peux-tu",
    "peux tu",
    "et me dire",
    "me dire",
    "revoit l'image",
    "revois l'image",
    "revoir l'image",
    "revoir",
    "sur cette image",
    "dans cette image",
    "de cette image",
    "cette image",
    "cet image",
    "l'image",
    "et maintenant",
    "maintenant",
    "s'il te plaît",
    "s'il vous plaît",
    "stp",
)

_FILLER_ONLY = {"et", "alors", "donc", "et alors", "alors là", "oui", "non"}

DEFAULT_MEDIA_QUERY = "Décris cette image en détail."


def clean_media_query(message: str, default: str = DEFAULT_MEDIA_QUERY) -> str:
    """Retourne une question VLM autonome : sans déixis conversationnelle, sans verbes creux.
    D'où vient le besoin : le VLM (VL-3B / worker PaddleOCR-VL — GLM-OCR, jamais livré, est abandonné)
    reçoit système + UNE question + l'image,
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


def decide_reinspection(
    message: str,
    current_entries: list[dict],
    candidate_entries: list[dict],
    asks_text_fn=None,
) -> dict | None:
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

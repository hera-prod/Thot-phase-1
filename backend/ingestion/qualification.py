# ingestion/qualification.py — Qualification initiale d'une image par la vision générale
#
# FONDEMENT (docs/intentions, lecture seule — citations exactes) :
# - modeles/lfm2.5-vl-3b.md §1.2 : « Qualification initiale : déterminer si une
#   image à ingérer représente un document à lire ou une image générale, puis
#   orienter vers la chaîne de traitement adaptée. »
# - modeles/lfm2.5-vl-3b.md §3.5 : « Qualification initiale : document ou image
#   générale | Vision générale »
# - modeles/paddleocr-vl-1.6.md §3.2 : « La qualification initiale est établie
#   par le modèle de vision générale : l'image représente-t-elle un document à
#   lire ? Si oui, la chaîne documentaire PP-DocLayoutV3 + PaddleOCR-VL prend le
#   relais pour la mise en page, la reconnaissance et l'assemblage. »
# - modeles/pp-doclayoutv3.md §4.3 : « La qualification initiale entre image
#   générale et document appartient au modèle de vision générale ;
#   PP-DocLayoutV3 n'est pas ce classificateur. »
# - mecanismes/ingestion/images.md §3.1 : le parcours comporte « une qualification
#   initiale par le modèle de vision générale, qui détermine si l'image
#   représente un document à lire ou une image générale ».
#
# DÉCISIONS MUETTES (convention interne, nommées ici une seule fois) :
# - PROMPT_QUALIFICATION : la consigne posée au VL. Un seul mot attendu.
# - FORMAT_QUALIFICATION : le verdict est la chaîne "document" ou "image".
#   Toute autre réponse est un verdict AMBIGU → ValueError (échec bruyant).
#   Jamais un seuil de caractères : le comptage IMAGE_DOC_MIN_CHARS est mort,
#   la décision appartient au modèle (lfm2.5-vl-3b.md §3.5, tableau §3.5).
from __future__ import annotations

import re

PROMPT_QUALIFICATION = """Tu qualifies une image avant ingestion.
Réponds par un seul mot : « document » ou « image ».
- « document » : l'image montre une page à lire — texte dense, capture d'écran d'application, facture, tableau, formulaire, document photographié ou scanné.
- « image » : photo, scène, illustration, graphique, sans contenu documentaire à lire.
Un seul mot, rien d'autre."""

# Convertit la réponse du VL en verdict : "document" | "image". Aucun autre état :
# un verdict ambigu est une ERREUR, jamais une route par défaut (échec bruyant).

_MOT_DOCUMENT = re.compile(r"\bdocument\b")
_MOT_IMAGE = re.compile(r"\bimage\b")


def verdict_qualification(reponse: str) -> str:
    """Lit la réponse du VL et renvoie le verdict. Pur.

    On PARSE, on ne valide pas : la réponse brute est lue ; exactement un mot
    reconnu tranche. Les deux mots (ou aucun) : la réponse ne tranche pas →
    ValueError qui cite la réponse tronquée. Un échec muet ferait ingérer une
    photo dans l'OCR ou un document en description, sans trace.
    """
    brut = (reponse or "").lower()
    doc = _MOT_DOCUMENT.search(brut)
    img = _MOT_IMAGE.search(brut)
    if doc is not None and img is None:
        return "document"
    if img is not None and doc is None:
        return "image"
    # Les deux mots (ou aucun) : la réponse ne tranche pas — on ne choisit pas
    # à la place du modèle, on refuse (échec bruyant, images.md §3.1 : « Résultat
    # ambigu | Repli borné ou état explicite de limite »).
    raise ValueError(
        f"qualification initiale : verdict ambigu ({(reponse or '')[:120]!r}) — "
        "attendu « document » ou « image »"
    )


def qualifier_image(png_bytes: bytes, appeler_vision) -> str:
    """Pose la qualification à la vision générale et renvoie le verdict.

    `appeler_vision(consigne: str, png_bytes: bytes) -> str` est la couche
    d'appel (loop) : seule elle sait joindre le contenu visuel réel au modèle
    LFM2.5-VL-3B — « Le modèle reçoit réellement les pixels par le parcours
    multimodal compatible » (mecanismes/ingestion/images.md §2.4). Une erreur
    de vision traverse ici sans être absorbée : sans verdict, il n'y a pas de
    route légitime.
    """
    if not png_bytes:
        raise ValueError("image vide")
    return verdict_qualification(appeler_vision(PROMPT_QUALIFICATION, png_bytes))

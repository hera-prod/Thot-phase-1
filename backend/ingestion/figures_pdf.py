# ingestion/figures_pdf.py — Recadrage, réexamen vision et blocs Markdown des figures d'un PDF (tâche 4.4)
#
# FONDEMENTS (docs/intentions, lecture seule — citations exactes) :
# - modeles/lfm2.5-vl-3b.md §3.3 : « Dans la chaîne documentaire, les figures
#   détectées sont recadrées puis réexaminées par le modèle de vision ; leur
#   résultat est réintégré au Markdown de la représentation du document. »
# - modeles/lfm2.5-vl-3b.md §3.3 : « Une figure utile peut être isolée depuis
#   une page et analysée comme une image. Le résultat conserve la référence au
#   PDF, à la page et à la région. »
# - modeles/lfm2.5-vl-3b.md §2.1 : « Un crop fourni pour une analyse ciblée
#   doit conserver son lien avec l'image ou la page d'origine. Si l'image a été
#   redimensionnée ou recadrée, les repères de localisation doivent rendre la
#   transformation explicable. »
# - modeles/pp-doclayoutv3.md §4.5 : « Une figure peut être recadrée et traitée
#   comme une image pour obtenir une description ou détecter un tableau
#   photographié. » et « Tout retraitement imbriqué doit être borné :
#   profondeur, nombre de régions, budget d'exécution et condition de
#   progression. Une figure identique qui revient au détecteur sans changement
#   utile ne doit pas déclencher une récursion illimitée. »
# - mecanismes/ingestion/pdf.md §3.4 : « Une description visuelle garde
#   l'identité du document, la page et la région. »
# - mecanismes/ingestion/pdf.md §4.5 : « Rendus et figures dérivées restent
#   liés au PDF ; ils ne deviennent pas automatiquement des images autonomes de
#   galerie. » → la figure est donc un PASSAGE du Markdown du document, jamais
#   un artefact autonome : ce module ne crée aucun artefact ni rattachement
#   galerie — la sémantique produit n'est pas muette là-dessus (mecanismes/
#   ingestion/images.md §1.2 :
#   « Une figure extraite ne devient pas automatiquement une image autonome de
#   galerie »).
#
# CONVENTIONS INTERNES NOMMÉES (points muets — pdf.md §3.6 : « Le format
# physique des repères est à définir » ; lfm2.5-vl-3b.md §5.5 : « Prompts
# d'ingestion et de réexamen testés en français » reste un point ouvert) :
# - REPÈRE de région : « page N · région K », N et K numérotation PHYSIQUE
#   commençant à 1 (pdf.md §2.2 : « utiliser une numérotation physique de
#   référence à partir de un ») ; K est l'index de la région DANS L'ORDRE DE
#   LECTURE rendu par `detect_regions` (paddle_worker trie déjà (y, x)).
# - BLOC Markdown de figure : un commentaire machine `<!-- thot:figure
#   page=N région=K -->` (grep-able, lisible par un parseur) suivi d'une ligne
#   humaine `**Figure** (page N · région K) : {description}`. La description
#   est une DESCRIPTION, jamais présentée comme la transcription du document
#   (representations_exploitables.md §3.2 : « Les descriptions ou analyses des
#   figures pertinentes » ; descriptions ≠ citations originales, pdf.md §3.6).
# - BORNES de retraitement imbriqué (pp-doclayoutv3.md §4.5) :
#   profondeur = 1 PAR CONSTRUCTION (le crop part vers la VISION GÉNÉRALE,
#   jamais re-revendé à `detect_regions`, donc pas de récursion possible) ;
#   nombre de régions borné par `CROPS_FIGURE_MAX_PAR_PAGE` ; budget
#   d'exécution = sérialisation existante de l'ingestion (`_serie`).
# - LABELS : le nom est comparé aux libellés réellement rendus par la révision
#   posée de PP-DocLayoutV3 ; l'ORDRE des classes n'est jamais utilisé pour
#   déduire une catégorie (pp-doclayoutv3.md §2.1 : « La taxonomie ne doit pas
#   être reconstruite à partir d'un ordre de classes supposé »). La config
#   posée (models/PP-DocLayoutV3_safetensors/config.json → id2label) rend
#   « image » et « chart » ; « figure_title » est un TITRE de figure, pas une
#   région de figure → exclu.
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.ingestion import gate as _gate


class RecadrageDiffere(RuntimeError):
    """La re-détection du layout n'a pas pu se faire MAINTENANT.

    Distincte d'une dérive de layout : `ResourceBusy`/`ModeleIndisponible`
    disent « ressource occupée ou modèle absent », pas « la région a changé ».
    L'appelant diffère la figure sans consommer d'essai ; une région introuvable
    ou métamorphosée reste, elle, une limite terminale motivée.
    """


def _est_indisponibilite_transitoire(exc: BaseException) -> bool:
    """Vrai pour une contention/indisponibilité qui se rejoue plus tard."""
    from backend.inference.resource_coordinator import ResourceBusy
    from backend.ingestion.paddle_client import ModeleIndisponible

    return isinstance(exc, (ResourceBusy, ModeleIndisponible))


@dataclass(frozen=True)
class FigurePlaceholder:
    """Une figure REPÉRÉE dont la description est différée à l'étape B.

    `bbox` est dans le repère pixel de la page rendue (même repère que
    `cropper_figure`). `label` et `score` sont ceux du layout, conservés pour que
    l'étape B sache quelle région recalculer sans redétecter au hasard.
    """

    page: int
    region: int
    label: str | None = None
    score: float | None = None
    bbox: tuple[float, float, float, float] | None = None


# Consigne de description du crop — reprise du bloc « Ingestion » de la fiche
# (lfm2.5-vl-3b.md, deux familles de consignes ligne 153 : « Elles servent
# d'exemples de rédaction, pas de prompts déjà validés » ; bloc « Ingestion »
# l.155-159). C'est bien CETTE famille qu'il faut pour le réexamen d'une
# figure : une description pour la RECHERCHE ultérieure du crop.
# (Le bloc « Réexamen » de la fiche ne convient pas : il répond à une question
# fournie, tandis qu'une figure n'en reçoit aucune — le §3.3 demande une
# description réintégrée au Markdown.)
PROMPT_FIGURE_REEXAMINEE = """Décris cette image en français pour permettre sa recherche ultérieure.
Précise les éléments visibles importants et leurs relations.
Distingue le texte lisible de tes descriptions.
Signale les détails incertains ou illisibles sans les inventer."""

# Libellés de région dont le contenu visuel est une FIGURE à recadrer. Pose
# d'après les noms (`label`) réellement produits par la révision posée —
# jamais d'après l'ordre numérique des classes (pp-doclayoutv3.md §2.1).
LABELS_REGION_FIGURE = frozenset(
    {"image", "figure", "illustration", "picture", "chart", "graphique"}
)

CROPS_FIGURE_MAX_PAR_PAGE = 8  # pp-doclayoutv3.md §4.5 : retraitement imbriqué borné

# Le libellé « image » est AMBIGU : il peut désigner une figure (à décrire) ou
# une photo de page insérée dans le document (à OCRiser). On ne paie pas un
# appel de qualification VL pour chaque région : on mesure la densité de texte
# OCR du crop, et au-delà de ce seuil la région est traitée comme du texte.
# Le coût reste borné par `CROPS_FIGURE_MAX_PAR_PAGE` et ne concerne QUE ce
# libellé — « figure », « chart », etc. partent directement en description.
LABELS_REGION_PHOTO_PAGE = frozenset({"image"})
# Seuil de densité OCR d'un crop « image », en caractères : au-delà, la région
# est traitée comme une photo de page (texte) plutôt que décrite. Le compromis
# est assumé, pas mesuré : 120 est PRUDENT — un crop de figure dense dont
# l'OCR rend beaucoup de bruit (légendes, axes, mots isolés) peut franchir le
# seuil et perdre sa DESCRIPTION au profit d'un texte mal interprété. Le
# remonter favoriserait les descriptions de figures au prix d'OCR complets
# payés sur des photos de page ; le descendre ferait l'inverse. Ce nombre est
# donc un cran de sûreté en faveur du texte, à réviser sur un corpus mesuré.
SEUIL_TEXTE_PHOTO_CROP = 120


def region_est_photo_de_page(
    crop: Any, ocr_crop: Callable[[Any], str]
) -> tuple[bool, str]:
    """Arbitre « figure vs photo de page » par densité de texte OCR du crop.

    `ocr_crop(crop) -> str` est la couche d'appel OCR fournie par l'appelant
    (elle seule connaît le worker). Un crop illisible ne déclenche AUCUN OCR :
    il n'y a pas de densité à mesurer. Rend `(est_photo, texte_ocr)` ; quand
    `est_photo` est vrai, l'appelant utilise `texte_ocr` comme bloc texte au
    lieu de décrire la région — le double travail OCR + description est évité.
    """
    if crop is None:
        return False, ""
    texte = (ocr_crop(crop) or "").strip()
    return len(texte) >= SEUIL_TEXTE_PHOTO_CROP, texte


def regions_figure(
    regions: list[dict], *, seuil: float | None = None
) -> list[tuple[int, dict]]:
    """Régions figure d'une page, chacune avec son INDEX DE LECTURE (à partir de 1).

    `regions` : régions adaptées par `gate.regions_vers_gate` (catégorie, score,
    boîte) — les régions stockées du câblage lot 2, consommées ici sans
    re-détection. Le seuil est celui du gate (`LAYOUT_CONFIDENCE_THRESHOLD`
    backend/config/defaults.py), une seule définition. Pur. La liste n'est
    PAS bornée ici : c'est l'APPELANT qui applique la borne de recadrage et
    documente ce qu'elle tranche — un bornage invisible ferait disparaître
    des figures sans trace (pdf.md §3.5 : « Pas de duplication
    silencieuse »).
    """
    limite = _gate.LAYOUT_CONFIDENCE_THRESHOLD if seuil is None else float(seuil)
    retenues: list[tuple[int, dict]] = []
    for index_region, region in enumerate(regions or [], start=1):
        categorie = str((region or {}).get("category", "")).strip().lower()
        if categorie not in LABELS_REGION_FIGURE:
            continue
        try:
            if float((region or {}).get("score", 0.0)) < limite:
                continue
        except (TypeError, ValueError):
            continue
        retenues.append((index_region, region))
    return retenues


def premiere_region_de(regions: list[dict], labels: frozenset[str]) -> int | None:
    """Index (1-based, ordre de lecture) de la PREMIÈRE région d'UN des labels. Pur."""
    for index_region, region in enumerate(regions or [], start=1):
        if str((region or {}).get("category", "")).strip().lower() in labels:
            return index_region
    return None


def cropper_figure(image: Any, boite: Any) -> Any | None:
    """Crop en pixels réels depuis la page DÉJÀ rendue, ou None si indéfini.

    La boîte arrive dans le repère pixel de l'image transmise à
    `detect_regions` — le worker demande `target_sizes=[image.size]`
    (paddle_worker._layout_regions) — donc le crop garde la transformation
    explicite vers la source (lfm2.5-vl-3b.md §2.1). Une boîte absente,
    illisible ou dégénérée ne produit PAS de crop : recadrer quand même une
    boîte dégénérée inventerait une frontière fictive ; la limite est notée
    dans le bloc de figure. Pur.
    """
    if image is None or not boite or len(boite) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in boite[:4])
    except (TypeError, ValueError):
        return None
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    taille = image.size
    # Bornage au contenu réel de la page : le détecteur peut sortir léger­ement
    # hors cadre ; un crop qui déborde croirait décrire une zone qui n'existe pas.
    x0 = max(0, int(x0))
    y0 = max(0, int(y0))
    x1 = min(taille[0], int(round(x1)))
    y1 = min(taille[1], int(round(y1)))
    if x0 >= x1 or y0 >= y1:
        return None  # boîte hors page : aucune intersection avec le contenu réel
    crop = image.crop((x0, y0, x1, y1))
    if crop.width < 2 or crop.height < 2:
        return None  # dégénéré : pas d'appel vision sur deux pixels
    return crop


def repere_region(numero_page: int, index_region: int) -> str:
    """Repère de localisation d'une région, convention interne nommée. Pur."""
    return f"page {numero_page} · région {index_region}"


def bbox_depuis_json(brut: Any) -> tuple[float, float, float, float] | None:
    """Boîte stockée en base, relue en tuple de flottants. Pure.

    Une boîte absente ou illisible rend `None` : le worker recalculera la
    région par détection, jamais un crop inventé autour de zéro.
    """
    if brut is None or brut == "":
        return None
    try:
        valeurs = json.loads(brut) if isinstance(brut, str) else brut
    except (TypeError, ValueError):
        return None
    if not isinstance(valeurs, (list, tuple)) or len(valeurs) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in valeurs[:4])
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def crop_depuis_pdf(
    chemin: str | Path,
    numero_page: int,
    index_region: int,
    *,
    bbox_json: Any = None,
    label: str | None = None,
    score: float | None = None,
    dpi: int | None = None,
    detecter_regions: Callable[..., list[dict]] | None = None,
    kind: str = "ingestion",
    owner: str | None = None,
) -> tuple[Any | None, str | None]:
    """Recalcule le crop d'une figure depuis le PDF, ou nomme la cause.

    DÉTERMINISME : `dpi` vaut par défaut `gate.PDF_RENDER_DPI_CONFIG` (200 ppp),
    la résolution qui a produit les bbox de l'étape A. Rendre la page à un autre
    DPI recadrerait une autre zone : le paramètre est explicite, pas un réglage
    caché.

    La bbox stockée est utilisée EN PRIORITÉ : elle désigne la région qui a été
    repérée, sans redétecter au hasard. Si elle est absente, la page est
    re-détectée et la région est retrouvée par son index de lecture et son
    libellé. Un index disparu ou un libellé qui a changé est un layout drift :
    on rend `None` avec la cause, jamais un crop d'une mauvaise zone.
    """
    from backend.ingestion import paddle_client as _pc

    page_numero = int(numero_page)
    if page_numero < 1:
        return None, f"numéro de page invalide : {numero_page!r}"
    try:
        import pymupdf
    except Exception as exc:  # pragma: no cover — dépendance d'exécution
        return None, f"PyMuPDF indisponible : {type(exc).__name__}: {exc}"

    try:
        document = pymupdf.open(str(chemin))
    except Exception as exc:
        return None, f"PDF illisible : {type(exc).__name__}: {exc}"
    try:
        if page_numero > document.page_count:
            return None, (
                f"page {page_numero} absente du PDF ({document.page_count} page(s))"
            )
        page = document.load_page(page_numero - 1)
        try:
            image = _gate.render_page_png(page, dpi=dpi)
        except Exception as exc:
            return None, f"rendu de la page impossible : {type(exc).__name__}: {exc}"

        bbox = bbox_depuis_json(bbox_json)
        if bbox is not None:
            crop = cropper_figure(image, bbox)
            if crop is None:
                return None, (
                    f"layout drift : la boîte stockée {bbox} ne recouvre plus "
                    "de contenu sur la page rendue"
                )
            return crop, None

        # Pas de boîte en base : re-détection explicite, puis retrouvaille par
        # index de lecture. C'est le seul chemin où le layout est relu.
        try:
            png = _pc.pil_to_png_bytes(image)
            detecter = detecter_regions or _pc.detect_regions
            regions_brutes = detecter(png, kind=kind, owner=owner)
            regions = _gate.regions_vers_gate(regions_brutes)
        except Exception as exc:
            if _est_indisponibilite_transitoire(exc):
                raise RecadrageDiffere(
                    f"re-détection du layout indisponible : {type(exc).__name__}: {exc}"
                ) from exc
            return (
                None,
                f"re-détection du layout impossible : {type(exc).__name__}: {exc}",
            )

        candidates = dict(regions_figure(regions))
        region = candidates.get(int(index_region))
        if region is None:
            return None, (
                f"layout drift : la région {index_region} n'est plus une figure "
                f"dans la page {numero_page}"
            )
        categorie = str(region.get("category", "")).strip().lower()
        if label and categorie != str(label).strip().lower():
            return None, (
                f"layout drift : la région {index_region} est devenue {categorie!r}, "
                f"attendu {str(label).strip().lower()!r}"
            )
        if score is not None:
            try:
                if abs(float(region.get("score", 0.0)) - float(score)) > 0.35:
                    return None, (
                        f"layout drift : confiance de la région {index_region} "
                        "trop différente de l'étape A"
                    )
            except (TypeError, ValueError):
                pass
        crop = cropper_figure(image, region.get("box"))
        if crop is None:
            return None, (
                f"layout drift : la région {index_region} ne produit plus de crop "
                "exploitable"
            )
        return crop, None
    finally:
        try:
            document.close()
        except Exception:
            pass


def bloc_figure(numero_page: int, index_region: int, description: str) -> str:
    """Bloc Markdown d'une figure réexaminée, avec son repère de page/région. Pur."""
    repere = repere_region(numero_page, index_region)
    return (
        f"<!-- thot:figure page={numero_page} région={index_region} -->\n"
        f"**Figure** ({repere}) : {description.strip()}"
    )


def bloc_figure_en_attente(numero_page: int, index_region: int) -> str:
    """Bloc positionnel d'une figure dont la description est DIFFÉRÉE. Pur.

    Le commentaire machine porte `attente` pour qu'un grep ou le futur patcheur
    distingue sans ambiguïté une figure décrite d'une figure encore due. Le texte
    humain dit la même chose : « description en attente », jamais une description
    inventée ni un silence. Le bloc garde son repère de page/région, donc il
    s'intercale à sa position de lecture comme une figure décrite.
    """
    repere = repere_region(numero_page, index_region)
    return (
        f"<!-- thot:figure page={numero_page} région={index_region} attente -->\n"
        f"**Figure** ({repere}) : description en attente"
    )


def bloc_figure_limite(numero_page: int, index_region: int, cause: str) -> str:
    """Bloc Markdown d'une figure repérée mais non réexaminée, cause nommée.

    pdf.md §4.1 : un état partiel reste explicite — « Une page rendue n'est
    pas nécessairement une page entièrement reconnue ». La figure garde son
    repère : sa présence et sa limite sont documentées, jamais un silence. Pur.
    """
    repere = repere_region(numero_page, index_region)
    return (
        f"<!-- thot:figure page={numero_page} région={index_region} -->\n"
        f"**Figure** ({repere}) : (réexamen indisponible — {cause.strip()})"
    )


def reexaminer_figure(png_bytes: bytes, appeler_vision) -> str:
    """Pose la description de la figure à la vision générale, avec les pixels du crop.

    `appeler_vision(consigne, png_bytes) -> str` est la couche d'appel
    (backend/ingestion/vision_appel.py) : seule elle accède au modèle. Le
    modèle reçoit les pixels RÉELS du crop — l'appelant (ou le test) vérifie
    l'ordre et le contenu, ici on parse la réponse : une réponse vide est une
    erreur nommée (une description inventée ne décrit rien de réel : la
    recherche irait pointer un contenu qui n'existe pas). Pur (hors appel).
    """
    if not png_bytes:
        raise ValueError("crop vide : aucune figure à examiner")
    if appeler_vision is None:
        raise ValueError(
            "couche d'appel vision absente : aucune figure à reviser sans elle"
        )
    reponse = (appeler_vision(PROMPT_FIGURE_REEXAMINEE, png_bytes) or "").strip()
    if not reponse:
        raise ValueError(
            "vision : réponse vide au réexamen d'un crop de figure — "
            "aucune description ne peut être ajoutée, ça se documente comme une limite"
        )
    # Normalisation des espaces : la réponse traverse le Markdown, où un
    # passage en ligne fuyerait le bloc en une ligne.
    return " ".join(reponse.split())

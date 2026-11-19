# ingestion/gate.py — Gate image/PDF (3 issues) + OCR PDF
#
# ROUTAGE (docs/intentions — lecture seule) :
# - La qualification initiale d'une image (document ? image générale ?) appartient
#   au modèle de vision générale LFM2.5-VL-3B, posée AVANT tout OCR
#   (modeles/lfm2.5-vl-3b.md §1.2 et §3.5 ; modeles/paddleocr-vl-1.6.md §3.2 :
#   « La qualification initiale est établie par le modèle de vision générale » ;
#   modeles/pp-doclayoutv3.md §4.3 : « PP-DocLayoutV3 n'est pas ce classificateur »).
#   L'ancien comptage IMAGE_DOC_MIN_CHARS a disparu : il faisait payer un OCR
#   halluciné à chaque photo, et demandait à un longueur de chaîne de répondre
#   à une question de contenu.
# - Sur la chaîne documentaire, PP-DocLayoutV3.detect_regions localise texte /
#   tableaux / figures (modeles/paddleocr-vl-1.6.md §2.1 : « Analyse de mise en
#   page : détecter les éléments, établir leur ordre de lecture et extraire les
#   régions ») et alimente le routage OCR puis, lot suivant, le recadrage.
#   Le layout n'intervient QUE sur la chaîne documentaire (images.md §3.2).
# - PDF : extraction native privilégiée sur page dense (pp-doclayoutv3.md §4.1) ;
#   la détection de régions est câblée sur les pages scannées, celles qui vont
#   réellement à l'OCR.
# - Pipeline tableur (normalisation lignes+en-têtes, échantillonnage) = lot suivant ;
#   ici on rend le texte brut borné, ask_files fera l'extraction précise à la demande.
from __future__ import annotations

import re
from typing import Any

# Seuils du gate (spec 01/03 — point unique backend/config/defaults.py, jamais de littéral recopié).
# PP-DocLayoutV3 réel : score de confiance par région, pas de surface (spec 01 correction gate).
# En attendant le runtime paddle : heuristique ci-dessous + interface à seuil compatible.
try:
    from backend.config.defaults import INPUT_LIMITS as _INPUT_LIMITS
    from backend.config.defaults import PDF_LAYOUT_MAX_PAGES_CONFIG as _PDF_CFG
    from backend.config.defaults import TOOL_LIMITS as _LIMITS

    NATIVE_MIN_CHARS = (
        50  # page PDF : texte natif >= 50 chars = dense (direct, sans modèle)
    )
    LAYOUT_CONFIDENCE_THRESHOLD = float(_LIMITS.get("layout_confidence_threshold", 0.3))
    LAYOUT_INPUT_SIZE = int(_LIMITS.get("layout_input_size", 800))
    LAYOUT_BATCH_SIZE = int(_LIMITS.get("layout_batch_size", 8))
    PDF_LAYOUT_MAX_PAGES_CONFIG = dict(_PDF_CFG)
    PDF_MAX_PAGES_CONFIG = int(_LIMITS.get("pdf_max_pages", 20))
    PDF_RENDER_DPI_CONFIG = int(_LIMITS.get("pdf_render_dpi", 200))
    PDF_MAX_PIXELS = int(_INPUT_LIMITS["pdf_max_pixels"])
except Exception:  # import sans config (tests purs)
    NATIVE_MIN_CHARS = 50
    LAYOUT_CONFIDENCE_THRESHOLD = 0.3
    LAYOUT_INPUT_SIZE = 800
    LAYOUT_BATCH_SIZE = 8
    PDF_LAYOUT_MAX_PAGES_CONFIG = {"max_num_input_imgs": None}
    PDF_MAX_PAGES_CONFIG = 20
    PDF_RENDER_DPI_CONFIG = 200
    PDF_MAX_PIXELS = 40_000_000


# Libellés de région qui décrivent du TEXTE à transcrire. Une seule
# définition, consommée à la fois par le gate (page entière) et par le routage
# région par région de l'ingestion — deux listes divergeraient au premier
# libellé ajouté.
LABELS_TEXTE = frozenset(
    {"text", "texte", "title", "titre", "header", "en-tete", "en-tête"}
)
LABELS_TABLE = frozenset({"table", "tableau"})

TABLE_SEP_RE = re.compile(r"[|\t;]")  # séparateurs de colonnes visibles
TABLE_MIN_ROWS = 3  # >= 3 lignes à séparateurs = structure tabulaire
TABLE_HINT_WORDS = (
    "tableau",
    "table",
    "colonne",
    "ligne",
    "facture",
    "total",
    "montant",
    "chiffre",
    "cells",
    "spreadsheet",
)

# Garde-fou lu AU POINT UNIQUE (TOOL_LIMITS "pdf_max_pages", defaults.py) :
# 20 pages, la MÊME valeur que `pdf_sync_threshold` consommée par l'ingestion.
# Sa cible finale relève d'un arbitrage produit à mesurer (pdf.md §4.2 :
# « Inspecter les paramètres réels et tester explicitement les documents longs »).
MAX_PDF_PAGES = PDF_MAX_PAGES_CONFIG
# 200 ppp : même source unique (« pdf_render_dpi », defaults.py). pdf.md §2.5 :
# « La résolution est un paramètre de qualité et de mémoire à mesurer. »
DPI_RENDU_PDF = PDF_RENDER_DPI_CONFIG


def filter_regions_by_confidence(
    regions: list[dict], threshold: float = LAYOUT_CONFIDENCE_THRESHOLD
) -> list[dict]:
    """Filtre PP-DocLayoutV3 par score de confiance (spec 01 correction : pas de surface). Pur.

    Chaque région porte {category, score} ; seules score >= threshold retenues. Seuils par
    catégorie possibles via dict threshold={table: 0.3, text: 0.3}. Aucun calcul de surface.
    """
    try:
        th = threshold if not isinstance(threshold, dict) else None
    except Exception:
        th = LAYOUT_CONFIDENCE_THRESHOLD
    out: list[dict] = []
    for r in regions or []:
        try:
            cat = str((r or {}).get("category", "")).lower()
            score = float((r or {}).get("score", 0.0))
            lim = (
                float(
                    threshold.get(
                        cat, threshold.get("default", LAYOUT_CONFIDENCE_THRESHOLD)
                    )
                )
                if isinstance(threshold, dict)
                else float(th if th is not None else LAYOUT_CONFIDENCE_THRESHOLD)
            )
            if score >= lim:
                out.append(r)
        except Exception:
            continue
    return out


def gate_from_regions(
    regions: list[dict], threshold: float = LAYOUT_CONFIDENCE_THRESHOLD
) -> str:
    """Gate 3 issues depuis régions filtrées (spec 01/03). Pur : table | document | scene."""
    kept = filter_regions_by_confidence(regions, threshold)
    cats = {str(r.get("category", "")).lower() for r in kept}
    if cats & LABELS_TABLE:
        return "table"
    if cats & LABELS_TEXTE:
        return "document"
    return "scene"


def check_pdf_config() -> None:
    """Garde-fou spec 01/03 : max_num_input_imgs=null exigé avant tout PDF >10 pages (sinon troncature silencieuse)."""
    cfg = PDF_LAYOUT_MAX_PAGES_CONFIG or {}
    if cfg.get("max_num_input_imgs") is not None:
        raise RuntimeError(
            "PP-DocLayoutV3 : max_num_input_imgs doit être null (sinon troncature silencieuse à 10 pages)."
        )


def is_table_question(query: str) -> bool:
    """La question porte-t-elle sur un tableau ? Heuristique mots-clés (le gate fin est DocLayout)."""
    q = (query or "").lower()
    return any(w in q for w in TABLE_HINT_WORDS)


def looks_tabular(text: str) -> bool:
    """Le texte extrait a-t-il une structure de tableau ? Pur."""
    rows = sum(
        1 for ln in (text or "").splitlines() if len(TABLE_SEP_RE.findall(ln)) >= 2
    )
    return rows >= TABLE_MIN_ROWS


def native_page_text(page) -> str:
    """Texte natif d'une page PyMuPDF (jamais de modèle). Pur."""
    try:
        return (page.get_text() or "").strip()
    except Exception:
        return ""


def page_a_des_images(page) -> bool:
    """La page contient-elle au moins une image ? Pur.

    Une page avec image n'est jamais « purement native » : ses figures doivent
    être repérées puis décrites par la vision (pp-doclayoutv3.md §4.5). Ce
    contrôle décide la route de la page, pas la route du document entier.
    """
    try:
        return bool(page.get_images(full=True))
    except Exception:
        return False


def _pixels_attendus(page, dpi: int) -> int | None:
    """Pixel count a page WILL allocate at this DPI, when its MediaBox is known.

    Une MediaBox de plusieurs mètres donne un `pix.width * pix.height` que
    PyMuPDF alloue avant que `Image.frombytes` ne soit appelé. Lire la page
    AVANT `get_pixmap` refuse le rendu sans payer l'allocation ; le contrôle
    après coup reste en filet pour les pages sans `rect` exploitable.
    """
    try:
        rect = page.rect
        largeur = float(rect.width)
        hauteur = float(rect.height)
    except (AttributeError, TypeError, ValueError):
        return None
    if largeur <= 0 or hauteur <= 0:
        return None
    return int((largeur * dpi / 72.0) + 0.999) * int((hauteur * dpi / 72.0) + 0.999)


def render_page_png(page, dpi: int | None = None):
    """Rend une page PyMuPDF en PIL.Image RGB. Pur (hors IO).

    Le défaut de résolution vient du POINT UNIQUE (`pdf_render_dpi`,
    defaults.py — pdf.md §2.5 : « Aucun nombre de DPI n'est adopté comme
    défaut universel ») : un littéral 200 en ligne désynchroniserait
    ingestion et porte.

    Deux contrôles de surface, dans cet ordre : la taille ATTENDUE (avant que
    PyMuPDF n'alloue) puis la taille RÉELLE du pixmap (avant `Image.frombytes`).
    Une page à MediaBox géante est refusée proprement au lieu d'allouer des Go.
    """
    from PIL import Image

    resolution = DPI_RENDU_PDF if dpi is None else dpi
    attendus = _pixels_attendus(page, resolution)
    if attendus is not None and attendus > PDF_MAX_PIXELS:
        raise ValueError(
            f"Page PDF trop grande : {attendus} pixels (maximum "
            f"{PDF_MAX_PIXELS}). Rendez le document à une résolution moindre."
        )
    pix = page.get_pixmap(dpi=resolution)
    if pix.width * pix.height > PDF_MAX_PIXELS:
        raise ValueError(
            f"Page PDF trop grande : {pix.width * pix.height} pixels (maximum "
            f"{PDF_MAX_PIXELS}). Rendez le document à une résolution moindre."
        )
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def parse_image_bytes(
    png_bytes: bytes,
    *,
    table: bool = False,
    kind: str = "interactive",
    owner: str | None = None,
) -> tuple[str, str]:
    """Parse un PNG via le worker. Retourne (kind, text) : document|table.

    kind=table si tâche table demandée ou structure tabulaire détectée.
    Lève RuntimeError si worker KO (l'appelant choisit le repli caption VL-3B).
    """
    from backend.ingestion import paddle_client as _pc

    text = _pc.parse_png(
        png_bytes, task="table" if table else "parse", kind=kind, owner=owner
    )
    text = (text or "").strip()
    if table or looks_tabular(text):
        return "table", text
    return "document", text


def regions_vers_gate(regions: list[dict]) -> list[dict]:
    """Adapte la sortie du worker (« label ») au contrat du gate (« category »).

    Pur. Le worker renvoie le libellé exact de la révision PP-DocLayoutV3
    (modeles/pp-doclayoutv3.md §2.1 : les noms de catégories viennent de la
    configuration de la révision) ; le gate consomme « category ». L'adaptation
    vit ICI, une seule fois — la dupliquer chez les appelants la désynchronise.
    """
    adaptees: list[dict] = []
    for r in regions or []:
        category = str(r.get("label") or r.get("category") or "").strip().lower()
        adaptees.append(
            {
                "category": category,
                "score": float(r.get("score", 0.0)),
                "box": r.get("box"),
                "polygon_points": r.get("polygon_points"),
            }
        )
    return adaptees


def gate_image(
    png_bytes: bytes,
    *,
    qualifier,
    kind: str = "interactive",
    owner: str | None = None,
) -> tuple[str, str]:
    """Gate image 3 issues — la décision appartient à la VISION, jamais au comptage.

    Ordre vérifié par les tests : le verdict du modèle LFM2.5-VL-3B arrive
    AVANT tout OCR (modeles/paddleocr-vl-1.6.md §3.2). Un OCR lancé sur une
    photo ne produit qu'un texte halluciné, payé pour rien.

    `qualifier(png_bytes) -> "document" | "image"` : closure fournie par la
    couche d'appel (backend/ingestion/qualification.py, PROMPT_QUALIFICATION).
    Une erreur de vision traverse : sans verdict, aucune route légitime.

    `kind`/`owner` sont transmis à tous les appels OCR sous-jacents
    (`detect_regions`, `parse_image_bytes`) : sans cela, l'ingestion
    contournerait la garde de `paddle_client._avec_lease_ocr` et ne céderait
    pas le GPU à un tour interactif actif.

    Retourne (issue, text) : document | table | scene (texte vide = image
    générale, l'appelant bascule vers la caption VL-3B).
    """
    verdict = qualifier(png_bytes)
    if verdict != "document":
        return "scene", ""
    from backend.ingestion import paddle_client as _pc

    # Chaîne documentaire : détection des régions d'abord (texte/tables/figures),
    # le routage OCR s'appuie dessus ; le recadrage (4.4, lot suivant) les
    # réutilisera (modeles/paddleocr-vl-1.6.md §2.1, §2.2).
    issue = gate_from_regions(
        regions_vers_gate(_pc.detect_regions(png_bytes, kind=kind, owner=owner))
    )
    if issue == "scene":
        # La qualification a tranché « document » et le layout n'a retenu aucune
        # région au seuil : repli borné documenté — page entière à l'OCR. Une
        # absence de régions ne démontre pas une scène naturelle
        # (modeles/pp-doclayoutv3.md §4.3).
        issue = "document"
    _, text = parse_image_bytes(
        png_bytes, table=(issue == "table"), kind=kind, owner=owner
    )
    return issue, text


def ocr_pdf(
    pdf_path: str, *, query: str = "", max_pages: int = MAX_PDF_PAGES
) -> tuple[str, list[str]]:
    """OCR PDF page par page.

    Extraction native PyMuPDF directe quand la page est dense ; pages scannées
    (ou question tableau) : rendu 200 DPI → détection PP-DocLayoutV3 (câblée,
    régions pour le routage OCR) → PaddleOCR-VL. Lève RuntimeError si worker ou
    layout KO sur une page scannée (l'appelant surface l'erreur, jamais de texte
    inventé).
    """
    import fitz

    want_table = is_table_question(query)
    doc: Any = fitz.open(
        pdf_path
    )  # fitz sans stubs : Any explicite pour enumerate/page
    pages: list[str] = []
    used_model = False
    for i, page in enumerate(doc):
        if i >= max_pages:
            pages.append(f"(limite : {max_pages} pages traitées)")
            break
        native = native_page_text(page)
        if len(native) >= NATIVE_MIN_CHARS and not want_table:
            pages.append(
                native
            )  # dense : extraction directe, 0 modèle (majorité des PDF natifs)
            continue
        # Page scannée ou question tableau : render 200 DPI -> PaddleOCR-VL.
        from backend.ingestion import paddle_client as _pc

        img = render_page_png(page)
        png = _pc.pil_to_png_bytes(img)
        # Câblage PP-DocLayoutV3 : les régions (texte/tables/figures) alimentent le
        # routage OCR et préparent le recadrage du lot suivant
        # (modeles/paddleocr-vl-1.6.md §2.1). Modèle absent → échec bruyant avec
        # la cause, jamais de contournement silencieux.
        regions = regions_vers_gate(_pc.detect_regions(png))
        issue_page = gate_from_regions(regions)
        task = "table" if (want_table or issue_page == "table") else "parse"
        text = _pc.parse_png(png, task=task)
        used_model = True
        # Si natif existant mais question tableau : on garde les deux (natif + relecture ciblée).
        if native and len(native) >= NATIVE_MIN_CHARS:
            pages.append(native + "\n" + (text or "").strip())
        else:
            pages.append((text or "").strip())
    try:
        doc.close()
    except Exception:
        pass
    if not used_model:
        return "native", pages
    if all(not p.strip() for p in pages):
        return "scan", pages
    return "mixte", pages

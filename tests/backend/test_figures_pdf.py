"""Tâches 4.4/4.7 — figures PDF (recadrage, réexamen vision, réintégration)
et sortie Markdown structurée de l'ingestion PDF.

Non-slow : tout est pur ou moqué ; le vrai VL (2,5 Go de VRAM réels) est
marqué `slow`. Chaque PDF de test porte un contenu distinct par page et par
figure, pour qu'une extraction croisée entre contenus soit vue comme une
erreur et non comme « du texte qui est arrivé ».
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from backend.ingestion import attachment_ingest as AI
from backend.ingestion import figures_pdf as FIG
from backend.ingestion import markdown_pdf as MD
from backend.ingestion import vision_appel as VA

ECHELLE = 200 / 72  # rendu 200 ppp : points PDF → pixels


def _pdf_scanne_avec_figure(chemin: Path) -> None:
    """PDF scanné : une page-image portant du texte ET une figure rouge placée.

    Le rouge uni est le marqueur du test : un crop tombant à côté (page
    entière, zone blanche) ne décode pas en rouge dominant.
    """
    import pymupdf

    source = pymupdf.open()
    page = source.new_page(width=595, height=842)
    page.insert_text((50, 60), "TEXTE-DE-PAGE-CARTE-4", fontsize=14)
    # Figure : bloc rouge 200×200 points à (120, 120)
    page.draw_rect(pymupdf.Rect(120, 120, 320, 320), color=(0, 0, 0), fill=(1, 0, 0))
    pixmap = page.get_pixmap(dpi=150)
    cible = pymupdf.open()
    page_cible = cible.new_page(width=595, height=842)
    page_cible.insert_image(page_cible.rect, stream=pixmap.tobytes("png"))
    cible.save(str(chemin))
    source.close()
    cible.close()


def _boite_figure() -> list[float]:
    """Boîte de la figure, dans le repère pixel d'une image rendue à 200 ppp.

    C'est bien le repère du CROP : `detect_regions` travaille sur l'image
    rendue (paddle_worker._layout_regions : `target_sizes=[image.size]`), le
    périmètre 120..320 points × ECHELLE dit donc la même boîte en pixels.
    """
    return [
        round(120 * ECHELLE, 1),
        round(120 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
    ]


# ---------- (4.4) sélection et recadrage : pur ----------


def test_regions_figure_choisit_par_le_nom_reel_pas_par_l_ordre():
    """pp-doclayoutv3.md §2.1 : la taxonomie n'est jamais déduite d'un ordre.

    La sélection porte sur le NOM rendu par la révision posée (« image »,
    « chart ») ; « figure_title » est un titre, pas une figure ; une région
    sous le seuil du gate n'est pas retenue.
    """
    regions = [
        {"category": "text", "score": 0.9, "box": [0, 0, 100, 40]},
        {"category": "image", "score": 0.85, "box": [0, 200, 400, 600]},
        {"category": "chart", "score": 0.5, "box": [0, 610, 400, 700]},
        {"category": "figure_title", "score": 0.99, "box": [0, 195, 400, 199]},
        {"category": "image", "score": 0.1, "box": [0, 700, 400, 800]},
    ]
    retenues = FIG.regions_figure(regions)
    assert [i for i, _ in retenues] == [2, 3]


def test_cropper_figure_rend_les_pixels_demandes_et_refuse_le_degenere():
    from PIL import Image

    image = Image.new("RGB", (100, 80), "white")
    assert FIG.cropper_figure(image, [10, 10, 50, 40]).size == (40, 30)
    # Boîte débordant de la page : bornage à la taille réelle, sans pixel inventé
    assert FIG.cropper_figure(image, [80, 60, 200, 200]).size == (20, 20)
    # Boîte entièrement hors page : NE renvoie RIEN — ce n'est pas du contenu
    assert FIG.cropper_figure(image, [500, 500, 900, 900]) is None
    assert FIG.cropper_figure(image, [40, 40, 41, 41]) is None, (
        "une boîte de 1 pixel n'est pas une figure à examiner"
    )
    assert FIG.cropper_figure(image, None) is None
    assert FIG.cropper_figure(image, []) is None


def test_le_bloc_figure_porte_le_repere_page_region():
    bloc = FIG.bloc_figure(3, 2, "Un diagramme de phases.")
    assert "<!-- thot:figure page=3 région=2 -->" in bloc
    assert "**Figure** (page 3 · région 2) : Un diagramme de phases." in bloc
    limite = FIG.bloc_figure_limite(3, 2, "X: Y")
    assert "**Figure** (page 3 · région 2) : (réexamen indisponible — X: Y)" in limite


def test_reexamen_vision_normalise_et_echoue_bruyamment_sur_reponse_vide():
    vues: list[str] = []

    def couche(consigne: str, pixels: bytes) -> str:
        vues.append(consigne)
        assert pixels
        return "  Schéma d'un\n  circuit.  "

    texte = FIG.reexaminer_figure(b"png", couche)
    assert texte == "Schéma d'un circuit.", "espaces normalisés : pas de retour en bloc"
    assert vues == [FIG.PROMPT_FIGURE_REEXAMINEE], "c'est LA consigne de réexamen"
    with pytest.raises(ValueError):
        FIG.reexaminer_figure(b"png", lambda c, p: "   ")
    with pytest.raises(ValueError):
        FIG.reexaminer_figure(b"", couche)


# ---------- (4.4) PDF scanné → figure croppée, réexaminée, réintégrée ----------


def test_le_crop_recoit_les_pixels_reels_et_la_figure_reste_en_attente(
    monkeypatch, tmp_path
):
    """Bout en bout étape A : page scannée → détection → crop → placeholder.

    L'ordre EST la preuve (layout AVANT l'OCR ; l'arbitrage de densité APRÈS,
    sur le crop) et le contenu du crop EST la preuve matérielle : les octets
    décodent en une image ROUGE à la taille de la boîte, pas en la page
    entière. AUCUN appel vision : la description est différée.
    """
    chemin = tmp_path / "figure.pdf"
    _pdf_scanne_avec_figure(chemin)
    ordre: list[str] = []
    boite = _boite_figure()

    def _detect(png: bytes, **k):
        ordre.append("layout")
        return [
            {"label": "text", "score": 0.9, "box": [0, 0, 595 * ECHELLE, 40 * ECHELLE]},
            {"label": "image", "score": 0.95, "box": boite},
        ]

    crops_vus: list[object] = []

    def _parse(png: bytes, *, task="parse", **k):
        ordre.append("ocr")
        # L'arbitrage de densité envoie le crop au worker : on garde le pixels
        # reçu pour prouver que ce n'est pas la page entière.
        if len(ordre) == 3:
            from PIL import Image

            crops_vus.append(Image.open(io.BytesIO(png)).convert("RGB"))
        return "TEXTE-DE-PAGE-CARTE-4 reconnu"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail("étape A : aucun appel VLM"),
    )
    monkeypatch.setattr(
        "backend.ingestion.figures_pdf.reexaminer_figure",
        lambda png, appeler: pytest.fail("étape A : aucun réexamen de figure"),
    )

    markdown = AI._ocr_pdf(str(chemin))

    assert ordre == ["layout", "ocr", "ocr"], (
        "l'OCR ne part jamais avant la détection ; le second OCR est l'arbitrage "
        "de densité du crop « image ». Aucune vision en étape A."
    )
    crop = crops_vus[0]
    attendue = round((320 - 120) * ECHELLE)
    assert abs(crop.width - attendue) <= 3, (
        f"l'arbitrage a reçu {crop.width} px de large, la figure en fait {attendue}"
    )
    moyenne = crop.resize((1, 1)).getpixel((0, 0))
    assert moyenne[0] > 200 and moyenne[1] < 100 and moyenne[2] < 100, (
        f"le crop reçu par l'arbitrage n'est pas la figure rouge : {moyenne}"
    )
    # Le placeholder reste POSITIONNEL et nomme la région dans l'ordre de lecture.
    assert "## Page 1" in markdown
    assert "TEXTE-DE-PAGE-CARTE-4" in markdown
    assert "<!-- thot:figure page=1 région=2 attente -->" in markdown, (
        "le repère cite la position de la région dans l'ordre de lecture"
    )
    assert "**Figure** (page 1 · région 2) : description en attente" in markdown
    assert (
        "<!-- thot:couverture chaine=ocr pages_attendues=1 pages_traitees=1 -->"
        in markdown
    )


def test_une_page_scannee_table_marque_un_tableau_avec_son_repere(
    monkeypatch, tmp_path
):
    chemin = tmp_path / "tableau.pdf"
    _pdf_scanne_avec_figure(chemin)
    taches: list[str] = []

    def _detect(png: bytes, **k):
        return [{"label": "table", "score": 0.9, "box": [0, 0, 100, 100]}]

    def _parse(png: bytes, *, task="parse", **k):
        taches.append(task)
        return "Date | Total\n2026 | 4200"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda c, p: pytest.fail(
            "aucune figure ici : la vision ne doit pas être appelée"
        ),
    )
    markdown = AI._ocr_pdf(str(chemin))
    assert taches == ["table"], "une région table route la TÂCHE table"
    assert "<!-- thot:table page=1 région=1 -->" in markdown
    assert "**Tableau** (page 1 · région 1) :\n\nDate | Total" in markdown


def test_etape_a_n_a_pas_besoin_de_la_vision_meme_absente(monkeypatch, tmp_path):
    """L'étape A réussit quand la vision est ABSENTE : elle ne l'appelle jamais.

    Le placeholder ne dépend d'aucune couche VLM — c'est précisément ce que
    l'ingestion en deux étages achète : le texte disponible est livré même sur
    une machine sans modèle de vision.
    """
    chemin = tmp_path / "figure.pdf"
    _pdf_scanne_avec_figure(chemin)
    # La région figure garde l'index 1 ; la région texte fournit le contenu
    # OCR de la page, que le flux par région ne confond plus avec la figure.
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [
            {"label": "image", "score": 0.9, "box": _boite_figure()},
            {"label": "text", "score": 0.9, "box": [0, 0, 100, 40]},
        ],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png", lambda png, **k: "texte de scan"
    )
    monkeypatch.setattr("backend.ingestion.vision_appel._couche", None)
    markdown = AI._ocr_pdf(str(chemin))
    assert "<!-- thot:figure page=1 région=1 attente -->" in markdown
    assert "description en attente" in markdown
    assert "réexamen indisponible" not in markdown
    assert "texte de scan" in markdown


def test_la_meme_image_sert_detection_et_ocr(monkeypatch, tmp_path):
    """La PREUVE du câblage : les régions du lot 2 sont consommées, pas recréées.

    `detect_regions` et `parse_png` reçoivent LES MÊMES OCTETS — un seul
    rendu de page porte les deux, et les figures se croppent dans ces mêmes
    pixels. Un second rendu (deux vérités possibles) casse ce test.
    """
    chemin = tmp_path / "unique.pdf"
    _pdf_scanne_avec_figure(chemin)
    vus: dict[str, bytes] = {}

    def _detect(png: bytes, **k):
        vus["layout"] = png
        return []

    def _parse(png: bytes, *, task="parse", **k):
        vus["ocr"] = png
        return "x"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    monkeypatch.setattr("backend.ingestion.vision_appel._couche", None)
    AI._ocr_pdf(str(chemin))
    assert vus["layout"] == vus["ocr"] and len(vus["ocr"]) > 100, (
        "détecteur et OCR doivent voir la MÊME image de page — la région du "
        "crop et la transcription parlent des mêmes pixels"
    )


# ---------- (4.7) Markdown structuré natif : arborescence et bornes ----------


def _pdf_natif(chemin: Path, pages: int, *, titre: str = "Rapport Embrun") -> None:
    """PDF natif à `pages` pages ; chacune porte SON contenu propre."""
    import pymupdf

    document = pymupdf.open()
    document.set_metadata({"title": titre})
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 110), f"CONTENU-{index + 1:02d}", fontsize=16)
        page.insert_text(
            (72, 150),
            f"Page physique {index + 1} : rapport, données révisées et conclusions.",
            fontsize=12,
        )
    document.save(str(chemin))
    document.close()


def test_la_sortie_native_est_structuree_avec_titre_couverture_et_pages(tmp_path):
    chemin = tmp_path / "natif.pdf"
    _pdf_natif(chemin, 3)
    rendu = AI._extraction_pdf_native(str(chemin))
    assert rendu is not None
    assert rendu.startswith("Rapport Embrun"), (
        "le titre réel (metadata.title) ouvre le document"
    )
    assert (
        "<!-- thot:couverture chaine=native pages_attendues=3 pages_traitees=3 -->"
        in rendu
    )
    assert "## Page 1" in rendu and "CONTENU-01" in rendu
    assert "## Page 2" in rendu and "CONTENU-02" in rendu
    assert "## Page 3" in rendu and "CONTENU-03" in rendu
    assert "## Page 4" not in rendu
    # Chaque page a un contenu propre à SON rang : le repère ne dit pas un autre
    ordre_fidele = (
        rendu.index("CONTENU-01")
        < rendu.index("CONTENU-02")
        < rendu.index("CONTENU-03")
    )
    assert ordre_fidele, "l'ordre des pages suit la lecture, pas l'assemblage"
    assert (
        MD.MENTION_PAGE_SANS_TEXTE_NATIF not in rendu
    )  # toutes les pages ont du texte
    assert AI.extraire_texte("art_test", "pdf", str(chemin)) == (rendu, "native")


def test_la_borne_de_pages_coupe_propre_et_documente(tmp_path):
    chemin = tmp_path / "long.pdf"
    _pdf_natif(chemin, 25)
    rendu = AI._extraction_pdf_native(str(chemin))
    assert rendu is not None
    assert "## Page 20" in rendu and "CONTENU-20" in rendu
    assert "## Page 21" not in rendu and "CONTENU-21" not in rendu, (
        "rien au-delà de la borne — et la borne se voit dans la couverture"
    )
    assert (
        "<!-- thot:couverture chaine=native pages_attendues=25 pages_traitees=20 "
        "limite_pages=20 pages_restantes=5 -->" in rendu
    )


def test_aucun_titre_fabrique_quand_metadata_ne_dit_rien(tmp_path):
    chemin = tmp_path / "sans-titre.pdf"
    _pdf_natif(chemin, 3, titre="")
    rendu = AI._extraction_pdf_native(str(chemin))
    assert rendu is not None
    assert rendu.startswith("<!-- thot:couverture"), (
        "pas de titre inventé quand metadata.title ne dit rien"
    )


# ---------- routage page/région (correctif ingestion) ----------


def _pdf_mixte_natif_et_scan(chemin: Path) -> None:
    """Page 1 native (texte seul), page 2 scannée (image sans couche texte)."""
    import pymupdf

    source = pymupdf.open()
    page_scan = source.new_page(width=595, height=842)
    page_scan.insert_text((72, 120), "PAGE-SCAN-OCR", fontsize=22)
    pixmap = page_scan.get_pixmap(dpi=150)

    document = pymupdf.open()
    page_native = document.new_page(width=595, height=842)
    page_native.insert_text((72, 100), "PAGE-NATIVE-TEXTE " * 5, fontsize=14)
    page_image = document.new_page(width=595, height=842)
    page_image.insert_image(page_image.rect, stream=pixmap.tobytes("png"))
    document.save(str(chemin))
    source.close()
    document.close()


def _pdf_natif_avec_figure(chemin: Path) -> None:
    """Page native : deux blocs de texte et une figure IMAGE entre eux."""
    import pymupdf

    source = pymupdf.open()
    page_fig = source.new_page(width=200, height=200)
    page_fig.draw_rect(pymupdf.Rect(0, 0, 200, 200), color=(0, 0, 0), fill=(1, 0, 0))
    pixmap = page_fig.get_pixmap(dpi=100)

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 100), "HAUT-DU-DOCUMENT " * 4, fontsize=14)
    page.insert_text((72, 700), "BAS-DU-DOCUMENT " * 4, fontsize=14)
    page.insert_image(pymupdf.Rect(120, 300, 320, 500), stream=pixmap.tobytes("png"))
    document.save(str(chemin))
    source.close()
    document.close()


def test_pdf_mixte_route_chaque_page_independamment(monkeypatch, tmp_path):
    """Un PDF mixte ne bascule plus tout le document : la page native reste
    native (zéro layout), la page scannée seule part à l'OCR."""
    chemin = tmp_path / "mixte.pdf"
    _pdf_mixte_natif_et_scan(chemin)
    appels_layout: list[int] = []

    def _detect(png, **k):
        appels_layout.append(1)
        return [{"label": "text", "score": 0.9, "box": [0, 0, 300, 80]}]

    def _parse(png, *, task="parse", **k):
        return "PAGE-SCAN-OCR reconnu par le modèle"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)

    markdown, mode = AI.extraire_texte("art", "pdf", str(chemin))
    assert mode == "mixte"
    assert len(appels_layout) == 1, "seule la page scannée demande le layout"
    assert "PAGE-NATIVE-TEXTE" in markdown
    assert "PAGE-SCAN-OCR" in markdown
    assert "## Page 1" in markdown and "## Page 2" in markdown


def test_pdf_natif_avec_figure_conserve_le_texte_et_pose_le_placeholder(
    monkeypatch, tmp_path
):
    """Page native avec image : le texte natif n'est PAS OCRisé, la figure reste
    en attente, et le placeholder s'intercale entre les blocs de texte."""
    chemin = tmp_path / "natif-figure.pdf"
    _pdf_natif_avec_figure(chemin)
    boite = [
        round(120 * ECHELLE, 1),
        round(300 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
        round(500 * ECHELLE, 1),
    ]
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [{"label": "figure", "score": 0.9, "box": boite}],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png",
        lambda *a, **k: pytest.fail("le texte natif ne doit pas être OCRisé"),
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail("étape A : aucun appel VLM"),
    )

    markdown, mode = AI.extraire_texte("art", "pdf", str(chemin))
    assert mode == "mixte"
    assert "HAUT-DU-DOCUMENT" in markdown
    assert "BAS-DU-DOCUMENT" in markdown
    assert "description en attente" in markdown
    assert markdown.index("HAUT-DU-DOCUMENT") < markdown.index("**Figure**"), (
        "le placeholder doit apparaître après le texte du haut"
    )
    assert markdown.index("**Figure**") < markdown.index("BAS-DU-DOCUMENT"), (
        "le placeholder doit s'intercaler avant le texte du bas, pas finir en page"
    )


def test_pdf_scanne_texte_et_figure_pas_de_double_description(monkeypatch, tmp_path):
    """Page scannée : la région texte part à l'OCR, la région figure en attente.
    Aucune région n'est à la fois OCRisée et envoyée à la vision (étape A)."""
    chemin = tmp_path / "scan-texte-figure.pdf"
    _pdf_scanne_avec_figure(chemin)
    boite = _boite_figure()
    appels_ocr: list[str] = []

    def _detect(png, **k):
        return [
            {
                "label": "text",
                "score": 0.9,
                "box": [0, 0, 595 * ECHELLE, 60 * ECHELLE],
            },
            {"label": "figure", "score": 0.9, "box": boite},
        ]

    def _parse(png, *, task="parse", **k):
        appels_ocr.append(task)
        return "TEXTE-OCR-DE-PAGE"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail("étape A : aucun appel VLM"),
    )

    markdown = AI._ocr_pdf(str(chemin))
    assert appels_ocr == ["parse"], (
        "seule la région texte part à l'OCR ; la figure n'est ni OCRisée ni décrite"
    )
    assert "TEXTE-OCR-DE-PAGE" in markdown
    assert "description en attente" in markdown
    assert "<!-- thot:figure page=1 région=2 attente -->" in markdown


def test_route_3_borne_les_placeholders_et_trace_les_regions_ecartees(
    monkeypatch, tmp_path
):
    """10 régions figure sur une page scannée : au plus 8 placeholders.

    La route 3 (texte natif insuffisant) ne doit PAS traiter toutes les régions
    figure : elle réutilise la borne `CROPS_FIGURE_MAX_PAR_PAGE`. Les deux
    régions écartées laissent un bloc `limite` qui nomme leur nombre — une
    borne silencieuse ferait disparaître des figures sans trace. Aucune vision
    n'est appelée : l'étape A ne fait que poser les placeholders.
    """
    chemin = tmp_path / "dix-figures.pdf"
    _pdf_scanne_avec_figure(chemin)

    regions = [
        {
            "label": "figure",
            "score": 0.9,
            "box": [10 + i * 50, 10 + i * 50, 90 + i * 50, 90 + i * 50],
        }
        for i in range(10)
    ]
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions", lambda png, **k: regions
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail("étape A : aucun appel VLM"),
    )

    markdown = AI._ocr_pdf(str(chemin))

    assert markdown.count("description en attente") == FIG.CROPS_FIGURE_MAX_PAR_PAGE, (
        "la route 3 ne doit pas traiter plus de régions que la borne"
    )
    assert "borne de recadrage dépassée : 2 région(s)" in markdown
    assert "<!-- thot:figure page=1 région=10 -->" in markdown, (
        "le bloc limite garde le repère de la dernière région écartée"
    )


def test_region_image_dense_devient_un_bloc_texte_sans_vision(monkeypatch, tmp_path):
    """Un crop « image » dont l'OCR est dense est un TEXTE, pas une figure.

    La densité est mesurée une fois ; au-delà du seuil, le texte OCR est
    conservé et la vision n'est PAS appelée : payer une description en plus
    d'un OCR déjà concluant serait le double travail que ce test interdit.
    """
    chemin = tmp_path / "image-dense.pdf"
    _pdf_scanne_avec_figure(chemin)
    texte_dense = "TEXTE-DENSE-DU-CROP " * 10

    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [{"label": "image", "score": 0.9, "box": [0, 0, 300, 300]}],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png", lambda png, **k: texte_dense
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail(
            "un crop image dense ne doit pas être décrit par la vision"
        ),
    )

    markdown = AI._ocr_pdf(str(chemin))

    assert "<!-- thot:texte page=1 région=1 -->" in markdown
    assert "TEXTE-DENSE-DU-CROP" in markdown
    assert "<!-- thot:figure" not in markdown


# ---------- (4.4) vrai VL — marque `slow`, skip si VRAM occupée ----------

MIN_FREE_VRAM_MB = 6000
"""Seuil repris du smoke manual pytest_lfm25_vl : sans VRAM libre, un skip —
deux process qui se battent pour un GPU n'est jamais un verdict sur le code."""


@pytest.mark.slow()
def test_vision_reelle_decrit_le_crop_recu(monkeypatch, tmp_path):
    """Le VRAI LFM2.5-VL-3B décrit un crop transmis en pixels (sans rééchelonnage).

    Le modèle tourne dans un SOUS-PROCESS : ses poids et sa réservation CUDA
    ne survivent pas au test — l'OS rend la VRAM entièrement, et la suite
    (orchestration réelle, qualification) ne se retrouve jamais à court d'un
    8B faute d'un résidu de poids visuels. Le sous-process ne requiert aucun
    câblage de main.py : il pose sa propre couche d'appel, câblage de
    production (`couche_managere`) inclus.

    Repris du smoke manual pytest_lfm25_vl : un essai sans VRAM libre n'est
    pas un échec du code — deux process qui se battent pour un GPU n'est
    jamais un verdict. La limite est nommée (skip), jamais un silence.
    """
    import os as _os
    import subprocess
    import sys as _sys
    from pathlib import Path

    from backend.inference.manager import ModelManager

    libre = ModelManager()._get_free_vram_mb() or 0
    if libre < MIN_FREE_VRAM_MB:
        pytest.skip(f"VRAM insuffisante ({libre} Mo libres, {MIN_FREE_VRAM_MB} requis)")

    # Le crop à décrire : rectangle rouge avec un mot blanc dessus.
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (256, 256), "white")
    dessin = ImageDraw.Draw(image)
    dessin.rectangle((32, 32, 224, 192), fill=(200, 0, 0))
    dessin.text((48, 48), "Alarme 3B", fill="white")
    crop = tmp_path / "crop.png"
    image.save(crop, format="PNG")
    reponse = tmp_path / "description.txt"

    SCRIPT = """
import sys
sys.path.insert(0, sys.argv[3])
from PIL import Image, ImageDraw

from backend.inference.manager import ModelManager
from backend.ingestion import figures_pdf as FIG
from backend.ingestion import vision_appel as VA

manager = ModelManager()
VA.enregistrer_couche_appel(VA.couche_managere(manager))
octets = open(sys.argv[1], "rb").read()
reponse = FIG.reexaminer_figure(octets, VA.appeler_vision_generale)
manager.unload_all()
with open(sys.argv[2], "w", encoding="utf-8") as sortie:
    sortie.write(reponse or "")
"""
    racine = Path(__file__).resolve().parents[2]
    complete = subprocess.run(
        [_sys.executable, "-c", SCRIPT, str(crop), str(reponse), str(racine)],
        cwd=str(racine),
        timeout=300,
        capture_output=True,
        text=True,
    )
    assert complete.returncode == 0, (
        f"le sous-process VL a échoué :\nstdout={complete.stdout}\n"
        f"stderr={complete.stderr[-2000:]}"
    )
    texte = reponse.read_text(encoding="utf-8").strip()
    assert texte, "le VL n'a rendu aucune description pour un crop lisible"
    assert "1885477" not in texte, "les pixels n'ont pas été traités comme du texte"

# Tests qualification initiale (tâche 4.2) — décision par vision, jamais par comptage.
# Non-slow : tout est pur ou moqué. Le test réel GPU est marké `slow`.
from __future__ import annotations

import pytest

from backend.ingestion import gate as G
from backend.ingestion import qualification as Q


# ---------- parsing du verdict (pur) ----------


def test_verdict_lit_le_mot_seul():
    assert Q.verdict_qualification("document") == "document"
    assert Q.verdict_qualification("Document.") == "document"
    assert Q.verdict_qualification("« document »") == "document"
    assert Q.verdict_qualification("image") == "image"
    assert Q.verdict_qualification("Image — une photo de chat.") == "image"


def test_verdict_ambigu_echoue_bruyamment():
    """Aucune route par défaut : réponse hors format → ValueError qui la cite."""
    with pytest.raises(ValueError) as exc:
        Q.verdict_qualification("je ne sais pas trop")
    assert "document" in str(exc.value) and "image" in str(exc.value)
    with pytest.raises(ValueError):
        Q.verdict_qualification("document et image en même temps")
    with pytest.raises(ValueError):
        Q.verdict_qualification("")


def test_prompt_est_la_convention_interne_nommee():
    """Le prompt posé au VL est une convention interne, déclarée une seule fois ici."""
    assert "document" in Q.PROMPT_QUALIFICATION
    assert "image" in Q.PROMPT_QUALIFICATION
    # Un seul mot attendu : le VL ne doit pas être invité à décrire (la description
    # est un autre usage, fiche lfm2.5-vl-3b.md §3.1).
    assert "un seul mot" in Q.PROMPT_QUALIFICATION


# ---------- porte image : ordre vision → (régions) → OCR ----------


def test_capture_riche_en_texte_route_document(monkeypatch):
    """Verdict document → chaîne documentaire (détection, puis OCR)."""
    ordre: list[str] = []

    def _qualifier(png_bytes: bytes) -> str:
        ordre.append("vision")
        assert png_bytes, "les pixels réels doivent transiter"
        return "document"

    def _detect(png_bytes: bytes, **k):
        ordre.append("layout")
        return [{"label": "text", "score": 0.9, "box": [0, 0, 100, 40]}]

    def _parse(png_bytes: bytes, *, task="parse", **k):
        ordre.append("ocr")
        return "Date | Client | Montant\n2026 | Dubois | 45 000"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    issue, text = G.gate_image(b"png-du-test", qualifier=_qualifier)
    assert issue == "document"
    assert "45 000" in text
    assert ordre == ["vision", "layout", "ocr"], (
        "l'OCR n'est JAMAIS lancé avant la décision VL"
    )


def test_photo_route_generale_et_aucun_ocr(monkeypatch):
    """Verdict image générale → ('scene', '') et l'OCR n'est JAMAIS appelé."""
    appels = {"ocr": 0, "layout": 0}

    def _qualifier(png_bytes: bytes) -> str:
        return "image"

    def _detect(*a, **k):
        appels["layout"] += 1
        return []

    def _parse(*a, **k):
        appels["ocr"] += 1
        return "texte n'aurait jamais dû être demandé"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    issue, text = G.gate_image(b"photo", qualifier=_qualifier)
    assert issue == "scene" and text == ""
    assert appels == {"ocr": 0, "layout": 0}


def test_regions_table_routent_l_ocr_en_tache_table(monkeypatch):
    """Régions PP-DocLayoutV3 câblées : une région table engage le prompt table."""
    taches: list[str] = []

    def _qualifier(png_bytes: bytes) -> str:
        return "document"

    def _detect(*a, **k):
        return [{"label": "table", "score": 0.95, "box": [0, 0, 200, 200]}]

    def _parse(*a, **k):
        taches.append(k.get("task", "parse"))
        return "Table Recognition: rendu"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    issue, _ = G.gate_image(b"png", qualifier=_qualifier)
    assert issue == "table"
    assert taches == ["table"]


def test_verdict_document_sans_regions_repli_borne_page_entiere(monkeypatch):
    """Layout muet sur une image jugée documentaire : page entière à l'OCR (repli
    borné documenté — pp-doclayoutv3.md §4.3, une absence de régions ne démontre
    pas une scène naturelle)."""
    appels = {}

    def _qualifier(png_bytes: bytes) -> str:
        return "document"

    def _detect(*a, **k):
        return []

    def _parse(*a, **k):
        appels["task"] = k.get("task", "parse")
        return "texte de la page"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    issue, text = G.gate_image(b"png", qualifier=_qualifier)
    assert issue == "document" and text == "texte de la page"
    assert appels["task"] == "parse"


# ---------- échecs bruyants ----------


def test_echec_de_qualification_traverse_sans_ocr(monkeypatch):
    """Vision KO → erreur explicite, jamais de repli silencieux vers l'OCR."""
    appels = {"ocr": 0}

    def _qualifier(png_bytes: bytes) -> str:
        raise RuntimeError("LFM2.5-VL-3B : mmproj absent (test)")

    def _parse(*a, **k):
        appels["ocr"] += 1
        return "ne doit pas arriver"

    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    with pytest.raises(RuntimeError, match="mmproj absent"):
        G.gate_image(b"png", qualifier=_qualifier)
    assert appels["ocr"] == 0


def test_layout_absent_echec_bruyant_avant_la_suite(monkeypatch):
    """detect_regions KO (modèle absent) → RuntimeError avec la cause, jamais
    de contournement silencieux (consigne 4.3)."""
    appels = {"ocr": 0}

    def _qualifier(png_bytes: bytes) -> str:
        return "document"

    def _detect(*a, **k):
        raise RuntimeError(
            "PP-DocLayoutV3 indisponible : modèle absent du cache local (test)"
        )

    def _parse(*a, **k):
        appels["ocr"] += 1
        return ""

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    with pytest.raises(RuntimeError, match="PP-DocLayoutV3 indisponible"):
        G.gate_image(b"png", qualifier=_qualifier)
    assert appels["ocr"] == 0


# ---------- le comptage a disparu ----------


def test_le_comptage_de_caracteres_est_mort():
    """IMAGE_DOC_MIN_CHARS remplacé par la décision VL : aucun résidu consommable."""
    assert not hasattr(G, "IMAGE_DOC_MIN_CHARS")


def test_gate_image_exige_le_qualifieur():
    """La qualification VL est obligatoire : gate_image sans qualifieur ne lance
    RIEN (l'ancien chemin heuristique est devenu impossible, pas juste humain)."""
    with pytest.raises(TypeError):
        G.gate_image(b"png")  # qualifier manquant


# ---------- chaîne PDF : câblage detect_regions (tâche 4.3) ----------

fitz = pytest.importorskip("fitz")


def _pdf_une_page_vide(tmp_path):
    pdf = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page()  # page blanche = pas de texte natif → chaîne OCR
    doc.save(str(pdf))
    doc.close()
    return str(pdf)


def test_ocr_pdf_cable_detect_regions_avant_ocr(tmp_path, monkeypatch):
    """Page scannée : PP-DocLayoutV3 détecte les régions AVANT l'OCR (4.3)."""
    ordre: list[str] = []
    taches: list[str] = []

    def _detect(png_bytes: bytes, **k):
        ordre.append("layout")
        return [{"label": "text", "score": 0.9, "box": [0, 0, 100, 40]}]

    def _parse(png_bytes: bytes, *, task="parse", **k):
        ordre.append("ocr")
        taches.append(task)
        return "corps du document scanné"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    mode, pages = G.ocr_pdf(_pdf_une_page_vide(tmp_path), query="résume ce document")
    # Une page vide relue par le modèle rend du texte : sémantique historique,
    # le mode distingue scan (pages vides) / mixte (texte rendu disponible).
    assert mode == "mixte"
    assert "document scanné" in pages[0]
    assert ordre == ["layout", "ocr"], "la détection précède l'OCR sur la chaîne PDF"
    assert taches == ["parse"]


def test_ocr_pdf_region_table_engage_la_tache_table(tmp_path, monkeypatch):
    """Une région « table » détectée route l'OCR vers le prompt Table Recognition."""

    def _detect(*a, **k):
        return [{"label": "table", "score": 0.95, "box": [10, 10, 300, 300]}]

    taches: list[str] = []

    def _parse(*a, **k):
        taches.append(k.get("task", "parse"))
        return "Table Recognition: rendu"

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    G.ocr_pdf(_pdf_une_page_vide(tmp_path), query="résume ce document")
    assert taches == ["table"]


def test_ocr_pdf_layout_ko_echec_bruyant(tmp_path, monkeypatch):
    """Layout indisponible → RuntimeError avec la cause, l'OCR ne part pas."""
    appels = {"ocr": 0}

    def _detect(*a, **k):
        raise RuntimeError("PP-DocLayoutV3 indisponible : cause du chargement (test)")

    def _parse(*a, **k):
        appels["ocr"] += 1
        return ""

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    with pytest.raises(RuntimeError, match="PP-DocLayoutV3 indisponible"):
        G.ocr_pdf(_pdf_une_page_vide(tmp_path), query="lis le texte")
    assert appels["ocr"] == 0


# ---------- adaptateur worker → gate (pur) ----------


def test_regions_vers_gate_adapte_la_sortie_worker():
    """Le worker parle « label » (fiche PP-DocLayoutV3 §2.1), le gate « category »."""
    regions = [
        {"label": "text", "score": 0.9, "box": [0, 0, 1, 1]},
        {
            "label": "table",
            "score": 0.8,
            "box": [0, 0, 2, 2],
            "polygon_points": [[0, 0]],
        },
        {"category": "figure", "score": 0.7},
    ]
    adaptees = G.regions_vers_gate(regions)
    assert [a["category"] for a in adaptees] == ["text", "table", "figure"]
    assert adaptees[1]["polygon_points"] == [[0, 0]]
    assert G.gate_from_regions(adaptees) == "table"


# ---------- qualification réelle (GPU, marké slow) ----------


@pytest.mark.slow()
def test_qualification_reelle_sur_capture_textuelle():
    """LM réel : une capture dense de texte → verdict « document ».

    Requiert le GGUF LFM2.5-VL-3B provisionné et de la VRAM libre ; sinon skip
    (deux process qui se battent pour un GPU n'est pas un verdict sur le code).
    Critère de la fiche ingestion (§5.1) : « Document et image générale
    distingués par le modèle de vision avant choix de la chaîne ».
    """
    pytest.importorskip("llama_cpp", reason="llama_cpp absent")
    from PIL import Image, ImageDraw, ImageFont

    from backend.config.paths import get_vision_general_default_path

    gguf = get_vision_general_default_path()
    if not gguf.exists():
        pytest.skip(f"modèle LFM2.5-VL-3B absent : {gguf}")

    from backend.inference.manager import ModelManager

    try:
        libre = ModelManager()._get_free_vram_mb()
    except Exception:
        libre = 0
    if libre < 6000:
        pytest.skip(f"VRAM insuffisante pour le test réel ({libre} Mo libres)")

    import io

    img = Image.new("RGB", (800, 600), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("DejaVuSans.ttf", 24)
    except Exception:
        f = None
    for i, ligne in enumerate(
        [
            "Facture n° 2026-041 — Société Dubois",
            "Date : 14/10/2026        Total HT : 45 000 EUR",
            "Désignation : prestation d'audit trimestriel",
            "TVA 20 % : 9 000 EUR      Total TTC : 54 000 EUR",
        ]
    ):
        d.text((40, 60 + i * 60), ligne, fill="black", font=f)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    from backend.inference.loop import _qualifier_image_par_vision
    from backend.ingestion.qualification import verdict_qualification

    manager = ModelManager()
    verdict = verdict_qualification(
        _qualifier_image_par_vision(manager, buf.getvalue())
    )
    assert verdict == "document"


@pytest.mark.slow()
def test_qualification_reelle_sur_mire_sans_texte():
    """LM réel : une mire unie (triangle décentré, aucun texte) → verdict « image »."""
    pytest.importorskip("llama_cpp", reason="llama_cpp absent")
    from PIL import Image, ImageDraw

    from backend.config.paths import get_vision_general_default_path

    gguf = get_vision_general_default_path()
    if not gguf.exists():
        pytest.skip(f"modèle LFM2.5-VL-3B absent : {gguf}")

    from backend.inference.manager import ModelManager

    try:
        libre = ModelManager()._get_free_vram_mb()
    except Exception:
        libre = 0
    if libre < 6000:
        pytest.skip(f"VRAM insuffisante pour le test réel ({libre} Mo libres)")

    import io

    img = Image.new("RGB", (512, 512), (36, 82, 140))
    d = ImageDraw.Draw(img)
    d.polygon([(150, 380), (256, 120), (362, 380)], fill=(240, 200, 70))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    from backend.inference.loop import _qualifier_image_par_vision
    from backend.ingestion.qualification import verdict_qualification

    manager = ModelManager()
    verdict = verdict_qualification(
        _qualifier_image_par_vision(manager, buf.getvalue())
    )
    assert verdict == "image"

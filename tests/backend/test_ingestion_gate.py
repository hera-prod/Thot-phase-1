# Tests gate brouillon — heuristiques pures (sans GPU) + worker gardé (GPU).
from __future__ import annotations

import os

import pytest

from backend.ingestion import gate as G


def test_table_question_mots_cles():
    assert G.is_table_question("lis le chiffre dans le tableau") is True
    assert G.is_table_question("quel est le montant total de la facture ?") is True
    assert G.is_table_question("décris les couleurs de cette photo") is False
    assert G.is_table_question("") is False


def test_looks_tabular():
    assert (
        G.looks_tabular(
            "Date | Client | Montant\n2026 | Dubois | 45000\n2025 | Martin | 9000"
        )
        is True
    )
    assert (
        G.looks_tabular("Le chiffre d'affaires du T2 s'élève à 45 000 euros.") is False
    )
    assert G.looks_tabular("") is False


def test_ocr_pdf_natif_sans_modele(tmp_path):
    """PDF à couche texte dense → extraction directe, 0 appel worker (pur)."""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "natif.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Rapport trimestriel : le chiffre d'affaires atteint 45 000 euros. " * 4,
    )
    doc.save(str(pdf))
    doc.close()
    mode, pages = G.ocr_pdf(str(pdf), query="résume ce document")
    assert mode == "native"
    assert "45 000" in pages[0]


def test_ocr_pdf_scan_sans_worker_renvoie_erreur_claire(tmp_path, monkeypatch):
    """Page scannée + worker KO → RuntimeError explicite (jamais de texte inventé).

    La détection de régions précède l'OCR sur la chaîne scannée (câblage
    PP-DocLayoutV3) : le moqueur couvre le point d'entrée effectif.
    """
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page()  # page blanche = pas de natif
    doc.save(str(pdf))
    doc.close()

    def _boom(*a, **k):
        raise RuntimeError("worker PaddleOCR-VL : venv introuvable (test)")

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _boom)
    with pytest.raises(RuntimeError, match="venv introuvable"):
        G.ocr_pdf(str(pdf), query="lis le texte")


@pytest.mark.slow()
def test_worker_roundtrip_facture():
    """Worker réel : transcription exacte (requiert venv + modèle + CUDA)."""
    import torch

    if not torch.cuda.is_available():
        pytest.skip("pas de CUDA")
    ok, detail = __import__(
        "backend.ingestion.paddle_client", fromlist=["check_available"]
    ).check_available()
    if not ok:
        pytest.skip(detail)
    from backend.ingestion import paddle_client as PC
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (800, 200), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("DejaVuSans.ttf", 40)
    except Exception:
        f = None
    d.text((30, 60), "Facture mars : 45 000 EUR", fill="black", font=f)
    text = PC.parse_png(PC.pil_to_png_bytes(img), task="parse", timeout_s=180)
    assert "45 000" in text
    assert "Facture" in text

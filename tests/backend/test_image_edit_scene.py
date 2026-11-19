"""Scène d'édition sans GPU : toile, fit centré, clamp-to-edge, masque.

Aucune base, aucun modèle : les octets PNG entrent, la scène sort. Ces tests
verrouillent la sémantique exacte du studio — un ratio officiel, une source
ajustée sans déformation, des marges prolongées et un masque d'outpainting.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from backend.services import image_edit_scene as scene


def _make_png(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (40, 80, 120))
    draw = ImageDraw.Draw(image)
    draw.line([(0, 0), (0, height - 1)], fill=(255, 0, 0))
    draw.line([(width - 1, 0), (width - 1, height - 1)], fill=(0, 0, 255))
    draw.line([(0, 0), (width - 1, 0)], fill=(0, 255, 0))
    draw.line([(0, height - 1), (width - 1, height - 1)], fill=(255, 255, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_outpaint_carre_vers_16_9_rect_masque_et_marge_gauche():
    source_bytes = _make_png(64, 64)
    built = scene.build_outpaint_scene(source_bytes, "16:9")

    assert built.canvas_width == 1280 and built.canvas_height == 720
    assert built.aspect_ratio == "16:9"
    assert built.source_size == {"width": 64, "height": 64}
    assert built.source_rect == {"x": 280, "y": 0, "width": 720, "height": 720}

    with _open(built.work_png) as work:
        assert work.size == (1280, 720)
        resized = (
            Image.open(io.BytesIO(source_bytes))
            .convert("RGB")
            .resize((720, 720), Image.Resampling.LANCZOS)
        )
        # La marge gauche vient exactement de la colonne 0 de la source ajustée.
        assert work.getpixel((0, 360)) == resized.getpixel((0, 360))
        # La marge droite vient de la dernière colonne.
        assert work.getpixel((1279, 360)) == resized.getpixel((719, 360))

    with _open(built.mask_png) as mask:
        assert mask.size == (1280, 720)
        assert mask.getpixel((640, 360)) == 0
        for corner in ((0, 0), (1279, 0), (0, 719), (1279, 719)):
            assert mask.getpixel(corner) == 255


def test_outpaint_refuse_une_source_deja_au_format():
    with pytest.raises(ValueError, match="OUTPAINT_NO_MARGIN"):
        scene.build_outpaint_scene(_make_png(1600, 900), "16:9")


def test_outpaint_refuse_des_marges_trop_fines():
    # 1001×1000 : ajustée dans 1024×1024, les deux marges valent 0 et 1 px.
    with pytest.raises(ValueError, match="OUTPAINT_NO_MARGIN"):
        scene.build_outpaint_scene(_make_png(1001, 1000), "1:1")


def test_outpaint_accepte_une_marge_utile_sur_un_seul_axe():
    built = scene.build_outpaint_scene(_make_png(1000, 1000), "16:9")
    assert built.canvas_width == 1280 and built.canvas_height == 720
    assert built.source_rect["width"] == 720
    assert built.source_rect["height"] == 720


def test_transparence_aplatie_sur_gris_128():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    built = scene.build_global_scene(buffer.getvalue(), "1:1")

    with _open(built.work_png) as work:
        assert work.mode == "RGB"
        assert work.getpixel((0, 0)) == (128, 128, 128)
        assert work.getpixel((512, 512)) == (128, 128, 128)


def test_nearest_format_choisit_le_log_ratio_le_plus_proche():
    assert scene.nearest_format(1024, 1024) == "1:1"
    assert scene.nearest_format(1600, 900) == "16:9"
    assert scene.nearest_format(900, 1600) == "9:16"
    assert scene.nearest_format(1200, 800) == "3:2"


def test_source_format_lit_le_format_le_plus_proche_de_la_source():
    assert scene.source_format(_make_png(1024, 1024)) == "1:1"
    assert scene.source_format(_make_png(1280, 720)) == "16:9"
    assert scene.source_format(_make_png(720, 1280)) == "9:16"


def test_source_format_refuse_des_octets_invalides():
    with pytest.raises(ValueError, match=r"\[PNG_INVALID\]"):
        scene.source_format(b"pas une image")


def test_global_sans_ratio_utilise_le_format_le_plus_proche():
    built = scene.build_global_scene(_make_png(1200, 800))

    assert built.aspect_ratio == "3:2"
    assert (built.canvas_width, built.canvas_height) == (1248, 832)
    assert built.mask_png is None
    assert built.source_rect == {"x": 0, "y": 0, "width": 1248, "height": 832}
    with _open(built.work_png) as work:
        assert work.size == (1248, 832)


def test_ratio_inconnu_et_octets_non_image():
    with pytest.raises(ValueError, match="ENUM_INVALID"):
        scene.build_outpaint_scene(_make_png(64, 64), "7:5")
    with pytest.raises(ValueError, match="PNG_INVALID"):
        scene.build_global_scene(b"pas une image", "1:1")


def test_source_trop_grande_refusee_sans_allouer(monkeypatch):
    monkeypatch.setattr(scene, "_MAX_SOURCE_PIXELS", 10)
    with pytest.raises(ValueError, match="TOO_LARGE"):
        scene.build_global_scene(_make_png(64, 64))

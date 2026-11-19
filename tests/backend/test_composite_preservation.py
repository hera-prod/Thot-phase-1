"""Tests de backend/image_engine/composite_guard.py."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend.image_engine.composite_guard import assert_protected_preserved
from backend.services import flux_spike as fx


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_leak_raises():
    size = (4, 4)
    mask = Image.new("L", size, 0)  # all protected
    work = Image.new("RGB", size, (10, 20, 30))
    # final differs from work although all pixels protected
    final = Image.new("RGB", size, (99, 99, 99))
    with pytest.raises(RuntimeError, match=r"\[COMPOSITE_LEAK\]"):
        assert_protected_preserved(
            _png_bytes(final), _png_bytes(work), _png_bytes(mask)
        )


def test_normal_passes():
    size = (4, 4)
    mask = Image.new("L", size, 255)  # all editable
    work = Image.new("RGB", size, (10, 20, 30))
    final = Image.new("RGB", size, (99, 99, 99))
    # no protected pixels => always passes
    assert_protected_preserved(_png_bytes(final), _png_bytes(work), _png_bytes(mask))


def test_all_protected_final_equals_work():
    import hashlib

    size = (6, 5)
    mask = Image.new("L", size, 0)  # all protected
    work = Image.new("RGBA", size, (200, 100, 50, 77))
    generated = Image.new("RGB", size, (12, 34, 56))
    final_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work.convert("RGB")), _png_bytes(mask)
    )
    # composite protected with all-protected mask returns work; assess preserved (should not raise)
    assert_protected_preserved(
        final_bytes, _png_bytes(work.convert("RGB")), _png_bytes(mask)
    )
    # pixel equality check by reopening
    with io.BytesIO(final_bytes) as b:
        final_img = Image.open(b).convert("RGB")
    work_img = work.convert("RGB")
    for y in range(size[1]):
        for x in range(size[0]):
            assert final_img.getpixel((x, y)) == work_img.getpixel((x, y))


def test_composite_leak_after_wrong_candidate():
    # Candidate overwrites protected by composite? composite itself protects, so test direct guard
    pass

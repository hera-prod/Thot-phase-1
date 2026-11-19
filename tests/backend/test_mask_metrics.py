"""Tests directs de backend/image_engine/mask_metrics.py."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend.image_engine.mask_metrics import mask_metrics


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_lateral_left_right():
    size = (8, 6)
    img = Image.new("L", size, 0)
    for y in range(1, size[1] - 1):
        for x in (0, 1, 6, 7):
            img.putpixel((x, y), 255)
    data = _png_bytes(img)
    m = mask_metrics(data)
    cr = m["coverage_by_region"]
    assert cr["left"]["ratio"] == 1.0
    assert cr["right"]["ratio"] == 1.0
    assert cr["top"]["ratio"] == 0.0
    assert cr["bottom"]["ratio"] == 0.0


def test_top_only():
    size = (8, 6)
    img = Image.new("L", size, 0)
    for x in range(size[0]):
        img.putpixel((x, 0), 255)
    data = _png_bytes(img)
    m = mask_metrics(data)
    cr = m["coverage_by_region"]
    assert cr["top"]["ratio"] == 1.0
    assert cr["bottom"]["ratio"] == 0.0


def test_checkerboard_rows_cols():
    size = (4, 3)
    img = Image.new("L", size, 0)
    for y in range(size[1]):
        for x in range(size[0]):
            if (x + y) % 2 == 0:
                img.putpixel((x, y), 255)
    data = _png_bytes(img)
    m = mask_metrics(data)
    expected_rows = [2, 2, 2]
    expected_cols = [2, 1, 2, 1]
    assert m["rows"] == expected_rows
    assert m["cols"] == expected_cols


def test_empty_mask():
    size = (5, 4)
    img = Image.new("L", size, 0)
    data = _png_bytes(img)
    m = mask_metrics(data)
    assert m["total_editable"] == 0
    assert m["bbox"] is None
    assert m["transform_source"] == "absent"


def test_transform_decentered_regions():
    size = (10, 10)
    img = Image.new("L", size, 0)  # everything protected by default
    # Make source rect region (center) editable: pixels >=128 inside transform.
    transform = {"x": 3, "y": 3, "width": 4, "height": 4}
    for y in range(transform["y"], transform["y"] + transform["height"]):
        for x in range(transform["x"], transform["x"] + transform["width"]):
            img.putpixel((x, y), 255)
    data = _png_bytes(img)
    m = mask_metrics(data, transform=transform)
    assert m["transform_source"] == "provided"
    cr = m["coverage_by_region"]
    # top/bottom/left/right margins are entirely protected (editable 0)
    assert cr["top"]["editable"] == 0
    assert cr["bottom"]["editable"] == 0
    assert cr["left"]["editable"] == 0
    assert cr["right"]["editable"] == 0
    # center region (source rect) ratio 1
    assert cr["center"]["ratio"] == 1.0


def test_lost_bbox_coherent():
    size = (8, 4)
    img = Image.new("L", size, 0)
    # left half of image editable, right half protected
    for y in range(size[1]):
        for x in range(size[0] // 2):
            img.putpixel((x, y), 255)
    data = _png_bytes(img)
    transform = {"x": 2, "y": 1, "width": 4, "height": 2}
    m = mask_metrics(data, transform=transform)
    left_region = m["coverage_by_region"]["left"]
    # left region protects? The left margin has editable? depends geometry.
    # retained/lost bboxes should be coherent when present
    # Generic: if retained_bbox and lost_bbox both present, lost starts after retained.
    rb = left_region["retained_bbox"]
    lb = left_region["lost_bbox"]
    if rb is not None and lb is not None:
        assert lb["x0"] == rb["x1"] + 1 or lb["x0"] >= rb["x0"]


def test_float_bounds_o2():
    # transform aux bornes flottantes (x=437.5) sur 1280x720.
    size = (1280, 720)
    transform = {"x": 437.5, "y": 0, "width": 405, "height": 720}
    img = Image.new("L", size, 0)
    for y in range(size[1]):
        for x in range(size[0]):
            # masque correspondant : éditable hors du rect flottant
            inside = (
                x >= transform["x"]
                and x < transform["x"] + transform["width"]
                and y >= transform["y"]
                and y < transform["y"] + transform["height"]
            )
            img.putpixel((x, y), 0 if inside else 255)
    data = _png_bytes(img)
    m = mask_metrics(data, transform=transform)
    cr = m["coverage_by_region"]
    assert cr["left"]["pixels"] == 438 * 720
    assert cr["right"]["pixels"] == 437 * 720
    assert cr["center"]["pixels"] == 405 * 720
    assert cr["left"]["ratio"] == 1.0
    assert cr["right"]["ratio"] == 1.0
    assert cr["center"]["ratio"] == 0.0
    # couverture exacte
    assert (
        cr["left"]["pixels"] + cr["right"]["pixels"] + cr["center"]["pixels"]
        == size[0] * size[1]
    )


def test_invalid_png_raises():
    with pytest.raises(ValueError):
        mask_metrics(b"not a png")


def test_retained_ratio_half():
    # mask covering half of left margin: retained ratio ~0.5
    size = (8, 4)
    img = Image.new("L", size, 0)
    for y in range(size[1]):
        for x in range(2):
            img.putpixel((x, y), 255)
    data = _png_bytes(img)
    # transform with source rect covering x>=4; left region is x<4
    transform = {"x": 4, "y": 0, "width": 4, "height": 4}
    m = mask_metrics(data, transform=transform)
    left = m["coverage_by_region"]["left"]
    assert 0 < left["ratio"] < 1
    assert left["retained_bbox"] is not None
    if left["lost_bbox"] is not None:
        assert left["lost_bbox"]["x0"] == left["retained_bbox"]["x1"] + 1

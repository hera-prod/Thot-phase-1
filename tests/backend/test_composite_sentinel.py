"""Tests ciblés pour la recomposition composite_protected (sentinelle déterministe)."""

from __future__ import annotations

import io

from PIL import Image

from backend.services import flux_spike as fx


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_composite_protected_all_allowed_returns_generated():
    size = (6, 5)
    generated = Image.new("RGB", size, (12, 34, 56))
    work = Image.new("RGBA", size, (200, 100, 50, 77))
    mask = Image.new("L", size, 255)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    for y in range(size[1]):
        for x in range(size[0]):
            assert out.getpixel((x, y)) == (12, 34, 56, 255)


def test_composite_protected_all_protected_returns_work():
    size = (6, 5)
    generated = Image.new("RGB", size, (12, 34, 56))
    work = Image.new("RGBA", size, (200, 100, 50, 77))
    mask = Image.new("L", size, 0)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    for y in range(size[1]):
        for x in range(size[0]):
            assert out.getpixel((x, y)) == (200, 100, 50, 77)


def test_composite_protected_threshold_127_protected_128_allowed():
    generated = Image.new("RGB", (2, 1), (5, 6, 7))
    work = Image.new("RGBA", (2, 1), (1, 2, 3, 4))
    mask = Image.new("L", (2, 1), 0)
    mask.putpixel((0, 0), 127)
    mask.putpixel((1, 0), 128)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    assert out.getpixel((0, 0)) == (1, 2, 3, 4)
    assert out.getpixel((1, 0)) == (5, 6, 7, 255)


def test_composite_protected_disconnected_protected_islands():
    size = (10, 8)
    generated = Image.new("RGB", size, (9, 9, 9))
    work = Image.new("RGBA", size, (77, 88, 99, 210))
    mask = Image.new("L", size, 255)

    for x in (1, 2):
        for y in (1, 2):
            mask.putpixel((x, y), 0)
    for x in (7, 8):
        for y in (5, 6):
            mask.putpixel((x, y), 31)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    for y in range(size[1]):
        for x in range(size[0]):
            mv = int(mask.getpixel((x, y)))  # type: ignore[arg-type]
            expected = (77, 88, 99, 210) if mv < 128 else (9, 9, 9, 255)
            assert out.getpixel((x, y)) == expected, (x, y)


def test_composite_protected_disconnected_allowed_islands():
    size = (10, 8)
    generated = Image.new("RGB", size, (9, 9, 9))
    work = Image.new("RGBA", size, (77, 88, 99, 210))
    mask = Image.new("L", size, 0)

    for x in (1, 2):
        for y in (1, 2):
            mask.putpixel((x, y), 200)
    for x in (7, 8):
        for y in (5, 6):
            mask.putpixel((x, y), 128)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    for y in range(size[1]):
        for x in range(size[0]):
            mv = int(mask.getpixel((x, y)))  # type: ignore[arg-type]
            expected = (77, 88, 99, 210) if mv < 128 else (9, 9, 9, 255)
            assert out.getpixel((x, y)) == expected, (x, y)


def test_composite_protected_sentinel_full_image_recomposition():
    size = (9, 7)
    generated = Image.new("RGBA", size)
    work = Image.new("RGBA", size, (42, 43, 44, 45))
    for y in range(size[1]):
        for x in range(size[0]):
            generated.putpixel(
                (x, y),
                (
                    (x * 37 + y * 13) % 256,
                    (x * 7 + y * 19) % 256,
                    (x * 11 + y * 5) % 256,
                    255,
                ),
            )

    mask = Image.new("L", size, 255)
    for y in (1, 4, 6):
        for x in range(0, size[0], 2):
            mask.putpixel((x, y), 8)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    for y in range(size[1]):
        for x in range(size[0]):
            mv = int(mask.getpixel((x, y)))  # type: ignore[arg-type]
            expected = (42, 43, 44, 45) if mv < 128 else generated.getpixel((x, y))
            assert out.getpixel((x, y)) == expected, (x, y)


def test_composite_protected_checkerboard_disconnected_mask():
    size = (8, 8)
    generated = Image.new("RGBA", size)
    work = Image.new("RGBA", size, (123, 45, 67, 89))
    for y in range(size[1]):
        for x in range(size[0]):
            generated.putpixel((x, y), (x, y, (x + y) % 256, 255))

    mask = Image.new("L", size, 255)
    for origin_x, origin_y in ((0, 0), (3, 0), (0, 5), (5, 4)):
        for dy in range(2):
            for dx in range(2):
                mask.putpixel((origin_x + dx, origin_y + dy), 0)

    out_bytes = fx.composite_protected(
        _png_bytes(generated), _png_bytes(work), _png_bytes(mask)
    )
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    for y in range(size[1]):
        for x in range(size[0]):
            mv = int(mask.getpixel((x, y)))  # type: ignore[arg-type]
            expected = (123, 45, 67, 89) if mv < 128 else generated.getpixel((x, y))
            assert out.getpixel((x, y)) == expected, (x, y)

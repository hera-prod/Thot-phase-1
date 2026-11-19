"""Tests déterministes pour les helpers normalisés de sampling masqué."""

from __future__ import annotations

import io

import numpy as np
import torch
from PIL import Image

from backend.image_engine import masked_sampling as ms


def test_prepare_latent_mask_resizes_nearest_and_thresholds():
    mask = Image.new("L", (4, 4), 0)
    for x in range(2):
        for y in range(2):
            mask.putpixel((x, y), 255)
    latent = ms.prepare_latent_mask(mask, (2, 2))
    assert latent.shape == (1, 1, 2, 2)
    assert latent.dtype == torch.bool
    assert latent[0, 0, 0, 0].item() is True
    assert latent[0, 0, 1, 1].item() is False


def test_prepare_latent_mask_handles_path_and_asymmetric_grid():
    import tempfile
    from pathlib import Path

    mask = Image.new("L", (6, 4), 100)
    mask.putpixel((5, 3), 200)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.png"
        mask.save(p)
        latent = ms.prepare_latent_mask(str(p), (2, 3))
    assert latent.shape == (1, 1, 2, 3)
    # nearest preserves at least the active pixel somewhere
    assert bool(latent.any().item()) is True


def test_prepare_latent_mask_bool_treated_as_255():
    arr = np.zeros((2, 2), dtype=bool)
    arr[0, 0] = True
    latent = ms.prepare_latent_mask(arr, (2, 2))
    assert latent[0, 0, 0, 0].item() is True
    assert latent[0, 0, 1, 1].item() is False


def test_known_latent_formula():
    source = torch.tensor([[[[1.0, 2.0], [3.0, 4.0]]]])
    noise = torch.tensor([[[[0.0, 0.0], [0.0, 0.0]]]])
    out = ms.known_latent(source, noise, 0.25)
    expected = 0.25 * noise + 0.75 * source
    assert torch.allclose(out, expected)


def test_known_latent_last_returns_source():
    source = torch.randn(1, 2, 2, 2)
    noise = torch.randn(1, 2, 2, 2)
    out = ms.known_latent(source, noise, 0.5, last=True)
    assert torch.equal(out, source)


def test_known_latent_validates_sigma_range():
    source = torch.zeros(1, 1, 1, 1)
    noise = torch.ones(1, 1, 1, 1)
    try:
        ms.known_latent(source, noise, 1.5)
    except ValueError:
        pass
    else:
        raise AssertionError("sigma hors plage doit lever ValueError")


def test_apply_mask_composition():
    latent = torch.full((1, 2, 2, 2), 9.0)
    known = torch.full((1, 2, 2, 2), 1.0)
    mask = torch.zeros(1, 2, 2, 2, dtype=torch.bool)
    mask[:, :, 0, 0] = True
    out = ms.apply_mask(latent, known, mask)
    assert out[0, 0, 0, 0].item() == 9.0
    assert out[0, 0, 1, 1].item() == 1.0


def test_blend_known_alias_matches_known_latent():
    source = torch.randn(1, 1, 2, 2)
    noise = torch.randn(1, 1, 2, 2)
    a = ms.blend_known(source, noise, 0.3)
    b = ms.known_latent(source, noise, 0.3)
    assert torch.equal(a, b)


def test_prepare_latent_mask_preserves_disconnected_regions():
    mask = Image.new("L", (8, 8), 0)
    # deux îlots blancs disjoints
    for x, y in ((1, 1), (2, 2), (6, 5), (7, 6)):
        mask.putpixel((x, y), 255)
    latent = ms.prepare_latent_mask(mask, (4, 4))
    assert latent.shape == (1, 1, 4, 4)
    assert int(latent.sum().item()) >= 1  # au moins un pixel actif


def test_masked_module_compiles():
    import backend.image_engine.masked_sampling as module

    assert module.prepare_latent_mask is not None


def test_prepare_latent_mask_uint8_values():
    import torch
    from backend.image_engine import masked_sampling as ms

    for val in (0, 127, 128, 255):
        t = torch.tensor([[val]], dtype=torch.uint8)
        out = ms.prepare_latent_mask(t, (1, 1), convention="uint8")
        assert out[0, 0, 0, 0].item() is (val >= 128)


def test_prepare_latent_mask_bool_values():
    import torch
    from backend.image_engine import masked_sampling as ms

    for val in (False, True):
        t = torch.tensor([[val]], dtype=torch.bool)
        out = ms.prepare_latent_mask(t, (1, 1), convention="bool")
        assert out[0, 0, 0, 0].item() is bool(val)


def test_prepare_latent_mask_unit_values():
    import torch
    from backend.image_engine import masked_sampling as ms

    pairs = ((0.0, False), (0.49, False), (0.5, True), (1.0, True))
    for val, expected in pairs:
        t = torch.tensor([[val]], dtype=torch.float32)
        out = ms.prepare_latent_mask(t, (1, 1), convention="unit")
        assert out[0, 0, 0, 0].item() is expected


def test_prepare_latent_mask_float_uint8_refused():
    import torch
    import pytest
    from backend.image_engine import masked_sampling as ms

    t = torch.tensor([[1.0]], dtype=torch.float32)
    with pytest.raises(ValueError, match=r"\[MASK_CONVENTION\]"):
        ms.prepare_latent_mask(t, (1, 1), convention="uint8")
    with pytest.raises(ValueError, match=r"\[MASK_CONVENTION\]"):
        ms.prepare_latent_mask(t, (1, 1))  # default


def test_prepare_latent_mask_nan_inf_refused():
    import torch
    import pytest
    import math
    from backend.image_engine import masked_sampling as ms

    for bad in (float("nan"), float("inf"), -float("inf")):
        t = torch.tensor([[bad]], dtype=torch.float32)
        with pytest.raises(ValueError):
            ms.prepare_latent_mask(t, (1, 1), convention="unit")


def test_prepare_latent_mask_shapes_refused():
    import torch
    import pytest
    from backend.image_engine import masked_sampling as ms

    with pytest.raises(ValueError):
        ms.prepare_latent_mask(
            torch.zeros(4, dtype=torch.float32), (1, 1), convention="unit"
        )
    with pytest.raises(ValueError):
        ms.prepare_latent_mask(
            torch.zeros(1, 1, 1, 1, 1, dtype=torch.float32), (1, 1), convention="unit"
        )


def test_prepare_latent_mask_bool_also_uint8():
    import torch
    from backend.image_engine import masked_sampling as ms

    t = torch.tensor([[True, False]], dtype=torch.bool)
    out = ms.prepare_latent_mask(t, (1, 2), convention="uint8")
    assert out[0, 0, 0, 0].item() is True
    assert out[0, 0, 0, 1].item() is False


def test_apply_mask_requires_bool():
    import torch
    import pytest
    from backend.image_engine import masked_sampling as ms

    latent = torch.ones(1, 1, 2, 2)
    known = torch.zeros(1, 1, 2, 2)
    with pytest.raises(ValueError):
        ms.apply_mask(latent, known, torch.tensor(0.5))
    out = ms.apply_mask(latent, known, torch.zeros(1, 1, 2, 2, dtype=torch.bool))
    assert out.shape == latent.shape


def test_prepare_latent_mask_nonregression_png_L():
    from PIL import Image
    import torch
    from backend.image_engine import masked_sampling as ms

    mask = Image.new("L", (8, 8), 0)
    for x in range(4):
        for y in range(4):
            mask.putpixel((x, y), 255)
    latent = ms.prepare_latent_mask(mask, (4, 4), convention="uint8")
    assert latent.shape == (1, 1, 4, 4)
    assert latent[0, 0, 0, 0].item() is True
    assert latent[0, 0, 3, 3].item() is False

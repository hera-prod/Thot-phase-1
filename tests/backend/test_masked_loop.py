"""Tests déterministes pour la boucle d'échantillonnage masqué."""

from __future__ import annotations

import torch
import pytest

from backend.image_engine import masked_loop as ml
from backend.image_engine import masked_sampling as ms


def _make_inputs(h: int = 24, w: int = 40, c: int = 32):
    source = torch.zeros(1, c, h, w, dtype=torch.float32)
    noise = torch.zeros(1, c, h, w, dtype=torch.float32)
    lat = torch.zeros(1, c, h, w, dtype=torch.float32)
    sigmas = [1.0, 0.75, 0.5, 0.25, 0.0]
    return lat, sigmas, source, noise


def _denoise(latent, t):
    # motif dépendant de x et de y, distinct de la source (zéro)
    h, w = latent.shape[-2:]
    yy = torch.arange(h, device=latent.device, dtype=torch.float32).view(1, 1, h, 1)
    xx = torch.arange(w, device=latent.device, dtype=torch.float32).view(1, 1, 1, w)
    return (latent * 0.0 + xx + yy).to(latent.dtype)


def _run(mask, h=24, w=40):
    lat, sigmas, source, noise = _make_inputs(h, w)
    mask_t = torch.as_tensor(mask).to(torch.bool)
    if mask_t.ndim == 2:
        mask_t = mask_t.unsqueeze(0).unsqueeze(0)
    out_lat, trace = ml.sample_masked(
        lat=lat,
        sigmas=sigmas,
        start_index=0,
        source_latent=source,
        noise=noise,
        mask_lat_bool=mask_t,
        denoise=_denoise,
        steps=4,
    )
    return out_lat, trace, source, mask_t


def _rect_mask(h, w, r0, r1, c0, c1):
    m = torch.zeros(h, w, dtype=torch.bool)
    m[r0:r1, c0:c1] = True
    return m


def test_loop_nonsquare_basic():
    mask = _rect_mask(24, 40, 0, 24, 0, 20)  # left band
    out, trace, source, m = _run(mask)
    assert out.shape == (1, 32, 24, 40)
    assert len(trace) == 4
    for e in trace:
        assert e["protected_max_delta"] == 0.0


def test_loop_two_left_right_bands():
    m = torch.zeros(24, 40, dtype=torch.bool)
    m[:, :8] = True
    m[:, 32:] = True
    out, trace, source, mask = _run(m)
    prot = ~mask.expand(1, 32, 24, 40)
    assert torch.equal(out[prot], source[prot])
    # dernière étape : protégé == source exactement
    assert torch.equal(out[prot], source[prot])


def test_loop_two_top_bottom_bands():
    m = torch.zeros(24, 40, dtype=torch.bool)
    m[:6, :] = True
    m[18:, :] = True
    out, trace, source, mask = _run(m)
    for e in trace:
        assert e["protected_max_delta"] == 0.0
    prot = ~mask.expand(1, 32, 24, 40)
    assert torch.equal(out[prot], source[prot])


def test_loop_interior_rectangle_decentered():
    mask = _rect_mask(24, 40, 6, 14, 12, 28)
    out, trace, source, m = _run(mask)
    prot = ~m.expand(1, 32, 24, 40)
    editable = m.expand(1, 32, 24, 40)
    assert torch.equal(out[prot], source[prot])
    assert bool(torch.ne(out[editable], source[editable]).any().item())


def test_loop_shifted_mask_changes_values():
    m1 = _rect_mask(24, 40, 4, 12, 4, 12)
    m2 = _rect_mask(24, 40, 5, 13, 5, 13)
    out1, tr1, _, _ = _run(m1)
    out2, tr2, _, _ = _run(m2)
    assert not torch.equal(out1, out2)


def test_loop_protected_exact_source_last():
    mask = _rect_mask(24, 40, 6, 14, 12, 28)
    out, trace, source, m = _run(mask)
    prot = ~m.expand(1, 32, 24, 40)
    assert torch.equal(out[prot], source[prot])


def test_loop_editable_differs_from_source():
    mask = _rect_mask(24, 40, 6, 14, 12, 28)
    out, trace, source, m = _run(mask)
    editable = m.expand(1, 32, 24, 40)
    assert bool(torch.ne(out[editable], source[editable]).any().item())


def test_loop_representation_equivalence_exact_upsample():
    h, w = 24, 40
    latent_mask = _rect_mask(h, w, 6, 14, 12, 28)
    pixel_mask = latent_mask.repeat_interleave(8, dim=0).repeat_interleave(8, dim=1)
    m_latent = ms.prepare_latent_mask(latent_mask, (h, w), convention="bool")
    m_pixel = ms.prepare_latent_mask(pixel_mask, (h, w), convention="bool")
    assert torch.equal(m_latent, m_pixel)


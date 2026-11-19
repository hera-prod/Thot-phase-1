from __future__ import annotations

from types import SimpleNamespace

import pytest
import torch

from backend.image_engine.models.vae import Flux2KleinVAE
from backend.image_engine.runtime.device_probe import DeviceInfo
from backend.image_engine.runtime.placement import (
    GIB,
    ImageActivationShortfallError,
    enforce_generation_memory,
    estimate_image_memory_bytes,
    plan_generation_memory,
)


def _cuda(free_gib: float = 8.0, total_gib: float = 12.0) -> DeviceInfo:
    return DeviceInfo(
        backend="cuda",
        name="test-cuda",
        total_vram_gb=total_gib,
        free_vram_gb=free_gib,
        unified_memory=False,
        ram_total_gb=32.0,
        ram_free_gb=24.0,
        device_index=1,
    )


def test_image_memory_estimate_grows_with_pixels_and_batch():
    _, _, _, one = estimate_image_memory_bytes(1024, 1024)
    _, _, _, larger = estimate_image_memory_bytes(1536, 1536)
    _, _, _, batched = estimate_image_memory_bytes(1024, 1024, batch_size=2)
    assert larger > one
    assert batched > one


def test_generation_plan_reserves_vram_and_enables_low_vram_tiling():
    plan = plan_generation_memory(_cuda(), 2048, 2048, mode="low_vram")
    assert plan.mode == "low_vram"
    assert plan.use_tiling is True
    assert plan.reserve_bytes >= 768 * 1024 * 1024
    assert plan.free_budget_bytes < int(8 * 1_000_000_000)
    assert plan.working_bytes > 0


def test_generation_preflight_refuses_unrealistic_activation_budget():
    plan = plan_generation_memory(_cuda(free_gib=1.5, total_gib=2.0), 2048, 2048)
    assert plan.fits is False
    with pytest.raises(ImageActivationShortfallError, match=r"MEMORY_PREFLIGHT_FAILED"):
        enforce_generation_memory(plan)


def test_generation_preflight_override_is_explicit(monkeypatch):
    plan = plan_generation_memory(_cuda(free_gib=1.5, total_gib=2.0), 2048, 2048)
    monkeypatch.setenv("THOT_IMAGE_ALLOW_OVERSIZED", "1")
    enforce_generation_memory(plan)


def test_device_info_keeps_selected_cuda_index():
    device = _cuda()
    assert device.torch_device == "cuda:1"
    assert device.free_vram_bytes == int(device.free_vram_gb * 1_000_000_000)


class _FakeVAE:
    def __init__(self, oom_count: int = 0):
        self.device = torch.device("cpu")
        self.dtype = torch.float32
        self.oom_count = oom_count
        self.error: Exception | None = None
        self.calls = 0
        self.tiling = False
        self.slicing = False

    def enable_tiling(self):
        self.tiling = True

    def disable_tiling(self):
        self.tiling = False

    def enable_slicing(self):
        self.slicing = True

    def disable_slicing(self):
        self.slicing = False

    def decode(self, value):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.calls <= self.oom_count:
            raise torch.cuda.OutOfMemoryError("simulated")
        return SimpleNamespace(sample=torch.ones(1, 3, 64, 64, dtype=value.dtype))


def test_vae_oom_recovery_retries_once_with_tiling():
    vae = Flux2KleinVAE(config=SimpleNamespace(), vae_path=None)
    fake = _FakeVAE(oom_count=1)
    vae._vae = fake
    result = vae.decode(torch.zeros(1, 32, 8, 8), recover_oom=True)
    assert result.shape == (1, 3, 64, 64)
    assert fake.calls == 2
    assert fake.tiling is True


def test_vae_second_oom_is_explicit():
    vae = Flux2KleinVAE(config=SimpleNamespace(), vae_path=None)
    fake = _FakeVAE(oom_count=2)
    vae._vae = fake
    with pytest.raises(RuntimeError, match=r"\[VAE_OOM\]"):
        vae.decode(torch.zeros(1, 32, 8, 8), recover_oom=True)
    assert fake.calls == 2
    assert fake.tiling is True


def test_vae_non_oom_is_not_retried():
    vae = Flux2KleinVAE(config=SimpleNamespace(), vae_path=None)
    fake = _FakeVAE()
    fake.error = ValueError("bad decode")
    vae._vae = fake
    with pytest.raises(ValueError, match="bad decode"):
        vae.decode(torch.zeros(1, 32, 8, 8), recover_oom=True)
    assert fake.calls == 1


def test_vae_constructor_missing_path_raises():
    with pytest.raises(RuntimeError, match=r"\[VAE_UNAVAILABLE\]"):
        Flux2KleinVAE(config=SimpleNamespace(), vae_path="/definitely/missing/vae")


def test_vae_encode_decode_missing_raises():
    vae = Flux2KleinVAE(config=SimpleNamespace(), vae_path=None)
    with pytest.raises(RuntimeError, match=r"\[VAE_UNAVAILABLE\]"):
        vae.encode(torch.zeros(1, 3, 8, 8))
    with pytest.raises(RuntimeError, match=r"\[VAE_UNAVAILABLE\]"):
        vae.decode(torch.zeros(1, 32, 1, 1))

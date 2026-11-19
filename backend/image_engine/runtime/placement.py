from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_RESERVE_BYTES = 768 * MIB
DEFAULT_FRACTION_RESERVE = 0.12
DEFAULT_LOW_VRAM_TILING_PIXELS = 2_000_000
MEMORY_MODES = ("auto", "balanced", "low_vram")


@dataclass(frozen=True)
class GenerationMemoryPlan:
    width: int
    height: int
    batch_size: int
    activation_bytes: int
    dequant_peak_bytes: int
    fixed_overhead_bytes: int
    working_bytes: int
    free_budget_bytes: int
    reserve_bytes: int
    fits: bool
    use_tiling: bool
    use_slicing: bool
    mode: str
    reason: str

    @property
    def working_gib(self) -> float:
        return self.working_bytes / GIB

    @property
    def budget_gib(self) -> float:
        return self.free_budget_bytes / GIB


class ImageActivationShortfallError(ValueError):
    pass


def normalize_memory_mode(value: str | None) -> str:
    if value is None or not str(value).strip():
        return "auto"
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized not in MEMORY_MODES:
        raise ValueError(
            f"[MEMORY_MODE_INVALID] mode mémoire inconnu: {value!r}; "
            f"valeurs: {', '.join(MEMORY_MODES)}"
        )
    return normalized


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"[MEMORY_INPUT_INVALID] {name} doit être un entier positif")
    return value


def estimate_image_memory_bytes(
    width: int,
    height: int,
    batch_size: int = 1,
    condition_pixels: int = 0,
) -> tuple[int, int, int, int]:
    width = _positive_int(width, "width")
    height = _positive_int(height, "height")
    batch_size = _positive_int(batch_size, "batch_size")
    condition_pixels = max(0, int(condition_pixels))
    megapixels = width * height / 1_000_000
    condition_megapixels = condition_pixels / 1_000_000
    activation = math.ceil(
        (0.70 + 0.75 * megapixels * batch_size + 0.20 * condition_megapixels) * GIB
    )
    dequant = math.ceil(0.60 * GIB)
    fixed = math.ceil(0.35 * GIB)
    working = activation + dequant + fixed
    return activation, dequant, fixed, working


def _device_bytes(device_info: Any, attribute: str) -> int:
    value = getattr(device_info, f"{attribute}_bytes", None)
    if isinstance(value, int) and value > 0:
        return value
    legacy = getattr(device_info, f"{attribute}_gb", 0.0)
    try:
        number = float(legacy)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number * 1_000_000_000)


def plan_generation_memory(
    device_info: Any,
    width: int,
    height: int,
    batch_size: int = 1,
    mode: str | None = None,
    condition_pixels: int = 0,
) -> GenerationMemoryPlan:
    activation, dequant, fixed, working = estimate_image_memory_bytes(
        width, height, batch_size, condition_pixels
    )
    requested_mode = normalize_memory_mode(mode)
    unified = bool(getattr(device_info, "unified_memory", False))
    discrete = getattr(device_info, "backend", "cpu") in ("cuda", "rocm") and not unified
    free_bytes = _device_bytes(device_info, "free_vram")
    total_bytes = _device_bytes(device_info, "total_vram")
    has_budget = free_bytes > 0 and (not discrete or total_bytes <= 0 or free_bytes <= total_bytes)
    reserve = 0
    budget = 0
    if discrete and has_budget:
        reserve = max(DEFAULT_RESERVE_BYTES, math.ceil(free_bytes * DEFAULT_FRACTION_RESERVE))
        budget = max(0, free_bytes - reserve)
    fits = not discrete or not has_budget or working <= budget
    use_tiling = requested_mode == "low_vram" or width * height >= DEFAULT_LOW_VRAM_TILING_PIXELS
    use_slicing = batch_size > 1
    if not discrete:
        reason = "mémoire unifiée ou CPU : le préflight GPU est ignoré"
    elif not has_budget:
        reason = "budget VRAM indisponible : préflight ouvert"
    elif fits:
        reason = (
            f"estimation {working / GIB:.2f} Gio, budget utilisable {budget / GIB:.2f} Gio"
        )
    else:
        reason = (
            f"estimation {working / GIB:.2f} Gio, budget utilisable {budget / GIB:.2f} Gio"
        )
    selected_mode = "low_vram" if requested_mode == "low_vram" or not fits else requested_mode
    if selected_mode == "auto":
        selected_mode = "balanced"
    return GenerationMemoryPlan(
        width=width,
        height=height,
        batch_size=batch_size,
        activation_bytes=activation,
        dequant_peak_bytes=dequant,
        fixed_overhead_bytes=fixed,
        working_bytes=working,
        free_budget_bytes=budget,
        reserve_bytes=reserve,
        fits=fits,
        use_tiling=use_tiling,
        use_slicing=use_slicing,
        mode=selected_mode,
        reason=reason,
    )


def oversized_generation_allowed() -> bool:
    return os.environ.get("THOT_IMAGE_ALLOW_OVERSIZED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def enforce_generation_memory(plan: GenerationMemoryPlan) -> None:
    if plan.fits or oversized_generation_allowed():
        return
    raise ImageActivationShortfallError(
        f"[MEMORY_PREFLIGHT_FAILED] Génération {plan.width}x{plan.height} estimée à "
        f"{plan.working_gib:.2f} Gio de mémoire de travail, mais seulement "
        f"{plan.budget_gib:.2f} Gio de VRAM sont utilisables. "
        "Réduisez la résolution ou libérez de la mémoire GPU. "
        "Les activations ne peuvent pas être offloadées comme les poids."
    )


def choose_mode(dev, resolution: int, override: str | None = None) -> str:
    if override:
        if override == "offload":
            return "offload"
        return override
    need_simul = 14.0 if resolution <= 1024 else 17.0
    need_phased = 8.5 if resolution <= 1024 else 12.0
    if dev.unified_memory:
        return "unified" if dev.ram_free_gb >= need_phased + 2 else "cpu"
    if dev.backend in ("cuda", "rocm"):
        if dev.free_vram_gb >= need_simul:
            return "full_gpu_simult"
        if dev.free_vram_gb >= need_phased:
            return "full_gpu_phased"
        if dev.free_vram_gb >= 6.0:
            return "hybrid_custom"
        return "cpu"
    return "cpu"


def device_of(mode: str) -> str:
    if mode in ("full_gpu_simult", "full_gpu_phased", "hybrid_custom", "hybrid"):
        return "cuda"
    if mode == "unified":
        return "mps"
    if mode == "offload":
        return "cuda"
    return "cpu"


def cap_ref_res(mode: str, target_res: int, free_vram_gb: float) -> int:
    if mode in ("full_gpu_simult", "full_gpu_phased"):
        if target_res <= 1024:
            if free_vram_gb >= 10.5:
                return 1024
            if free_vram_gb >= 9.5:
                return 768
            return 512
        return 512
    return 512

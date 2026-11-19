"""Validation H — oracle diffusers (workaround config= explicite, issue #13001) + criteres statistiques."""

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image


def delta_mean(img_a: Image.Image, img_b: Image.Image) -> float:
    a = np.array(img_a).astype(np.float32) / 255.0
    b = np.array(img_b).astype(np.float32) / 255.0
    if a.shape != b.shape:
        b = np.array(img_b.resize(img_a.size, Image.BILINEAR)).astype(np.float32) / 255.0
    return float(np.abs(a - b).mean())


def delta_std(img_a: Image.Image, img_b: Image.Image) -> float:
    a = np.array(img_a).astype(np.float32) / 255.0
    b = np.array(img_b).astype(np.float32) / 255.0
    if a.shape != b.shape:
        b = np.array(img_b.resize(img_a.size, Image.BILINEAR)).astype(np.float32) / 255.0
    return float(abs(a.std() - b.std()))


def psnr(img_a: Image.Image, img_b: Image.Image) -> float:
    a = np.array(img_a).astype(np.float32)
    b = np.array(img_b.resize(img_a.size, Image.BILINEAR)).astype(np.float32) if a.shape != np.array(img_b).shape else np.array(img_b).astype(np.float32)
    mse = np.mean((a - b) ** 2)
    if mse == 0:
        return float("inf")
    return float(20 * np.log10(255.0 / np.sqrt(mse)))


def _load_structural_similarity() -> Callable[..., float] | None:
    try:
        metrics = importlib.import_module("skimage.metrics")
    except ImportError:
        return None
    candidate = getattr(metrics, "structural_similarity", None)
    if not callable(candidate):
        return None
    return cast(Callable[..., float], candidate)


def ssim_simple(img_a: Image.Image, img_b: Image.Image) -> float:
    structural_similarity = _load_structural_similarity()
    if structural_similarity is not None:
        a = np.array(img_a.convert("L"))
        b = np.array(img_b.convert("L").resize(img_a.size, Image.BILINEAR))
        try:
            return float(structural_similarity(a, b, data_range=255))
        except ImportError:
            pass

    a = np.array(img_a.convert("L")).astype(np.float32).flatten()
    b = np.array(img_b.convert("L").resize(img_a.size, Image.BILINEAR)).astype(np.float32).flatten()
    a_n = (a - a.mean()) / (a.std() + 1e-8)
    b_n = (b - b.mean()) / (b.std() + 1e-8)
    return float(np.clip((a_n * b_n).mean(), -1, 1))


def validate_output(image: Image.Image, reference: Path | None = None) -> dict:
    """
    Criteres v3 H: PSNR > 20 dB, Δmean/Δstd < 15, + verification visuelle agent
    (4 cas: sujet simple, texte/menu, infographie, manga + 1 TI2I).
    Gates: VRAM ≤ 8,5 Go, RAM anon ≤ 3 Go, swap ≤0.16 Go (seuil calibré, voir memprof).
    """
    result = {
        "size": image.size,
        "mode": image.mode,
        "mean": float(np.array(image).mean()),
        "std": float(np.array(image).std()),
    }
    if reference and Path(reference).exists():
        ref = Image.open(reference).convert("RGB")
        result["delta_mean"] = delta_mean(image, ref)
        result["delta_std"] = delta_std(image, ref)
        result["psnr"] = psnr(image, ref)
        result["ssim"] = ssim_simple(image, ref)
        # Gates v3
        result["psnr_pass"] = result["psnr"] > 20
        result["delta_pass"] = result["delta_mean"] < 0.15 and result["delta_std"] < 0.15
    return result

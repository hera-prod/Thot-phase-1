"""Helpers pour l'échantillonnage masqué des latents FLUX.2.

Trois opérations pures, sans chargement de poids ni accès GPU :

* :func:`prepare_latent_mask` — réduit un masque pixels en masque latent
  booléen (interpolation *nearest*, seuil par convention).
* :func:`known_latent` — calcule le latent « connu » (bruité ou final) pour
  les zones hors masque.
* :func:`apply_mask` — compose ``torch.where(mask, latent, known)``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

__all__ = [
    "apply_mask",
    "known_latent",
    "prepare_latent_mask",
]

_VALID_CONVENTIONS = frozenset({"uint8", "bool", "unit"})


def _as_hw(target_hw: Any) -> tuple[int, int]:
    """Parse ``target_hw`` en couple ``(H, W)`` strictement positif."""
    if isinstance(target_hw, int):
        items: tuple[int, ...] = (int(target_hw), int(target_hw))
    else:
        try:
            items = tuple(int(x) for x in target_hw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"[MASK_CONVENTION] target_hw invalide : {exc}") from exc
    if len(items) != 2:
        raise ValueError("target_hw doit contenir exactement deux entiers (H, W).")
    if items[0] <= 0 or items[1] <= 0:
        raise ValueError("target_hw doit être strictement positif.")
    return (int(items[0]), int(items[1]))


def _src_to_tensor(src_mask: Any, convention: str) -> torch.Tensor:
    """Convertit une source de masque en tenseur, en respectant la convention."""
    if isinstance(src_mask, (str, os.PathLike, Path)):
        with Image.open(src_mask) as img:
            src_mask = img.convert("L")
    if isinstance(src_mask, Image.Image):
        src_mask = src_mask.convert("L")
        arr = np.asarray(src_mask)
        return torch.from_numpy(np.asarray(arr).copy())

    if isinstance(src_mask, np.ndarray):
        arr = np.asarray(src_mask)
        if arr.dtype.kind == "f" and convention == "uint8":
            raise ValueError(
                "[MASK_CONVENTION] masque flottant avec convention uint8 refusé"
            )
        return torch.from_numpy(np.asarray(arr).copy())

    if isinstance(src_mask, torch.Tensor):
        if src_mask.is_floating_point() and convention == "uint8":
            raise ValueError(
                "[MASK_CONVENTION] masque flottant avec convention uint8 refusé"
            )
        return src_mask

    tensor = torch.as_tensor(src_mask)
    if tensor.is_floating_point() and convention == "uint8":
        raise ValueError(
            "[MASK_CONVENTION] masque flottant avec convention uint8 refusé"
        )
    return tensor


def prepare_latent_mask(
    src_mask: Any, target_hw: Any, *, convention: str = "uint8"
) -> torch.Tensor:
    """Renvoie un masque latent booléen après interpolation *nearest-exact*.

    Le seuil dépend de ``convention`` :

    * ``"uint8"`` : valeurs entières 0..255, seuil ``>= 128`` ;
    * ``"bool"`` : ``True`` équivaut à 255 ;
    * ``"unit"`` : flottants 0.0..1.0, seuil ``>= 0.5``.

    La sortie a la forme ``[B, 1, H, W]`` (ou ``[B, C, H, W]`` selon l'entrée)
    booléenne. Aucune mise à l'échelle silencieuse n'est effectuée.
    """
    if convention not in _VALID_CONVENTIONS:
        raise ValueError(f"[MASK_CONVENTION] convention inconnue : {convention!r}")

    target: tuple[int, int] = _as_hw(target_hw)
    mask: torch.Tensor = _src_to_tensor(src_mask, convention)

    if mask.ndim not in (2, 3, 4):
        raise ValueError(
            f"[MASK_CONVENTION] forme non supportée : ndim={mask.ndim} "
            "(attendu 2D, 3D [B,H,W] ou 4D [B,C,H,W])"
        )

    # Normalise en [B, C, H, W].
    if mask.ndim == 2:
        mask = mask.unsqueeze(0).unsqueeze(0)
    elif mask.ndim == 3:
        mask = mask.unsqueeze(1)

    # Conversion dtype -> flottant dans l'espace de la convention.
    if mask.dtype == torch.bool:
        if convention == "unit":
            raise ValueError(
                "[MASK_CONVENTION] convention unit exige un tenseur flottant"
            )
        scale = 255.0
        mask_f = mask.to(torch.float32) * scale
    elif convention == "uint8":
        if mask.dtype.is_floating_point:
            # Sécurité : un flottant ne doit jamais arriver ici.
            raise ValueError(
                "[MASK_CONVENTION] masque flottant avec convention uint8 refusé"
            )
        if mask.is_complex():
            raise ValueError("[MASK_CONVENTION] dtype complexe non supporté")
        values_f = mask.to(torch.float64)
        if torch.isnan(values_f).any() or torch.isinf(values_f).any():
            raise ValueError(
                "[MASK_CONVENTION] valeurs non finies dans le masque uint8"
            )
        if bool((values_f < 0).any().item()) or bool((values_f > 255).any().item()):
            raise ValueError("[MASK_CONVENTION] valeur hors plage uint8")
        mask_f = mask.to(torch.float32)
    elif convention == "bool":
        if mask.dtype != torch.bool:
            raise ValueError(
                "[MASK_CONVENTION] convention bool exige un tenseur booléen"
            )
        mask_f = mask.to(torch.float32) * 255.0
    elif convention == "unit":
        if not mask.is_floating_point():
            raise ValueError(
                "[MASK_CONVENTION] convention unit exige un tenseur flottant"
            )
        mask_f = mask.to(torch.float32)
    else:  # pragma: no cover - gardé par la validation amont
        raise ValueError(f"[MASK_CONVENTION] convention inconnue : {convention!r}")

    if mask_f.is_floating_point():
        if not torch.isfinite(mask_f).all():
            raise ValueError("[MASK_CONVENTION] masque contient des valeurs non finies")

    if convention == "unit":
        if bool((mask_f < 0).any().item()) or bool((mask_f > 1).any().item()):
            raise ValueError("[MASK_CONVENTION] valeur hors plage unit")
    elif convention in ("uint8", "bool"):
        if bool((mask_f < 0).any().item()) or bool((mask_f > 255).any().item()):
            raise ValueError("[MASK_CONVENTION] valeur hors plage uint8")

    if convention == "unit":
        boolean = mask_f >= 0.5
    else:
        boolean = mask_f >= 128.0

    resized = F.interpolate(mask_f, size=target, mode="nearest-exact")
    if convention == "unit":
        out = resized >= 0.5
    else:
        out = resized >= 128.0
    return out.to(torch.bool)


def known_latent(
    source: Any,
    noise: Any,
    sigma: Any,
    last: bool = False,
) -> torch.Tensor:
    """Calcule le latent connu pour les zones hors masque.

    ``known = (1 - sigma) * source + sigma * noise`` ; au dernier pas
    (``last=True``) le latent connu est ``source`` lui-même.
    """
    source_t = torch.as_tensor(source)
    noise_t = torch.as_tensor(noise)

    if source_t.shape != noise_t.shape:
        raise ValueError("source et noise doivent avoir la même forme.")
    if not torch.isfinite(source_t).all() or not torch.isfinite(noise_t).all():
        raise ValueError("source ou noise contient des valeurs non finies.")

    if last:
        return source_t.clone()

    sigma_t = torch.as_tensor(sigma)
    try:
        sigma_t = sigma_t.to(dtype=source_t.dtype, device=source_t.device)
    except RuntimeError as exc:
        raise ValueError(f"sigma incompatible : {exc}") from exc

    if sigma_t.numel() == 1:
        try:
            sigma_value = float(sigma_t.detach().reshape(-1)[0])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"sigma non scalaire invalide : {exc}") from exc
        if not (0.0 <= sigma_value <= 1.0):
            raise ValueError("sigma doit être compris entre 0 et 1.")
    if not torch.isfinite(sigma_t).all():
        raise ValueError("sigma contient des valeurs non finies.")

    return (1.0 - sigma_t) * source_t + sigma_t * noise_t


def apply_mask(latent: Any, known: Any, mask: Any) -> torch.Tensor:
    """Compose ``torch.where(mask, latent, known)``.

    ``mask`` doit être un ``torch.bool`` ; sinon ``ValueError``.
    """
    latent_t = torch.as_tensor(latent)
    known_t = torch.as_tensor(known)
    mask_t = torch.as_tensor(mask)

    if mask_t.dtype != torch.bool:
        raise ValueError("[MASK_CONVENTION] apply_mask exige un masque torch.bool")
    if latent_t.dtype != known_t.dtype:
        known_t = known_t.to(latent_t.dtype)

    return torch.where(mask_t, latent_t, known_t)


def blend_known(source, noise, sigma, last=False):
    """Alias public historique de ``known_latent``.

    Conservé sous ce nom pour correspondre au contrat demandé.
    """

    return known_latent(source, noise, sigma, last=last)

"""Boucle d'échantillonnage masqué extraite de ``spike_t2i`` (P-09).

La fonction :func:`sample_masked` reproduit exactement l'ordre numérique de la
boucle historique ``spike_t2i.py`` (412-454) : masque des valeurs connues,
débruitage, pas d'Euler, puis remasquage. Le débruiteur est injecté pour
permettre les tests sans GPU ni poids.
"""

from __future__ import annotations

from typing import Any, Callable

import torch

from backend.image_engine.masked_sampling import apply_mask, known_latent

__all__ = ["sample_masked"]


def _broadcast_mask(mask: Any, shape: tuple[int, ...]) -> torch.Tensor:
    """Aligne un masque (ndim <= len(shape)) sur ``shape``."""
    result = torch.as_tensor(mask)
    while result.ndim < len(shape):
        result = result.unsqueeze(0)
    return result.expand(*shape).to(torch.bool)


def sample_masked(
    *,
    lat: Any,
    sigmas: Any,
    start_index: int,
    source_latent: Any,
    noise: Any,
    mask_lat_bool: Any,
    denoise: Callable[[Any, Any], Any],
    steps: int = 4,
) -> tuple[torch.Tensor, list[dict]]:
    """Exécute la boucle Euler masquée et renvoie ``(latent_final, trace)``.

    ``denoise(latent, sigma)`` doit renvoyer un tenseur de la même forme que
    ``latent``.
    """
    lat_t = torch.as_tensor(lat)
    has_mask = mask_lat_bool is not None
    if has_mask:
        if source_latent is None or noise is None:
            raise ValueError("source_latent et noise sont requis avec un masque.")

    trace: list[dict] = []

    for i in range(start_index, steps):
        t = sigmas[i]
        tn = sigmas[i + 1]

        if has_mask:
            known_t = known_latent(
                torch.as_tensor(source_latent).float(),
                torch.as_tensor(noise).float(),
                t,
            )
            lat_t = apply_mask(lat_t, known_t.to(lat_t.dtype), mask_lat_bool)

        v = denoise(lat_t, t)
        v_t = torch.as_tensor(v)
        lat_t = (lat_t.float() + (tn - t) * v_t.float()).to(lat_t.dtype)

        if has_mask:
            if i < steps - 1:
                known_next = known_latent(
                    torch.as_tensor(source_latent).float(),
                    torch.as_tensor(noise).float(),
                    tn,
                    last=False,
                )
            else:
                known_next = known_latent(
                    torch.as_tensor(source_latent).float(),
                    torch.as_tensor(noise).float(),
                    tn,
                    last=True,
                )

            mask_full = _broadcast_mask(mask_lat_bool, tuple(lat_t.shape))
            editable_mask = mask_full
            protected_mask = ~mask_full

            known_next_f = known_next.to(lat_t.dtype).float()
            # Mesure AVANT le remasquage de fin de pas (trace informative).
            diff_protected_pre = (lat_t.float() - known_next_f).abs()
            if bool(protected_mask.any().item()):
                protected_preapply_max_delta = float(
                    diff_protected_pre[protected_mask].max().item()
                )
            else:
                protected_preapply_max_delta = 0.0
            protected_preapply_changed = (
                int((diff_protected_pre[protected_mask] > 0).sum().item())
                if bool(protected_mask.any().item())
                else 0
            )

            lat_t = apply_mask(lat_t, known_next.to(lat_t.dtype), mask_lat_bool)

            # Mesure APRES application du masque (invariant post).
            diff_protected_post = (lat_t.float() - known_next_f).abs()
            if bool(protected_mask.any().item()):
                protected_postapply_max_delta = float(
                    diff_protected_post[protected_mask].max().item()
                )
            else:
                protected_postapply_max_delta = 0.0
            changed_protected = (
                int((diff_protected_post[protected_mask] > 0).sum().item())
                if bool(protected_mask.any().item())
                else 0
            )

            protected_max_delta = protected_postapply_max_delta

            if source_latent is not None:
                src_t = torch.as_tensor(source_latent).to(lat_t.dtype)
                editable_changed = int(
                    torch.ne(
                        lat_t[editable_mask], src_t.expand_as(lat_t)[editable_mask]
                    )
                    .sum()
                    .item()
                )
            else:
                editable_changed = 0
        else:
            protected_preapply_max_delta = 0.0
            protected_preapply_changed = 0
            protected_postapply_max_delta = 0.0
            changed_protected = 0
            protected_max_delta = 0.0
            editable_changed = 0

        trace.append(
            {
                "step": i,
                "t": t,
                "tn": tn,
                "editable_changed": editable_changed,
                "protected_preapply_max_delta": protected_preapply_max_delta,
                "protected_preapply_changed": protected_preapply_changed,
                "protected_postapply_max_delta": protected_postapply_max_delta,
                "changed_protected": changed_protected,
                "protected_max_delta": protected_max_delta,
            }
        )

    return lat_t, trace

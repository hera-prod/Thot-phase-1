"""Scheduler Flow Matching klein — schedule officiel des modèles Klein distillés.

Le pipeline Klein construit les sigmas d'entrée avec ``linspace(1, 1/N, N)``,
applique le shift exponentiel dynamique de ``compute_empirical_mu`` puis ajoute
le sigma terminal nul. Le transformer reçoit ensuite ``sigma * 1000``.
"""

import math
from typing import Optional

import torch
from ..core.config import FLUX2_KLEIN_CONFIG

SIGMA_MAX = 1.0


def compute_empirical_mu(image_seq_len: int, num_steps: int) -> float:
    a1, b1 = 8.73809524e-05, 1.89833333
    a2, b2 = 0.00016927, 0.45666666
    if image_seq_len > 4300:
        return float(a2 * image_seq_len + b2)
    m_200 = a2 * image_seq_len + b2
    m_10 = a1 * image_seq_len + b1
    a = (m_200 - m_10) / 190.0
    b = m_200 - 200.0 * a
    return float(a * num_steps + b)


def time_shift_exponential(mu: float, t: torch.Tensor) -> torch.Tensor:
    e = math.exp(mu)
    return e / (e + (1 / t - 1))


class Flux2KleinScheduler:
    """Sigmas Klein : linspace(1, 1/N, N) + shift(mu(seq_len, N)) + terminal 0."""

    def __init__(self, num_steps: int = 4, sigmas=None):
        self.num_steps = num_steps
        if sigmas is not None:
            self.sigmas = torch.tensor(sigmas, dtype=torch.float32)
        else:
            self.sigmas = torch.tensor([1.0, 0.0], dtype=torch.float32)

    def set_timesteps(self, num_steps: int, image_seq_len: int, device="cpu"):
        """Replicate the Klein scheduler (N steps -> N+1 sigmas, terminal 0)."""
        if num_steps <= 0:
            raise ValueError("num_steps doit être strictement positif")
        self.num_steps = num_steps
        base = torch.linspace(
            SIGMA_MAX, 1.0 / num_steps, num_steps, dtype=torch.float32
        )
        mu = compute_empirical_mu(image_seq_len, num_steps)
        shifted = time_shift_exponential(mu, base)
        self.sigmas = torch.cat([shifted, torch.zeros(1)]).to(device)
        return self.sigmas

    def step(
        self, v_pred: torch.Tensor, x: torch.Tensor, t: float, t_next: float
    ) -> torch.Tensor:
        dt = t_next - t
        return (x.float() + v_pred.float() * dt).to(v_pred.dtype)

    def get_velocity(self, x, x0, t):
        return (x - x0) / max(t, 1e-5)

    def scale_noise(
        self,
        sample: torch.Tensor,
        timestep: torch.Tensor,
        noise: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """Forward process de flux matching (diffusers) : x_t = sigma * noise + (1 - sigma) * x0."""
        if noise is None:
            raise ValueError("[BAD_ARGS] scale_noise exige un bruit `noise`.")
        sigma = (
            float(timestep)
            if not hasattr(timestep, "shape") or timestep.dim() == 0
            else float(timestep.flatten()[0])
        )
        out = sigma * noise + (1.0 - sigma) * sample
        return out

"""Chargement du VAE FLUX.2 Klein (safetensors)."""
import torch
from safetensors.torch import load_file


def load_vae(vae_path: str, device="cuda", dtype=torch.bfloat16):
    """Charge le VAE depuis safetensors."""
    state_dict = load_file(vae_path, device="cpu")
    # Convertir en dtype cible
    state_dict = {k: v.to(dtype=dtype, device=device) for k, v in state_dict.items()}
    return state_dict

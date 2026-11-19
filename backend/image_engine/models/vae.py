"""VAE pour FLUX.2 Klein — wrapper AutoencoderKLFlux2 reel."""

import gc
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn


# UNVERIFIED SCALING/SHIFT NOTE (recherche diffusers 0.39.0, 2026-10-28).
# `diffusers.AutoencoderKLFlux2` ne definit ni `scaling_factor` ni `shift_factor`
# (ni en attribut de classe/instance, ni dans sa config). Dans
# `diffusers/modular_pipelines/flux2/encoders.py`, la conversion VAE↔diffusion
# n'utilise que la BN (`running_mean`/`running_var` + `batch_norm_eps`), avec
# patchify 2x2 — aucun scaling/shift supplementaire. `modular_pipelines/flux2/decoders.py`
# applique l'inverse BN puis unpatch. Aucun facteur scaling/shift n'est donc introduit
# ici : inventer des valeurs casserait le décodage. Hypothese scaling_factor/shift_factor
# = UNVERIFIED (non utilise).


class Flux2KleinVAE(nn.Module):
    """
    VAE FLUX.2 Klein (AutoencoderKLFlux2 reel).

    - Encode: RGB [B, 3, H, W] -> latents [B, 32, H/8, W/8]
    - Decode: latents [B, 32, H/8, W/8] -> RGB [B, 3, H, W]
    - Conversion VAE↔diffusion : BN (running stats) + patchify 2x2, comme diffusers.
    - Pas de scaling_factor/shift_factor verified : voir note module.
    """

    def __init__(
        self,
        config,
        vae_path: str | None = None,
        device: str | torch.device = "cpu",
        dtype=torch.bfloat16,
    ):
        super().__init__()
        self.config = config
        self._vae_path = vae_path
        self._device = device
        self._dtype = dtype
        self._vae: Any = None
        # Construction stricte : un chemin fourni et absent -> erreur explicite.
        if vae_path is not None:
            if not Path(vae_path).exists():
                print(f"[VAE] Chemin VAE introuvable : {vae_path}")
                raise RuntimeError(
                    f"[VAE_UNAVAILABLE] Chemin VAE introuvable : {vae_path}"
                )
            self._load_real_vae(vae_path, device, dtype)

    def _load_real_vae(self, vae_path: str, device, dtype):
        # vae_path peut etre un dossier (models/vae) ou un fichier safetensors
        try:
            import diffusers
        except Exception as exc:
            print(f"[VAE] diffusers indisponible : {exc}")
            self._vae = None
            raise RuntimeError(
                "[VAE_UNAVAILABLE] diffusers indisponible pour charger le VAE."
            ) from exc

        try:
            vae_class = getattr(diffusers, "AutoencoderKLFlux2")
        except AttributeError as exc:
            print(f"[VAE] AutoencoderKLFlux2 indisponible : {exc}")
            self._vae = None
            raise RuntimeError(
                "[VAE_UNAVAILABLE] AutoencoderKLFlux2 indisponible."
            ) from exc

        p = Path(vae_path)
        # Si c'est un fichier, prendre le dossier parent
        load_path = str(p.parent if p.is_file() else p)
        try:
            self._vae = vae_class.from_pretrained(
                load_path, torch_dtype=dtype, local_files_only=True
            ).to(device)
            self._vae.eval()
        except Exception as e:
            print(f"[VAE] Echec chargement reel ({e})")
            self._vae = None
            raise RuntimeError(f"[VAE_UNAVAILABLE] Echec chargement VAE : {e}") from e

        if self._vae is None:
            print("[VAE] VAE None apres chargement")
            raise RuntimeError("[VAE_UNAVAILABLE] VAE indisponible apres chargement.")

    def _ensure_vae(self, device, dtype):
        if self._vae is None and self._vae_path:
            self._load_real_vae(self._vae_path, device, dtype)
        return self._vae is not None

    def configure_memory(self, *, tiling: bool = False, slicing: bool = False) -> None:
        if self._vae is None:
            raise RuntimeError("[VAE_UNAVAILABLE] VAE non chargé.")
        for enabled, name in ((tiling, "tiling"), (slicing, "slicing")):
            method = getattr(self._vae, f"enable_{name}", None)
            disable = getattr(self._vae, f"disable_{name}", None)
            if enabled and callable(method):
                method()
            elif not enabled and callable(disable):
                disable()

    def _reclaim_device_memory(self) -> None:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    @staticmethod
    def _patchify(latents: torch.Tensor) -> torch.Tensor:
        batch_size, num_channels, height, width = latents.shape
        latents = latents.view(batch_size, num_channels, height // 2, 2, width // 2, 2)
        latents = latents.permute(0, 1, 3, 5, 2, 4)
        latents = latents.reshape(batch_size, num_channels * 4, height // 2, width // 2)
        return latents

    @staticmethod
    def _unpatchify(latents: torch.Tensor) -> torch.Tensor:
        batch_size, num_channels, height, width = latents.shape
        latents = latents.reshape(batch_size, num_channels // 4, 2, 2, height, width)
        latents = latents.permute(0, 1, 4, 2, 5, 3)
        latents = latents.reshape(batch_size, num_channels // 4, height * 2, width * 2)
        return latents

    def _bn(self):
        if self._vae is None:
            raise RuntimeError("[VAE_UNAVAILABLE] VAE non chargé.")
        return getattr(self._vae, "bn", None)

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        if self._vae is None:
            raise RuntimeError("[VAE_UNAVAILABLE] VAE non chargé.")
        with torch.no_grad():
            x_in = x.to(device=self._vae.device, dtype=self._vae.dtype)
            latent_dist = self._vae.encode(x_in).latent_dist
            if hasattr(latent_dist, "mode"):
                raw = latent_dist.mode()
            else:
                raw = latent_dist.mean
            bn = self._bn()
            if bn is not None:
                # Canonical Flux2 VAE↔diffusion conversion (voir diffusers
                # modular_pipelines/flux2/encoders.py) : patchify 2×2, BN-norm.
                running_mean = getattr(bn, "running_mean")
                running_var = getattr(bn, "running_var")
                eps = getattr(self._vae.config, "batch_norm_eps", 1e-5)
                mean = running_mean.view(1, -1, 1, 1).to(raw.device, raw.dtype)
                std = torch.sqrt(
                    running_var.view(1, -1, 1, 1).to(raw.device, raw.dtype) + eps
                )
                patched = self._patchify(raw)
                patched = (patched - mean) / std
                raw = self._unpatchify(patched)
            return raw.to(device=x.device, dtype=x.dtype)

    def _prepare_decode_latents(self, z: torch.Tensor) -> torch.Tensor:
        bn = self._bn()
        if bn is None:
            return z
        running_mean = getattr(bn, "running_mean")
        running_var = getattr(bn, "running_var")
        eps = getattr(self._vae.config, "batch_norm_eps", 1e-5)
        mean = running_mean.view(1, -1, 1, 1).to(z.device, z.dtype)
        std = torch.sqrt(running_var.view(1, -1, 1, 1).to(z.device, z.dtype) + eps)
        patched = self._patchify(z)
        patched = patched * std + mean
        return self._unpatchify(patched)

    def _decode_once(self, z: torch.Tensor) -> torch.Tensor:
        if self._vae is None:
            raise RuntimeError("[VAE_UNAVAILABLE] VAE non chargé.")
        with torch.no_grad():
            z_in = self._prepare_decode_latents(z).to(
                device=self._vae.device, dtype=self._vae.dtype
            )
            return self._vae.decode(z_in).sample.to(device=z.device, dtype=z.dtype)

    def decode(self, z: torch.Tensor, *, recover_oom: bool = False) -> torch.Tensor:
        try:
            return self._decode_once(z)
        except torch.cuda.OutOfMemoryError:
            if not recover_oom:
                raise
        self._reclaim_device_memory()
        self.configure_memory(tiling=True)
        try:
            return self._decode_once(z)
        except torch.cuda.OutOfMemoryError as exc:
            raise RuntimeError(
                "[VAE_OOM] Le décodage VAE a échoué après activation du tiling."
            ) from exc

    def to(self, *args, **kwargs):
        super().to(*args, **kwargs)
        if self._vae is not None:
            self._vae.to(*args, **kwargs)
        return self

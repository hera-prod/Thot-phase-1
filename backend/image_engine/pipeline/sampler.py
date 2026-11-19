"""Sampler C — 4 steps, sigmas reels, pas de CFG (v3 §4)."""
import torch
from PIL import Image
from .scheduler import Flux2KleinScheduler


class Flux2KleinSampler:
    """x = x + v*dt, normalisation via vae_scale_factor 0.3611 (CONFIRMÉ)."""

    def __init__(self, transformer, vae, scheduler: Flux2KleinScheduler, config):
        self.transformer = transformer
        self.vae = vae
        self.scheduler = scheduler
        self.config = config

    @torch.inference_mode()
    def sample(self, text_emb, width=1024, height=1024, seed=42, ref_emb=None, device="cuda", dtype=torch.bfloat16):
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
        latents = torch.randn((1, self.config.vae_latent_channels, height // 8, width // 8), device=device, dtype=dtype)
        use_cache = ref_emb is not None
        # Sigmas reels
        sigmas = self.scheduler.sigmas
        for i in range(self.config.num_inference_steps):
            t = float(sigmas[i])
            tn = float(sigmas[i+1])
            # Nouveau transformer: forward(latents, t, ctx_12288)
            if hasattr(self.transformer, 'forward'):
                # Try new signature (latents, t, ctx_12288)
                try:
                    v_pred = self.transformer(latents, torch.tensor([t*1000], device=device, dtype=dtype), text_emb, ref_kv=ref_emb, kv_cache_mode="extract" if (use_cache and i==0) else ("cached" if use_cache else None))
                except TypeError:
                    # Fallback old signature
                    v_pred = self.transformer(latents, torch.tensor([t*1000], device=device, dtype=dtype), text_emb, ref_context=ref_emb, use_cache=use_cache, step_idx=i)
            else:
                v_pred = self.transformer(latents, torch.tensor([t*1000], device=device, dtype=dtype), text_emb)
            latents = self.scheduler.step(v_pred, latents, t, tn)
        image = self.vae.decode(latents)
        image = (image + 1.0) / 2.0
        image = image.clamp(0, 1)
        image = image.squeeze(0).permute(1, 2, 0).cpu().float().numpy()
        image = (image * 255).astype("uint8")
        return Image.fromarray(image)

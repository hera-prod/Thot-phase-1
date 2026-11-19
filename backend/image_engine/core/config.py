"""Configuration FLUX.2 Klein 9B-KV — reel d'apres GGUF QuantStack."""
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class Flux2KleinConfig:
    """Constantes extraites de l'architecture FLUX.2 Klein (GGUF)."""

    # === TRANSFORMER (9B, GGUF: 8 double_blocks + 24 single_blocks) ===
    hidden_dim: int = 4096
    num_double_blocks: int = 8
    num_single_blocks: int = 24
    num_heads: int = 32
    head_dim: int = 128
    num_layers: int = 32
    ffn_dim: int = 12288
    in_channels: int = 32            # VAE latent channels (AutoencoderKLFlux2)
    out_channels: int = 32
    patch_size: int = 2
    context_dim: int = 4096          # Qwen3-8B hidden

    # === VAE === (AutoencoderKLFlux2 reel : 32 canaux, 8x downsample)
    vae_in_channels: int = 3
    vae_latent_channels: int = 32
    vae_downsample_factor: int = 8
    vae_scale_factor: float = 0.3611

    # === TEXT ENCODER (Qwen3-8B) ===
    text_encoder_hidden_dim: int = 4096
    text_encoder_num_layers: int = 36
    text_encoder_vocab_size: int = 151936

    # === SAMPLER === (Klein distillé : 4 étapes, Euler, guidance=1.0)
    num_inference_steps: int = 4
    guidance_scale: float = 1.0
    sigmas: list | None = None
    timesteps: list | None = None

    def get_sigmas(self, image_seq_len: int = 4096) -> list[float]:
        if self.sigmas is not None:
            return self.sigmas
        from ..pipeline.scheduler import Flux2KleinScheduler
        scheduler = Flux2KleinScheduler(num_steps=self.num_inference_steps)
        return scheduler.set_timesteps(self.num_inference_steps, image_seq_len).tolist()

    # === RESOLUTIONS ===
    trained_resolutions: List[Tuple[int, int]] | None = None

    def __post_init__(self):
        if self.trained_resolutions is None:
            self.trained_resolutions = [
                (1024, 1024), (2048, 2048),
                (1024, 1536), (1536, 1024),
                (768, 1344), (1344, 768),
            ]


FLUX2_KLEIN_CONFIG = Flux2KleinConfig()

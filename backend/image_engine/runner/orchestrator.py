"""Orchestrateur principal flux2-klein-mini — Approche.md §4 (offload text encoder)."""

import ctypes
import gc
import torch
from pathlib import Path
from typing import Optional
from PIL import Image
import torchvision.transforms as T

libc = ctypes.CDLL("libc.so.6")

from ..core.config import FLUX2_KLEIN_CONFIG
from ..loaders.gguf_loader import load_transformer, load_text_encoder
from ..loaders.vae_loader import load_vae
from ..loaders.tokenizer_loader import load_tokenizer
from ..models.transformer import Flux2KleinTransformer
from ..models.text_encoder import Flux2KleinTextEncoder
from ..models.vae import Flux2KleinVAE
from ..pipeline.scheduler import Flux2KleinScheduler
from ..runtime.device_probe import probe_device
from ..runtime.placement import choose_mode, device_of, cap_ref_res
from .memprof import RunRecord
import os

# Validation refs (v2 §3.4) — consensus communautaire 4 images max
MAX_REFS_SOFT = 4
MAX_REFS_HARD = 6
MAX_REF_TOKENS = 4500


def validate_refs(ref_paths: list, ref_res: int):
    if len(ref_paths) > MAX_REFS_HARD:
        raise ValueError(
            f"Limite absolue de {MAX_REFS_HARD} images de référence dépassée."
        )
    if len(ref_paths) > MAX_REFS_SOFT:
        print(
            f"⚠️ Warning : >{MAX_REFS_SOFT} références peuvent diluer le guidage et dégrader la qualité."
        )
    tokens_per_ref = (ref_res // 16) ** 2
    if len(ref_paths) * tokens_per_ref > MAX_REF_TOKENS:
        raise ValueError(
            f"Budget de tokens de référence dépassé ({len(ref_paths) * tokens_per_ref} > {MAX_REF_TOKENS}). Baissez la résolution des refs à 512."
        )


class Flux2KleinMini:
    """
    Pipeline autonome FLUX.2 Klein 9B-KV.

    Supporte :
    - T2I pur (texte -> image)
    - Edition (texte + image reference -> image editee) avec KV cache
    """

    def __init__(
        self,
        transformer_path: str,
        text_encoder_path: str,
        vae_path: str,
        tokenizer_path: str,
        device: str = "cuda",
        mode: str | None = None,
        resolution: int = 1024,
    ):
        # Sondage materiel + placement (rapport_gestion_memoire §1-3)
        dev = probe_device()
        chosen = choose_mode(
            dev, resolution, override=mode or os.environ.get("FLUX2KLEIN_MODE")
        )
        # device_of: cuda/mps/cpu selon mode
        target_device = device_of(chosen)
        # Si l'utilisateur force device="cuda" mais mode=hybrid/cpu, on respecte le mode
        if target_device == "cpu":
            device = "cpu"
        elif target_device == "mps":
            device = "mps"
        self.device = torch.device(
            device if (device != "cuda" or torch.cuda.is_available()) else "cpu"
        )
        self.config = FLUX2_KLEIN_CONFIG
        self.dev_info = dev
        self.mode = chosen
        # RunRecord initial
        rec = RunRecord.capture(dev, chosen)
        print(
            f"[Device] backend={dev.backend} name={dev.name} freeVRAM {dev.free_vram_gb:.1f}/{dev.total_vram_gb:.1f} Go freeRAM {dev.ram_free_gb:.1f}/{dev.ram_total_gb:.1f} Go unified={dev.unified_memory}"
        )
        print(
            f"[Placement] mode={chosen} (resolution {resolution}) -> device {self.device} — {rec}"
        )
        # PORTES APPLIQUÉES, au même endroit que le sondeur et AVANT tout
        # chargement : c'est le point où l'on sait encore refuser pour un centime.
        # Ce `rec` n'était qu'imprimé — donc jamais opposable, donc décoratif.
        rec.exiger_conforme(etape="démarrage de l'orchestrateur")
        tr_dev = device_of(chosen)
        qwen_dev = (
            tr_dev
            if chosen in ("full_gpu_simult", "full_gpu_phased", "unified")
            else "cpu"
        )
        print(
            f"[Placement] tr_dev={tr_dev} qwen_dev={qwen_dev} (full_gpu_phased: qwen GPU puis release avant TR)"
        )

        print(f"[Init] Chargement sur {self.device} (mode {self.mode})...")

        # 1. Charger transformer (GGUF) — lazy per bloc, pas d'OOM (Approche.md)
        print("[Init] Chargement transformer (5,72 Go, lazy mmap)...")
        # Note: load_transformer dense dequante tout (18 Go BF16 CUDA) → OOM sur 12 Go.
        # Le builder lazy (RawVRAM + LazyLinear, pic ~7 Go) est le SEUL chemin CUDA viable ;
        # le dense ne reste que pour CPU/offload. Index conservé vivant ( lectures forward).
        if self.device.type == "cuda":
            from ..models.transformer_lazy import build_lazy_transformer

            self.transformer, self._tr_index = build_lazy_transformer(
                transformer_path, self.config, device="cuda"
            )
        else:
            # Note: load_transformer dequante tout en une fois (18 Go RssAnon si non corrige).
            # Avec le correctif Approche.md, chaque dequant est H2D + del CPU, pas de retention.
            transformer_sd = load_transformer(transformer_path, str(self.device))
            self.transformer = Flux2KleinTransformer(self.config).to(self.device)
            self.transformer.load_state_dict(transformer_sd, strict=False)
            self.transformer.eval()
            del transformer_sd
            self._tr_index = None
        gc.collect()
        try:
            libc.malloc_trim(0)
        except Exception:
            pass
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # 2. Charger text encoder (GGUF) — sera offloade apres encodage (Approche.md §4)
        print("[Init] Chargement text encoder (4,68 Go)...")
        if self.device.type == "cuda":
            # PAS de dict dense (8B BF16 = 16 Go → OOM) : index lazy mmap + encodeur natif
            # qui streame par couche (pic ~0 Go mesuré dans le spike). Même recette que T2I.
            self.text_encoder = Flux2KleinTextEncoder(
                self.config, tokenizer_path, gguf_path=text_encoder_path
            )
            self.text_encoder.eval()
        else:
            text_encoder_sd = load_text_encoder(text_encoder_path, str(self.device))
            self.text_encoder = Flux2KleinTextEncoder(self.config, tokenizer_path).to(
                self.device
            )
            self.text_encoder.load_state_dict(text_encoder_sd, strict=False)
            self.text_encoder.eval()
            del text_encoder_sd
        gc.collect()
        try:
            libc.malloc_trim(0)
        except Exception:
            pass

        # 3. Charger VAE (AutoencoderKLFlux2 reel)
        print("[Init] Chargement VAE (168 Mo, AutoencoderKLFlux2)...")
        # Branchement reel: vae_path est un dossier (models/vae) ou fichier safetensors
        from pathlib import Path as _P

        vae_dir = str(_P(vae_path).parent if _P(vae_path).is_file() else vae_path)
        dtype = torch.bfloat16 if self.device.type == "cuda" else torch.float32
        self.vae = Flux2KleinVAE(
            self.config, vae_path=vae_dir, device=str(self.device), dtype=dtype
        )
        self.vae.eval()

        # 4. Scheduler
        self.scheduler = Flux2KleinScheduler(num_steps=self.config.num_inference_steps)

        print("[Init] Pret !")

    def _check_resolution(self, width: int, height: int):
        if width % 64 != 0 or height % 64 != 0:
            raise ValueError(
                f"Résolution non multiple de 64 : {width}x{height} → VAE 8× + patch 2×2 requiert multiple de 64"
            )
        if max(width, height) > 2048:
            print(
                f"⚠️ Haute résolution {width}x{height} : qualité variable (distillation 4 steps optimale à 1024²)"
            )

    @torch.inference_mode()
    def generate(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        seed: int = 42,
        ref_image_path: Optional[str] = None,
    ) -> Image.Image:
        """
        Genere une image.

        Args:
            prompt: description texte
            width, height: resolution (multiples de 64)
            seed: graine aleatoire
            ref_image_path: image de reference pour edition (optionnel)

        Retour : PIL Image
        """
        self._check_resolution(width, height)
        # 1. Encoder texte — offload immediat apres (Approche.md §4, ~4,7 Go liberes)
        print(f"[Generate] Encodage texte : '{prompt[:50]}...'")
        if self.text_encoder is None:
            raise RuntimeError("Text encoder is not available for prompt encoding.")
        text_emb = self.text_encoder.encode(prompt, device=str(self.device))
        # Text encoder plus necessaire apres encodage (taps 9/18/27 deja concats)
        # On le garde pour ref_emb si edition, sinon on l'offload
        if ref_image_path is None:
            del self.text_encoder
            self.text_encoder = None
            gc.collect()
            try:
                libc.malloc_trim(0)
            except Exception:
                pass
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        # 2. Preparer latents initiaux
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
        latents = torch.randn(
            (1, self.config.vae_latent_channels, height // 8, width // 8),
            device=self.device,
            dtype=torch.bfloat16,
        )

        # 3. Preparer reference (si fournie) — text encoder encore vivant pour ref
        ref_emb = None
        if ref_image_path is not None:
            print(f"[Generate] Encodage reference : {ref_image_path}")
            ref_image = self._load_image(ref_image_path, height, width)
            ref_latents = self.vae.encode(ref_image)
            _ = ref_latents
            # text_encoder encore present (on ne l'a pas supprime si ref)
            if self.text_encoder is not None:
                ref_emb = self.text_encoder.encode(
                    f"Reference image: {ref_image_path}", device=str(self.device)
                )
            # Offload maintenant (plus besoin)
            if hasattr(self, "text_encoder") and self.text_encoder is not None:
                del self.text_encoder
                self.text_encoder = None
                gc.collect()
                try:
                    libc.malloc_trim(0)
                except Exception:
                    pass
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

        # 4. Boucle de sampling (4 steps)
        print(f"[Generate] Sampling ({self.config.num_inference_steps} steps)...")
        for i in range(self.config.num_inference_steps):
            t = float(self.scheduler.sigmas[i])
            t_next = float(self.scheduler.sigmas[i + 1])

            v_pred = self.transformer(
                latents,
                timestep=torch.tensor(
                    [t * 1000], device=self.device, dtype=torch.bfloat16
                ),
                context=text_emb,
                ref_context=ref_emb,
                use_cache=(ref_emb is not None),
                step_idx=i,
            )

            latents = self.scheduler.step(v_pred, latents, t, t_next)

        # 5. Decoder latents -> RGB
        print("[Generate] Decodage VAE...")
        image = self.vae.decode(latents)
        image = (image + 1.0) / 2.0  # [-1, 1] -> [0, 1]
        image = image.clamp(0, 1)

        # 6. Convertir en PIL Image
        image = image.squeeze(0).permute(1, 2, 0).cpu().float().numpy()
        image = (image * 255).astype("uint8")
        return Image.fromarray(image)

    @torch.inference_mode()
    def generate_edit(
        self,
        prompt: str,
        ref_paths: list[str],
        width: int = 1024,
        height: int = 1024,
        seed: int = 42,
    ) -> Image.Image:
        """Edition via KV cache (v2 §3) — max 4 refs, plafond tokens 4500."""
        self._check_resolution(width, height)
        # Validation refs
        ref_res = cap_ref_res(self.mode, max(width, height), self.dev_info.free_vram_gb)
        validate_refs(ref_paths, ref_res)
        print(
            f"[Edit] {len(ref_paths)} refs @ {ref_res}² → {(ref_res // 16) ** 2 * len(ref_paths)} tokens (≤4500)"
        )
        # Load refs
        refs = [self._load_image(p, ref_res, ref_res) for p in ref_paths]
        # KV cache
        from ..pipeline.kv_cache import Flux2KVCache

        kv = Flux2KVCache(8, 24)
        # Sampling avec KV cache (step0 extract, 1-3 cached)
        lat = self._sample_edit(prompt, refs, width, height, seed, kv)
        kv.release() if hasattr(kv, "release") else kv.clear()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        # Decode
        image = self.vae.decode(lat)
        image = (image + 1.0) / 2.0
        image = image.clamp(0, 1)
        image = image.squeeze(0).permute(1, 2, 0).cpu().float().numpy()
        image = (image * 255).astype("uint8")
        return Image.fromarray(image)

    def _sample_edit(self, prompt, refs, W, H, seed, kv):
        """Sample avec KV cache — le vrai forward_kv_extract / sampling cached reste à brancher."""
        raise NotImplementedError(
            "Le sampler d'édition n'est pas implémenté. "
            "Brancher le vrai forward_kv_extract + sampling cached dans "
            "backend/image_engine/runner/orchestrator.py:_sample_edit."
        )

    def _load_image(self, path: str, height: int, width: int) -> torch.Tensor:
        """Charge et pretraite une image de reference."""
        image = Image.open(path).convert("RGB")
        transform = T.Compose(
            [
                T.Resize((height, width)),
                T.ToTensor(),
                T.Normalize([0.5], [0.5]),  # [0, 1] -> [-1, 1]
            ]
        )
        transformed = transform(image)
        if not isinstance(transformed, torch.Tensor):
            raise TypeError("Reference image transform must return a torch.Tensor.")
        return transformed.unsqueeze(0).to(self.device, dtype=torch.bfloat16)

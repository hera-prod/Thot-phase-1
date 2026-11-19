"""Qwen3-8B Tap Encoder — taps 9/18/27 (rapport_correction v3 §3)."""

import logging
from pathlib import Path

import torch
from torch import nn

try:
    from transformers import AutoTokenizer
except ImportError:
    AutoTokenizer = None

logger = logging.getLogger(__name__)


class Qwen3TapEncoder(nn.Module):
    """
    Qwen3-8B avec taps 9/18/27, concat -> [B,S,12288].
    CONFIRMÉ: txt_in 12288=3×4096 prouve le concat.
    Chargement via LazyGGUFTensor sur Qwen3-8B-Q4_K_M.gguf.
    """

    TAPS = (8, 17, 26)  # cf. Qwen3NativeEncoder (hidden_states HF [9/18/27])

    def __init__(
        self,
        config,
        tokenizer_path: str,
        gguf_path: str | None = None,
        device="cpu",
        dtype=torch.bfloat16,
    ):
        super().__init__()
        self.config = config
        self.tokenizer = None
        if AutoTokenizer and Path(tokenizer_path).exists():
            self.tokenizer = AutoTokenizer.from_pretrained(
                tokenizer_path, trust_remote_code=True
            )
            if self.tokenizer.pad_token is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token

        # Lazy GGUF pour Qwen3 — 36 layers, hidden 4096
        self.gguf_path = gguf_path
        self.device = device
        self.dtype = dtype
        self.lazy = None
        self.embed_tokens = None
        self.layers = nn.ModuleList()
        self.norm = None

        # Autorise la construction sans poids pour les smoke tests, pas l'inférence.
        self._is_placeholder = gguf_path is None or not Path(gguf_path).exists()
        if self._is_placeholder:
            logger.warning(
                "Qwen3TapEncoder sans GGUF valide : forward() lèvera RuntimeError."
            )
        else:
            assert gguf_path is not None
            self._init_lazy(gguf_path, device, dtype)

    def _init_lazy(self, gguf_path: str, device, dtype):
        from ..loaders.gguf_loader import Flux2KleinGGUFLoader

        loader = Flux2KleinGGUFLoader(gguf_path, device="cpu")  # mmap
        # Index Qwen3 tensors: embed, layers, norm
        self.lazy = loader
        self._native = None  # Qwen3NativeEncoder construit au premier forward
        logger.info(
            "Qwen3TapEncoder : GGUF lazy indexé (%d tenseurs).", len(loader.tensors)
        )

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        """
        input_ids: [B,S]
        Retour: [B,S,12288] (concat taps 9/18/27)
        Forward Qwen3 natif (models/qwen3_forward.py), parite 0.9986 vs llama_cpp.
        Convention : EOS (151643) ajoute par encode(), comme llama (add_eos_token=True).
        """
        if self._is_placeholder or self.lazy is None:
            raise RuntimeError(
                "Qwen3TapEncoder en mode placeholder (pas de GGUF valide) : "
                "forward() ne peut pas fabriquer d'embeddings. Fournir un chemin GGUF valide."
            )

        if self._native is None:
            from .qwen3_forward import Qwen3NativeEncoder

            dev = "cuda" if torch.cuda.is_available() else "cpu"
            self._native = Qwen3NativeEncoder(self.lazy, device=dev, dtype=self.dtype)
        with torch.no_grad():
            taps, _ = self._native.forward_taps(input_ids)
        return taps

    EOS_ID = 151643  # tokenizer.ggml.eos_token_id ; llama l'ajoute (add_eos_token=True)

    def encode(
        self, text: str | list[str], device="cpu", dtype=torch.bfloat16
    ) -> torch.Tensor:
        """Encode texte -> [B,S,12288] (taps concat, EOS final inclus)."""
        if self.tokenizer is None:
            raise RuntimeError(
                "Qwen3TapEncoder sans tokenizer : encode() requiert transformers "
                "et un tokenizer_path valide."
            )
        if isinstance(text, str):
            text = [text]

        toks = self.tokenizer(
            text, return_tensors="pt", padding=True, truncation=True, max_length=511
        )
        input_ids = toks["input_ids"].to(device)
        # Ajoute EOS comme llama (parite 0.9986 validee avec EOS)
        eos = torch.full(
            (input_ids.shape[0], 1), self.EOS_ID, dtype=input_ids.dtype, device=device
        )
        return self.forward(torch.cat([input_ids, eos], dim=1))

    def offload(self):
        """Offload complet apres encodage (~4,7 Go liberes)."""
        if self.lazy:
            self.lazy.seal()
            self.lazy = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


# Alias pour compat avec ancien code
Flux2KleinTextEncoder = Qwen3TapEncoder

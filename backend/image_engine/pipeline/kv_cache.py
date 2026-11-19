"""KV cache FLUX.2 Klein — FP8 (float8_e4m3fn) pour <8 Go avec 4 refs (rapport_kv-cache §2)."""
import torch
from typing import Tuple


class Flux2KVLayerCache:
    """Cache K/V par couche, stocke en FP8 pour diviser par 2 la memoire."""

    def __init__(self):
        self.k_cache: torch.Tensor | None = None  # float8_e4m3fn
        self.v_cache: torch.Tensor | None = None  # float8_e4m3fn
        # Compat ancien noms
        self.k_ref: torch.Tensor | None = None
        self.v_ref: torch.Tensor | None = None

    def store(self, k_ref: torch.Tensor, v_ref: torch.Tensor):
        """Stocke les K/V des references en FP8."""
        try:
            self.k_cache = k_ref.to(torch.float8_e4m3fn)
            self.v_cache = v_ref.to(torch.float8_e4m3fn)
        except Exception:
            # Fallback si FP8 non supporte (CPU ou vieux GPU)
            self.k_cache = k_ref.detach()
            self.v_cache = v_ref.detach()
        self.k_ref = self.k_cache
        self.v_ref = self.v_cache

    def get(self) -> Tuple[torch.Tensor, torch.Tensor]:
        """Retourne les K/V en FP8 (prets pour SDPA)."""
        k_cache = self.k_cache
        v_cache = self.v_cache
        if k_cache is None or v_cache is None:
            raise RuntimeError("KV cache has not been populated yet.")
        return k_cache, v_cache

    def clear(self):
        """Libere le cache."""
        self.k_cache = None
        self.v_cache = None
        self.k_ref = None
        self.v_ref = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def memory_gb(self) -> float:
        k_cache = self.k_cache
        v_cache = self.v_cache
        if k_cache is None or v_cache is None:
            return 0.0
        # float8 = 1 octet, bf16 = 2
        bytes_k = k_cache.numel() * k_cache.element_size()
        bytes_v = v_cache.numel() * v_cache.element_size()
        return (bytes_k + bytes_v) / 1e9

    def memory_mb(self) -> float:
        return self.memory_gb() * 1024


class Flux2KVCache:
    """Cache complet : 8 double + 24 single."""

    def __init__(self, n_double=8, n_single=24):
        self.double_block_caches = [Flux2KVLayerCache() for _ in range(n_double)]
        self.single_block_caches = [Flux2KVLayerCache() for _ in range(n_single)]
        self.num_ref_tokens: int = 0

    def store(self, layer_idx: int, is_double: bool, k_ref: torch.Tensor, v_ref: torch.Tensor):
        if is_double:
            self.double_block_caches[layer_idx].store(k_ref, v_ref)
        else:
            self.single_block_caches[layer_idx].store(k_ref, v_ref)

    def get(self, layer_idx: int, is_double: bool) -> Tuple[torch.Tensor, torch.Tensor]:
        if is_double:
            return self.double_block_caches[layer_idx].get()
        else:
            return self.single_block_caches[layer_idx].get()

    # Compat
    def get_double(self, layer_idx: int) -> Flux2KVLayerCache:
        return self.double_block_caches[layer_idx]

    def get_single(self, layer_idx: int) -> Flux2KVLayerCache:
        return self.single_block_caches[layer_idx]

    def clear(self):
        for c in self.double_block_caches + self.single_block_caches:
            c.clear()
        self.num_ref_tokens = 0

    def release(self):
        self.clear()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def total_memory_gb(self) -> float:
        return sum(c.memory_gb() for c in self.double_block_caches + self.single_block_caches)

    def memory_mb(self) -> float:
        return self.total_memory_gb() * 1024


# Compat
KVCache = Flux2KVCache

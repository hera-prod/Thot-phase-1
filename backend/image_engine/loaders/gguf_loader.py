"""GGUF loader F — lazy par bloc, mmap + bf16 a la volee (v3 §5 + Approche.md §4)."""
import ctypes
import gc
import numpy as np
import torch
from gguf import GGUFReader
from typing import Dict

libc = ctypes.CDLL("libc.so.6")


class LazyGGUFTensor:
    """Lazy: mmap → dequant SANS retention CPU, direct device (Approche.md)."""

    def __init__(self, name: str, data, shape: tuple, qtype):
        self.name = name
        self.data = data  # mmap view, pas copie
        self.shape_ggml = shape
        self.shape_pt = tuple(reversed(shape))
        self.qtype = qtype
        # PAS de _cache CPU — la retention CPU est le bug (18 Go RssAnon)

    def dequantize(self, dtype=torch.bfloat16, device="cuda") -> torch.Tensor:
        """Dequant SANS retention CPU: H2D immediat + del arr (Approche.md §4)."""
        from gguf.quants import dequantize as dequantize_gguf
        qname = getattr(self.qtype, "name", str(self.qtype))
        if qname == "F32":
            arr = np.frombuffer(self.data, dtype=np.float32).astype(np.float32)  # copie
        elif qname == "F16":
            arr = np.frombuffer(self.data, dtype=np.float16).astype(np.float32)
        else:
            arr = dequantize_gguf(self.data, self.qtype).astype(np.float32)  # fp32 transitoire CPU
        t = torch.from_numpy(arr.reshape(self.shape_pt))
        t = t.to(dtype=dtype, device=device)  # H2D immediat
        del arr  # libere le CPU (3-5 Go d'arenes glibc)
        if t.dim() == 2:
            t = t.t().contiguous()
        return t  # PAS de _cache CPU

    def dequantize_cached(self, dtype=torch.bfloat16, device="cuda") -> torch.Tensor:
        # Compat: plus de cache CPU, on dequantifie a chaque fois (ou cache VRAM si besoin)
        return self.dequantize(dtype, device)


class Flux2KleinGGUFLoader:
    """Loader GGUF — mmap index (0.02s, 201 tenseurs), dequant par bloc (0.208s/tensor)."""

    def __init__(self, gguf_path: str, device: str = "cpu"):
        self.path = gguf_path
        self.device = device
        self.reader = GGUFReader(gguf_path)
        self.tensors: Dict[str, LazyGGUFTensor] = {}
        self._index()

    def _index(self):
        reader = self.reader
        if reader is None:
            raise RuntimeError("GGUF reader is unavailable while indexing tensors.")
        for tensor in reader.tensors:
            self.tensors[tensor.name] = LazyGGUFTensor(
                name=tensor.name,
                data=tensor.data,
                shape=tuple(tensor.shape),
                qtype=tensor.tensor_type,
            )

    def get_lazy(self, name: str) -> LazyGGUFTensor | None:
        return self.tensors.get(name)

    def get_block_lazy(self, prefix: str, dtype=torch.bfloat16, device="cuda") -> Dict[str, torch.Tensor]:
        """Dequant uniquement les tenseurs d'un bloc (pic ~0,5 Go, pas d'OOM)."""
        out = {}
        for n, lazy in self.tensors.items():
            if n.startswith(prefix):
                out[n] = lazy.dequantize(dtype=dtype, device=device)
        return out

    def get_state_dict(self, component: str, dtype=torch.bfloat16) -> Dict[str, torch.Tensor]:
        # Compat: si prefixe existe, filtre, sinon tout (top-level double_blocks)
        has_prefix = any(n.startswith(f"{component}.") for n in self.tensors)
        prefix = f"{component}." if has_prefix else ""
        sd = {}
        for name, lazy in self.tensors.items():
            if prefix and not name.startswith(prefix):
                continue
            clean = name[len(prefix):] if prefix else name
            sd[clean] = lazy.dequantize(dtype=dtype, device=self.device)
        if not sd and prefix:
            for name, lazy in self.tensors.items():
                sd[name] = lazy.dequantize(dtype=dtype, device=self.device)
        return sd

    def seal(self):
        """Release mmap + arenes (Approche.md §4, sem GGUFModelPatcher.load(): mmap_released)."""
        # Le mmap est retenu par les vues self.tensors[*].data
        # Pour le liberer, il faut aussi effacer ces vues apres que tous les blocs ont ete dequantifies
        if hasattr(self, "reader") and self.reader is not None:
            try:
                # Tente de fermer le mmap du reader si possible
                reader_file = getattr(self.reader, "_file", None)
                close_reader = getattr(reader_file, "close", None)
                if callable(close_reader):
                    close_reader()
            except Exception:
                pass
            del self.reader
            self.reader = None
        # Note: self.tensors garde encore les vues mmap (RssFile). Elles ne seront liberees
        # que quand le loader lui-meme est detruit ou quand clear() est appele apres le dernier bloc.
        gc.collect()
        try:
            libc.malloc_trim(0)
        except Exception:
            pass
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def clear(self):
        """Efface toutes les vues mmap (RssFile -> 0) apres que tous les blocs ont ete dequantifies en VRAM."""
        self.tensors.clear()
        gc.collect()
        try:
            libc.malloc_trim(0)
        except Exception:
            pass


def load_transformer(gguf_path: str, device="cuda", dtype=torch.bfloat16):
    loader = Flux2KleinGGUFLoader(gguf_path, device)
    sd = loader.get_state_dict("transformer", dtype)
    loader.seal()
    return sd


def load_text_encoder(gguf_path: str, device="cuda", dtype=torch.bfloat16):
    loader = Flux2KleinGGUFLoader(gguf_path, device)
    sd = loader.get_state_dict("text_encoder", dtype)
    loader.seal()
    return sd

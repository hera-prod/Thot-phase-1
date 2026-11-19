"""Builder lazy du transformer (RawVRAM + LazyLinear, zero dense) — source unique.

POURQUOI ICI (et plus seulement dans scripts/spike_t2i_lazy.py) : c'est le SEUL chemin
transformer qui tient en VRAM 12 Go (pic ~7 Go mesuré). Le dense (load_transformer,
18 Go BF16 CUDA) OOM toujours. L'orchestrateur (I2I validé août 2026) et le spike T2I
partagent désormais ce builder — un seul binding à maintenir.

Contenu porté mot-pour-mot du spike (22/09/2026, runs verts) :
- 121/121 Linear liés (dont final_adaLN/proj_out vers final_layer.*),
- adaLN GGUF : moitiés scale/shift permutées → reorder (résidu 0.0 vs officiel),
- normes affines GGUF *.scale → params bf16,
- échec bruyant si un seul paramètre reste non lié (jamais de poids random silencieux).
"""
from __future__ import annotations

import gc
from typing import Callable

import torch
import torch.nn as nn


class _FixedLinear(nn.Module):
    """Petit tenseur non quantifié (BF16/F32) pré-déquantifié une fois."""

    def __init__(self, w: torch.Tensor):
        super().__init__()
        self.w = w

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.linear(x, self.w)


def build_lazy_transformer(gguf_path: str, config, device: str | torch.device = "cuda",
                           official_adaln: bool = False,
                           log: Callable[[str], None] | None = None):
    """Construit le transformer sur meta-device et lie les poids GGUF lazily.

    Retourne (tr, index) — index (RawVRAM) DOIT rester vivant pendant l'inférence
    (les LazyLinear lisent dedans à chaque forward). Échec bruyant si incomplet.
    """
    from ..core.config import FLUX2_KLEIN_CONFIG  # noqa: F401 (re-export compat)
    from ..loaders.gguf_loader import Flux2KleinGGUFLoader
    from ..loaders.raw_vram import RawVRAMIndex
    from ..models.qwen3_forward import _weight_pt
    from ..models.transformer import Flux2KleinTransformer, LazyLinear

    def say(msg: str):
        (log or (lambda m: print(m, flush=True)))(msg)

    def gb(tag: str):
        if torch.cuda.is_available():
            say(f"[MEM] {tag}: {torch.cuda.memory_allocated() / 1024**3:.2f} Go")

    gb("entree build")
    index = RawVRAMIndex(gguf_path, device=device, storage_device="cpu")
    gb("apres RawVRAMIndex")
    with torch.device("meta"):
        tr = Flux2KleinTransformer(config)  # 9B params, zero octet (jamais materialiser en dense)
    gb("apres construct meta")

    def bind(root, dotted, lazy_mod):
        *pparts, leaf = dotted.split(".")
        p = root
        for q in pparts:
            p = p[int(q)] if q.isdigit() else getattr(p, q)
        if isinstance(p, nn.ModuleDict):
            p[leaf] = lazy_mod
        elif leaf.isdigit():
            p[int(leaf)] = lazy_mod
        else:
            setattr(p, leaf, lazy_mod)

    bound, unbound = [], []
    cpu_loader = Flux2KleinGGUFLoader(gguf_path)  # mmap CPU (libere plus bas)

    for name, mod in list(tr.named_modules()):
        if isinstance(mod, nn.Linear):
            base = f"{name}"
            cands = [base + ".weight",
                     base.replace(".linear", ".lin") + ".weight",
                     "final_layer.linear.weight" if base == "proj_out" else base + ".weight",
                     "final_layer.adaLN_modulation.1.weight" if base == "final_adaLN" else base + ".weight"]
            hit = next((c for c in cands if c in index.raw), None)
            if hit is None:
                unbound.append(name)
                continue
            qtype = index.meta[hit][1]
            if qtype in ("BF16", "F32", "F16"):
                w = _weight_pt(cpu_loader.get_lazy(hit)).to(device, torch.bfloat16)
                if hit == "final_layer.adaLN_modulation.1.weight":
                    # Moitiés 4096 (scale/shift) permutées dans le GGUF (résidu 0.0 après reorder).
                    _half = w.shape[0] // 2
                    w = w[torch.cat([torch.arange(_half, w.shape[0]), torch.arange(0, _half)]).to(w.device)]
                bind(tr, name, _FixedLinear(w))
            else:
                bind(tr, name, LazyLinear(index, hit))
            bound.append(hit)
    if official_adaln:
        import json
        from pathlib import Path
        from safetensors import safe_open
        snap = (Path.home() / ".cache/huggingface/hub/models--black-forest-labs--FLUX.2-klein-9b-kv"
                / "snapshots/a6dfb36eca3a3906eb2fd460795adfb844e5fcce")
        idx = json.load(open(snap / "transformer/diffusion_pytorch_model.safetensors.index.json"))
        fn = idx["weight_map"]["norm_out.linear.weight"]
        with safe_open(snap / "transformer" / fn, framework="pt") as f:
            w = f.get_tensor("norm_out.linear.weight").to(device, torch.bfloat16)
        setattr(tr, "final_adaLN", _FixedLinear(w))
        say("[LazyTR] final_adaLN = OFFICIEL (diagnostic)")
    gc.collect()
    gb("apres swap Linear")
    import numpy as np
    from gguf.quants import dequantize as dq
    for modname, mod in list(tr.named_modules()):
        if isinstance(mod, (nn.LayerNorm, nn.RMSNorm)) and mod.elementwise_affine \
                and next(mod.parameters(), None) is not None \
                and next(mod.parameters()).device.type == "meta":
            wname = f"{modname}.weight"
            gg = wname.replace(".weight", ".scale").replace(".qk_norm.", ".norm.")
            lazy = cpu_loader.get_lazy(gg) or cpu_loader.get_lazy(wname)
            if lazy is None:
                unbound.append(modname + " (norm?)")
                continue
            arr = np.frombuffer(lazy.data, dtype=np.float32).astype(np.float32) \
                if getattr(lazy.qtype, "name", "") == "F32" else dq(lazy.data, lazy.qtype).astype(np.float32)
            mod.weight = nn.Parameter(
                torch.from_numpy(arr.reshape(lazy.shape_pt)).to(device, torch.bfloat16))
            if isinstance(mod, nn.LayerNorm):
                mod.bias = nn.Parameter(torch.zeros_like(mod.weight))
            bound.append(gg)
    leftovers = [n for n, m in tr.named_modules() if isinstance(m, nn.Linear)]
    meta_left = [n for n, p in tr.named_parameters() if p.device.type == "meta"]
    cpu_loader.seal()
    cpu_loader.clear()
    gc.collect()
    if unbound or leftovers or meta_left:
        raise RuntimeError(f"NON LIE: unbound={unbound} linear={leftovers} meta={meta_left}")
    n_lazy = sum(1 for _ in tr.modules() if isinstance(_, LazyLinear))
    say(f"[LazyTR] {n_lazy} LazyLinear lies, {len(bound)} tenseurs, 0 random")
    tr.eval()
    return tr, index

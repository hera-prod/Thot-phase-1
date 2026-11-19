"""Comparaison single-bloc 0 : notre forward vs officiel."""
import sys
import json
import torch
import torch.nn as nn
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # racine projet (paquet `backend`)
from backend.image_engine.core.paths import MODELS_DIR  # models/flux2-klein
from backend.image_engine.loaders.diffusers_shim import ensure_diffusers_loaders
ensure_diffusers_loaders()

SNAP = Path.home() / ".cache/huggingface/hub/models--black-forest-labs--FLUX.2-klein-9b-kv/snapshots/a6dfb36eca3a3906eb2fd460795adfb844e5fcce"
IDX = json.load(open(SNAP / "transformer/diffusion_pytorch_model.safetensors.index.json"))["weight_map"]


def off_many(names):
    from safetensors import safe_open
    from collections import defaultdict
    by_file = defaultdict(list)
    for n in names:
        by_file[IDX[n]].append(n)
    out = {}
    for fn, ns in by_file.items():
        with safe_open(SNAP / "transformer" / fn, framework="pt") as f:
            for n in ns:
                out[n] = f.get_tensor(n)
    return out


def cos(a, b):
    a = a.float().flatten().double()
    b = b.float().flatten().double()
    return (a @ b / (a.norm() * b.norm())).item()


def main():
    from diffusers.models.transformers.transformer_flux2 import Flux2SingleTransformerBlock
    from diffusers.models.transformers.transformer_flux2 import Flux2PosEmbed
    dev = "cuda"
    dt = torch.bfloat16
    torch.manual_seed(1)
    blk = Flux2SingleTransformerBlock(dim=4096, num_attention_heads=32, attention_head_dim=128,
                                      mlp_ratio=3.0, eps=1e-6, bias=False).to(dev, dt).eval()
    names = [k for k in IDX if k.startswith("single_transformer_blocks.0.")]
    sd = off_many(names)
    blk.load_state_dict({k.split("single_transformer_blocks.0.", 1)[1]: v for k, v in sd.items()})
    print("params officiels:", sorted(blk.state_dict().keys()), flush=True)
    Lt, Li = 8, 16
    S = Lt + Li
    h = torch.randn(1, S, 4096, device=dev, dtype=dt)
    temb = torch.randn(1, 4096, device=dev, dtype=dt)
    import torch.nn.functional as F
    mod = torch.randn(1, 3 * 4096, device=dev, dtype=dt)  # shift/scale/gate bruts
    pe = Flux2PosEmbed(theta=2000, axes_dim=[32, 32, 32, 32])
    txt_ids = torch.cartesian_prod(torch.zeros(1, dtype=torch.long), torch.zeros(1, dtype=torch.long),
                                   torch.zeros(1, dtype=torch.long), torch.arange(Lt)).unsqueeze(0)
    img_ids = torch.cartesian_prod(torch.zeros(1, dtype=torch.long), torch.arange(4),
                                   torch.arange(4), torch.zeros(1, dtype=torch.long)).unsqueeze(0)
    ids = torch.cat([txt_ids, img_ids], dim=1)
    fc, fs = pe(ids[0])
    with torch.no_grad():
        out_off = blk(hidden_states=h, encoder_hidden_states=None, temb_mod=mod,
                      image_rotary_emb=(fc.to(dev, dt), fs.to(dev, dt)))
    # notre bloc
    from backend.image_engine.loaders.gguf_loader import Flux2KleinGGUFLoader
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex
    from backend.image_engine.models.transformer import Flux2KleinSingleBlock, LazyLinear, build_rope_joint
    from backend.image_engine.models.qwen3_forward import _weight_pt
    import numpy as np
    from gguf.quants import dequantize as dq
    loader = Flux2KleinGGUFLoader(str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf"))
    index = RawVRAMIndex(str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf"), device=dev)
    with torch.device("meta"):
        myb = Flux2KleinSingleBlock(4096)
    myb.linear1 = LazyLinear(index, "single_blocks.0.linear1.weight")
    myb.linear2 = LazyLinear(index, "single_blocks.0.linear2.weight")
    for modname, modm in list(myb.named_modules()):
        if isinstance(modm, nn.RMSNorm):
            gg = f"single_blocks.0.{modname}.weight".replace(".weight", ".scale").replace(".qk_norm.", ".norm.")
            lazy = loader.get_lazy(gg)
            if lazy is None:
                raise RuntimeError(f"Missing GGUF tensor: {gg}")
            arr = np.frombuffer(lazy.data, dtype=np.float32).astype(np.float32) \
                if getattr(lazy.qtype, "name", "") == "F32" else dq(lazy.data, lazy.qtype).astype(np.float32)
            modm.weight = nn.Parameter(torch.from_numpy(arr.reshape(lazy.shape_pt)).to(dev, dt))
    myb.eval()
    mc, ms = build_rope_joint(ids[0].to(dev))
    with torch.no_grad():
        out_mine = myb(h, mod, mc, ms)
    print("single0 cos:", cos(out_off, out_mine), flush=True)


if __name__ == "__main__":
    main()

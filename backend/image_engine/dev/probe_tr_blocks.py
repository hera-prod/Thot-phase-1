"""Comparaison bloc-a-bloc : notre forward lazy vs blocs officiels (safetensors locaux)."""
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
    from diffusers.models.transformers.transformer_flux2 import (
        Flux2TransformerBlock, Flux2Modulation, Flux2TimestepGuidanceEmbeddings)
    dev = "cuda"
    dt = torch.bfloat16
    torch.manual_seed(0)

    # --- temb + modulations ---
    tge = Flux2TimestepGuidanceEmbeddings(in_channels=256, embedding_dim=4096,
                                            bias=False, guidance_embeds=False).to(dev, dt).eval()
    sd = off_many([k for k in IDX if k.startswith("time_guidance_embed.")])
    tge.load_state_dict({k.split("time_guidance_embed.", 1)[1]: v for k, v in sd.items()})
    t = torch.tensor([1.0], device=dev, dtype=dt)
    temb_off = tge(t, None)
    # notre chemin
    from backend.image_engine.models.qwen3_forward import _weight_pt  # noqa
    from backend.image_engine.loaders.gguf_loader import Flux2KleinGGUFLoader
    loader = Flux2KleinGGUFLoader(str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf"))
    from backend.image_engine.models.transformer import Flux2KleinTransformer
    from backend.image_engine.core.config import FLUX2_KLEIN_CONFIG
    t_emb = Flux2KleinTransformer._timestep_embedding(t, dim=256)
    in_w = _weight_pt(loader.get_lazy("time_in.in_layer.weight")).to(dev, dt)
    out_w = _weight_pt(loader.get_lazy("time_in.out_layer.weight")).to(dev, dt)
    import torch.nn.functional as F
    temb_mine = F.linear(F.silu(F.linear(t_emb, in_w)), out_w)
    print("temb cos:", cos(temb_off, temb_mine), flush=True)

    mod_off = Flux2Modulation(dim=4096, mod_param_sets=2, bias=False).to(dev, dt).eval()
    sd = off_many(["double_stream_modulation_img.linear.weight"])
    mod_off.load_state_dict({"linear.weight": sd["double_stream_modulation_img.linear.weight"]})
    vec_off = mod_off(temb_off)
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex
    from backend.image_engine.models.transformer import LazyLinear
    index = RawVRAMIndex(str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf"), device=dev)
    lazy_mod = LazyLinear(index, "double_stream_modulation_img.lin.weight")
    vec_mine = lazy_mod(torch.nn.functional.silu(temb_mine))
    print("modvec cos:", cos(vec_off, vec_mine), flush=True)

    # --- bloc double 0 ---
    blk_off = Flux2TransformerBlock(dim=4096, num_attention_heads=32, attention_head_dim=128,
                                    mlp_ratio=3.0, eps=1e-6, bias=False).to(dev, dt).eval()
    names = [k for k in IDX if k.startswith("transformer_blocks.0.")]
    sd = off_many(names)
    blk_off.load_state_dict({k.split("transformer_blocks.0.", 1)[1]: v for k, v in sd.items()})
    # rope officielle
    from diffusers.models.transformers.transformer_flux2 import Flux2PosEmbed
    pe = Flux2PosEmbed(theta=2000, axes_dim=[32, 32, 32, 32])
    Lt, Li = 8, 16
    txt_ids = torch.cartesian_prod(torch.zeros(1, dtype=torch.long), torch.zeros(1, dtype=torch.long),
                                   torch.zeros(1, dtype=torch.long), torch.arange(Lt)).unsqueeze(0)
    img_ids = torch.cartesian_prod(torch.zeros(1, dtype=torch.long), torch.arange(4),
                                   torch.arange(4), torch.zeros(1, dtype=torch.long)).unsqueeze(0)
    ids = torch.cat([txt_ids, img_ids], dim=1)  # [1, 24, 4]
    freqs_cos, freqs_sin = pe(ids[0])
    # notre rope
    from backend.image_engine.models.transformer import build_rope_joint
    mc, ms = build_rope_joint(ids[0].to(dev))
    print("rope cos-close:", torch.allclose(mc, freqs_cos.to(dev).float(), atol=1e-4), flush=True)

    B = 1
    x = torch.randn(B, Li, 4096, device=dev, dtype=dt)
    txt = torch.randn(B, Lt, 4096, device=dev, dtype=dt)
    with torch.no_grad():
        enc_out, img_out = blk_off(hidden_states=x, encoder_hidden_states=txt,
                                   temb_mod_img=vec_off, temb_mod_txt=vec_off,
                                   image_rotary_emb=(freqs_cos.to(dev, dt), freqs_sin.to(dev, dt)))
    # --- notre bloc double 0 (lazy) ---
    from backend.image_engine.models.transformer import (
        Flux2KleinDoubleBlock, LazyLinear, build_rope_joint)
    with torch.device("meta"):
        myblk = Flux2KleinDoubleBlock(4096)
    # bind qkv/proj
    for stream in ("img_attn", "txt_attn"):
        for leaf in ("qkv", "proj"):
            mod = myblk.img_attn if stream == "img_attn" else myblk.txt_attn
            mod[leaf] = LazyLinear(index, f"double_blocks.0.{stream}.{leaf}.weight")
    for stream in ("img_mlp", "txt_mlp"):
        for leaf in ("0", "2"):
            getattr(myblk, stream)[leaf] = LazyLinear(index, f"double_blocks.0.{stream}.{leaf}.weight")
    # bind normes (scales F32)
    import numpy as np
    from gguf.quants import dequantize as dq
    for modname, mod in list(myblk.named_modules()):
        if isinstance(mod, nn.RMSNorm):
            gg = f"double_blocks.0.{modname}.weight".replace(".weight", ".scale")
            lazy = loader.get_lazy(gg)
            if lazy is None:
                raise RuntimeError(f"Missing GGUF tensor: {gg}")
            arr = np.frombuffer(lazy.data, dtype=np.float32).astype(np.float32) \
                if getattr(lazy.qtype, "name", "") == "F32" else dq(lazy.data, lazy.qtype).astype(np.float32)
            from backend.image_engine.models.qwen3_forward import _weight_pt  # noqa
            mod.weight = nn.Parameter(torch.from_numpy(
                arr.reshape(lazy.shape_pt)).to(dev, dt))
    myblk.eval()
    with torch.no_grad():
        my_img, my_txt = myblk(x, txt, vec_mine, vec_mine, mc.to(dev), ms.to(dev))
    print("bloc0 img cos:", cos(img_out, my_img), flush=True)
    print("bloc0 txt cos:", cos(enc_out, my_txt), flush=True)


if __name__ == "__main__":
    main()

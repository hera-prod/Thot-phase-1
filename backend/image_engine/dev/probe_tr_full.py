"""Parite exhaustive GGUF vs officiel (toutes les familles de tenseurs)."""
import sys
import json
import torch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # racine projet (paquet `backend`)
from backend.image_engine.core.paths import MODELS_DIR  # models/flux2-klein
from backend.image_engine.loaders.gguf_loader import Flux2KleinGGUFLoader
from backend.image_engine.models.qwen3_forward import _weight_pt

SNAP = Path.home() / ".cache/huggingface/hub/models--black-forest-labs--FLUX.2-klein-9b-kv/snapshots/a6dfb36eca3a3906eb2fd460795adfb844e5fcce"
IDX = json.load(open(SNAP / "transformer/diffusion_pytorch_model.safetensors.index.json"))["weight_map"]


def off(name):
    from safetensors import safe_open
    with safe_open(SNAP / "transformer" / IDX[name], framework="pt") as f:
        return f.get_tensor(name).double()


def cos(a, b):
    a = a.flatten().double()
    b = b.flatten().double()
    return (a @ b / (a.norm() * b.norm())).item()


def main():
    loader = Flux2KleinGGUFLoader(str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf"))
    G = lambda n: _weight_pt(loader.get_lazy(n)).double().cpu()
    pairs = [
        ("double_blocks.0.txt_attn.qkv.weight[0:4096]", G("double_blocks.0.txt_attn.qkv.weight")[:4096],
         "transformer_blocks.0.attn.add_q_proj.weight"),
        ("double_blocks.0.txt_attn.qkv.weight[4096:8192]", G("double_blocks.0.txt_attn.qkv.weight")[4096:8192],
         "transformer_blocks.0.attn.add_k_proj.weight"),
        ("double_blocks.0.txt_attn.qkv.weight[8192:]", G("double_blocks.0.txt_attn.qkv.weight")[8192:],
         "transformer_blocks.0.attn.add_v_proj.weight"),
        ("txt query_norm.scale", G("double_blocks.0.txt_attn.norm.query_norm.scale"),
         "transformer_blocks.0.attn.norm_added_q.weight"),
        ("txt key_norm.scale", G("double_blocks.0.txt_attn.norm.key_norm.scale"),
         "transformer_blocks.0.attn.norm_added_k.weight"),
        ("img query_norm.scale", G("double_blocks.0.img_attn.norm.query_norm.scale"),
         "transformer_blocks.0.attn.norm_q.weight"),
        ("img_mlp.0", G("double_blocks.0.img_mlp.0.weight"), "transformer_blocks.0.ff.linear_in.weight"),
        ("img_mlp.2", G("double_blocks.0.img_mlp.2.weight"), "transformer_blocks.0.ff.linear_out.weight"),
        ("txt_mlp.0", G("double_blocks.0.txt_mlp.0.weight"), "transformer_blocks.0.ff_context.linear_in.weight"),
        ("single linear1 full", G("single_blocks.0.linear1.weight"),
         "single_transformer_blocks.0.attn.to_qkv_mlp_proj.weight"),
        ("single linear1 qkv", G("single_blocks.0.linear1.weight")[:12288],
         "single_transformer_blocks.0.attn.to_qkv_mlp_proj.weight"),
        ("single linear2", G("single_blocks.0.linear2.weight"),
         "single_transformer_blocks.0.attn.to_out.weight"),
        ("mod img", G("double_stream_modulation_img.lin.weight"),
         "double_stream_modulation_img.linear.weight"),
        ("final linear (proj_out)", G("final_layer.linear.weight"), "proj_out.weight"),
        ("time out", G("time_in.out_layer.weight"),
         "time_guidance_embed.timestep_embedder.linear_2.weight"),
    ]
    for label, g, oname in pairs:
        try:
            o = off(oname)
        except KeyError:
            print(f"{label}: OFFICIEL MANQUANT {oname}", flush=True)
            continue
        flag = "" if g.shape == o.shape else f" SHAPE {tuple(g.shape)} vs {tuple(o.shape)}"
        c = cos(g.reshape(o.shape), o) if g.numel() == o.numel() else float("nan")
        print(f"{label}: cos={c:.5f}{flag}", flush=True)


if __name__ == "__main__":
    main()

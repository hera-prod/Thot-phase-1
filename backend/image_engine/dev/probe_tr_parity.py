"""Parite poids GGUF dequant vs checkpoint officiel (safetensors locaux, zero download)."""
import sys
import json
import torch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # racine projet (paquet `backend`)
from backend.image_engine.core.paths import MODELS_DIR  # models/flux2-klein
from backend.image_engine.loaders.gguf_loader import Flux2KleinGGUFLoader

SNAP = Path.home() / ".cache/huggingface/hub/models--black-forest-labs--FLUX.2-klein-9b-kv/snapshots/a6dfb36eca3a3906eb2fd460795adfb844e5fcce"


def load_official(sub: str, name: str) -> torch.Tensor:
    idx = json.load(open(SNAP / sub / "diffusion_pytorch_model.safetensors.index.json")) \
        if (SNAP / sub / "diffusion_pytorch_model.safetensors.index.json").exists() else None
    if idx is not None:
        fname = idx["weight_map"][name]
        from safetensors import safe_open
        with safe_open(SNAP / sub / fname, framework="pt") as f:
            return f.get_tensor(name).float()
    # shard unique ?
    from safetensors.torch import load_file
    for sf in (SNAP / sub).glob("*.safetensors"):
        sd = load_file(str(sf))
        if name in sd:
            return sd[name].float()
    raise KeyError(name)


def gguf_dequant(loader, name):
    from backend.image_engine.models.qwen3_forward import _weight_pt
    lazy = loader.get_lazy(name)
    assert lazy is not None, name
    return _weight_pt(lazy).float().cpu()


def cos(a, b):
    return torch.nn.functional.cosine_similarity(a.flatten(), b.flatten(), dim=0).item()


def main():
    loader = Flux2KleinGGUFLoader(str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf"))
    pairs = [
        ("txt_in.weight", ("transformer", "context_embedder.weight"), None),
        ("img_in.weight", ("transformer", "x_embedder.weight"), None),
        ("final_layer.adaLN_modulation.1.weight", ("transformer", "norm_out.linear.weight"), None),
        ("time_in.in_layer.weight", ("transformer", "time_guidance_embed.timestep_embedder.linear_1.weight"), None),
        ("double_blocks.0.img_attn.qkv.weight", ("transformer", "transformer_blocks.0.attn.to_q.weight"), (0, 4096)),
        ("double_blocks.0.img_attn.qkv.weight", ("transformer", "transformer_blocks.0.attn.to_k.weight"), (4096, 8192)),
        ("double_blocks.0.img_attn.qkv.weight", ("transformer", "transformer_blocks.0.attn.to_v.weight"), (8192, 12288)),
    ]
    for gg, (sub, off), rows in pairs:
        g = gguf_dequant(loader, gg)
        o = load_official(sub, off)
        if rows is not None:
            g = g[rows[0]:rows[1]]
        print(f"{gg} vs {off}: shape {tuple(g.shape)}/{tuple(o.shape)} cos={cos(g, o):.5f}", flush=True)


if __name__ == "__main__":
    main()

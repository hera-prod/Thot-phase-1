#!/usr/bin/env python3
"""Download all models for backend.image_engine (cibles : models/flux2-klein)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # racine projet (paquet `backend`)
from backend.image_engine.core.paths import MODELS_DIR  # models/flux2-klein


def run(cmd: str, desc: str):
    print(f"  [download] {desc}")
    subprocess.check_call(cmd, shell=True)
    print(f"  [done]   {desc}")


def rename(filespec: str, newname: str, directory: str):
    target = Path(directory) / filespec
    new_path = Path(directory) / newname
    matches = list(Path(directory).glob(filespec))
    if matches:
        matches[0].rename(new_path)
        print(f"  [renamed] {matches[0].name} -> {newname}")


def main():
    models_dir = MODELS_DIR
    md = str(models_dir)
    for d in ["transformer", "text_encoder", "vae-tmp", "tok-tmp", "vae", "tokenizer"]:
        (models_dir / d).mkdir(parents=True, exist_ok=True)

    # 1. Transformer GGUF Q4_K_M
    run(
        f"hf download QuantStack/FLUX.2-Klein-9B-KV-GGUF --include '*Q4_K_M*' --local-dir {md}/transformer/",
        "QuantStack/FLUX.2-Klein-9B-KV-GGUF (Q4_K_M)",
    )
    rename("*Q4_K_M*.gguf", "flux2-klein-9b-kv-Q4_K_M.gguf", f"{md}/transformer")

    # 2. Text encoder — Qwen3-8B vocab-compatible avec le tokenizer Klein
    run(
        f"hf download Qwen/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf --local-dir {md}/text_encoder/",
        "Qwen/Qwen3-8B-GGUF (Qwen3-8B-Q4_K_M)",
    )
    print("  [ready] Qwen3-8B-Q4_K_M.gguf")

    # 3. VAE (gated)
    run(
        f"hf download black-forest-labs/FLUX.2-klein-9b-kv --include 'vae/*' --local-dir {md}/vae-tmp/",
        "black-forest-labs/FLUX.2-klein-9b-kv (VAE) — gated",
    )
    vae_src = models_dir / "vae-tmp" / "vae"
    if vae_src.exists():
        vae_dst = models_dir / "vae"
        for f in vae_src.iterdir():
            (vae_dst / f.name).write_bytes(f.read_bytes())
        print(f"  [copied] VAE to {md}/vae/")

    # 4. Tokenizer (gated)
    run(
        f"hf download black-forest-labs/FLUX.2-klein-9B --include 'tokenizer/*' --local-dir {md}/tok-tmp/",
        "black-forest-labs/FLUX.2-klein-9B (tokenizer) — gated",
    )
    tok_src = models_dir / "tok-tmp" / "tokenizer"
    if tok_src.exists():
        tok_dst = models_dir / "tokenizer"
        for f in tok_src.iterdir():
            (tok_dst / f.name).write_bytes(f.read_bytes())
        print(f"  [copied] tokenizer to {md}/tokenizer/")

    # Cleanup temp dirs
    import shutil
    for tmp in [models_dir / "vae-tmp", models_dir / "tok-tmp"]:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n[✓] All models downloaded and placed.")


if __name__ == "__main__":
    main()

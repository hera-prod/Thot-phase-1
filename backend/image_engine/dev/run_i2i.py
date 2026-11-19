"""Point d'entree CLI pour le moteur image (T2I + édition I2I)."""
import argparse
from pathlib import Path
from backend.image_engine.core.paths import (
    TRANSFORMER_GGUF, TEXT_ENCODER_GGUF, VAE_DIR, TOKENIZER_DIR,
)
from backend.image_engine.runner.orchestrator import Flux2KleinMini


def main():
    parser = argparse.ArgumentParser(description="FLUX.2 Klein 9B-KV Runtime")
    parser.add_argument("--prompt", type=str, required=True, help="Prompt texte")
    parser.add_argument("--width", type=int, default=1024, help="Largeur (multiple de 64)")
    parser.add_argument("--height", type=int, default=1024, help="Hauteur (multiple de 64)")
    parser.add_argument("--seed", type=int, default=42, help="Graine aleatoire")
    parser.add_argument("--ref-image", type=str, default=None, help="Image de reference (edition)")
    parser.add_argument("--output", type=str, default="output.png", help="Fichier de sortie")
    parser.add_argument("--device", type=str, default="cuda", help="Device (cuda/cpu)")
    parser.add_argument("--mode", type=str, default=None, choices=["full_gpu_simult","full_gpu_phased","hybrid","hybrid_custom","unified","cpu","offload"], help="Placement override (rapport_gestion_memoire) ou via FLUX2KLEIN_MODE")
    parser.add_argument("--model", type=str, default=None, help="GGUF transformer (défaut Q4_K_M ; Q3_K_M pour <8 Go)")

    args = parser.parse_args()

    # Chemins des modeles (models/flux2-klein)
    transformer_path = args.model or str(TRANSFORMER_GGUF)
    text_encoder_path = str(TEXT_ENCODER_GGUF)
    vae_path = str(VAE_DIR / "diffusion_pytorch_model.safetensors")
    tokenizer_path = str(TOKENIZER_DIR)

    # Initialisation — sondage + placement (rapport_gestion_memoire)
    resolution = max(args.width, args.height)
    pipeline = Flux2KleinMini(
        transformer_path=transformer_path,
        text_encoder_path=text_encoder_path,
        vae_path=vae_path,
        tokenizer_path=tokenizer_path,
        device=args.device,
        mode=args.mode,
        resolution=resolution,
    )

    # Generation
    mode = "Edition" if args.ref_image else "T2I"
    print(f"\n=== Mode {mode} ===")
    print(f"Prompt : {args.prompt}")
    if args.ref_image:
        print(f"Reference : {args.ref_image}")
    print(f"Resolution : {args.width}x{args.height}")
    print(f"Seed : {args.seed}\n")

    image = pipeline.generate(
        prompt=args.prompt,
        width=args.width,
        height=args.height,
        seed=args.seed,
        ref_image_path=args.ref_image,
    )

    # Sauvegarde
    image.save(args.output)
    print(f"\nImage sauvegardee : {args.output}")


if __name__ == "__main__":
    main()

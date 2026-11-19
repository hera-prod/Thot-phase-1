"""Serveur FastAPI exposant flux2-klein-mini comme API REST."""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from pathlib import Path
from PIL import Image
import torch
import torchvision.transforms as T
import time
from collections.abc import Callable
from typing import cast

# Imports pipeline — paquet intégré `backend.image_engine`
try:
    from backend.image_engine.runner.orchestrator import Flux2KleinMini
    from backend.image_engine.core.paths import (
        TRANSFORMER_GGUF as TRANSFORMER_Q3_PATH,
        TRANSFORMER_GGUF_Q3 as TRANSFORMER_Q3_FILE,
        TEXT_ENCODER_GGUF as TEXT_ENCODER_PATH,
        VAE_DIR as VAE_PATH,
        TOKENIZER_DIR as TOKENIZER_PATH,
    )
except ImportError:
    from backend.image_engine.runner.orchestrator import Flux2KleinMini
    from backend.image_engine.core.paths import (
        TRANSFORMER_GGUF as TRANSFORMER_Q3_PATH,
        TRANSFORMER_GGUF_Q3 as TRANSFORMER_Q3_FILE,
        TEXT_ENCODER_GGUF as TEXT_ENCODER_PATH,
        VAE_DIR as VAE_PATH,
        TOKENIZER_DIR as TOKENIZER_PATH,
    )

# Fallback si anciens noms Q3 insuffisants → prendre Q4 existant
# TRANSFORMER_GGUF pointe vers Q4_K_M; on essaie Q3 s'il existe sinon Q4
_transformer_candidates = [
    Path(TRANSFORMER_Q3_FILE),
    Path(TRANSFORMER_Q3_PATH),
]
TRANSFORMER_Q3_PATH = next((str(p) for p in _transformer_candidates if p.exists()), str(TRANSFORMER_Q3_PATH))

app = FastAPI(title="FLUX.2 Klein API", version="1.0.0")

# === Configuration ===
MAX_REFS = 4
MAX_REF_SIZE_MB = 10
ALLOWED_FORMATS = {"PNG", "JPEG", "JPG", "WEBP"}
BASE_GEN_SIZE = 1024  # Base pour la génération
BASE_REF_SIZE = 512   # Base pour les références

# === Ratios supportés ===
ASPECT_RATIOS = {
    "1:1": (1, 1),
    "16:9": (16, 9),
    "9:16": (9, 16),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "3:2": (3, 2),
    "2:3": (2, 3),
}

# === Initialisation du pipeline (une seule fois au démarrage) ===
pipeline = Flux2KleinMini(
    transformer_path=str(TRANSFORMER_Q3_PATH),
    text_encoder_path=str(TEXT_ENCODER_PATH),
    vae_path=str(VAE_PATH),
    tokenizer_path=str(TOKENIZER_PATH),
    mode="full_gpu_phased"
)

# === Modèles Pydantic ===
class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="Text description of the image to generate")
    aspect_ratio: str = Field("1:1", description="Aspect ratio: 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3")
    seed: int = Field(42, description="Random seed for reproducibility")
    ref_images: Optional[list[str]] = Field(default_factory=list, description="List of reference image paths (max 4)")

class GenerateResponse(BaseModel):
    image_path: str
    status: str
    vram_peak_gb: float
    generation_time_s: float
    dimensions: tuple[int, int]
    n_refs: int

# === Fonctions utilitaires ===
def compute_dimensions(aspect_ratio: str, base_size: int) -> tuple[int, int]:
    """Convertit ratio en width/height multiples de 64."""
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Ratio non supporté: {aspect_ratio}")

    ratio_w, ratio_h = ASPECT_RATIOS[aspect_ratio]

    if ratio_w >= ratio_h:
        width = base_size
        height = int(base_size * ratio_h / ratio_w)
    else:
        height = base_size
        width = int(base_size * ratio_w / ratio_h)

    # Arrondir au multiple de 64
    width = (width // 64) * 64
    height = (height // 64) * 64

    # Garantir minimum 256
    width = max(256, width)
    height = max(256, height)

    return width, height

def resize_ref_image(image_path: str, aspect_ratio: str) -> torch.Tensor:
    """Charge et redimensionne une image de référence."""
    path = Path(image_path)

    # Vérifier existence
    if not path.exists():
        raise FileNotFoundError(f"Image de référence introuvable: {image_path}")

    # Vérifier format
    try:
        image = Image.open(path)
        image_format = (image.format or "").upper()
        if image_format == "JPEG":
            image_format = "JPG"
        if image_format not in ALLOWED_FORMATS:
            raise ValueError(f"Format non supporté: {image_format}")
    except FileNotFoundError:
        raise
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Impossible de lire l'image: {e}")

    # Vérifier taille
    file_size = path.stat().st_size / 1024 / 1024
    if file_size > MAX_REF_SIZE_MB:
        raise ValueError(f"Image trop lourde: {file_size:.1f} Mo (max {MAX_REF_SIZE_MB} Mo)")

    # Calculer dimensions cibles
    width, height = compute_dimensions(aspect_ratio, BASE_REF_SIZE)

    # Redimensionner avec anti-aliasing
    transform = T.Compose([
        T.Resize((height, width), interpolation=T.InterpolationMode.LANCZOS),
        T.ToTensor(),
        T.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]),  # [0,1] → [-1,1]
    ])

    image = image.convert("RGB")
    transformed = transform(image)
    if not isinstance(transformed, torch.Tensor):
        raise TypeError("Reference image transform must return a torch.Tensor.")
    return transformed.unsqueeze(0)  # [1, 3, H, W]

# === Endpoints ===
@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    """Génère une image (T2I ou TI2I)."""
    try:
        # Validation des références
        if req.ref_images:
            if len(req.ref_images) > MAX_REFS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Maximum {MAX_REFS} références autorisées"
                )

        # Validation ratio
        if req.aspect_ratio not in ASPECT_RATIOS:
            raise HTTPException(status_code=400, detail=f"Ratio non supporté: {req.aspect_ratio}. Ratios supportés: {list(ASPECT_RATIOS.keys())}")

        # Calculer dimensions de génération
        gen_width, gen_height = compute_dimensions(req.aspect_ratio, BASE_GEN_SIZE)

        # Redimensionner les références automatiquement (validation format/taille/existence)
        resized_refs = []
        if req.ref_images:
            for ref_path in req.ref_images:
                resized = resize_ref_image(ref_path, req.aspect_ratio)
                resized_refs.append(resized)

        # Générer l'image — chronométrer
        t0 = time.time()
        # Pipeline réel : T2I vs TI2I
        # L'API TI2I passe par generate_edit si disponible, sinon generate avec ref_image_path
        image = None
        if resized_refs:
            # Sauver les refs redimensionnées en temp pour compatibilité pipeline qui attend des paths
            # Le rapport spec passe les tenseurs directement; on supporte les deux
            tmp_dir = Path("outputs/api/_tmp_refs")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_paths = []
            for i, t in enumerate(resized_refs):
                # t: [1,3,H,W] in [-1,1] → [0,1] → PNG
                arr = ((t.squeeze(0).permute(1, 2, 0).cpu().float() + 1) / 2).clamp(0, 1).numpy()
                arr = (arr * 255).astype("uint8")
                p = tmp_dir / f"ref_{int(time.time())}_{i}.png"
                Image.fromarray(arr).save(p)
                tmp_paths.append(str(p))
            # Appel pipeline
            if hasattr(pipeline, "generate_edit"):
                try:
                    image = pipeline.generate_edit(
                        prompt=req.prompt,
                        ref_paths=tmp_paths,
                        width=gen_width,
                        height=gen_height,
                        seed=req.seed,
                    )
                except TypeError:
                    # fallback signature ref_image_paths
                    image = pipeline.generate(
                        prompt=req.prompt,
                        width=gen_width,
                        height=gen_height,
                        seed=req.seed,
                        ref_image_path=tmp_paths[0] if tmp_paths else None,
                    )
            else:
                # Ancien pipeline: tenter ref_image_paths
                try:
                    legacy_generate = cast(Callable[..., object], getattr(pipeline, "generate"))
                    image = legacy_generate(
                        prompt=req.prompt,
                        width=gen_width,
                        height=gen_height,
                        seed=req.seed,
                        ref_image_paths=resized_refs,
                    )
                except TypeError:
                    image = pipeline.generate(
                        prompt=req.prompt,
                        width=gen_width,
                        height=gen_height,
                        seed=req.seed,
                        ref_image_path=tmp_paths[0] if tmp_paths else None,
                    )
        else:
            image = pipeline.generate(
                prompt=req.prompt,
                width=gen_width,
                height=gen_height,
                seed=req.seed,
            )

        dt = time.time() - t0

        # Sauvegarde
        out_dir = Path("outputs/api")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"generated_{int(time.time())}_{req.seed}.png"
        # image est PIL Image
        if isinstance(image, dict) and "image" in image:
            image = image["image"]
        if isinstance(image, Image.Image):
            image.save(out_path)
        elif isinstance(image, torch.Tensor):
            # tensor fallback
            arr = image.squeeze(0).permute(1, 2, 0).cpu().float().numpy()
            Image.fromarray((arr * 255).astype("uint8")).save(out_path)
        else:
            # dict avec image_path deja
            if isinstance(image, dict) and "image_path" in image:
                out_path = Path(image["image_path"])
            else:
                raise RuntimeError(f"Type de retour pipeline inattendu: {type(image)}")

        # VRAM peak
        try:
            from backend.image_engine.runner.memprof import RunRecord
            rec = RunRecord.capture(pipeline.dev_info, pipeline.mode)
            vram_peak = rec.vram_peak_mb / 1024
        except Exception:
            vram_peak = float(torch.cuda.max_memory_allocated() / 1024**3) if torch.cuda.is_available() else 0.0

        return GenerateResponse(
            image_path=str(out_path),
            status="success",
            vram_peak_gb=round(vram_peak, 2),
            generation_time_s=round(dt, 2),
            dimensions=(gen_width, gen_height),
            n_refs=len(req.ref_images) if req.ref_images else 0
        )

    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    """Vérifie que le serveur est opérationnel."""
    return {"status": "ok", "model": "Flux2KleinMini", "mode": getattr(pipeline, "mode", "unknown")}


@app.get("/")
async def root():
    return {"message": "FLUX.2 Klein API — POST /generate"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

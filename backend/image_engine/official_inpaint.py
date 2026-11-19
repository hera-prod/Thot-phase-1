"""Backend d'inpainting/outpainting *officiel* FLUX.2 Klein 9B.

Utilise le pipeline diffusers `Flux2KleinInpaintPipeline` (support natif d'un
masque + image de référence conditionnant), au lieu de la contrainte de
sampling "latent-blend" de `spike_t2i.py`. Il s'agit de la voie officielle
pour FLUX.2 Klein, qui est un modèle T2I + édition par référence.
"""

from __future__ import annotations

import io
import os
import threading

try:
    import torch
    from diffusers import Flux2KleinInpaintPipeline
    from PIL import Image as _PILImage

    _IMPORT_OK = True
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    Flux2KleinInpaintPipeline = None  # type: ignore
    _PILImage = None  # type: ignore
    _IMPORT_OK = False


_PIPELINE_LOCK = threading.Lock()
_PIPELINE = None


def is_available() -> bool:
    return bool(_IMPORT_OK and Flux2KleinInpaintPipeline is not None)


def _default_model_id() -> str:
    env = os.environ.get("THOT_FLUX2_MODEL")
    if env:
        return env
    env = os.environ.get("THOT_FLUX_MODEL_ID")
    if env:
        return env
    return "black-forest-labs/FLUX.2-klein-9b-kv"


def local_weights_compatible(model_id: str | None = None) -> tuple[bool, str]:
    """Vérifie que le dossier modèle est chargeable par Flux2KleinInpaintPipeline.

    Ne télécharge rien et n'appelle jamais le réseau.
    """
    from pathlib import Path

    candidate = None
    if model_id is not None and str(model_id).strip() and Path(str(model_id).strip()).exists():
        candidate = str(model_id).strip()
    else:
        candidate = _default_model_id()

    path = Path(candidate)
    if not path.exists():
        # Si candidate n'est pas un chemin local existant mais un identifiant
        # de dépôt distant, on le considère non local.
        return False, "model_id_not_local"

    if not path.is_dir():
        return False, "model_id_not_local"

    if not (path / "model_index.json").is_file():
        return False, "gguf_without_model_index"

    transformer_dir = path / "transformer"
    safetensors = []
    if transformer_dir.is_dir():
        safetensors = list(transformer_dir.glob("*.safetensors"))
    if not safetensors:
        return False, "transformer_safetensors_missing"

    return True, "ok"


def _load_pipeline():
    global _PIPELINE
    with _PIPELINE_LOCK:
        if _PIPELINE is not None:
            return _PIPELINE
        if not _IMPORT_OK:
            raise RuntimeError("diffusers indisponible")

        model_id = _default_model_id()
        local_files_only = os.environ.get(
            "THOT_FLUX2_LOCAL_FILES_ONLY", "0"
        ).lower() not in (
            "0",
            "false",
            "no",
        )
        pipe = Flux2KleinInpaintPipeline.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, local_files_only=local_files_only
        )
        if torch.cuda.is_available():
            seq = os.environ.get("THOT_FLUX2_SEQUENTIAL_OFFLOAD", "")
            if seq.lower() in {"1", "true", "yes"}:
                try:
                    pipe.enable_sequential_cpu_offload()
                except Exception as exc:
                    raise RuntimeError(
                        f"[OFFLOAD_FAILED] enable_sequential_cpu_offload failed: {exc}"
                    ) from exc
            else:
                try:
                    pipe.enable_model_cpu_offload()
                except Exception as exc:
                    raise RuntimeError(
                        f"[OFFLOAD_FAILED] enable_model_cpu_offload failed: {exc}"
                    ) from exc
        _PIPELINE = pipe
        return _PIPELINE


def _pil_from_path(path: str):
    with _PILImage.open(path) as img:
        if img.mode == "RGBA":
            background = _PILImage.new("RGB", img.size, (128, 128, 128))
            background.paste(img, mask=img.getchannel("A"))
            img = background
        return img.convert("RGB")


def backend_info() -> dict:
    """Métadonnées observables du backend officiel Diffusers (sans charger le pipeline)."""
    info: dict[str, object] = {
        "model_id": _default_model_id(),
        "model_revision": "unknown",
        "pipeline_class": "Flux2KleinInpaintPipeline",
    }
    try:
        _compatible, _reason = local_weights_compatible(None)
        info["local_weights_compatible"] = bool(_compatible)
        info["local_weights_reason"] = str(_reason)
    except Exception:
        info["local_weights_compatible"] = False
        info["local_weights_reason"] = "model_id_not_local"
    if _IMPORT_OK and Flux2KleinInpaintPipeline is not None:
        try:
            cls = Flux2KleinInpaintPipeline
            info["pipeline_class"] = f"{cls.__module__}.{cls.__qualname__}"
        except Exception:
            pass
        try:
            import inspect

            default = inspect.signature(cls.__call__).parameters.get("guidance_scale")
            if default is not None and default.default is not inspect.Parameter.empty:
                info["guidance_scale_default"] = round(float(default.default), 4)
        except Exception:
            pass
    try:
        pipe = _PIPELINE
        if pipe is not None:
            config = getattr(pipe, "config", None)
            if isinstance(config, dict):
                info["is_distilled"] = config.get("is_distilled")
    except Exception:
        pass
    return info


def run_official_inpaint(
    prompt: str,
    seed: int,
    width: int,
    height: int,
    init_image_path: str,
    mask_image_path: str,
    strength: float = 0.8,
    num_inference_steps: int = 4,
    on_stage=None,
    staged_path: str | None = None,
) -> tuple[bytes, dict]:
    if not _IMPORT_OK:
        raise RuntimeError("diffusers indisponible")

    pipe = _load_pipeline()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    generator = torch.Generator(device=device).manual_seed(int(seed))

    image = _pil_from_path(init_image_path)
    mask = _PILImage.open(mask_image_path).convert("L")

    def _cb(pipe_obj, i, t, callback_kwargs=None):
        if on_stage is not None:
            try:
                on_stage("diffusion", f"Diffusion — pas {i + 1}/{num_inference_steps}")
            except Exception:
                pass
        if isinstance(callback_kwargs, dict):
            return callback_kwargs
        return {}

    output = pipe(
        prompt=prompt,
        image=image,
        mask_image=mask,
        height=height,
        width=width,
        strength=strength,
        num_inference_steps=num_inference_steps,
        generator=generator,
        callback_on_step_end=_cb,
        callback_on_step_end_tensor_inputs=["latents"],
        output_type="pil",
    ).images[0]

    buf = io.BytesIO()
    output.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    stats = {
        "seed": int(seed),
        "width": int(output.size[0]),
        "height": int(output.size[1]),
        "backend": "diffusers-flux2-klein-inpaint",
    }

    if staged_path:
        with open(staged_path, "wb") as fh:
            fh.write(png_bytes)

    return png_bytes, stats

"""Chemins du moteur image (ex-flux2-klein-mini, intégré au projet).

Modèles lourds (git-ignorés) : `<data>/models/flux2-klein/` où `<data>` suit
THOT_DATA_DIR (injecté par Electron : portable projet ou XDG) puis `<repo>/data`.
"""

import os
import sys
from pathlib import Path


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _app_data_dir() -> Path:
    explicit = _environment_path("THOT_APP_DATA_DIR")
    if explicit is not None:
        return explicit
    runtime_root = _runtime_root()
    if (runtime_root / "portable.json").exists():
        return runtime_root
    from backend.config.paths import get_app_data_dir

    return Path(get_app_data_dir()).expanduser().resolve()


def _data_dir() -> Path:
    explicit = _environment_path("THOT_DATA_DIR")
    if explicit is not None:
        return explicit
    app_data_dir = _app_data_dir()
    if app_data_dir.name == "data":
        return app_data_dir
    return app_data_dir / "data"


def _model_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _app_data_dir() / path
    return path.resolve()


def _model_candidates() -> tuple[Path, ...]:
    bases = (_data_dir(), _app_data_dir(), _runtime_root())
    candidates: list[Path] = []
    for base in bases:
        candidate = (base / "models" / "flux2-klein").resolve()
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _has_model_layout(path: Path) -> bool:
    return (path / "transformer").is_dir() or (
        path / "tokenizer" / "tokenizer.json"
    ).is_file()


def resolve_models_dir(value: str | Path | None = None) -> Path:
    if value is not None:
        return _model_path(value)
    explicit = os.environ.get("THOT_FLUX_MODELS_DIR", "").strip()
    if explicit:
        return _model_path(explicit)
    candidates = _model_candidates()
    for candidate in candidates:
        if _has_model_layout(candidate):
            return candidate
    return candidates[0]


def _models_dir() -> Path:
    return resolve_models_dir()


DATA_DIR = _data_dir()
MODELS_DIR = _models_dir()
TRANSFORMER_GGUF = MODELS_DIR / "transformer" / "flux2-klein-9b-kv-Q4_K_M.gguf"
TRANSFORMER_GGUF_Q3 = MODELS_DIR / "transformer" / "Flux-2-Klein-9B-KV-Q3_K_M.gguf"
TEXT_ENCODER_GGUF = MODELS_DIR / "text_encoder" / "Qwen3-8B-Q4_K_M.gguf"
VAE_DIR = MODELS_DIR / "vae"
VAE_CONFIG = VAE_DIR / "config.json"
VAE_WEIGHTS = VAE_DIR / "diffusion_pytorch_model.safetensors"
TOKENIZER_DIR = MODELS_DIR / "tokenizer"

TEMP_DIR = DATA_DIR / "temp"


def assert_models_exist() -> None:
    """Verifie que tous les modeles requis sont presents."""
    missing = []
    for p in [TRANSFORMER_GGUF, TEXT_ENCODER_GGUF, VAE_WEIGHTS, VAE_CONFIG]:
        if not p.exists():
            missing.append(str(p))
    if not (TOKENIZER_DIR / "tokenizer.json").exists():
        missing.append(str(TOKENIZER_DIR / "tokenizer.json"))
    if missing:
        raise FileNotFoundError("Modeles manquants:\n  " + "\n  ".join(missing))

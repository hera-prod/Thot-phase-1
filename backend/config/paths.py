# backend/config/paths.py — chemins Phase 1 (fusion v0.8 + notes FR scaffold)
# Notes FR : garde-fou CWD aléatoire du sidecar, absolu via Path.home() / tempfile
# But : résoudre data_dir, modèles, uploads temporaires sans .env — tout hardcodé
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def is_frozen() -> bool:
    """True si exécuté depuis binaire gelé PyInstaller (Thot-phase-1 sidecar)."""
    return bool(getattr(sys, "frozen", False))


def get_bundle_dir() -> Path:
    """
    Répertoire du bundle (lecture seule).
    - PyInstaller onefile : sys._MEIPASS
    - PyInstaller onedir : dossier du binaire
    - Dev : racine projet (parents de backend/config/paths.py)
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_app_data_dir() -> Path:
    """
    Répertoire données utilisateur (XDG/AppData).
    Jamais CWD — toujours absolu via Path.home().
    """
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "thot"


def get_user_data_dir() -> Path:
    """Alias sémantique de get_app_data_dir()."""
    return get_app_data_dir()


def resolve_data_dir(value: Path | str | None) -> Path:
    """Résout data_dir en absolu. None → get_app_data_dir()/data."""
    if value is None:
        return (get_app_data_dir() / "data").resolve()
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = get_app_data_dir() / p if is_frozen() else Path.cwd() / p
    return p.resolve()


def resolve_child_path(value: Path | str | None, default_name: str, data_dir: Path) -> Path:
    """Sous-chemin relatif à data_dir (uploads, etc.)."""
    candidate = Path(value) if value is not None else Path(default_name)
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    return candidate.resolve()


def resolve_file_path(value: Path | str | None, default_name: str, data_dir: Path) -> Path:
    """Fichier relatif à data_dir (thot-phase-1.db)."""
    candidate = Path(value) if value is not None else Path(default_name)
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    return candidate.resolve()


# --- Phase 1 : modèles locaux (hardcodé, pas BaseSettings) ---
def get_local_model_default_path() -> Path:
    """Chemin LFM2.5-8B principal — priorise CWD/models/ (dev) puis app_data."""
    for base in [Path.cwd(), get_bundle_dir(), get_app_data_dir()]:
        for name in ["LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf", "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"]:
            cand = base / "models" / name
            if cand.exists():
                return cand
            direct = base / name
            if direct.exists():
                return direct
    return get_app_data_dir() / "models" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"


def get_vision_general_default_path() -> Path:
    for base in [Path.cwd(), get_bundle_dir(), get_app_data_dir()]:
        for cand in [
            base / "models" / "Vision-general" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf",
            base / "models" / "Vision-general" / "LFM2.5-3B-UD-Q4_K_M.gguf",
            base / "models" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf",
        ]:
            if cand.exists():
                return cand
    return Path.cwd() / "models" / "Vision-general" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf"


def get_vision_document_default_path() -> Path:
    for base in [Path.cwd(), get_bundle_dir(), get_app_data_dir()]:
        for cand in [
            base / "models" / "Vision-document" / "GLM-OCR-f16.gguf",
            base / "models" / "Vision-document" / "GLM-OCR-Q4_K_M.gguf",
            base / "models" / "GLM-OCR-f16.gguf",
        ]:
            if cand.exists():
                return cand
    return Path.cwd() / "models" / "Vision-document" / "GLM-OCR-f16.gguf"


def resolve_model_path(value: str | Path | None) -> str:
    """Résout chemin modèle en absolu string."""
    if not value:
        return str(get_local_model_default_path().resolve())
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = get_app_data_dir() / p if is_frozen() else Path.cwd() / p
    return str(p.resolve())


# --- Temporaire éphémère (RGPD) ---
THOT_TMP = Path(tempfile.gettempdir()) / "thot"  # /tmp/thot — nettoyé systemd 10j + cleanup_request()


__all__ = [
    "THOT_TMP",
    "get_app_data_dir",
    "get_bundle_dir",
    "get_local_model_default_path",
    "get_vision_document_default_path",
    "get_vision_general_default_path",
    "get_user_data_dir",
    "is_frozen",
    "resolve_child_path",
    "resolve_data_dir",
    "resolve_file_path",
    "resolve_model_path",
]

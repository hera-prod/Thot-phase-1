# backend/config/paths.py — chemins Phase 1 (fusion v0.8 + notes FR scaffold)
# Notes FR : garde-fou CWD aléatoire du sidecar, absolu via Path.home() / tempfile
# But : résoudre data_dir, modèles, uploads temporaires sans .env — tout hardcodé
# Contrat portable (C19 / portabilite.md) : mode portable_strict si portable.json
# présent à côté du lanceur/bundle, sinon mode installé XDG.
from __future__ import annotations

import os
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


def _find_portable_root(bundle: Path | None = None) -> Path | None:
    """Cherche portable.json (marqueur mode portable_strict) autour du bundle/exe."""
    # Env explicite prioritaire
    if os.environ.get("THOT_PORTABLE") == "1":
        # THOT_APP_DATA_DIR ou bundle parent sert de racine
        env_root = os.environ.get("THOT_APP_DATA_DIR") or os.environ.get("THOT_DATA_DIR")
        if env_root:
            return Path(env_root).expanduser().resolve().parent if Path(env_root).name == "data" else Path(env_root).expanduser().resolve()
    candidates: list[Path] = []
    if bundle is not None:
        candidates.append(bundle)
        candidates.append(bundle.parent)
    # En frozen : bundle et exe sont pertinents. En dev, ne pas polluer la détection
    # avec le projet racine (sinon tout dev devient portable à cause du fichier à la racine).
    if is_frozen() or os.environ.get("THOT_PORTABLE") == "1":
        b = get_bundle_dir()
        candidates.extend([b, b.parent, Path.cwd(), Path.cwd().parent])
    # Exe parent si frozen
    if is_frozen():
        candidates.append(Path(sys.executable).resolve().parent)
        candidates.append(Path(sys.executable).resolve().parent.parent)
    # En dev non-portable, on vérifie quand même si THOT_DATA_DIR/THOT_APP_DATA_DIR pointe vers portable
    # (cas sidecar lancé par Rust qui a déjà résolu la racine)
    for env_key in ("THOT_APP_DATA_DIR", "THOT_DATA_DIR"):
        env_val = os.environ.get(env_key)
        if env_val:
            p = Path(env_val).expanduser()
            candidates.append(p if p.is_absolute() else Path.cwd() / p)
            if p.name == "data":
                candidates.append(p.parent)
            else:
                candidates.append(p / "data")
    seen: set[Path] = set()
    for base in candidates:
        try:
            base = base.resolve()
        except Exception:
            continue
        if base in seen:
            continue
        seen.add(base)
        if (base / "portable.json").exists():
            return base
        # Variante : data/portable.json (ancien)
        if (base / "data" / "portable.json").exists():
            return base
    return None


def is_portable_mode() -> bool:
    """True si portable_strict détecté (portable.json ou THOT_PORTABLE=1)."""
    if os.environ.get("THOT_PORTABLE") == "1":
        return True
    # THOT_DATA_DIR injecté par Rust en portable => on considère portable
    # mais on ne peut pas le déduire sans marqueur, donc check fichier
    return _find_portable_root() is not None


def get_portable_root() -> Path | None:
    """Racine du dossier portable (contient portable.json), ou None si mode installé."""
    return _find_portable_root()


def get_app_data_dir() -> Path:
    """
    Répertoire données utilisateur.
    - Si THOT_APP_DATA_DIR env (injecté par Rust) → priorité
    - Si portable_strict (portable.json trouvé) → <racine>/data (résolution depuis lanceur, pas CWD)
    - Sinon XDG/AppData (jamais CWD)
    """
    # 1. Env Rust prioritaire (source vérité Tauri)
    env_app = os.environ.get("THOT_APP_DATA_DIR")
    if env_app:
        return Path(env_app).expanduser().resolve()
    # 2. THOT_DATA_DIR direct (fallback Rust ancien)
    env_data = os.environ.get("THOT_DATA_DIR")
    if env_data:
        p = Path(env_data).expanduser().resolve()
        # Si env pointe vers data/, remonter au parent pour cohérence app_data
        if p.name == "data":
            return p.parent
        return p
    # 3. Portable strict : dossier qui contient portable.json
    portable = _find_portable_root()
    if portable is not None:
        return portable
    # 4. Mode installé XDG
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
        # get_app_data_dir() peut déjà être la racine portable ; data s'y trouve
        app = get_app_data_dir()
        # Si app contient déjà portable.json, data est app/data
        if (app / "portable.json").exists():
            return (app / "data").resolve()
        # Si app est déjà un dossier data (cas THOT_DATA_DIR)
        if app.name == "data":
            return app.resolve()
        return (app / "data").resolve()
    p = Path(value).expanduser()
    if not p.is_absolute():
        # En portable, toujours relatif à la racine portable, pas CWD
        if is_portable_mode():
            p = get_app_data_dir() / p
        else:
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


# --- Helpers portables ---
def get_data_dir() -> Path:
    """Raccourci : dossier data résolu selon mode."""
    return resolve_data_dir(None)


def get_temp_dir() -> Path:
    """
    Dossier temporaire :
    - portable_strict → <racine>/data/temp (reste sur même volume, pas d'écriture hors dossier)
    - sinon → /tmp/thot (ou %TEMP%/thot sur Windows)
    """
    if is_portable_mode():
        return get_data_dir() / "temp"
    return Path(tempfile.gettempdir()) / "thot"


def get_media_dir() -> Path:
    """Dossier blobs portables : data/blobs (nouveau) avec fallback data/media."""
    # Nouveau contrat : blobs/ ; ancien : media/ — on privilégie blobs
    d = get_data_dir()
    blobs = d / "blobs"
    media = d / "media"
    # Si media existe déjà et blobs non, garder media pour migration douce
    if media.exists() and not blobs.exists():
        return media
    return blobs


# --- Phase 1 : modèles locaux (hardcodé, pas BaseSettings) ---
def _model_search_bases() -> list[Path]:
    """Bases de recherche modèles : portable > bundle > CWD > app_data."""
    bases: list[Path] = []
    portable = _find_portable_root()
    if portable is not None:
        bases.append(portable)
    bases.append(get_bundle_dir())
    # CWD seulement en dev non-frozen
    if not is_frozen():
        bases.append(Path.cwd())
    bases.append(get_app_data_dir())
    # Déduplique en conservant l'ordre
    seen: set[Path] = set()
    uniq: list[Path] = []
    for b in bases:
        try:
            r = b.resolve()
        except Exception:
            continue
        if r not in seen:
            seen.add(r)
            uniq.append(b)
    return uniq


def get_local_model_default_path() -> Path:
    """Chemin LFM2.5-8B principal — priorise portable/bundle/CWD/app_data."""
    for base in _model_search_bases():
        for name in ["LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf", "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"]:
            cand = base / "models" / name
            if cand.exists():
                return cand
            direct = base / name
            if direct.exists():
                return direct
    # Fallback : portable/data ou app_data
    return get_app_data_dir() / "models" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"


def get_vision_general_default_path() -> Path:
    for base in _model_search_bases():
        for cand in [
            base / "models" / "Vision-general" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf",
            base / "models" / "Vision-general" / "LFM2.5-3B-UD-Q4_K_M.gguf",
            base / "models" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf",
        ]:
            if cand.exists():
                return cand
    return get_app_data_dir() / "models" / "Vision-general" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf"


def get_vision_document_default_path() -> Path:
    for base in _model_search_bases():
        for cand in [
            base / "models" / "Vision-document" / "GLM-OCR-f16.gguf",
            base / "models" / "Vision-document" / "GLM-OCR-Q4_K_M.gguf",
            base / "models" / "GLM-OCR-f16.gguf",
        ]:
            if cand.exists():
                return cand
    return get_app_data_dir() / "models" / "Vision-document" / "GLM-OCR-f16.gguf"


def resolve_model_path(value: str | Path | None) -> str:
    """Résout chemin modèle en absolu string."""
    if not value:
        return str(get_local_model_default_path().resolve())
    p = Path(value).expanduser()
    if not p.is_absolute():
        # En portable, relatif à la racine portable
        if is_portable_mode():
            p = get_app_data_dir() / p
        else:
            p = get_app_data_dir() / p if is_frozen() else Path.cwd() / p
    return str(p.resolve())


# --- Temporaire éphémère (RGPD) ---
# Compat : constante historique ; préférer get_temp_dir() pour le portable
THOT_TMP = Path(tempfile.gettempdir()) / "thot"  # /tmp/thot — nettoyé systemd 10j + cleanup_request()
# Alias dynamique (utiliser get_temp_dir() dans le nouveau code)


__all__ = [
    "THOT_TMP",
    "get_app_data_dir",
    "get_bundle_dir",
    "get_data_dir",
    "get_media_dir",
    "get_portable_root",
    "get_temp_dir",
    "get_local_model_default_path",
    "get_vision_document_default_path",
    "get_vision_general_default_path",
    "get_user_data_dir",
    "is_frozen",
    "is_portable_mode",
    "resolve_child_path",
    "resolve_data_dir",
    "resolve_file_path",
    "resolve_model_path",
]

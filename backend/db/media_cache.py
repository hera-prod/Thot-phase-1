"""Cache média persistant — Réponses.md n°7 : data/blobs/{sha256}.ext (portable) ou ~/.local/share/thot/media"""
from pathlib import Path
import shutil
import hashlib
import mimetypes

def get_media_root() -> Path:
    """Racine blobs : portable data/blobs si portable_strict, sinon XDG thot/media."""
    try:
        from backend.config.paths import get_media_dir  # type: ignore
        return get_media_dir()
    except Exception:
        return Path.home() / ".local" / "share" / "thot" / "media"

def persist_attachment(tmp_path: str) -> tuple[str, str, int]:
    """Copie /tmp/thot/{req}/{hash}.ext -> media/{hash}.ext, retourne (relative_path, sha256, size)."""
    src = Path(tmp_path)
    if not src.exists():
        raise FileNotFoundError(tmp_path)
    # Hash déjà dans nom de fichier, sinon calcule
    sha256 = src.stem  # hash est le nom sans ext
    # Vérifie que c'est bien un hash hex 64
    if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256.lower()):
        data = src.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
    ext = src.suffix or mimetypes.guess_extension(mimetypes.guess_type(str(src))[0] or "") or ".png"
    media_root = get_media_root()
    media_root.mkdir(parents=True, exist_ok=True)
    dst = media_root / f"{sha256}{ext}"
    if not dst.exists():
        shutil.copy2(src, dst)
    size = dst.stat().st_size
    rel = f"media/{sha256}{ext}"
    return rel, sha256, size

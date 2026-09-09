# Gestion fichiers temporaires — upload → data/temp/{request_id}/{hash}.ext en portable, sinon /tmp/thot
# Garde-fous : traversal, MIME, taille, cleanup RGPD
import tempfile
import shutil
import hashlib
from pathlib import Path
import re

try:
    from backend.config.paths import get_temp_dir  # type: ignore
    THOT_TMP = get_temp_dir()
except Exception:
    THOT_TMP = Path(tempfile.gettempdir()) / "thot"  # fallback


def _get_tmp_root() -> Path:
    """Résout le dossier temp courant (portable vs système)."""
    try:
        from backend.config.paths import get_temp_dir  # type: ignore
        return get_temp_dir()
    except Exception:
        return THOT_TMP
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5 MB max par image
MAX_PDF_SIZE = 20 * 1024 * 1024    # 20 MB max par PDF

def save_upload(request_id: str, filename: str, data: bytes) -> tuple[str, str]:
    """Sauvegarde un fichier uploadé dans temp/{request_id}/{hash}.ext et retourne (path, hash)."""
    # 1. Garde-fou : request_id alphanumérique uniquement (évite injection ../)
    assert re.match(r"^[a-zA-Z0-9_-]+$", request_id), "request_id invalide"
    # 2. Garde-fou : on ne garde que le nom de fichier, pas le chemin (strip ../)
    safe_name = Path(filename).name
    assert Path(safe_name).suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf", ".webp"), "extension non autorisée"
    assert len(data) <= MAX_PDF_SIZE, "fichier trop volumineux"
    # 3. Validation MIME via magic bytes (plus fiable que l'extension)
    if data.startswith(b"\x89PNG"):
        mime = "image/png"
    elif data.startswith(b"\xFF\xD8\xFF"):
        mime = "image/jpeg"
    elif data.startswith(b"%PDF"):
        mime = "application/pdf"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError(f"MIME non autorisé (magic bytes inconnus)")

    tmp_root = _get_tmp_root()
    request_dir = tmp_root / request_id
    request_dir.mkdir(parents=True, exist_ok=True)
    file_hash = hashlib.sha256(data).hexdigest()
    file_path = request_dir / f"{file_hash}{Path(safe_name).suffix.lower()}"
    # traversal guard
    assert str(file_path.resolve()).startswith(str(tmp_root.resolve())), "traversal"
    file_path.write_bytes(data)
    return str(file_path), f"sha256:{file_hash}"

def cleanup_request(request_id: str):
    """Nettoie tous les fichiers temporaires d'une requête (après message_completed, même si erreur)."""
    tmp_root = _get_tmp_root()
    path = (tmp_root / request_id).resolve()
    # Garde-fou : on ne supprime JAMAIS en dehors du temp
    assert str(path).startswith(str(tmp_root.resolve())) and path != tmp_root.resolve(), "tentative de rmtree dangereuse"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)  # supprime le dossier request_id et tout son contenu

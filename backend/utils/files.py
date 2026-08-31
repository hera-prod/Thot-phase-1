# Gestion fichiers temporaires — upload → /tmp/thot/{request_id}/{hash}.ext
# Garde-fous : traversal, MIME, taille, cleanup RGPD
import tempfile
import shutil
import hashlib
from pathlib import Path
import re

THOT_TMP = Path(tempfile.gettempdir()) / "thot"  # /tmp/thot sur Linux, %TEMP%/thot sur Windows
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5 MB max par image
MAX_PDF_SIZE = 20 * 1024 * 1024    # 20 MB max par PDF

def save_upload(request_id: str, filename: str, data: bytes) -> tuple[str, str]:
    """Sauvegarde un fichier uploadé dans /tmp/thot/{request_id}/{hash}.ext et retourne (path, hash)."""
    # 1. Garde-fou : request_id alphanumérique uniquement (évite injection ../)
    assert re.match(r"^[a-zA-Z0-9_-]+$", request_id), "request_id invalide"
    # 2. Garde-fou : on ne garde que le nom de fichier, pas le chemin (strip ../)
    safe_name = Path(filename).name
    assert Path(safe_name).suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf"), "extension non autorisée"
    assert len(data) <= MAX_PDF_SIZE, "fichier trop volumineux"
    # 3. Validation MIME via magic bytes (plus fiable que l'extension)
    if data.startswith(b"\x89PNG"):
        mime = "image/png"
    elif data.startswith(b"\xFF\xD8\xFF"):
        mime = "image/jpeg"
    elif data.startswith(b"%PDF"):
        mime = "application/pdf"
    else:
        raise ValueError(f"MIME non autorisé (magic bytes inconnus)")

    request_dir = THOT_TMP / request_id
    request_dir.mkdir(parents=True, exist_ok=True)
    file_hash = hashlib.sha256(data).hexdigest()
    file_path = request_dir / f"{file_hash}{Path(safe_name).suffix.lower()}"
    # traversal guard
    assert str(file_path.resolve()).startswith(str(THOT_TMP.resolve())), "traversal"
    file_path.write_bytes(data)
    return str(file_path), f"sha256:{file_hash}"

def cleanup_request(request_id: str):
    """Nettoie tous les fichiers temporaires d'une requête (après message_completed, même si erreur)."""
    path = (THOT_TMP / request_id).resolve()
    # Garde-fou : on ne supprime JAMAIS en dehors de /tmp/thot
    assert str(path).startswith(str(THOT_TMP.resolve())) and path != THOT_TMP.resolve(), "tentative de rmtree dangereuse"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)  # supprime le dossier request_id et tout son contenu

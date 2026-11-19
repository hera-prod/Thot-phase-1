# Gestion fichiers temporaires — upload → data/temp/{request_id}/{hash}.ext en portable, sinon /tmp/thot
# Garde-fous : traversal, MIME, taille, cleanup RGPD
import tempfile
import shutil
import hashlib
import os
from pathlib import Path

from backend.config.defaults import INPUT_LIMITS
from backend.inference.ipc import is_valid_request_id

try:
    from backend.config.paths import get_temp_dir  # type: ignore

    THOT_TMP = Path(get_temp_dir()).expanduser().resolve()
except Exception:
    THOT_TMP = (Path(tempfile.gettempdir()) / "thot").expanduser().resolve()  # fallback


def _get_tmp_root() -> Path:
    """Résout canoniquement le dossier temp courant (portable vs système)."""
    try:
        from backend.config.paths import get_temp_dir  # type: ignore

        return Path(get_temp_dir()).expanduser().resolve()
    except Exception:
        return Path(THOT_TMP).expanduser().resolve()


def _require_request_id(request_id: object) -> str:
    """Parse an IPC request ID into one safe path component."""
    if not is_valid_request_id(request_id):
        raise ValueError("request_id invalide")
    return request_id


def _resolve_contained(root: Path, candidate: Path) -> Path:
    """Resolve *candidate* and require it to be a strict descendant of *root*."""
    canonical_root = root.expanduser().resolve()
    canonical_candidate = candidate.expanduser().resolve()
    if (
        canonical_candidate == canonical_root
        or canonical_root not in canonical_candidate.parents
    ):
        raise ValueError("chemin temporaire hors racine")
    return canonical_candidate


# Alias historiques : les tests et appelants les importent, mais la valeur vient
# du point unique `INPUT_LIMITS` (Phase 7.2). `MAX_IMAGE_SIZE` valait 5 Mio et
# n'était plus utilisé ; il suit désormais la règle des 20 Mio pour ne pas
# ressusciter une limite fantôme.
MAX_IMAGE_SIZE = int(INPUT_LIMITS["image_bytes"])
MAX_PDF_SIZE = int(INPUT_LIMITS["upload_bytes"])


def _fsync_dir(directory: Path) -> None:
    """Rend durable l'entrée de répertoire après `os.replace` (best-effort POSIX)."""
    try:
        fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_upload(request_id: str, filename: str, data: bytes) -> tuple[str, str]:
    """Sauvegarde un fichier uploadé dans temp/{request_id}/{hash}.ext et retourne (path, hash)."""
    # 1. Parse the IPC identifier before it can become a path component.
    safe_request_id = _require_request_id(request_id)
    # 2. Garde-fou : on ne garde que le nom de fichier, pas le chemin (strip ../)
    safe_name = Path(filename).name
    # La validation de TYPE vit désormais dans `backend.ipc.attachments.sniff_mime` :
    # elle connaît les tableurs et refuse une extension qui ment. Cette fonction
    # ne garde que l'écriture, pour rester le point unique de dépôt sur disque.
    from backend.ipc.attachments import sniff_mime

    # Le JUGEMENT regarde la fenêtre partagée de binary_guard (taille de
    # l'échantillon qui sert aussi de borne de détection binaire), jamais un
    # littéral local : douze octets textuels suivis de binaire passaient
    # cette validation au seul en-tête, en contradiction avec la fenêtre
    # élargie du chemin de dépôt qui l'appelle (attachments.save_upload) —
    # deux verdicts sur « où commence le binaire ».
    from backend.utils.binary_guard import TAILLE_ECHANTILLON

    sniff_mime(data[:TAILLE_ECHANTILLON], safe_name)
    # Un `assert` disparaît sous `python -O` : la borne doit être une vraie
    # décision, pas une vérification optionnelle.
    if len(data) > MAX_PDF_SIZE:
        raise ValueError(
            f"Fichier trop volumineux : maximum {MAX_PDF_SIZE // 1024 // 1024} Mo."
        )

    tmp_root = _get_tmp_root()
    request_dir = _resolve_contained(tmp_root, tmp_root / safe_request_id)
    request_dir.mkdir(parents=True, exist_ok=True)
    file_hash = hashlib.sha256(data).hexdigest()
    file_path = _resolve_contained(
        tmp_root,
        request_dir / f"{file_hash}{Path(safe_name).suffix.lower()}",
    )
    # Écriture atomique : le renderer ne doit jamais lire un fichier à moitié
    # écrit, et un crash ne doit pas laisser un nom définitif tronqué. Le
    # temporaire naît dans le MÊME dossier, puis `os.replace` publie les octets
    # d'un seul geste.
    fd, temporaire = tempfile.mkstemp(
        prefix=f".{file_hash}.", suffix=".tmp", dir=str(request_dir)
    )
    chemin_temporaire = Path(temporaire)
    try:
        with os.fdopen(fd, "wb") as flux:
            flux.write(data)
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(chemin_temporaire, file_path)
        _fsync_dir(request_dir)
    finally:
        try:
            chemin_temporaire.unlink()
        except FileNotFoundError:
            pass
    return str(file_path), f"sha256:{file_hash}"


def cleanup_request(request_id: str) -> None:
    """Nettoie tous les fichiers temporaires d'une requête (après message_completed, même si erreur)."""
    safe_request_id = _require_request_id(request_id)
    tmp_root = _get_tmp_root()
    request_path = tmp_root / safe_request_id
    path = _resolve_contained(tmp_root, request_path)
    if request_path.is_symlink():
        request_path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(
            path, ignore_errors=True
        )  # supprime le dossier request_id et tout son contenu

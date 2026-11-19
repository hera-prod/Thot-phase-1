"""Chaîne des pièces jointes : dépôt, staging, promotion, lecture.

Ce que le pont Electron faisait en `rusqlite` et sur le système de fichiers, porté
ici. Le contrat de référence était `electron/main.cjs` (lignes 537, 1308-1600)
et `backend/utils/files.py` en était déjà la moitié Python.

Deux règles gouvernent tout le module :

  - une extension n'est jamais une preuve. Le type vient des SIGNATURES ; une
    extension qui ment est refusée, pas « corrigée ». Un `.pdf` qui contient un
    PNG n'est pas un PDF.
  - aucun chemin fourni ou dérivé ne sort de sa racine. Un lien symbolique
    pointe ailleurs, donc chaque composant est résolu AVANT d'être utilisé.

Limites : 20 Mo à l'entrée, 10 Mo pour une vignette.
"""

from __future__ import annotations

import base64
import binascii
import traceback

import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config.defaults import INPUT_LIMITS
from backend.ipc.images import _iso_text, _param
from backend.utils.binary_guard import TAILLE_ECHANTILLON

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = int(INPUT_LIMITS["upload_bytes"])
MAX_PREVIEW_BYTES = int(INPUT_LIMITS["preview_bytes"])
STAGED_DIR_NAME = "staged"
STAGING_TTL_S = 24 * 60 * 60  # staging = tampon, jamais archive
MAX_ITEMS_PER_CALL = 8
STAGE_ITEM_RE = re.compile(r"^sti_([0-9a-f]{8,64})$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Racines depuis lesquelles un drop est admissible. Un chemin fourni par le
# renderer n'est PAS une autorisation : il ne peut désigner que l'espace de
# travail de l'utilisateur, jamais « /etc » ni la racine d'un disque.
RACINES_DEPOT_AUTORISEES: tuple[Path, ...] = (
    Path.home(),
    Path("/media"),
    Path("/mnt"),
    Path("/tmp"),
)

# Signature -> (mime, kind). La signature EST la preuve ; l'extension ne l'est pas.
#   PNG  : \x89PNG\r\n\x1a\n
#   JPEG : \xFF\xD8\xFF
#   WEBP : RIFF....WEBP
#   PDF  : %PDF
#   ZIP  : PK\x03\x04 — le conteneur d'un .xlsx ET d'un .ods : on ne peut pas les
#          distinguer par la signature, d'où la désambiguïsation par extension.
SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "image"),
    (b"\xff\xd8\xff", "image/jpeg", "image"),
    (b"%PDF", "application/pdf", "pdf"),
)
ZIP_MAGIC = b"PK\x03\x04"

ALLOWED_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".csv": "text/csv",
    # Texte brut et Markdown : sans signature (fiche texte.md §1.2 — « .txt, .md
    # sont des exemples mentionnés »), donc acceptés comme le .csv, au contenu
    # contrôlé : l'extension n'y prouve RIEN, le ratio binaire tranche.
    ".txt": "text/plain",
    ".md": "text/markdown",
}
# Types dont la signature ne prouve rien (texte ou conteneur ambigu) : l'extension
# est alors une PRÉ-ÉRENCE, jamais une preuve, et le contenu est contrôlé pour
# refuser du binaire déguisé.
WEAK_TYPES = {
    "text/csv",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.ms-excel",
}
# Un tableur n'a pas de preview : le frontend doit afficher une icône, pas une
# image cassée. `kind` non-image le fait déjà.
_BINARY_RATIO = 0.30


# ── Échantillon du jugement « texte ou binaire » ────────────────────────────────
# La fenêtre que sniff_mime juge n'est JAMAIS un littéral local : c'est
# TAILLE_ECHANTILLON (backend/utils/binary_guard), le même échantillon
# qu'y lit le guard binaire : une seule borne, déclarée une fois. Douze
# octets suffisent aux SIGNATURES (elles sont au tête du fichier), pas au
# JUGEMENT de ratio — un .txt qui commence par quelques octets textuels puis
# tombe dans le binaire passait la porte avec un en-tête seul, et son
# `kind` annonçait `text` en silence. La signature prime toujours ; le
# ratio, lui, regarde assez loin pour dire une vérité.
def _octets_de_jugement(data: bytes) -> bytes:
    """L'échantillon que `sniff_mime` juge : la fenêtre partagée du guard."""
    return data[:TAILLE_ECHANTILLON]


def _nom_securise(filename: Any) -> str:
    """Nom de fichier réduit à son dernier composant — jamais un chemin."""
    raw = str(filename or "").strip()
    if not raw:
        raise ValueError("Nom de fichier manquant.")
    name = Path(raw).name
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise ValueError("Nom de fichier invalide.")
    return name


def _suffix(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"Extension non autorisée : {suffix or '(aucune)'}. "
            f"Attendu : {', '.join(sorted(ALLOWED_SUFFIXES))}."
        )
    return suffix


def sniff_mime(head: bytes, name: str) -> tuple[str, str]:
    """Déduit (mime, kind) de la signature, avec la suffixe en cas d'ambiguïté.

    L'extension ne sert qu'à départager deux formats qui partagent la même
    signature (`.xlsx` et `.ods` sont tous deux un ZIP) ou à valider un format
    sans signature propre (`.csv`). Elle ne peut jamais valider un format à
    signature franche : un `%PDF` nommé `.png` est un PDF refusé, pas un PNG.
    """
    suffix = _suffix(name)
    declared = ALLOWED_SUFFIXES[suffix]
    for magic, mime, kind in SIGNATURES:
        if head.startswith(magic):
            if declared != mime:
                raise ValueError(
                    f"Contenu {mime} mais extension {suffix} : le fichier ne "
                    "correspond pas à son nom."
                )
            return mime, kind
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        if declared != "image/webp":
            raise ValueError("Contenu image/webp mais extension " + suffix + ".")
        return "image/webp", "image"
    if head.startswith(ZIP_MAGIC):
        if declared not in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.ms-excel",
        ):
            raise ValueError(
                "Conteneur ZIP mais extension " + suffix + " : tableur attendu."
            )
        return declared, "sheet"
    if declared == "text/csv":
        # Pas de signature : on refuse un contenu manifestement binaire plutôt que
        # de faire confiance au nom. Un CSV est du texte.
        sample = head
        if (
            sample
            and sum(1 for b in sample if b < 9 or (13 < b < 32)) / len(sample)
            > _BINARY_RATIO
        ):
            raise ValueError("Contenu binaire dans un .csv : signature inconnue.")
        return "text/csv", "sheet"
    if declared in ("text/plain", "text/markdown"):
        # Même garde que le .csv, même raison : un .txt/.md n'a aucune
        # signature, donc le contenu seul décide. Un ratio de bytes de
        # contrôle élevé dit « ce fichier n'est pas du texte » — le refuser
        # ici vaut mieux qu'un kind `text` mensonger sur un binaire.
        sample = head
        if (
            sample
            and sum(1 for b in sample if b < 9 or (13 < b < 32)) / len(sample)
            > _BINARY_RATIO
        ):
            raise ValueError(f"Contenu binaire dans un {suffix} : signature inconnue.")
        return declared, "text"
    raise ValueError("Signature inconnue : le type du fichier ne peut pas être établi.")


def _racine_confinee(root: Path) -> Path:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Racine absente : {root}")
    return root


def _confine(root: Path, candidate: Path) -> Path:
    """Résout `candidate` et refuse toute sortie de `root`, liens symboliques inclus."""
    racine = _racine_confinee(root)
    try:
        resolu = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Fichier introuvable ou lien symbolique cassé.") from exc
    if resolu != racine and racine not in resolu.parents:
        raise ValueError("Chemin hors du dossier autorisé : refus.")
    return resolu


def _composants_caches(chemin: Path) -> bool:
    """Vrai dès qu'un composant du chemin commence par un point.

    `.ssh`, `.config`, `..` : aucun n'est un fichier que l'utilisateur a
    intentionnellement déposé. Les refuser ferme la lecture des dossiers de
    configuration et supprime la traversée `..` avant même la résolution.
    """
    return any(part.startswith(".") for part in chemin.parts if part not in ("/", ""))


def _sous_racine_autorisee(resolu: Path) -> bool:
    """Vrai si le chemin RÉSOLU reste dans l'une des racines du drop."""
    for racine in RACINES_DEPOT_AUTORISEES:
        try:
            racine_resolue = racine.resolve()
        except OSError:
            continue
        if resolu == racine_resolue or racine_resolue in resolu.parents:
            return True
    return False


def purger_staging_expires(
    ttl_s: int = STAGING_TTL_S, *, maintenant: float | None = None
) -> int:
    """Purge les fichiers expirés de `data/temp/staged`, fichiers `.tmp` inclus.

    Le staging est une zone temporaire, pas une archive : un fichier partiel ou
    un temporaire d'écriture atomique laissé par un crash ne doit pas grossir
    `data/temp` sans borne. Le balayage est best-effort, journalise son compte,
    et ne bloque jamais le démarrage.
    """
    try:
        dossier = _staged_dir()
    except Exception as exc:
        log.warning("[staging] dossier indisponible : %s: %s", type(exc).__name__, exc)
        return 0
    seuil = (time.time() if maintenant is None else float(maintenant)) - max(
        0, int(ttl_s)
    )
    purges = 0
    for candidat in dossier.iterdir():
        try:
            if not (candidat.is_file() or candidat.is_symlink()):
                continue
            if candidat.stat().st_mtime >= seuil:
                continue
            candidat.unlink()
            purges += 1
        except OSError as exc:
            log.warning("[staging] purge impossible pour %s : %s", candidat.name, exc)
    if purges:
        log.info("[staging] %d fichier(s) expiré(s) purgé(s)", purges)
    return purges


def purger_temporaires_temp_expires(
    ttl_s: int = STAGING_TTL_S, *, maintenant: float | None = None
) -> int:
    """Purge les `.tmp` de `temp/{request_id}` plus vieux que `ttl_s`.

    `save_upload` écrit son temporaire dans le dossier de la requête avant
    `os.replace`. Un arrêt brutal entre les deux laisse ce `.tmp` derrière lui ;
    le fichier définitif, lui, n'est jamais tronqué. On ne touche PAS aux
    fichiers définitifs de la requête : seule l'extension `.tmp` est balayée.
    """
    try:
        from backend.ipc.paths_bridge import temp_root

        racine = temp_root()
    except Exception as exc:  # noqa: BLE001 - un nettoyage ne bloque pas le démarrage
        log.warning("[temp] racine indisponible : %s: %s", type(exc).__name__, exc)
        return 0
    seuil = (time.time() if maintenant is None else float(maintenant)) - max(
        0, int(ttl_s)
    )
    purges = 0
    try:
        dossiers = [d for d in racine.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError as exc:
        log.warning("[temp] balayage impossible : %s", exc)
        return 0
    for dossier in dossiers:
        if dossier.name == STAGED_DIR_NAME:
            continue  # `purger_staging_expires` couvre déjà le staging
        for candidat in dossier.glob("*.tmp"):
            try:
                if candidat.is_symlink() or not candidat.is_file():
                    continue
                if candidat.stat().st_mtime >= seuil:
                    continue
                candidat.unlink()
                purges += 1
            except OSError as exc:
                log.warning("[temp] purge impossible pour %s : %s", candidat.name, exc)
    if purges:
        log.info("[temp] %d temporaire(s) expiré(s) purgé(s)", purges)
    return purges


def purger_temporaires_blobs_expires(
    ttl_s: int = STAGING_TTL_S, *, maintenant: float | None = None
) -> int:
    """Purge les `.tmp` laissés par un crash entre `mkstemp` et `os.replace`.

    Les écritures atomiques suppriment leur temporaire dans tous les chemins
    d'échec NORMAL. Un arrêt brutal entre les deux laisse un `.tmp` dans le
    dossier de son empreinte ; ce balayage l'emporte au démarrage suivant,
    après le TTL, sans toucher à une écriture en cours (le TTL est de 24 h).
    """
    try:
        from backend.ipc.paths_bridge import blobs_root

        racine = blobs_root()
    except Exception as exc:  # noqa: BLE001 - un nettoyage ne bloque pas le démarrage
        log.warning("[blobs] racine indisponible : %s: %s", type(exc).__name__, exc)
        return 0
    seuil = (time.time() if maintenant is None else float(maintenant)) - max(
        0, int(ttl_s)
    )
    purges = 0
    for candidat in racine.rglob("*.tmp"):
        try:
            if candidat.is_symlink() or not candidat.is_file():
                continue
            if candidat.stat().st_mtime >= seuil:
                continue
            candidat.unlink()
            purges += 1
        except OSError as exc:
            log.warning(
                "[blobs] purge .tmp impossible pour %s : %s", candidat.name, exc
            )
    if purges:
        log.info("[blobs] %d temporaire(s) expiré(s) purgé(s)", purges)
    return purges


def _suffix_from_mime(mime: str) -> str:
    """Extension correspondant au type DÉTECTÉ — jamais au nom d'origine."""
    for suffixe, connu in ALLOWED_SUFFIXES.items():
        if connu == mime:
            return suffixe
    return ".bin"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_fichier(chemin: Path) -> str:
    """Empreinte d'un fichier existant, lue en flux (jamais tout en mémoire)."""
    empreinte = hashlib.sha256()
    with chemin.open("rb") as flux:
        for bloc in iter(lambda: flux.read(1024 * 1024), b""):
            empreinte.update(bloc)
    return empreinte.hexdigest()


def _fsync_dir(dossier: Path) -> None:
    """Rend durable l'entrée de répertoire après un `os.replace` (best-effort POSIX).

    Le `fsync` du fichier temporaire garantit les octets ; celui du répertoire
    garantit que le NOUVEAU NOM survit à une coupure. Les systèmes qui ne
    supportent pas l'ouverture d'un dossier ou `fsync` sur un dossier échouent
    sans conséquence ici : la durabilité est déjà acquise au niveau fichier.
    """
    try:
        fd = os.open(str(dossier), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _ecrire_fichier_atomique(cible: Path, data: bytes) -> None:
    """Écrit un fichier en trois temps : temporaire, `fsync`, `os.replace`.

    Un seul helper sert au staging (`_ecrire_stage`) et aux blobs
    (`_ecrire_blob_atomique`) : les deux chemins ont la même exigence — un
    crash ou un ENOSPC ne doit jamais laisser le nom DÉFINITIF avec des octets
    partiels. Le temporaire naît dans le MÊME répertoire que la cible, donc
    `os.replace` est atomique sur un même système de fichiers, jamais à travers
    un montage. Il est supprimé dans tous les cas, succès ou échec.
    """
    fd, temporaire = tempfile.mkstemp(
        prefix=f".{cible.stem[:16]}.", suffix=".tmp", dir=str(cible.parent)
    )
    chemin_temporaire = Path(temporaire)
    try:
        with os.fdopen(fd, "wb") as flux:
            flux.write(data)
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(chemin_temporaire, cible)
        _fsync_dir(cible.parent)
    finally:
        try:
            chemin_temporaire.unlink()
        except FileNotFoundError:
            pass


def _ecrire_blob_atomique(cible: Path, data: bytes, digest: str) -> dict[str, Any]:
    """Écrit un blob atomiquement, ou réutilise un fichier identique vérifié.

    Un fichier existant n'est réutilisé que si sa TAILLE et son EMPREINTE
    concordent. Un fichier partiel laissé par un crash est donc traité comme
    corrompu et remplacé, au lieu d'être adopté sur la seule foi de
    `exists()` — le défaut que ce chemin corrige.
    """
    if cible.exists() and not cible.is_symlink() and cible.is_file():
        try:
            meme_taille = cible.stat().st_size == len(data)
            meme_empreinte = _sha256_fichier(cible) == digest if meme_taille else False
        except OSError:
            meme_taille = meme_empreinte = False
        if meme_taille and meme_empreinte:
            return {"written": False, "reused": True, "path": str(cible)}
    if cible.exists() and not cible.is_file() and not cible.is_symlink():
        raise RuntimeError(
            f"Chemin de blob occupé par autre chose qu'un fichier : {cible}"
        )

    _ecrire_fichier_atomique(cible, data)
    return {
        "written": True,
        "reused": False,
        "path": str(cible),
        "replaced": True,
    }


def _compenser_blobs(nouveaux: dict[str, Path], db: Session) -> list[dict]:
    """Supprime les blobs écrits pour une transaction qui a échoué.

    La règle qui protège un blob PARTAGÉ : on ne supprime un fichier que si
    aucune ligne `blobs` ne porte son empreinte APRÈS le rollback. Un blob
    déjà présent en base avant la promotion n'est jamais dans `nouveaux` ;
    celui qui l'est n'a été écrit que pour cette transaction, et le rollback a
    effacé sa ligne. Si une autre transaction (ou un autre artefact du lot
    précédemment commité) référence la même empreinte, la ligne existe encore
    et le fichier reste.

    Ne lève jamais : une compensation ratée est journalisée, pas transformée
    en seconde panne par-dessus la première.
    """
    erreurs: list[dict] = []
    for digest, chemin in nouveaux.items():
        try:
            ligne = db.execute(
                text("SELECT id FROM blobs WHERE sha256 = :sha"), {"sha": digest}
            ).first()
        except Exception as exc:  # noqa: BLE001 - compensation best-effort, mais dite
            erreurs.append(
                {
                    "digest": digest,
                    "path": str(chemin),
                    "erreur": f"lecture base impossible ({type(exc).__name__}: {exc})",
                }
            )
            log.warning(
                "[attachments] compensation impossible pour %s : %s", chemin, exc
            )
            continue
        if ligne is not None:
            continue
        try:
            chemin.unlink(missing_ok=True)
        except OSError as exc:
            erreurs.append(
                {
                    "digest": digest,
                    "path": str(chemin),
                    "erreur": f"suppression impossible ({type(exc).__name__}: {exc})",
                }
            )
            log.warning("[attachments] blob non compensé %s : %s", chemin, exc)
    return erreurs


# ── Lecture / dépôt ──────────────────────────────────────────────────────────


def save_upload(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Écrit un téléversement dans le dossier de la requête et le valide.

    Retourne le chemin ABSOLU, que le renderer utilisera pour `image_paths`. La
    validation est faite ici, une fois pour toutes : le type est établi par
    signature, jamais par l'extension.
    """
    from backend.utils.files import save_upload as _legacy_save

    request_id = str(_param(params, "requestId", "request_id") or "")
    if not REQUEST_ID_RE.match(request_id):
        raise ValueError("request_id invalide.")
    name = _nom_securise(_param(params, "filename", "name"))
    data = _octets(_param(params, "data", "bytes"), max_bytes=MAX_UPLOAD_BYTES)

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Fichier trop volumineux : {len(data) // 1024 // 1024} Mo, "
            f"maximum {MAX_UPLOAD_BYTES // 1024 // 1024} Mo."
        )
    if not data:
        raise ValueError("Fichier vide.")
    mime, kind = sniff_mime(_octets_de_jugement(data), name)
    chemin, empreinte = _legacy_save(request_id, name, data)
    return {
        "ok": True,
        "path": chemin,
        "hash": empreinte,
        "mime": mime,
        "kind": kind,
        "name": name,
        "size_bytes": len(data),
    }


def _octets(raw: Any, *, max_bytes: int | None = None) -> bytes:
    """Accepte `ArrayBuffer` (Electron), `number[]` (ancien pont), `str` base64 et `bytes`.

    Le transport JSON ne peut pas porter un `ArrayBuffer` ; il porte donc une
    chaîne base64. L'ancien tableau de nombres (~3,6 caractères par octet)
    faisait dépasser la borne de ligne avant même le contrôle de taille et
    gonflait la mémoire d'un facteur supplémentaire. Le décodage vérifie la
    taille ESTIMÉE avant d'allouer, puis la taille réelle après décodage.
    """
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, memoryview):
        return raw.tobytes()
    if isinstance(raw, str):
        return _octets_base64(raw, max_bytes=max_bytes)
    if isinstance(raw, list):
        return bytes(bytearray(int(v) & 0xFF for v in raw))
    raise ValueError("Contenu binaire illisible.")


def _base64_max_chars(max_bytes: int) -> int:
    """Nombre de caractères base64 qui peuvent porter `max_bytes` octets."""
    return 4 * ((max_bytes + 2) // 3)


def _octets_base64(raw: str, *, max_bytes: int | None = None) -> bytes:
    """Décode une chaîne base64, en refusant AVANT décodage ce qui dépasse.

    `validate=True` est le point qui distingue « base64 » de « à peu près
    base64 » : sans lui, `b64decode` ignore les caractères invalides et un
    payload corrompu passerait pour un fichier tronqué.
    """
    from backend.config.defaults import INPUT_LIMITS as _LIMITES

    plafond = int(max_bytes if max_bytes is not None else _LIMITES["base64_bytes_max"])
    payload = raw.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    payload = payload.replace("\n", "").replace("\r", "")
    if not payload:
        return b""
    if len(payload) > _base64_max_chars(plafond):
        raise ValueError(
            f"Fichier trop volumineux : maximum {plafond // 1024 // 1024} Mo."
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Contenu base64 invalide.") from exc
    if len(data) > plafond:
        raise ValueError(
            f"Fichier trop volumineux : {len(data) // 1024 // 1024} Mo, "
            f"maximum {plafond // 1024 // 1024} Mo."
        )
    return data


# ── Staging ──────────────────────────────────────────────────────────────────


def _staged_dir() -> Path:
    from backend.ipc.paths_bridge import temp_root

    dossier = temp_root() / STAGED_DIR_NAME
    dossier.mkdir(parents=True, exist_ok=True)
    return _racine_confinee(dossier)


def _purger_stage(dossier: Path, token: str) -> None:
    """Retire de `dossier` le fichier stagé d'un jeton, s'il y en a un.

    Un jeton mal formé ne désigne rien : il ne purge donc rien, et n'interrompt pas
    le nettoyage des autres. L'absence du fichier est le cas normal — un lot
    interrompu l'a déjà consommé — et ne doit pas rester une erreur.
    """
    match = STAGE_ITEM_RE.match(token)
    if not match:
        return
    for candidat in dossier.glob(f"{match.group(1)}*"):
        try:
            candidat.unlink()
        except OSError:
            pass


def _ecrire_stage(
    db: Session, params: dict[str, Any], data: bytes, name: str
) -> dict[str, Any]:
    request_id = str(_param(params, "requestId", "request_id") or "")
    if not REQUEST_ID_RE.match(request_id):
        raise ValueError("request_id invalide.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Fichier trop volumineux : maximum {MAX_UPLOAD_BYTES // 1024 // 1024} Mo."
        )
    mime, kind = sniff_mime(_octets_de_jugement(data), name)
    digest = _sha256(data)
    dossier = _staged_dir()
    # Nom = empreinte + extension. L'empreinte seule nommerait le fichier
    # `<sha>.bin` et le type serait perdu : la vignette ne pourrait plus distinguer
    # un PNG d'un tableur. L'extension vient de la SIGNATURE, pas du nom d'origine.
    suffix = Path(name).suffix.lower() or _suffix_from_mime(mime)
    nom_stade = f"{digest}{suffix}"
    for ancien in dossier.glob(f"{digest}.*"):
        if ancien.name != nom_stade:
            try:
                ancien.unlink()
            except OSError:
                pass
    # Viser un nom définitif TRONQUÉ est le défaut que ce chemin ferme : un
    # ENOSPC ou un crash ne doit pas laisser `<sha>.pdf` partiel derrière lui.
    _ecrire_fichier_atomique(dossier / nom_stade, data)
    return {
        "stage_item_id": f"sti_{digest[:16]}",
        "sha256": digest,
        "name": name,
        "mime": mime,
        "kind": kind,
        "size_bytes": len(data),
        "draft_id": str(_param(params, "draftId", "draft_id") or ""),
    }


def stage_dropped_paths(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Stage des fichiers choisis dans le système, par chemin absolu."""
    raw = _param(params, "paths", "files") or []
    if not isinstance(raw, list):
        raise ValueError("Liste de chemins illisible.")
    if not raw:
        raise ValueError("Aucun fichier à traiter.")
    if len(raw) > MAX_ITEMS_PER_CALL:
        raise ValueError(f"Maximum {MAX_ITEMS_PER_CALL} fichiers par appel.")

    items: list[dict[str, Any]] = []
    rejets: list[dict[str, Any]] = []
    for chemin in raw:
        nom = ""
        try:
            brut = Path(os.path.expanduser(str(chemin)))
            if _composants_caches(brut):
                raise ValueError(
                    "Chemin caché refusé : un dépôt ne lit pas un dossier de configuration."
                )
            # Résolution AVANT lecture : un lien symbolique sortant est refusé,
            # et le parent résolu sert de racine de confinement.
            parent = _racine_confinee(brut.parent)
            source = _confine(parent, brut)
            if _composants_caches(source) or not _sous_racine_autorisee(source):
                raise ValueError(
                    "Chemin hors des racines autorisées (dossier personnel, /media, "
                    "/mnt, /tmp) : refus."
                )
            nom = source.name
            # `stat()` et la taille passent AVANT `read_bytes()` : un fichier
            # géant est refusé sans être chargé en mémoire.
            if not source.is_file():
                raise ValueError("Ce chemin ne désigne pas un fichier.")
            if source.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"Fichier trop volumineux : maximum {MAX_UPLOAD_BYTES // 1024 // 1024} Mo."
                )
            data = source.read_bytes()
            items.append(_ecrire_stage(db, params, data, nom))
        except ValueError as exc:
            rejets.append({"name": nom or Path(str(chemin)).name, "message": str(exc)})
        except OSError as exc:
            rejets.append(
                {
                    "name": nom or Path(str(chemin)).name,
                    "message": f"Lecture impossible : {exc.strerror}.",
                }
            )
    return {"items": items, "rejected": rejets}


def stage_picker_bytes(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Stage un fichier choisi via le sélecteur natif, transmis en base64.

    Le frontend vérifie `file.size` AVANT `arrayBuffer()` et envoie une chaîne
    base64. Le backend estime la taille décodée AVANT `b64decode` : un fichier
    de 21 Mio est donc refusé sans jamais être matérialisé en octets.
    """
    name = _nom_securise(_param(params, "filename", "name"))
    data = _octets(_param(params, "bytes", "data"), max_bytes=MAX_UPLOAD_BYTES)
    if not data:
        raise ValueError("Fichier vide.")
    return {"items": [_ecrire_stage(db, params, data, name)], "rejected": []}


def get_staged_preview_bytes(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Aperçu immédiat d'un fichier stagé, transmis en base64.

    `emit` sérialise la réponse en JSON (`json.dumps`) : des `bytes` Python y
    lèvent `TypeError` et transforment chaque aperçu en `handler_error`. Le
    champ `data` porte donc une CHAÎNE base64 — sérialisable, et décodée par le
    renderer. `encoding` rend le contrat explicite au lieu de laisser le
    frontend deviner.
    """
    token = str(_param(params, "path", "stageItemId", "stage_item_id", "token") or "")
    match = STAGE_ITEM_RE.match(token) or re.match(r"^sti_([0-9a-f]{8,64})$", token)
    prefix = match.group(1) if match else None
    if not prefix:
        raise ValueError("Jeton de staging invalide.")
    dossier = _staged_dir()
    trouve = None
    for candidat in sorted(dossier.glob(f"{prefix}*")):
        if candidat.is_file():
            trouve = candidat
            break
    if trouve is None:
        raise ValueError("Fichier stagé introuvable.")
    resolu = _confine(dossier, trouve)
    data = resolu.read_bytes()
    if len(data) > MAX_PREVIEW_BYTES:
        data = data[:MAX_PREVIEW_BYTES]
    mime = ALLOWED_SUFFIXES.get(resolu.suffix.lower(), "application/octet-stream")
    return {
        "path": resolu.name,
        "name": resolu.name,
        "data": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
        "mime": mime,
    }


# ── Promotion : staged -> blob + artifact + lien ─────────────────────────────


def _digest_stage_verifie(source: Path, data: bytes, token: str, prefix: str) -> str:
    """Empreinte du contenu stagé, refusée si elle ne correspond pas au jeton.

    Le nom du fichier stagé est l'empreinte COMPLÈTE ; le jeton n'en garde que
    le préfixe. Un fichier tronqué (crash, ENOSPC) a un nom intact mais un
    contenu différent : le recalculer et le comparer au jeton est le seul
    contrôle qui le distingue d'un fichier sain.
    """
    digest = _sha256(data)
    if not digest.startswith(prefix):
        raise ValueError(
            f"Fichier stagé tronqué ou altéré ({token}) : l'empreinte du contenu "
            f"({digest[:16]}) ne correspond pas au jeton de staging."
        )
    if source.stem != digest:
        raise ValueError(
            f"Fichier stagé incohérent ({token}) : son nom ne porte pas l'empreinte "
            "de son contenu."
        )
    return digest


def artifact_ingestion_retry(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Relance l'extraction d'UN document du dépôt, à la demande de l'utilisateur.

    La cible est un identifiant d'artefact, jamais un chemin ni un choix de modèle :
    `modeles_autorises=True` est décidé ICI, côté serveur, parce que l'utilisateur
    a demandé une extraction et que le bouton ne peut pas savoir quel extracteur
    sera nécessaire.

    Ce qui est vérifié avant d'enfiler : l'artefact existe et n'est pas supprimé,
    il est partagé dans un espace ACTIF (lien `workspace` non retiré, espace non
    en corbeille), et son état est une dette (`depose`, `echec`, `partiel`,
    `tronque`). `extrait`, `vide`, `binaire_refuse` et `en_cours` sont refusés
    avec une raison lisible — relancer ne changerait pas leur verdict, ou
    usurperait le propriétaire déjà au travail.

    SINGLE-FLIGHT : si l'artefact est en file, en cours ou couvert par un claim
    frais, la réponse est `accepted: False`, jamais un faux « relancé ».
    """
    from backend.ingestion import attachment_ingest as _ing
    from backend.ingestion import etat as _etat

    artifact_id = _param(params, "artifactId", "artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError(
            "[ARTIFACT_REQUIRED] La relance exige le document visé : "
            "l'identifiant manque."
        )
    artifact_id = artifact_id.strip()

    artefact = (
        db.execute(
            text("SELECT id, ingest_state, deleted_at FROM artifacts WHERE id = :id"),
            {"id": artifact_id},
        )
        .mappings()
        .first()
    )
    if artefact is None or artefact["deleted_at"] is not None:
        raise LookupError("[ARTIFACT_NOT_FOUND] Document introuvable ou supprimé.")

    lien = (
        db.execute(
            text(
                "SELECT l.scope_id AS workspace_id, w.state AS workspace_state"
                " FROM artifact_links l JOIN workspaces w ON w.id = l.scope_id"
                " WHERE l.artifact_id = :id AND l.scope_kind = 'workspace'"
                "   AND l.removed_at IS NULL"
                " ORDER BY l.created_at ASC, l.id ASC LIMIT 1"
            ),
            {"id": artifact_id},
        )
        .mappings()
        .first()
    )
    if lien is None:
        raise LookupError(
            "[LINK_NOT_FOUND] Ce document n'est pas partagé dans un espace actif."
        )
    if str(lien["workspace_state"]) != "active":
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")

    etat = (str(artefact["ingest_state"] or "")).strip().lower() or _etat.ETAT_INCONNU

    def refus(raison: str) -> dict[str, Any]:
        return {
            "ok": True,
            "accepted": False,
            "artifact_id": artifact_id,
            "state": etat,
            "reason": raison,
        }

    if etat == _etat.ETAT_EN_COURS:
        return refus("extraction déjà en cours : un propriétaire détient le document.")
    if etat == _etat.ETAT_EXTRAIT:
        return refus("extraction déjà aboutie : il n'y a rien à relancer.")
    if etat == _etat.ETAT_VIDE:
        return refus(
            "aucun texte n'a été reconnu : relancer l'extraction ne produirait pas "
            "un autre verdict."
        )
    if etat == _etat.ETAT_BINAIRE:
        return refus(
            "contenu non textuel : aucun extracteur n'en tirera du texte, et le "
            "réessai ne ferait que réécrire le même refus."
        )
    if etat not in (
        _etat.ETAT_DEPOSE,
        _etat.ETAT_ECHEC,
        _etat.ETAT_PARTIEL,
        _etat.ETAT_TRONQUE,
    ):
        return refus(
            f"état « {etat} » non relançable : relancer l'extraction n'y changerait rien."
        )

    if _ing.execution_en_cours(artifact_id):
        return refus("extraction déjà en file ou en cours : rien n'a été relancé.")

    # Aucune transaction ouverte pendant l'admission : le worker ouvre ses propres
    # sessions, et une lecture non refermée bloquerait SQLite sous écriture.
    db.rollback()
    enfiles = _ing.lancer_ingestion([artifact_id], modeles_autorises=True)
    if not enfiles:
        return refus("extraction déjà en file : rien n'a été relancé.")
    return {
        "ok": True,
        "accepted": True,
        "artifact_id": artifact_id,
        "state": etat,
    }


def attachments_prepare(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Promeut des fichiers stagés en pièces jointes persistées.

    Deux destinations, et UNE seule à la fois : une conversation, ou le dépôt d'un
    espace. Le dépôt d'un espace crée un lien `scope_kind='workspace'` : le fichier
    devient alors lisible par TOUTES les discussions de cet espace, parce que
    `scope.py:92-142` le déduit de la conversation et non d'un identifiant fourni
    ici. Un fichier versé dans l'espace ne passe par aucune discussion.

    `names` transporte le nom d'origine, que le staging perd (il écrit
    `<sha256><ext>`, le seul nom qui survit au redémarrage). Ce nom est une
    ÉTIQUETTE : il ne touche jamais le disque — les blobs restent adressés par
    leur `sha256` — ni aucune autorisation. Son absence est tolérée et retombe sur
    le nom dérivé. Fourni, il est ALIGNÉ sur `stageItemIds` : un tableau plus
    court est refusé, car l'entrée manquante afficherait l'empreinte que ce
    paramètre sert à-tenir hors de l'écran.

    Tout ou rien : les promotions et leurs écritures sont dans UNE transaction.
    Une pièce jointe à moitié écrite est pire qu'une pièce jointe absente, parce
    qu'elle laisse une ligne orpheline que personne ne saura expliquer.
    """
    from backend.ipc.paths_bridge import blobs_root

    conversation_id = str(
        _param(params, "conversationId", "conversation_id") or ""
    ).strip()
    workspace_id = str(_param(params, "workspaceId", "workspace_id") or "").strip()
    ids = _param(params, "stageItemIds", "stage_item_ids") or []
    if not isinstance(ids, list) or not ids:
        raise ValueError("Aucune pièce jointe à préparer.")
    staged = _staged_dir()
    ids_artefacts: list[str] = []
    nouveaux_blobs: dict[str, Path] = {}
    try:
        if conversation_id and workspace_id:
            raise ValueError(
                "Une pièce jointe va à une discussion OU au dépôt d'un espace, jamais aux deux."
            )
        if not conversation_id and not workspace_id:
            raise ValueError("conversation_id ou workspace_id manquant.")
        if len(ids) > MAX_ITEMS_PER_CALL:
            raise ValueError(f"Maximum {MAX_ITEMS_PER_CALL} pièces jointes par appel.")
        if workspace_id:
            # AVANT la moindre écriture : un dépôt annoncé dans un espace qui n'existe
            # pas laisserait un fichier orphelin que l'utilisateur ne saura jamais
            # retrouver, et il croirait le fichier déposé dans cet espace.
            from backend.db.models import Workspace as _Ws

            ws = (
                db.query(_Ws)
                .filter(_Ws.id == workspace_id, _Ws.state == "active")
                .first()
            )
            if ws is None:
                raise LookupError(
                    "[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille."
                )
        noms_recus = _param(params, "names") or []
        if not isinstance(noms_recus, list):
            raise ValueError(
                "`names` illisible : une liste de noms d'origine, ou rien."
            )
        if noms_recus and len(noms_recus) != len(ids):
            raise ValueError(
                f"`names` compte {len(noms_recus)} entrées pour {len(ids)} pièces "
                "jointes : un nom par pièce jointe, dans le même ordre."
            )
        # Un nom par pièce jointe, décision prise ICI. Une entrée manquante se
        # rabattait autrefois sur le nom dérivé — l'empreinte sha256 affichée en
        # clair, ce que ce paramètre existe pour empêcher — et le passage sans bruit.
        noms = [nom if isinstance(nom, str) else "" for nom in noms_recus] or [
            ""
        ] * len(ids)

        blobs = blobs_root()

        maintenant = datetime.now(timezone.utc).isoformat()
        # L'horodatage de l'ÉTAT d'ingestion est naïf UTC, et non ISO aware comme le
        # `created_at` ci-dessus : deux horloges dans la même promotion obligeraient
        # chaque lecteur à deviner laquelle comparer, et l'âge d'un `en_cours` mal lu
        # fait rejouer une extraction encore en train. `etat.utc_naif` est la seule
        # écriture acceptable ; on l'appelle plutôt que de la réécrire.
        from backend.ingestion.etat import utc_naif

        maintenant_etat = utc_naif()
        préparés: list[dict[str, Any]] = []
        for index, brut in enumerate(ids):
            token = str(brut)
            match = STAGE_ITEM_RE.match(token)
            if not match:
                raise ValueError(f"Jeton de staging invalide : {token}")
            prefix = match.group(1)
            source = next(
                (c for c in sorted(staged.glob(f"{prefix}*")) if c.is_file()), None
            )
            if source is None:
                raise ValueError(f"Fichier stagé introuvable : {token}")
            source = _confine(staged, source)
            data = source.read_bytes()
            digest = _digest_stage_verifie(source, data, token, prefix)
            # L'extension vient de la SIGNATURE, lue sur les octets : le nom du fichier
            # stagé n'est pas fiable pour dire ce qu'il contient.
            mime, kind = sniff_mime(_octets_de_jugement(data), source.name)
            préparés.append(
                {
                    "digest": digest,
                    "data": data,
                    "mime": mime,
                    "kind": kind,
                    # Le nom d'origine est celui que l'utilisateur reconnaît. Son nom de
                    # fichier ne l'est plus : le staging n'a gardé que l'empreinte.
                    "name": _nom_depotable(noms[index].strip()) or source.name,
                }
            )

        for item in préparés:
            digest = item["digest"]
            cible = _confine_dir(blobs, digest[:2]) / f"{digest}.bin"
            # La ligne `blobs` est lue AVANT l'écriture : elle dit si cette
            # adresse appartient déjà à un document partagé. Un fichier réutilisé
            # par une entrée existante ne sera jamais supprimé par la compensation
            # d'un échec de transaction.
            existe = db.execute(
                text("SELECT id FROM blobs WHERE sha256 = :sha"), {"sha": digest}
            ).first()
            ecriture = _ecrire_blob_atomique(cible, item["data"], digest)
            if ecriture["written"]:
                # Même si une ligne `blobs` préexiste (état incohérent : fichier
                # manquant pour une ligne vivante), le fichier vient d'être
                # écrit et doit pouvoir être compensé. `_compenser_blobs`
                # relit la base APRÈS le rollback : une ligne partagée le
                # protège, une absence le supprime.
                nouveaux_blobs[digest] = cible
            # Un blob est ADRESSÉ PAR SON CONTENU : le même fichier déposé deux fois
            # partage une seule ligne et un seul fichier sur le disque. Sans cela la
            # base gonflerait à chaque transfert du même document.
            if existe:
                blob_id = existe[0]
            else:
                blob_id = _nouveau_id("blob")
                db.execute(
                    text(
                        "INSERT INTO blobs (id, sha256, relative_path, size_bytes, mime_detected,"
                        " created_at, state) VALUES (:id, :sha256, :rel, :size, :mime, :now, 'ready')"
                    ),
                    {
                        "id": blob_id,
                        "sha256": digest,
                        "rel": f"blobs/{digest[:2]}/{digest}.bin",
                        "size": len(item["data"]),
                        "mime": item["mime"],
                        "now": maintenant,
                    },
                )
            artifact_id = _nouveau_id("art")
            # `ingest_state` est écrit ICI, explicitement, même si le défaut de la
            # colonne vaut déjà `depose` : le dépôt est le moment où la dette naît, et
            # le dire dans l'écriture rend la valeur lisible dans la même phrase que
            # le reste de la promotion. Le dire aussi dans le modèle (`server_default`)
            # protège les écritures brutes qui n'iraient pas par ici — pas le dépôt
            # réel, qui n'a plus à s'en soucier.
            db.execute(
                text(
                    "INSERT INTO artifacts (id, blob_id, kind, original_filename,"
                    " display_name, ingest_state, ingest_reason, ingest_updated_at,"
                    " created_at) VALUES (:id, :blob, :kind, :nom, :nom, 'depose',"
                    " :raison, :maintenant_etat, :now)"
                ),
                {
                    "id": artifact_id,
                    "blob": blob_id,
                    "kind": item["kind"],
                    "nom": item["name"],
                    "raison": "déposé, extraction en attente",
                    "maintenant_etat": maintenant_etat,
                    "now": maintenant,
                },
            )
            # `artifact_links` est générique : pas de colonne `conv_id` mais
            # `scope_kind` + `scope_id`. C'est ce couple qui rattache l'artefact à
            # sa portée, et c'est ainsi que la galerie et le dépôt le lisent ensuite.
            db.execute(
                text(
                    "INSERT INTO artifact_links (id, artifact_id, scope_kind, scope_id,"
                    " role, created_at) VALUES (:id, :artifact, :kind, :portee,"
                    " :role, :now)"
                ),
                {
                    "id": _nouveau_id("art"),
                    "artifact": artifact_id,
                    "kind": "conversation" if conversation_id else "workspace",
                    "portee": conversation_id or workspace_id,
                    "role": "attachment" if conversation_id else "shared",
                    "now": maintenant,
                },
            )
            ids_artefacts.append(artifact_id)

        # Le label public (`doc_N`) est alloue ICI, a la promotion. Sans cela il
        # n existerait qu'apres qu un affichage l aurait declenche par effet de
        # bord : l orchestrateur recoit un `doc_1` refuse alors que la piece
        # jointe est parfaitement valide. Allouer a l ecriture rend l etiquette
        # existente par construction, pas par chance de navigation.
        # Un depot d'espace n'en recoit pas : `doc_1` designe une piece jointe
        # dans le tour courant d'une conversation, et un fichier de depot n'a pas
        # de tour. Lui donner un `doc_N` le ferait passer pour ce qu'il n'est pas.
        if conversation_id:
            from backend.search import docrefs

            for artefact in ids_artefacts:
                docrefs.allocate_doc_label(db, conversation_id, artefact)
        db.commit()
    except Exception:
        rollback_ok = False
        try:
            db.rollback()
            rollback_ok = True
        except Exception as rollback_exc:
            log.error(
                "[attachments] rollback impossible après échec de promotion : %s",
                rollback_exc,
            )
        if rollback_ok and nouveaux_blobs:
            _compenser_blobs(nouveaux_blobs, db)
        raise
    finally:
        # Le staging est un tampon, pas une archive : il disparaît, sinon
        # `data/temp` grossit sans borne. Le périmètre est celui de l'appel —
        # ses jetons reçus, promotion ou échec — parce qu'un lot qui échoue ne
        # peut rien nettoyer derrière lui, et qu'un jeton reçu par un autre
        # appel n'appartient pas à celui-ci.
        for token in ids:
            _purger_stage(staged, str(token))
    _declencher_ingestion(ids_artefacts)
    # PAS de `items` ici. Le dépôt n'a que faire de recharger l'inventaire par
    # `espace_depot`, qui rend le `display_name` relu en base — et l'autre
    # appelant, le chat, fait `prep.items.map(i => i.artifactId)` : des clés
    # snake_case y vaudraient `undefined`, `.map` réussirait, sa longueur
    # vaudrait 1, et son repli `image_paths` ne se déclencherait plus. Un fichier
    # joint disparaîtrait en silence. Aucune clé que personne ne lit.
    return {
        "ok": True,
        "artifact_ids": ids_artefacts,
        "count": len(ids_artefacts),
        "scope_kind": "conversation" if conversation_id else "workspace",
    }


def _declencher_ingestion(ids_artefacts: list[str]) -> None:
    """Lance l'extraction de texte en arrière-plan, après la promotion.

    Le dépôt a déjà répondu : l'utilisateur voit son fichier immédiatement.
    L'extraction suit parce qu'un PDF de vingt pages demande plusieurs minutes
    d'OCR, et bloquer le dépôt donnerait l'impression d'un gel sans laisser
    aucune trace du fichier.
    """
    try:
        from backend.ingestion import attachment_ingest as _ing

        _ing.lancer_ingestion(list(ids_artefacts))
    except Exception:
        # Le dépôt a réussi ; une ingestion qui ne démarre pas ne doit pas
        # annuler cela. Le fichier reste consultable, simplement non indexé.
        traceback.print_exc()


def get_attachment_bytes(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Renvoie `{relative_path, mime}` pour un id d'artefact.

    Les OCTETS ne passent pas ici : les sérialiser en JSON `number[]` sur stdout
    coûterait plusieurs mégaoctets par image. Le processus principal résout le
    chemin par cette commande, puis lit le fichier lui-même.
    """
    artifact_id = _param(params, "artifactId", "artifact_id", "id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("Pièce jointe introuvable")
    row = (
        db.execute(
            text(
                "SELECT b.relative_path, b.mime_detected, b.size_bytes"
                " FROM artifacts a JOIN blobs b ON b.id = a.blob_id"
                " WHERE a.id = :id AND a.deleted_at IS NULL"
            ),
            {"id": artifact_id},
        )
        .mappings()
        .first()
    )
    if row is None or not row["relative_path"]:
        raise ValueError("Pièce jointe introuvable")
    return {
        "relative_path": row["relative_path"],
        "mime": row["mime_detected"] or "application/octet-stream",
        "size_bytes": int(row["size_bytes"] or 0),
    }


def _confine_dir(root: Path, sous_dossier: str) -> Path:
    racine = _racine_confinee(root)
    cible = racine / sous_dossier
    cible.mkdir(parents=True, exist_ok=True)
    resolu = cible.resolve()
    if racine not in resolu.parents:
        raise ValueError("Dossier de sortie hors racine.")
    return resolu


def _nouveau_id(prefixe: str) -> str:
    return f"{prefixe}_{uuid.uuid4().hex[:16]}"


# Ce qu'un nom affiché ne doit pas porter : un séparateur de chemin le ferait
# passer pour une adresse, un caractère de contrôle le ferait déborder de sa
# ligne, U+202E le ferait lire à l'envers (« facture‮gpj.exe.pdf » s'affiche
# « facture.pdf »). C'est un risque d'AFFICHAGE seul — l'octet reste dans le
# blob, adressé par son empreinte, et le nom ne touche jamais le disque.
_CARACTERES_A_RETIRER_DU_NOM = re.compile(r"[/\\\n\r\0‮]")


def _nom_depotable(nom: str) -> str:
    """Le nom sous lequel un fichier sera présenté, s'il vaut la peine d'être montré.

    Un nom d'origine est une étiquette, pas une adresse : il ne sert jamais à
    retrouver le fichier, qui reste adressé par son empreinte. On retire donc de
    l'étiquette ce qui la ferait passer pour autre chose qu'elle-même, et on garde
    tout le reste.

    RETIRER, plutôt que garder la queue : tronquer au dernier séparateur
    transformerait « notes du 12/03.txt » en « 03.txt », et deux fichiers datés
    deviendraient indistinguables sur l'écran — les artefacts, eux, restant
    distincts.

    RETIRER, plutôt que refuser : un refus retomberait sur le nom dérivé du
    fingerprint, c'est-à-dire l'empreinte affichée en clair — exactement ce que
    `names` existe pour empêcher.
    """
    if not nom or len(nom) > 200:
        return ""
    nom = _CARACTERES_A_RETIRER_DU_NOM.sub("", nom).strip().strip(".")
    return nom if 0 < len(nom) <= 200 else ""


__all__ = [
    "get_attachment_bytes",
    "save_upload",
    "stage_dropped_paths",
    "stage_picker_bytes",
    "attachments_prepare",
    "get_staged_preview_bytes",
    "purger_staging_expires",
    "sniff_mime",
    "MAX_UPLOAD_BYTES",
    "MAX_PREVIEW_BYTES",
]

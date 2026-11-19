# services/image_service.py — Service image commun Phase 2 (C07/C08/C10/C11/C13)
# QUI APPELLE : outil chat `generate_image` (via loop.py) OU studio direct (via main.py
# `image.generate`). Les deux passent par CE service, le même registre Artifact/Blob/
# ArtifactLink et le même ordonnancement — seul le chat passe par l'orchestrateur.
# POURQUOI UN SERVICE COMMUN : une seule politique de validation, d'idempotence, de
# publication et de provenance, quel que soit le point d'entrée (phase_2 + backend_images.md).
from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable

FORMATS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "3:2": (1248, 832),
    "2:3": (832, 1248),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "5:4": (1120, 896),
    "4:5": (896, 1120),
    "21:9": (1568, 672),
    "9:21": (672, 1568),
}

PROMPT_MAX_CHARS = 2000
REFERENCES_MAX = 4
TOTAL_IMAGES_MAX = 4
MAX_SEED = (1 << 32) - 1

_UNAVAILABLE_REASON = (
    "Backend image FLUX.2 Klein 9B indisponible : activez THOT_IMAGE_BACKEND=flux2 "
    "ou l'interrupteur image, puis vérifiez les composants et CUDA. "
    "La génération T2I n'est pas exécutée sans cet opt-in."
)


def is_available() -> tuple[bool, str]:
    try:
        from backend.services import flux_spike as _fx

        return _fx.is_available()
    except Exception:
        return False, _UNAVAILABLE_REASON


def check_install() -> tuple[bool, str]:
    """Opt-in + poids + CUDA. Indépendant de la mémoire : ne coûte aucun déchargement."""
    try:
        from backend.services import flux_spike as _fx

        return _fx.check_install()
    except Exception:
        return False, _UNAVAILABLE_REASON


def check_vram() -> tuple[bool, str]:
    """Marge VRAM. À appeler APRÈS la libération du modèle texte (voir `generate`)."""
    try:
        from backend.services import flux_spike as _fx

        return _fx.check_vram()
    except Exception:
        return False, _UNAVAILABLE_REASON


# --- Détecteur de REVENDICATION d'image (garde anti-mensonge du générateur) ---
# Consumer : loop.py, qui retire au texte final toute promesse d'image non publiée
# ("j'ai généré", "le rendu que j'ai produit"). Ce n'est PAS un détecteur d'intention :
# il ne décide jamais de lancer une diffusion, il constate une affirmation. La décision
# de créer appartient au seul orchestrateur (voir plan_media_tools, loop.py).
# Deux entrées, un seul lexique :
#   * ce qu'ON PEINT — le sujet de la demande (« une photo de papillon ») ;
#   * ce qui EN SORT — un rendu de pipeline 3D/scène. « Crée un rendu du tableau de
#     bord » ne demande PAS une image (faux positif coûteux : une vraie diffusion de
#     60 à 600 s), mais « le rendu que j'ai produit » est une promesse d'image bien
#     réelle : le mot doit rester, ici, dans le détecteur de revendication.
IMAGE_CLAIM_NOUNS = (
    r"image|photo|photographie|illustration|visuel|paysage|portrait|affiche|rendu"
)
IMAGE_CLAIM_NOUN_RE = re.compile(r"\b(?:" + IMAGE_CLAIM_NOUNS + r")s?\b", re.IGNORECASE)


def stable_key(
    prompt: str, aspect_ratio: str, seed: int | None, reference_ids: list[str]
) -> str:
    """Clé idempotente stable C09/C11 : run_id + logical_action_id + output_index la portent."""
    payload = json.dumps(
        {
            "p": (prompt or "").strip(),
            "f": aspect_ratio,
            "s": seed,
            "r": list(reference_ids or []),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_seed(seed: int | None) -> int:
    if seed is None:
        return secrets.randbits(32)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise ValueError(f"[BAD_ARGS] seed doit être un entier entre 0 et {MAX_SEED}.")
    return seed


def validate_request(
    prompt: str,
    aspect_ratio: str,
    reference_ids: list[str] | None,
    seed: int | None = None,
    mask_kind=None,
    mode=None,
) -> tuple[str, str, list[str], int]:
    mode_norm = (mode or "").strip().lower()
    # Mode-less callers keep the historical behaviour (empty prompt rejected).
    p = (prompt or "").strip()
    if not p:
        if mode_norm in ("inpaint", "outpaint"):
            p = "Reconstruct and extend naturally the masked area matching the surrounding scene."
        else:
            raise ValueError("[EMPTY_QUERY] Décris l'image à créer (prompt vide).")
    if len(p) > PROMPT_MAX_CHARS:
        raise ValueError(f"[TOO_LONG] Prompt limité à {PROMPT_MAX_CHARS} caractères.")
    f = (aspect_ratio or "1:1").strip()
    if f not in FORMATS:
        raise ValueError(f"[ENUM_INVALID] aspect_ratio parmi {', '.join(FORMATS)}.")
    raw_refs = (
        [reference_ids] if isinstance(reference_ids, str) else (reference_ids or [])
    )
    refs = [str(r).strip() for r in raw_refs if str(r or "").strip()]
    if len(refs) > REFERENCES_MAX:
        raise ValueError(
            f"[TOO_MANY] Max {REFERENCES_MAX} références (0-4, profil à reproduire)."
        )
    if isinstance(mask_kind, str):
        mk = mask_kind.strip()
        mask_kind = mk.lower() if mk else None
    if mask_kind is not None and mask_kind not in ("fill", "edit"):
        raise ValueError(
            "[BAD_ARGS] [MASK_KIND_MISMATCH] mask_kind doit être 'fill' ou 'edit'."
        )
    return p, f, refs, validate_seed(seed)


def check_references(db, reference_ids: list[str], scope_id: str | None) -> list[dict]:
    """Vérifie chaque référence : Artifact existe, Blob présent, portée accessible.

    Ne substitue jamais silencieusement : référence absente/interdite → ValueError ID_UNKNOWN
    ou SCOPE_DENIED, jamais une génération sans référence à la place.
    """
    from backend.db.models import Artifact, ArtifactLink, Blob

    out: list[dict] = []
    for rid in reference_ids or []:
        art = (
            db.query(Artifact)
            .filter(Artifact.id == rid, Artifact.deleted_at.is_(None))
            .first()
        )
        if not art:
            # Tente résolution doc_N -> artifact via doc_refs (confort, pas devinette LLM)
            resolved = _resolve_doc_label(db, rid, scope_id)
            if resolved is None or resolved.deleted_at is not None:
                raise ValueError(f"[ID_UNKNOWN] Référence inconnue : {rid}.")
            art = resolved
        blob = db.query(Blob).filter(Blob.id == art.blob_id).first()
        if not blob:
            raise ValueError(f"[ID_UNKNOWN] Blob manquant pour {rid}.")
        if scope_id:
            link = (
                db.query(ArtifactLink)
                .filter(
                    ArtifactLink.artifact_id == art.id,
                    ArtifactLink.scope_kind == "conversation",
                    ArtifactLink.scope_id == scope_id,
                    ArtifactLink.removed_at.is_(None),
                )
                .first()
            )
            if not link:
                raise ValueError(
                    f"[SCOPE_DENIED] Référence {rid} hors portée accessible."
                )
        if (art.kind or "") != "image":
            raise ValueError(f"[BAD_ARGS] Référence {rid} non image (kind={art.kind}).")
        out.append({"artifact_id": art.id, "kind": art.kind, "blob_id": blob.id})
    return out


def _resolve_doc_label(db, label: str, conv_id: str | None):
    """doc_N (conversation) -> Artifact. None si introuvable."""
    from backend.db.models import Artifact, DocRef

    if not label.startswith("doc_") or not conv_id:
        return None
    ref = (
        db.query(DocRef)
        .filter(
            DocRef.conv_id == conv_id,
            DocRef.doc_label == label,
            DocRef.removed_at.is_(None),
        )
        .first()
    )
    if not ref:
        return None
    return db.query(Artifact).filter(Artifact.id == ref.artifact_id).first()


def _card_quality(db, artifact_id: str) -> dict:
    """Provenance qualité de la fiche courante."""
    quality: str | None = None
    try:
        from backend.db.models import ArtifactCard

        card = (
            db.query(ArtifactCard)
            .filter(
                ArtifactCard.artifact_id == artifact_id, ArtifactCard.is_current == 1
            )
            .first()
        )
        if card and card.summary:
            prov = json.loads(card.summary)
            if isinstance(prov, dict) and isinstance(prov.get("quality"), str):
                quality = prov["quality"]
    except Exception:
        quality = None
    deprecated = bool(quality) and quality.startswith("UNVALIDATED")
    experimental = deprecated or (bool(quality) and "experimental" in quality.lower())
    return {"quality": quality, "experimental": experimental, "deprecated": deprecated}


def link_artifact_to_conversation(
    db, artifact_id: str, conversation_id: str | None
) -> bool:
    """Rattache un artefact DÉJÀ publié à UNE conversation. True si lien créé.

    Même écriture que la publication (ArtifactLink conversation/generated), sans
    republier le PNG : un rejeu doit coûter ZÉRO diffusion. Sans ce lien, un
    rejeu est invisible depuis la conversation qui l'a demandé et l'artefact
    reste porté par la seule conversation d'origine.
    """
    if not artifact_id or not conversation_id:
        return False
    from backend.db.models import ArtifactLink

    if (
        db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == artifact_id,
            ArtifactLink.scope_kind == "conversation",
            ArtifactLink.scope_id == conversation_id,
        )
        .first()
    ):
        return False
    db.add(
        ArtifactLink(
            id=f"lnk_{artifact_id}_{conversation_id}",
            artifact_id=artifact_id,
            scope_kind="conversation",
            scope_id=conversation_id,
            role="generated",
        )
    )
    db.flush()
    return True


def list_generated_images(
    db, scope_id: str | None = None, include_imports: bool = False, limit: int = 50
) -> list[dict]:
    """Galerie = VUE du registre Artifact (C10), jamais une table dupliquée.

    Par défaut : créations image (kind=image, scope library/conversation). Imports visibles
    seulement sur filtre explicite ; caches/extractions exclus.
    """
    from backend.db.models import Artifact, ArtifactLink, ArtifactCard

    limit = max(1, min(int(limit or 50), 100))
    q = db.query(Artifact).filter(
        Artifact.kind == "image", Artifact.deleted_at.is_(None)
    )
    rows = q.order_by(Artifact.created_at.desc()).limit(limit * 3).all()
    out: list[dict] = []
    for art in rows:
        links = (
            db.query(ArtifactLink)
            .filter(
                ArtifactLink.artifact_id == art.id, ArtifactLink.removed_at.is_(None)
            )
            .all()
        )
        roles = {(l.scope_kind or "", l.role or "") for l in links}
        is_import = any(r == ("conversation", "attachment") for r in roles) and not any(
            s == "library" for s, _ in roles
        )
        if is_import and not include_imports:
            continue
        if scope_id and not any(l.scope_id == scope_id for l in links):
            continue
        out.append(
            {
                "artifact_id": art.id,
                "display_name": art.display_name,
                "original_filename": art.original_filename,
                "kind": art.kind,
                "created_at": art.created_at.isoformat() if art.created_at else None,
                "scopes": [
                    {"scope_kind": l.scope_kind, "scope_id": l.scope_id, "role": l.role}
                    for l in links
                ],
                "is_import": is_import,
                **_card_quality(db, art.id),
            }
        )
        if len(out) >= limit:
            break
    return out


def _rendre_la_vram_du_worker_ocr() -> str:
    """Arrête le worker PaddleOCR et rend sa VRAM. Renvoie ce qui a été fait.

    APPELÉ AVANT `check_vram`, jamais après : le worker est un occupant
    RÉSIDENT (≈ 1,9 Go de VRAM entre deux requêtes). Sonder la marge sans l'avoir
    rendu, c'est mesurer la carte dans un état que la diffusion ne verra pas.

    CE QUE LA MESURE DU 05/10/2026 CHANGE, ET CE QU'ELLE NE CHANGE PAS. Le pic de
    diffusion mesuré est descendu de « ~7,9 Go » — un chiffre qui ne venait
    d'aucune mesure de ce code — à **3,06 Go** (`flux_spike.PIC_VRAM_MESURE_GB`,
    1024², deux rejets identiques). L'addition 1,9 + 3,06 = 4,96 Go tient donc sous
    les 8 Go de la Bible §1.4 : **la violation cumulée que ce paragraphe citait
    n'est plus établie par les chiffres.** La fonction reste, pour trois raisons
    qui ne dépendent pas de cette addition :

    * la RÈGLE, qui est de séquence : un modèle LOURD en VRAM à la fois
      (`models/README.md` §2). Le worker Paddle et la diffusion sont deux modèles
      lourds ; les faire cohabiter tient parce que la somme passe, pas parce
      qu'elle est permise, et « ça passe aujourd'hui » n'est pas une permission ;
    * le SEUIL, qui est dérivé de la diffusion SEULE (`PIC_VRAM_MESURE_GB` + marge
      0,5). Sonder la carte pendant que l'occupant qu'on va évincer est encore là,
      c'est faire dépendre l'admission d'un occupant que le run suivant n'aura pas ;
    * et l'incertitude de mesure : 3,06 Go est un pic mesuré sur CETTE carte, avec
      CETTE version. Une admission qui tiendrait à 1 Go près ne tiendrait pas sur
      une autre machine, et le refus ci-dessous est le seul qui dise que non.

    LE REFUS EST FRANC. Si le worker ne peut pas être arrêté, on ne diffuse pas
    « en espérant que ça passe » : le budget est la contrainte de conception
    principale, et une génération non conforme ne vaut pas une image.
    """
    from backend.ingestion import paddle_client

    try:
        rapport = paddle_client.liberer_pour_diffusion()
    except paddle_client.WorkerOccupe as exc:
        raise RuntimeError(
            f"[SERVICE_UNAVAILABLE] Worker OCR occupé, génération refusée : {exc}"
        ) from exc
    except paddle_client.LiberationImpossible as exc:
        raise RuntimeError(
            f"[SERVICE_UNAVAILABLE] Worker OCR non arrêté, génération refusée : {exc}"
        ) from exc
    if rapport.worker_etait_vivant:
        print(f"[mémoire] {rapport}", flush=True)
    return str(rapport)


_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
_IMAGE_STUDIO_MAX_BYTES = 32 * 1024 * 1024


def _image_studio_root():
    import os
    from pathlib import Path
    from backend.config.paths import get_temp_dir

    root = Path(get_temp_dir()).expanduser()
    if root.is_symlink():
        raise ValueError("[BAD_ARGS] Racine de staging invalide.")
    root = root.resolve() / "image-studio"
    if root.is_symlink():
        raise ValueError("[BAD_ARGS] Racine de staging invalide.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def stage_internal_png(data: bytes) -> tuple[str, str]:
    """Écrit un PNG interne dans la racine de staging et rend (token, sha256).

    Le token est secret et le fichier n'est lisible que par son propriétaire.
    L'écriture passe par un temporaire du même dossier puis `os.replace` :
    un lecteur ne voit jamais un PNG à moitié écrit.
    """
    import os
    import tempfile

    root = _image_studio_root()
    if root.is_symlink():
        raise ValueError("[BAD_ARGS] Racine de staging invalide.")
    try:
        payload = bytes(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("[PNG_INVALID] PNG interne illisible.") from exc
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("[PNG_INVALID] PNG interne invalide.")
    if len(payload) > _IMAGE_STUDIO_MAX_BYTES:
        raise ValueError("[PNG_TOO_LARGE] PNG interne trop volumineux.")

    token = secrets.token_hex(16)
    target = root / f"{token}.png"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{token}-", suffix=".png.tmp", dir=str(root)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return token, hashlib.sha256(payload).hexdigest()


def discard_staged_tokens(tokens) -> None:
    """Supprime silencieusement les PNG de staging nommés par des tokens valides.

    Un token invalide n'est jamais transformé en chemin, et un lien symbolique
    n'est jamais suivi : on ne supprime rien hors de la racine de staging.
    """
    root = _image_studio_root()
    if root.is_symlink():
        return
    for token in tokens or []:
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            continue
        path = root / f"{token}.png"
        if path.is_symlink():
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _resolve_work_token(token, supplied_sha256=None):
    import io as _io
    import os
    from pathlib import Path
    from PIL import Image

    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise ValueError("[BAD_ARGS] Token image invalide.")
    root = _image_studio_root()
    if root.is_symlink():
        raise ValueError("[BAD_ARGS] Racine de staging invalide.")
    path = root / f"{token}.png"
    if path.is_symlink():
        raise ValueError("[BAD_ARGS] Token image refusé (lien symbolique).")
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ValueError(f"[ID_UNKNOWN] Token image introuvable : {token}.") from exc
    if st.st_size > _IMAGE_STUDIO_MAX_BYTES:
        raise ValueError("[PNG_TOO_LARGE] Token image trop volumineux.")
    try:
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("[PNG_INVALID] Token image n'est pas un PNG.")
        with Image.open(_io.BytesIO(data)) as img:
            img.verify()
        with Image.open(_io.BytesIO(data)) as img:
            img.load()
            width, height = img.size
        digest = hashlib.sha256(data).hexdigest()
        expected = (
            None if supplied_sha256 is None else str(supplied_sha256).strip().lower()
        )
        if expected is not None and expected != digest:
            raise ValueError("[HASH_MISMATCH] Hash du token image ne correspond pas.")
        return data, width, height, digest
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"[PNG_INVALID] Token image illisible : {exc}") from exc
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _resolve_reference_tokens(tokens, supplied_sha256):
    if isinstance(supplied_sha256, str):
        shas = [supplied_sha256]
    elif isinstance(supplied_sha256, (list, tuple)):
        shas = list(supplied_sha256)
    else:
        shas = [] if supplied_sha256 is None else [supplied_sha256]
    collected = []
    for index, token in enumerate(tokens or []):
        expected = shas[index] if index < len(shas) else None
        collected.append(_resolve_work_token(str(token).strip(), expected))
    return collected


def _write_temp_bytes(data):
    import os
    import tempfile
    from pathlib import Path
    from backend.config.paths import get_data_dir

    temp_root = Path(get_data_dir()).expanduser().resolve() / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="img_", suffix=".png", dir=str(temp_root))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return path


def _validate_source_artifact(db, source_artifact_id, conv_id):
    from backend.db.models import Artifact, ArtifactLink

    if not source_artifact_id:
        return None
    art = db.query(Artifact).filter(Artifact.id == source_artifact_id).first()
    if not art or art.deleted_at is not None:
        raise ValueError(
            f"[ID_UNKNOWN] source_artifact_id inconnu : {source_artifact_id}."
        )
    if (art.kind or "") != "image":
        raise ValueError(f"[BAD_ARGS] source_artifact_id non image (kind={art.kind}).")
    if conv_id:
        link = (
            db.query(ArtifactLink)
            .filter(
                ArtifactLink.artifact_id == art.id,
                ArtifactLink.scope_kind == "conversation",
                ArtifactLink.scope_id == conv_id,
                ArtifactLink.removed_at.is_(None),
            )
            .first()
        )
        if not link:
            raise ValueError(
                f"[SCOPE_DENIED] source_artifact_id hors portée : {source_artifact_id}."
            )
    from backend.db.models import Blob

    blob = db.query(Blob).filter(Blob.id == art.blob_id).first()
    if blob is None:
        raise ValueError(f"[ID_UNKNOWN] Blob manquant pour {source_artifact_id}.")
    return art


def load_source_image(
    db, artifact_id: str, conv_id: str | None
) -> tuple[bytes, object]:
    """Octets de l'image source validée, sans décodage de pixels.

    La validation est celle de `_validate_source_artifact` : mêmes refus, même
    portée. Les pixels appartiennent à `image_edit_scene`, pas à ce service.
    """
    art = _validate_source_artifact(db, artifact_id, conv_id)
    data = _load_artifact_bytes(db, artifact_id)
    return data, art


def _load_artifact_bytes(db, artifact_id):
    from pathlib import Path
    from backend.config.paths import get_data_dir
    from backend.db.models import Artifact, Blob

    art = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if not art:
        raise ValueError(f"[ID_UNKNOWN] Référence inconnue : {artifact_id}.")
    blob = db.query(Blob).filter(Blob.id == art.blob_id).first()
    if not blob:
        raise ValueError(f"[ID_UNKNOWN] Blob manquant pour {artifact_id}.")
    data_dir = Path(get_data_dir()).expanduser().resolve()
    target = (data_dir / blob.relative_path).resolve()
    if target != data_dir and data_dir not in target.parents:
        raise ValueError("[INTERNAL_ERROR] Blob inaccessible.")
    if not target.is_file():
        raise ValueError("[INTERNAL_ERROR] Blob inaccessible.")
    return target.read_bytes()


def generate(
    manager,
    run_id: str,
    prompt: str,
    aspect_ratio: str = "1:1",
    reference_ids=None,
    seed: int | None = None,
    conv_id=None,
    idempotency_key=None,
    cancel_flag=None,
    on_stage=None,
    db_session_factory=None,
    origin: str = "model_inference",
    before_commit=None,
    staged_path=None,
    mode=None,
    work_token=None,
    mask_token=None,
    reference_tokens=None,
    work_sha256=None,
    mask_sha256=None,
    reference_sha256=None,
    source_artifact_id=None,
    canvas_width=None,
    canvas_height=None,
    strength=None,
    session_id=None,
    revision=None,
    mask_kind=None,
    source_transform: dict | None = None,
    source_size: dict | None = None,
) -> dict:
    from backend.inference.resource_coordinator import (
        ATTENTE_CHAT_S,
        ResourceBusy,
        get_resource_coordinator,
    )
    from backend.services import flux_spike as _fx
    from PIL import Image

    raw_refs = (
        [reference_ids] if isinstance(reference_ids, str) else (reference_ids or [])
    )
    refs = [str(r).strip() for r in raw_refs if str(r or "").strip()]
    ref_tokens = [
        str(t).strip() for t in (reference_tokens or []) if str(t or "").strip()
    ]

    # Normalisation mask_kind (fill | edit | None). Valeurs exactes requises.
    if isinstance(mask_kind, str):
        _mk = mask_kind.strip()
        mask_kind = _mk.lower() if _mk else None
    if mask_kind is not None and mask_kind not in ("fill", "edit"):
        raise ValueError(
            "[BAD_ARGS] [MASK_KIND_MISMATCH] mask_kind doit être 'fill' ou 'edit'."
        )

    mode_norm = (mode or "").strip().lower()
    if not mode_norm:
        total_ref = len(refs) + len(ref_tokens)
        if total_ref > 4:
            raise ValueError("[TOO_MANY] Max 4.")
        mode_norm = "reference" if total_ref >= 1 else "create"
    if mode_norm not in {
        "create",
        "reference",
        "global",
        "local",
        "inpaint",
        "outpaint",
    }:
        raise ValueError(f"[BAD_ARGS] mode inconnu : {mode_norm}.")

    if mode_norm == "create" and (refs or ref_tokens):
        mode_norm = "reference"

    is_edit = mode_norm in {"reference", "global", "local", "inpaint", "outpaint"}

    if (len(refs) + len(ref_tokens)) > 4:
        raise ValueError("[TOO_MANY] Max 4.")

    # mask_kind autorisé uniquement pour les modes locaux.
    if mode_norm not in ("local", "inpaint", "outpaint") and mask_kind is not None:
        raise ValueError(
            f"[BAD_ARGS] [MASK_KIND_MISMATCH] Mode {mode_norm} interdit mask_kind."
        )

    # Strict BAD_ARGS avant tout scheduling/ressource pour les modes à image source.
    if mode_norm == "reference":
        total_ref_count = len(refs) + len(ref_tokens)
        if work_token:
            source_count = total_ref_count + 1
            if source_count != 1:
                raise ValueError(
                    "[BAD_ARGS] Mode reference requiert exactement une référence ou un work token."
                )
        else:
            if total_ref_count < 1 or total_ref_count > 4:
                raise ValueError(
                    "[BAD_ARGS] Mode reference requiert entre 1 et 4 références."
                )
    elif mode_norm == "global":
        if not work_token:
            raise ValueError("[BAD_ARGS] Mode global requiert un work token.")
    elif mode_norm in ("local", "inpaint", "outpaint"):
        if not work_token or not mask_token:
            raise ValueError(f"[BAD_ARGS] Mode {mode_norm} requiert work et mask.")
        required_mask_kind = "edit" if mode_norm == "local" else "fill"
        if mask_kind is None:
            raise ValueError(
                "[BAD_ARGS] [MASK_KIND_MISMATCH] Mode "
                f"{mode_norm} requiert mask_kind='{required_mask_kind}'."
            )
        if mask_kind != required_mask_kind:
            raise ValueError(
                "[MASK_KIND_MISMATCH] [BAD_ARGS] Mode "
                f"{mode_norm} requiert mask_kind='{required_mask_kind}', reçu '{mask_kind}'."
            )

    p = (prompt or "").strip()
    if not p:
        if mode_norm in ("inpaint", "outpaint"):
            p = "Reconstruct and extend naturally the masked area matching the surrounding scene."
        else:
            raise ValueError("[EMPTY_QUERY] Décris l'image à créer (prompt vide).")
    if len(p) > PROMPT_MAX_CHARS:
        raise ValueError(f"[TOO_LONG] Prompt limité à {PROMPT_MAX_CHARS} caractères.")

    if source_artifact_id:
        from backend.db.database import SessionLocal

        _SL = db_session_factory if db_session_factory is not None else SessionLocal
        dbv = _SL()
        try:
            _validate_source_artifact(dbv, source_artifact_id, conv_id)
        finally:
            try:
                dbv.close()
            except Exception:
                pass

    resolved_work = _resolve_work_token(work_token, work_sha256) if work_token else None
    resolved_mask = _resolve_work_token(mask_token, mask_sha256) if mask_token else None
    resolved_ref_tokens = (
        _resolve_reference_tokens(ref_tokens, reference_sha256) if ref_tokens else []
    )

    ref_artifact_bytes = None
    ref_artifact_bytes_list = []
    if refs:
        from backend.db.database import SessionLocal

        _SL = db_session_factory if db_session_factory is not None else SessionLocal
        db = _SL()
        try:
            checked = check_references(db, refs, conv_id)
            for _checked in checked or []:
                ref_artifact_bytes_list.append(
                    _load_artifact_bytes(db, _checked["artifact_id"])
                )
        finally:
            try:
                db.close()
            except Exception:
                pass
        if ref_artifact_bytes_list:
            ref_artifact_bytes = ref_artifact_bytes_list[0]

    all_ref_bytes = list(ref_artifact_bytes_list) + [
        r[0] for r in (resolved_ref_tokens or [])
    ]
    total_ref_count = len(refs) + len(ref_tokens)
    multi_ref = mode_norm == "reference" and not work_token and total_ref_count >= 1

    # dimensions
    if is_edit:
        if canvas_width and canvas_height:
            width, height = int(canvas_width), int(canvas_height)
        else:
            ar = (aspect_ratio or "1:1").strip()
            if ar not in FORMATS:
                ar = "1:1"
            width, height = FORMATS[ar]
        if (width, height) not in set(FORMATS.values()):
            raise ValueError("[BAD_ARGS] Dimensions canevas invalides.")
        if mode_norm in ("local", "inpaint", "outpaint"):
            if not work_token or not mask_token:
                raise ValueError(f"[BAD_ARGS] Mode {mode_norm} requiert work et mask.")
            if resolved_work is None or resolved_mask is None:
                raise ValueError("[BAD_ARGS] Tokens work/mask manquants.")
            if (resolved_work[1], resolved_work[2]) != (width, height):
                raise ValueError("[HASH_MISMATCH] Dimensions work != canvas.")
            if (resolved_mask[1], resolved_mask[2]) != (width, height):
                raise ValueError("[HASH_MISMATCH] Dimensions mask != canvas.")
            # Masque non vide : au moins un pixel >= 128 (via mask_metrics).
            try:
                from backend.image_engine.mask_metrics import mask_metrics as _mm

                _mm_result = _mm(resolved_mask[0], transform=source_transform)
                if _mm_result["total_editable"] == 0:
                    raise ValueError(
                        "[MASK_EMPTY] [BAD_ARGS] Masque vide (aucun pixel >= 128)."
                    )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(
                    f"[MASK_EMPTY] [BAD_ARGS] Masque illisible : {exc}"
                ) from exc
        elif mode_norm == "global":
            if not work_token:
                raise ValueError("[BAD_ARGS] Mode global requiert work.")
            if resolved_work is None:
                raise ValueError("[BAD_ARGS] Token work manquant.")
            if (resolved_work[1], resolved_work[2]) != (width, height):
                raise ValueError("[HASH_MISMATCH] Dimensions work != canvas.")
            if mask_token is not None:
                raise ValueError("[BAD_ARGS] Mode global interdit le mask.")
        elif mode_norm == "reference":
            if work_token and resolved_work is not None:
                if (resolved_work[1], resolved_work[2]) not in set(FORMATS.values()):
                    # allow non-standard? still use dims as canvas maybe
                    pass
            if (
                work_token
                and resolved_work is not None
                and (resolved_work[1], resolved_work[2]) in set(FORMATS.values())
            ):
                width, height = resolved_work[1], resolved_work[2]
    else:
        ar = (aspect_ratio or "1:1").strip()
        if ar not in FORMATS:
            raise ValueError(f"[ENUM_INVALID] aspect_ratio parmi {', '.join(FORMATS)}.")
        width, height = FORMATS[ar]
        if work_token or mask_token:
            raise ValueError("[BAD_ARGS] Mode create n'accepte pas work/mask.")

    if (
        is_edit
        and mode_norm == "reference"
        and not work_token
        and (ref_artifact_bytes is not None or resolved_ref_tokens)
    ):
        ar = (aspect_ratio or "1:1").strip()
        if ar in FORMATS:
            width, height = FORMATS[ar]

    seed = validate_seed(seed)

    _fx._validate_dimensions(width, height)

    if mode_norm == "reference" or mode_norm == "global":
        strength_default = 0.65
    elif mode_norm == "local":
        strength_default = 0.55
    elif mode_norm == "inpaint":
        strength_default = 0.5
    elif mode_norm == "outpaint":
        strength_default = 1.0
    else:
        strength_default = 0.65
    if strength is None:
        strength = strength_default
    try:
        strength = float(strength)
    except (TypeError, ValueError) as exc:
        raise ValueError("[BAD_ARGS] strength invalide.") from exc
    import math as _math

    if not _math.isfinite(strength) or strength < 0.05 or strength > 1.0:
        raise ValueError("[BAD_ARGS] strength doit être entre 0.05 et 1.0.")

    available, reason = check_install()
    if not available:
        raise RuntimeError(f"[SERVICE_UNAVAILABLE] {reason}")

    coordinator = get_resource_coordinator()
    coordinator_lease = None
    try:
        coordinator_lease = coordinator.acquire(
            "image", f"image:{run_id}", timeout_s=ATTENTE_CHAT_S
        )
    except ResourceBusy as exc:
        raise RuntimeError(
            "[SERVICE_UNAVAILABLE] Génération refusée : le moteur est occupé par "
            "une tâche de fond. Réessayez dans un instant."
        ) from exc
    admitted = False
    tmp_files = []
    try:
        try:
            admitted = (
                True
                if manager is None
                else manager.acquire_admission(run_id, timeout=30.0)
            )
            if manager is not None and not admitted:
                raise RuntimeError(
                    "[OUT_OF_MEMORY] Un run lourd est déjà en cours (famille exclusive C07)."
                )
            if manager is not None:
                try:
                    manager.unload_all()
                except Exception as exc:
                    raise RuntimeError(
                        f"[OUT_OF_MEMORY] Libération VRAM impossible : {exc}"
                    )
        except ResourceBusy:
            raise
        _rendre_la_vram_du_worker_ocr()
        available, reason = check_vram()
        if not available:
            raise RuntimeError(f"[OUT_OF_MEMORY] {reason}")

        init_image_path = None
        mask_path_for_spike = None
        if is_edit:
            if work_token and resolved_work is not None:
                init_image_path = _write_temp_bytes(resolved_work[0])
                tmp_files.append(init_image_path)
            elif mode_norm == "reference" and not multi_ref:
                data = None
                if resolved_ref_tokens:
                    data = resolved_ref_tokens[0][0]
                elif ref_artifact_bytes is not None:
                    data = ref_artifact_bytes
                if data is None:
                    raise ValueError(
                        "[BAD_ARGS] Mode reference requiert une image de référence."
                    )
                init_image_path = _write_temp_bytes(data)
                tmp_files.append(init_image_path)
            if mask_token and resolved_mask is not None:
                mask_path_for_spike = _write_temp_bytes(resolved_mask[0])
                tmp_files.append(mask_path_for_spike)

        reference_image_paths = []
        for _d in all_ref_bytes:
            _p = _write_temp_bytes(_d)
            reference_image_paths.append(_p)
            tmp_files.append(_p)

        work_digest = resolved_work[3] if resolved_work is not None else None
        mask_digest = resolved_mask[3] if resolved_mask is not None else None
        if all_ref_bytes:
            reference_digest = hashlib.sha256(all_ref_bytes[0]).hexdigest()
        else:
            reference_digest = None
        # Digests réellement résolus par référence (pour la capture d'identité).
        resolved_ref_digests = [
            hashlib.sha256(b).hexdigest() for b in ref_artifact_bytes_list
        ] + [r[3] for r in (resolved_ref_tokens or [])]
        reference_identifiers = list(refs) + list(ref_tokens)

        if not is_edit or multi_ref:
            png, stats = _fx.run_t2i(
                p,
                seed,
                width=width,
                height=height,
                cancel_flag=cancel_flag,
                on_stage=on_stage,
                staged_path=staged_path,
                reference_images=reference_image_paths if multi_ref else None,
            )
        elif init_image_path is None or mode_norm not in {
            "reference",
            "global",
            "local",
            "inpaint",
            "outpaint",
        }:
            raise RuntimeError(
                "[EDIT_UNAVAILABLE] Mode édition impossible à préparer sans image initiale."
            )
        else:
            _source_rect = None
            if mode_norm in ("outpaint", "inpaint") and isinstance(
                source_transform, dict
            ):
                try:
                    _source_rect = (
                        float(source_transform.get("x")),
                        float(source_transform.get("y")),
                        float(source_transform.get("width")),
                        float(source_transform.get("height")),
                    )
                except (TypeError, ValueError):
                    _source_rect = None
            png, stats = _fx.run_edit(
                p,
                seed,
                width=width,
                height=height,
                cancel_flag=cancel_flag,
                on_stage=on_stage,
                staged_path=staged_path,
                init_image=init_image_path,
                mask=mask_path_for_spike,
                mode=mode_norm,
                strength=strength,
                source_rect=_source_rect,
                reference_images=reference_image_paths or None,
            )

        if not isinstance(stats, dict):
            raise RuntimeError(
                "[INTERNAL_ERROR] Le worker image n'a pas retourné de statistiques."
            )
        _fx._validate_png(png, width, height)
        stats = dict(stats)
        actual_seed = stats.get("seed")
        if actual_seed is None:
            actual_seed = seed
        if (
            isinstance(actual_seed, bool)
            or not isinstance(actual_seed, int)
            or not 0 <= actual_seed <= MAX_SEED
        ):
            raise RuntimeError(
                "[INTERNAL_ERROR] Le worker image a retourné un seed invalide."
            )
        if actual_seed != seed:
            raise RuntimeError(
                f"[SEED_MISMATCH] Worker seed {actual_seed}, seed demandé {seed}."
            )
        actual_width = stats.get("width", width)
        actual_height = stats.get("height", height)
        if actual_width != width or actual_height != height:
            raise RuntimeError(
                f"[DIMENSIONS_MISMATCH] Worker {actual_width}x{actual_height}, attendu {width}x{height}."
            )

        _candidate_before_composite = png
        _did_composite = False

        if (
            is_edit
            and mask_token
            and resolved_mask is not None
            and resolved_work is not None
        ):
            import os

            import os as _os_diag

            _mask_diag_metrics = None
            if resolved_mask is not None:
                try:
                    from backend.image_engine.mask_metrics import (
                        mask_metrics as _mask_metrics_fn,
                    )

                    _mask_diag_metrics = _mask_metrics_fn(
                        resolved_mask[0], transform=source_transform
                    )
                except Exception:
                    _mask_diag_metrics = None
            if (
                resolved_mask is not None
                and resolved_work is not None
                and _os_diag.environ.get("THOT_IMAGE_MASK_DIAGNOSTICS") == "1"
                and _mask_diag_metrics is not None
            ):
                print(
                    "[IMAGE_MASK_DIAG] "
                    + json.dumps(_mask_diag_metrics, separators=(",", ":")),
                    flush=True,
                )
            # Métriques réutilisées par la capture.
            resolved_mask_metrics = _mask_diag_metrics
            png = _fx.composite_protected(
                png,
                resolved_work[0],  # type: ignore[index]
                resolved_mask[0],  # type: ignore[index]
            )
            _did_composite = True

            if (
                _did_composite
                and resolved_work is not None
                and resolved_mask is not None
            ):
                from backend.image_engine.composite_guard import (
                    assert_protected_preserved as _assert_protected_preserved,
                )

                _assert_protected_preserved(png, resolved_work[0], resolved_mask[0])

        _fx._validate_png(png, width, height)

        # Capture opt-in (THOT_IMAGE_CAPTURE_DIR): persiste un run réel pour diagnostic.
        _capture_root = None
        try:
            import os as _os_capture

            _capture_root = _os_capture.environ.get("THOT_IMAGE_CAPTURE_DIR")
        except Exception:
            _capture_root = None
        if _capture_root:
            try:
                import re as _re_cid

                from pathlib import Path as _Path

                _root = _Path(_capture_root).resolve()
                if not _re_cid.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(run_id)):
                    raise ValueError("invalid_run_id")
                _capture_dir = _root / str(run_id)
                # Vérification anti-traversée : le chemin résolu doit rester sous la racine.
                if not _capture_dir.resolve().is_relative_to(_root):
                    raise ValueError("invalid_run_id")
                _capture_dir.mkdir(parents=True, exist_ok=True)
                # Supprimer un manifest.json résiduel du même dossier (sinon obsolète).
                try:
                    (_capture_dir / "manifest.json").unlink()
                except FileNotFoundError:
                    pass
                # running status
                _capture_dir.joinpath("capture_status.json").write_text(
                    json.dumps({"status": "running"}),
                    encoding="utf-8",
                )

                # Écriture des fichiers.
                if resolved_work is not None:
                    _capture_dir.joinpath("work.png").write_bytes(resolved_work[0])
                if resolved_mask is not None:
                    _capture_dir.joinpath("mask_backend.png").write_bytes(
                        resolved_mask[0]
                    )
                _capture_dir.joinpath("candidate_before_composite.png").write_bytes(
                    _candidate_before_composite
                )
                if _did_composite and resolved_mask is not None:
                    _capture_dir.joinpath("mask_composite.png").write_bytes(
                        resolved_mask[0]
                    )
                _capture_dir.joinpath("final.png").write_bytes(png)

                # Hashes recalculés depuis disque.
                _expected_files: list[str] = [
                    "candidate_before_composite.png",
                    "final.png",
                ]
                if resolved_work is not None:
                    _expected_files.append("work.png")
                if resolved_mask is not None:
                    _expected_files.append("mask_backend.png")
                if _did_composite and resolved_mask is not None:
                    _expected_files.append("mask_composite.png")

                files: dict[str, dict[str, object]] = {}
                for _name in _expected_files:
                    _path = _capture_dir / _name
                    _data = _path.read_bytes()
                    files[_name] = {
                        "sha256": hashlib.sha256(_data).hexdigest(),
                        "bytes": len(_data),
                    }

                # Comparaison aux digests transmis.
                transmitted: dict[str, str] = {}
                if work_digest is not None:
                    transmitted["work.png"] = str(work_digest)
                if mask_digest is not None:
                    transmitted["mask_backend.png"] = str(mask_digest)

                hash_mismatch: dict[str, dict[str, object]] = {}
                for _name, _trans in transmitted.items():
                    if _name in files:
                        _actual = str(files[_name]["sha256"])
                        if str(_trans).strip().lower() != _actual:
                            hash_mismatch[_name] = {
                                "expected": _trans,
                                "actual": _actual,
                            }

                # Comparaison des digests des références (P-01).
                _reference_bytes = list(all_ref_bytes or [])
                _reference_actual_sha = [
                    hashlib.sha256(b).hexdigest() for b in _reference_bytes
                ]
                _reference_token_sha = [
                    hashlib.sha256(str(_ident).encode("utf-8")).hexdigest()
                    for _ident in (reference_identifiers or [])
                ]
                if len(_reference_token_sha) != len(_reference_actual_sha):
                    # Alignement défensif (ne pas crasher un run complet).
                    if len(_reference_token_sha) < len(_reference_actual_sha):
                        _reference_token_sha = _reference_token_sha + [
                            ""
                            for _ in range(
                                len(_reference_actual_sha) - len(_reference_token_sha)
                            )
                        ]
                    else:
                        _reference_token_sha = _reference_token_sha[
                            : len(_reference_actual_sha)
                        ]
                for _idx in range(len(_reference_actual_sha)):
                    _supplied = None
                    if _idx < len(resolved_ref_digests or []):
                        _supplied = resolved_ref_digests[_idx]
                    if (
                        _supplied is not None
                        and str(_supplied).strip().lower()
                        != _reference_actual_sha[_idx]
                    ):
                        hash_mismatch[f"reference_{_idx}"] = {
                            "expected": _supplied,
                            "actual": _reference_actual_sha[_idx],
                        }
                # tokens_sha256 = SHA-256 des identifiants réellement utilisés (tokens/staging).
                _tok_norm = []
                for _idx, _h in enumerate(_reference_token_sha):
                    _h_norm = str(_h).strip().lower()
                    if __import__("re").fullmatch(r"[a-f0-9]{64}", _h_norm) is None:
                        _ident = (
                            reference_identifiers[_idx]
                            if _idx < len(reference_identifiers or [])
                            else str(_idx)
                        )
                        _tok_norm.append(
                            hashlib.sha256(str(_ident).encode("utf-8")).hexdigest()
                        )
                    else:
                        _tok_norm.append(_h_norm)
                _references_manifest = {
                    "count": len(_reference_actual_sha),
                    "sha256": _reference_actual_sha,
                    "tokens_sha256": _tok_norm,
                }

                # Métriques masque (calculées une fois).
                try:
                    _capture_mask_metrics = resolved_mask_metrics  # type: ignore[name-defined]
                except NameError:
                    _capture_mask_metrics = None
                if resolved_mask is not None and _capture_mask_metrics is None:
                    try:
                        from backend.image_engine.mask_metrics import (
                            mask_metrics as _mm_fn,
                        )

                        _capture_mask_metrics = _mm_fn(
                            resolved_mask[0], transform=source_transform
                        )
                    except Exception:
                        _capture_mask_metrics = None

                _steps_executed = stats.get("steps_executed")
                _backend_eff = (
                    stats.get("backend_effective")
                    or stats.get("backend")
                    or _fx.BACKEND_NAME
                )
                _pipeline_class = stats.get("pipeline_class")
                if _pipeline_class is None:
                    _pipeline_class = "backend.image_engine.spike_t2i"
                _fallback_used = stats.get("fallback_used", False)
                if _fallback_used is None:
                    _fallback_used = False

                _origin_norm = origin if origin not in (None, "") else "model_inference"
                _run_type = (
                    "synthetic_sentinel"
                    if str(origin).strip() == "synthetic_sentinel"
                    else "model_inference"
                )
                _algorithm = "composite_protected" if _did_composite else "none"

                _is_masked_run = (
                    mode_norm in ("local", "inpaint", "outpaint")
                    or mask_kind is not None
                    or resolved_mask is not None
                )
                # Détermination du statut.
                _capture_status = "invalid" if hash_mismatch else None
                _capture_reason = None
                if _capture_status is None:
                    if _is_masked_run and (
                        source_transform is None or source_size is None
                    ):
                        _capture_reason = "missing_source_transform"
                    elif _steps_executed is None:
                        _capture_reason = "steps_unreported"
                    else:
                        _missing_files = [
                            _n
                            for _n in _expected_files
                            if not (_capture_dir / _n).is_file()
                        ]
                        if _missing_files:
                            _capture_reason = "missing_file"
                        else:
                            _capture_status = "complete"
                if _capture_status is None:
                    _capture_status = "incomplete" if _capture_reason else "complete"

                # SHA non hex-64 (transmis ou calculés) ⇒ invalid.
                _sha_re = __import__("re")
                _hex64 = _sha_re.compile(r"^[a-f0-9]{64}$")
                if _capture_status != "invalid":
                    for _n, _info in files.items():
                        if not _hex64.match(str(_info["sha256"])):
                            _capture_status = "invalid"
                            break

                _source_sha256 = (
                    hashlib.sha256(resolved_work[0]).hexdigest()
                    if resolved_work is not None
                    else None
                )
                try:
                    import os as _os_sr
                    from backend.image_engine.source_ref import (
                        resolve_source_reference_mode as _resolve_sr,
                    )

                    _source_reference_manifest = _resolve_sr(
                        mode_norm, resolved_mask is not None, _os_sr.environ
                    )
                except Exception:
                    _source_reference_manifest = (
                        "original" if mode_norm == "outpaint" else "none"
                    )
                _manifest = {
                    "run_id": run_id,
                    "width": width,
                    "height": height,
                    "mode": mode_norm,
                    "mask_kind": mask_kind,
                    "strength": strength,
                    "prompt": p,
                    "seed": actual_seed,
                    "origin": _origin_norm,
                    "run_type": _run_type,
                    "algorithm": _algorithm,
                    "inference": {
                        "backend": _backend_eff,
                        "pipeline_class": _pipeline_class,
                        "steps_executed": _steps_executed,
                        "fallback_used": _fallback_used,
                        "source_reference": _source_reference_manifest,
                    },
                    "source": {
                        "canvas": {"width": width, "height": height},
                        "transform": source_transform,
                        "source_size": source_size,
                        "source_sha256": _source_sha256,
                    },
                    "references": _references_manifest,
                    "mask_metrics": _capture_mask_metrics,
                    "files": files,
                    "hash_mismatch": hash_mismatch,
                    "capture_status": _capture_status,
                    "capture_reason": _capture_reason,
                }
                _capture_dir.joinpath("manifest.json").write_text(
                    json.dumps(_manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                _capture_dir.joinpath("capture_status.json").write_text(
                    json.dumps({"status": _capture_status}),
                    encoding="utf-8",
                )
            except Exception as _capture_exc:
                _stage = "capture"
                try:
                    import re as _re_exc

                    from pathlib import Path as _Path2

                    _cd_root = _Path2(_capture_root).resolve()
                    if _re_exc.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(run_id)) and (
                        _cd_root / str(run_id)
                    ).resolve().is_relative_to(_cd_root):
                        _cd = _cd_root / str(run_id)
                        _cd.mkdir(parents=True, exist_ok=True)
                        # Supprimer un manifest.json éventuellement déjà écrit.
                        try:
                            (_cd / "manifest.json").unlink()
                        except FileNotFoundError:
                            pass
                        _cd.joinpath("capture_status.json").write_text(
                            json.dumps(
                                {
                                    "status": "invalid"
                                    if str(_capture_exc) == "invalid_run_id"
                                    else "incomplete",
                                    "reason": str(_capture_exc),
                                    "stage": _stage,
                                }
                            ),
                            encoding="utf-8",
                        )
                    else:
                        _cd_root.mkdir(parents=True, exist_ok=True)
                        _cd_root.joinpath("capture_status.json").write_text(
                            json.dumps(
                                {
                                    "status": "invalid",
                                    "reason": "invalid_run_id",
                                    "stage": _stage,
                                }
                            ),
                            encoding="utf-8",
                        )
                except Exception:
                    pass
                _strict = (
                    __import__("os").environ.get("THOT_IMAGE_CAPTURE_STRICT") == "1"
                )
                if _strict:
                    raise
                print(f"[IMAGE_CAPTURE_FAILED] {_capture_exc}", flush=True)

        stats.update(
            {
                "width": width,
                "height": height,
                "seed": actual_seed,
                "backend": _fx.BACKEND_NAME,
                "mode": mode_norm,
                "mask_kind": mask_kind,
            }
        )

        if origin in (None, "", "studio", "model_inference", "synthetic_sentinel"):
            provenance_origin = (
                "image_create" if mode_norm in ("create", "reference") else "image_edit"
            )
        else:
            provenance_origin = origin
        from typing import Any

        db2: Any
        if db_session_factory is None:
            from backend.db.database import SessionLocal

            db2 = SessionLocal()
        else:
            db2 = (
                db_session_factory()
                if callable(db_session_factory)
                else db_session_factory
            )
        try:
            if cancel_flag is not None and cancel_flag.is_set():
                raise RuntimeError(
                    "[CANCELLED] Run image annulé avant le début de la publication."
                )
            if on_stage is not None:
                on_stage(
                    {"id": "publication", "label": "Enregistrement local de l’image"}
                )
            pub = _fx.publish_generated(
                db2,
                png,
                {
                    "prompt": p,
                    "display_name": p[:80] or "Image Flux",
                    "aspect_ratio": aspect_ratio
                    if isinstance(aspect_ratio, str) and aspect_ratio in FORMATS
                    else f"{width}:{height}",
                    "checkpoint": _fx.CHECKPOINT,
                    "model": _fx.MODEL_NAME,
                    "backend": _fx.BACKEND_NAME,
                    "width": width,
                    "height": height,
                    "dimensions": {"width": width, "height": height},
                    "seed": actual_seed,
                    "origin": provenance_origin,
                    "idempotency_key": idempotency_key,
                    "run_id": run_id,
                    "mode": mode_norm,
                    "mask_kind": mask_kind,
                    "source_artifact_id": source_artifact_id,
                    "session_id": session_id,
                    "revision": revision,
                    "work_sha256": work_digest,
                    "mask_sha256": mask_digest,
                    "reference_sha256": reference_digest,
                },
                scope_conversation_id=conv_id,
                before_commit=before_commit,
            )
        finally:
            try:
                db2.close()
            except Exception:
                pass
        pub["stats"] = stats
        pub["experimental"] = True
        return pub
    except ResourceBusy:
        raise
    finally:
        for tf in tmp_files:
            try:
                from pathlib import Path

                Path(tf).unlink()
            except OSError:
                pass
        if manager is not None and admitted:
            try:
                manager.release_admission(run_id)
            except Exception:
                pass
        if coordinator_lease is not None:
            try:
                coordinator.release(coordinator_lease)
            except Exception:
                pass

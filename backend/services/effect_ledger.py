from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.db.models import EventOutbox, ImageEffect


class EffectConflict(RuntimeError):
    pass


class EffectInProgress(RuntimeError):
    pass


class EffectUncertain(EffectInProgress):
    pass


@dataclass(frozen=True)
class EffectClaim:
    effect_id: str
    status: str
    fingerprint: str
    output_artifact_id: str | None
    effective_seed: int | None
    owner_token: str | None
    created: bool


def derive_effect_seed(effect_id: str) -> int:
    digest = hashlib.sha256(
        f"thot-image-effect:{effect_id}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16)


def image_request_fingerprint(
    prompt: str,
    image_format: str,
    reference_ids: list[str],
    seed: int | None,
    extra: dict | None = None,
) -> str:
    ordered_refs = [str(value or "").strip() for value in (reference_ids or [])]
    ordered_refs_sorted = sorted(ordered_refs)
    extra_canonical = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            extra_canonical[str(key)] = value
    payload = {
        "prompt": (prompt or "").strip(),
        "image_format": (image_format or "1:1").strip(),
        "reference_ids": ordered_refs_sorted,
        "seed": None if seed is None else int(seed),
        "extra": extra_canonical,
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _get(
    db: Any, run_id: str, logical_action_id: str, output_index: int
) -> ImageEffect | None:
    return db.scalar(
        select(ImageEffect).where(
            ImageEffect.run_id == run_id,
            ImageEffect.logical_action_id == logical_action_id,
            ImageEffect.output_index == output_index,
        )
    )


def claim_image_effect(
    db: Any,
    *,
    run_id: str,
    logical_action_id: str,
    output_index: int,
    fingerprint: str,
    tool_call_id: str | None = None,
    effective_seed: int | None = None,
) -> EffectClaim:
    if not run_id or not logical_action_id or not fingerprint:
        raise ValueError("run_id, logical_action_id et fingerprint sont requis")
    if output_index < 0:
        raise ValueError("output_index doit être non négatif")
    existing = _get(db, run_id, logical_action_id, output_index)
    if existing is None:
        existing = (
            db.query(ImageEffect)
            .filter(
                ImageEffect.fingerprint == fingerprint,
                ImageEffect.status.in_(
                    ("claimed", "running", "uncertain", "published")
                ),
            )
            .order_by(ImageEffect.created_at.asc())
            .first()
        )
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise EffectConflict("EFFECT_KEY_CONFLICT")
        return EffectClaim(
            effect_id=existing.id,
            status=existing.status,
            fingerprint=existing.fingerprint,
            output_artifact_id=existing.output_artifact_id,
            effective_seed=existing.effective_seed,
            owner_token=existing.owner_token,
            created=False,
        )

    effect_id = f"img_eff_{uuid.uuid4().hex}"
    seed = (
        effective_seed if effective_seed is not None else derive_effect_seed(effect_id)
    )
    row = ImageEffect(
        id=effect_id,
        run_id=run_id,
        logical_action_id=logical_action_id,
        output_index=output_index,
        fingerprint=fingerprint,
        tool_call_id=tool_call_id,
        owner_token=secrets.token_hex(16),
        effective_seed=seed,
        status="claimed",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = _get(db, run_id, logical_action_id, output_index)
        if existing is None:
            existing = (
                db.query(ImageEffect)
                .filter(
                    ImageEffect.fingerprint == fingerprint,
                    ImageEffect.status.in_(
                        ("claimed", "running", "uncertain", "published")
                    ),
                )
                .order_by(ImageEffect.created_at.asc())
                .first()
            )
        if existing is None:
            raise EffectConflict("EFFECT_CLAIM_FAILED") from exc
        if existing.fingerprint != fingerprint:
            raise EffectConflict("EFFECT_KEY_CONFLICT") from exc
        return _claim(existing, False)
    db.refresh(row)
    return EffectClaim(
        effect_id=row.id,
        status=row.status,
        fingerprint=row.fingerprint,
        output_artifact_id=row.output_artifact_id,
        effective_seed=row.effective_seed,
        owner_token=row.owner_token,
        created=True,
    )


def mark_running(db: Any, claim: EffectClaim) -> EffectClaim:
    row = db.get(ImageEffect, claim.effect_id)
    if row is None:
        raise EffectConflict("EFFECT_NOT_FOUND")
    if row.status == "published":
        return _claim(row, False)
    if row.status == "uncertain":
        raise EffectUncertain("EFFECT_UNCERTAIN")
    if row.status == "running":
        raise EffectInProgress("EFFECT_IN_PROGRESS")
    if row.status != "claimed":
        raise EffectConflict(f"EFFECT_{row.status.upper()}")
    row.status = "running"
    row.attempt = int(row.attempt or 0) + 1
    db.commit()
    db.refresh(row)
    return _claim(row, False)


def _cleanup_staged_file(db: Any, effect_id: str) -> None:
    row = db.get(ImageEffect, effect_id)
    if row is None or not row.staged_path:
        return
    try:
        Path(row.staged_path).unlink(missing_ok=True)
    except OSError:
        pass
    row.staged_path = None
    db.commit()


def execute_claimed_image(
    db: Any,
    claim: EffectClaim,
    generate: Any,
    on_publish: Any = None,
) -> tuple[EffectClaim, Any]:
    if claim.status == "published" and claim.output_artifact_id:
        return claim, None
    if claim.status != "claimed":
        raise EffectConflict(f"EFFECT_{claim.status.upper()}")
    running = mark_running(db, claim)

    def before_commit(session: Any, artifact_id: str) -> None:
        row = session.get(ImageEffect, running.effect_id)
        if row is not None:
            row.status = "published"
            row.output_artifact_id = artifact_id
            row.error = None
        if on_publish is not None:
            on_publish(session, artifact_id)

    try:
        result = generate(running.effective_seed, running.effect_id, before_commit)
    except Exception as exc:
        try:
            mark_uncertain(db, running.effect_id, f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise
    row = db.get(ImageEffect, running.effect_id)
    if row is None or row.status != "published":
        published = mark_published(db, running.effect_id, str(result["artifact_id"]))
    else:
        published = _claim(row, False)
    _cleanup_staged_file(db, running.effect_id)
    return published, result


def set_staged_path(db: Any, effect_id: str, staged_path: str) -> None:
    row = db.get(ImageEffect, effect_id)
    if row is None:
        raise EffectConflict("EFFECT_NOT_FOUND")
    if row.status not in {"claimed", "running"}:
        raise EffectConflict(f"EFFECT_{row.status.upper()}")
    row.staged_path = staged_path
    db.commit()


def clear_staged_path(db: Any, effect_id: str) -> None:
    row = db.get(ImageEffect, effect_id)
    if row is not None:
        row.staged_path = None
        db.commit()


def mark_published(db: Any, effect_id: str, artifact_id: str) -> EffectClaim:
    if not artifact_id:
        raise ValueError("artifact_id est requis")
    row = db.get(ImageEffect, effect_id)
    if row is None:
        raise EffectConflict("EFFECT_NOT_FOUND")
    if row.status == "published" and row.output_artifact_id == artifact_id:
        return _claim(row, False)
    if row.status != "running":
        raise EffectConflict(f"EFFECT_{row.status.upper()}")
    row.status = "published"
    row.output_artifact_id = artifact_id
    row.error = None
    db.commit()
    db.refresh(row)
    return _claim(row, False)


def mark_failed(db: Any, effect_id: str, error: str) -> EffectClaim:
    row = db.get(ImageEffect, effect_id)
    if row is None:
        raise EffectConflict("EFFECT_NOT_FOUND")
    if row.status == "published":
        raise EffectConflict("EFFECT_ALREADY_PUBLISHED")
    row.status = "failed"
    row.error = (error or "")[:2000]
    db.commit()
    db.refresh(row)
    return _claim(row, False)


def mark_uncertain(db: Any, effect_id: str, error: str) -> EffectClaim:
    row = db.get(ImageEffect, effect_id)
    if row is None:
        raise EffectConflict("EFFECT_NOT_FOUND")
    if row.status == "published":
        raise EffectConflict("EFFECT_ALREADY_PUBLISHED")
    row.status = "uncertain"
    row.error = (error or "")[:2000]
    db.commit()
    db.refresh(row)
    return _claim(row, False)


def artifact_event_key(run_id: str | None, artifact_id: str) -> str:
    return f"artifact_created:{run_id or artifact_id}:{artifact_id}"


def mark_event_published_by_key(db: Any, event_key: str) -> bool:
    row = db.query(EventOutbox).filter(EventOutbox.event_key == event_key).first()
    if row is None:
        return False
    mark_event_published(db, row.id)
    return True


def enqueue_event(
    db: Any,
    *,
    event_key: str,
    run_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    if (
        db.query(EventOutbox).filter(EventOutbox.event_key == event_key).first()
        is not None
    ):
        return False
    db.add(
        EventOutbox(
            id=f"out_{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:32]}",
            event_key=event_key,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
        )
    )
    return True


def reconcile_interrupted_effects(db: Any) -> int:
    rows = (
        db.query(ImageEffect)
        .filter(ImageEffect.status.in_(("claimed", "running")))
        .all()
    )
    for row in rows:
        row.status = "uncertain"
        row.error = "process interrupted before publication"
    if rows:
        db.commit()
    return len(rows)


def retire_deleted_publication(db: Any, effect_id: str, reason: str) -> EffectClaim:
    """Retire un effet PUBLIÉ dont les pixels ont disparu (artefact supprimé).

    D'OÙ CE CAS : ``image_delete`` (Electron) supprime l'artefact ET ses
    ``artifact_links`` en soft-delete, mais laisse la ligne ``ImageEffect`` en
    ``published``. Un effet publié n'est rejouable que si son artefact l'est
    (`is_artifact_replayable`) : la suppression rendait donc la demande
    définitivement impossible à servir, dans TOUTES les conversations, sans rien
    effacer jamais.

    « Publié mais disparu » n'est pas « publié ailleurs » : on ne peut pas
    rattacher des pixels morts, il n'y a rien à rattacher. L'effet devient donc
    un effet ÉCHOUÉ — ce qui libère l'index unique d'empreinte et laisse la
    prochaine demande identique réclamer un effet NEUF. Fail loud : un effet
    déjà dans un état terminal est renvoyé tel quel, jamais réécrit.
    """
    row = db.get(ImageEffect, effect_id)
    if row is None:
        raise EffectConflict("EFFECT_NOT_FOUND")
    if row.status != "published" or not row.output_artifact_id:
        return _claim(row, False)
    row.status = "failed"
    row.error = (reason or "")[:2000]
    db.commit()
    db.refresh(row)
    return _claim(row, False)


def is_artifact_replayable(
    db: Any, artifact_id: str, conversation_id: str | None = None
) -> bool:
    from backend.db.models import Artifact, ArtifactLink, Blob

    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.deleted_at is not None or artifact.kind != "image":
        return False
    blob = db.get(Blob, artifact.blob_id)
    if blob is None or blob.state not in (None, "available"):
        return False
    if conversation_id:
        link = (
            db.query(ArtifactLink)
            .filter(
                ArtifactLink.artifact_id == artifact.id,
                ArtifactLink.scope_kind == "conversation",
                ArtifactLink.scope_id == conversation_id,
                ArtifactLink.removed_at.is_(None),
            )
            .first()
        )
        return link is not None
    return True


def is_effect_replayable(
    db: Any, effect_id: str, conversation_id: str | None = None
) -> bool:
    row = db.get(ImageEffect, effect_id)
    if row is None or row.status != "published" or not row.output_artifact_id:
        return False
    return is_artifact_replayable(db, row.output_artifact_id, conversation_id)


def pending_events(db: Any, limit: int = 50) -> list[EventOutbox]:
    return list(
        db.query(EventOutbox)
        .filter(EventOutbox.status == "pending")
        .order_by(EventOutbox.created_at.asc())
        .limit(limit)
        .all()
    )


def discard_event(db: Any, event_id: str) -> None:
    row = db.get(EventOutbox, event_id)
    if row is not None:
        row.status = "discarded"
        db.commit()


def mark_event_published(db: Any, event_id: str) -> None:
    row = db.get(EventOutbox, event_id)
    if row is None:
        raise EffectConflict("EVENT_NOT_FOUND")
    row.status = "published"
    row.attempts = int(row.attempts or 0) + 1
    row.published_at = func.now()
    db.commit()


def _claim(row: ImageEffect, created: bool) -> EffectClaim:
    return EffectClaim(
        effect_id=row.id,
        status=row.status,
        fingerprint=row.fingerprint,
        output_artifact_id=row.output_artifact_id,
        effective_seed=row.effective_seed,
        owner_token=row.owner_token,
        created=created,
    )

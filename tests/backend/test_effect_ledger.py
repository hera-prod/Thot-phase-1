from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Artifact, ArtifactLink, Base, Blob, EventOutbox, ImageEffect
from backend.services.effect_ledger import (
    EffectConflict,
    artifact_event_key,
    execute_claimed_image,
    EffectInProgress,
    claim_image_effect,
    enqueue_event,
    is_effect_replayable,
    mark_event_published,
    mark_failed,
    mark_published,
    mark_running,
    pending_events,
    reconcile_interrupted_effects,
    mark_uncertain,
)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_claim_is_stable_and_reuses_published_effect() -> None:
    db = make_session()
    first = claim_image_effect(
        db,
        run_id="run_1",
        logical_action_id="action_1",
        output_index=0,
        fingerprint="fp_1",
    )
    assert first.created is True
    assert first.status == "claimed"
    assert first.effective_seed is not None

    running = mark_running(db, first)
    assert running.status == "running"
    published = mark_published(db, first.effect_id, "art_1")
    assert published.status == "published"
    assert published.output_artifact_id == "art_1"

    replay = claim_image_effect(
        db,
        run_id="run_1",
        logical_action_id="action_1",
        output_index=0,
        fingerprint="fp_1",
    )
    assert replay.created is False
    assert replay.effect_id == first.effect_id
    assert replay.output_artifact_id == "art_1"


def test_claim_conflict_does_not_execute() -> None:
    db = make_session()
    claim_image_effect(db, run_id="run_1", logical_action_id="a", output_index=0, fingerprint="one")
    with pytest.raises(EffectConflict, match="EFFECT_KEY_CONFLICT"):
        claim_image_effect(db, run_id="run_1", logical_action_id="a", output_index=0, fingerprint="two")


def test_uncertain_effect_cannot_be_claimed_or_retried_implicitly() -> None:
    db = make_session()
    claim = claim_image_effect(db, run_id="run_1", logical_action_id="a", output_index=0, fingerprint="one")
    mark_running(db, claim)
    mark_uncertain(db, claim.effect_id, "process killed")
    with pytest.raises(EffectInProgress, match="UNCERTAIN"):
        mark_running(db, claim)
    replay = claim_image_effect(db, run_id="run_1", logical_action_id="a", output_index=0, fingerprint="one")
    assert replay.status == "uncertain"


def test_failed_effect_can_be_explicitly_claimed_only_after_new_logical_action() -> None:
    db = make_session()
    claim = claim_image_effect(db, run_id="run_1", logical_action_id="a", output_index=0, fingerprint="one")
    mark_running(db, claim)
    mark_failed(db, claim.effect_id, "worker failed")
    with pytest.raises(EffectConflict, match="EFFECT_FAILED"):
        mark_running(db, claim)
    new_claim = claim_image_effect(db, run_id="run_1", logical_action_id="b", output_index=0, fingerprint="one")
    assert new_claim.created is True


def test_event_outbox_claim_is_idempotent() -> None:
    db = make_session()
    assert enqueue_event(
        db,
        event_key="artifact_created:run_1:art_1",
        run_id="run_1",
        event_type="artifact_created",
        payload={"artifact_id": "art_1"},
    ) is True
    db.commit()
    assert enqueue_event(
        db,
        event_key="artifact_created:run_1:art_1",
        run_id="run_1",
        event_type="artifact_created",
        payload={"artifact_id": "art_1"},
    ) is False
    db.commit()
    event = pending_events(db)[0]


def test_event_outbox_ids_remain_distinct_for_multiple_artifacts_in_one_run() -> None:
    db = make_session()
    run_id = "550e8400-e29b-41d4-a716-446655440000"
    first = artifact_event_key(run_id, "art_111111111111")
    second = artifact_event_key(run_id, "art_222222222222")
    assert enqueue_event(db, event_key=first, run_id=run_id, event_type="artifact_created", payload={"artifact_id": "art_111111111111"}) is True
    assert enqueue_event(db, event_key=second, run_id=run_id, event_type="artifact_created", payload={"artifact_id": "art_222222222222"}) is True
    db.commit()
    rows = db.query(EventOutbox).order_by(EventOutbox.event_key).all()
    assert len(rows) == 2
    assert rows[0].id != rows[1].id


def test_published_effect_replay_respects_deleted_artifact() -> None:
    db = make_session()
    db.add(Blob(id="blob_1", sha256="cd" * 32, relative_path="blobs/cd/x.bin", size_bytes=1, mime_detected="image/png"))
    db.add(Artifact(id="art_1", blob_id="blob_1", kind="image", original_filename="x.png", display_name="x"))
    db.add(ArtifactLink(id="link_1", artifact_id="art_1", scope_kind="conversation", scope_id="conv_1", role="generated"))
    db.commit()
    claim = claim_image_effect(db, run_id="run_1", logical_action_id="a", output_index=0, fingerprint="one")
    mark_running(db, claim)
    mark_published(db, claim.effect_id, "art_1")
    assert is_effect_replayable(db, claim.effect_id, "conv_1") is True
    artifact = db.get(Artifact, "art_1")
    assert artifact is not None
    artifact.deleted_at = datetime.now()
    db.commit()


def test_same_fingerprint_in_another_run_cannot_restart_uncertain_effect() -> None:
    db = make_session()
    first = claim_image_effect(db, run_id="run_a", logical_action_id="a", output_index=0, fingerprint="same")
    mark_running(db, first)
    second = claim_image_effect(db, run_id="run_b", logical_action_id="b", output_index=0, fingerprint="same")
    assert second.effect_id == first.effect_id
    assert second.status == "running"
    db.query(ImageEffect).filter(ImageEffect.id == first.effect_id).one().status = "uncertain"
    db.commit()
    third = claim_image_effect(db, run_id="run_c", logical_action_id="c", output_index=0, fingerprint="same")
    assert third.effect_id == first.effect_id
    assert third.status == "uncertain"


def test_worker_failure_marks_effect_uncertain_without_retry() -> None:
    db = make_session()
    claim = claim_image_effect(db, run_id="run_a", logical_action_id="a", output_index=0, fingerprint="one")
    with pytest.raises(RuntimeError, match="worker crash"):
        execute_claimed_image(
            db,
            claim,
            lambda seed, effect_id, before_commit: (_ for _ in ()).throw(RuntimeError("worker crash")),
        )
    replay = claim_image_effect(db, run_id="run_b", logical_action_id="b", output_index=0, fingerprint="one")
    assert replay.status == "uncertain"


def test_unseeded_effect_reuses_same_effective_seed() -> None:
    db = make_session()
    first = claim_image_effect(db, run_id="run_a", logical_action_id="a", output_index=0, fingerprint="same")
    second = claim_image_effect(db, run_id="run_b", logical_action_id="b", output_index=0, fingerprint="same")
    assert second.effect_id == first.effect_id
    assert second.effective_seed == first.effective_seed
    assert first.effective_seed is not None
    assert 0 <= first.effective_seed <= 0xFFFFFFFF


def test_reconcile_marks_claimed_and_running_effects_uncertain() -> None:
    db = make_session()
    claim = claim_image_effect(db, run_id="run_a", logical_action_id="a", output_index=0, fingerprint="same")
    assert reconcile_interrupted_effects(db) == 1
    refreshed = claim_image_effect(db, run_id="run_b", logical_action_id="b", output_index=0, fingerprint="same")
    assert refreshed.status == "uncertain"
    assert claim.effect_id == refreshed.effect_id

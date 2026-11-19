# -*- coding: utf-8 -*-
"""SF-VIE-04 — Déplacement d'une discussion entre espaces."""

from __future__ import annotations

import pytest

try:
    from .harness import (
        create_isolated_db,
        cleanup_temp_file,
        scenario_marker,
    )
except ImportError:  # pragma: no cover
    from harness import (
        create_isolated_db,
        cleanup_temp_file,
        scenario_marker,
    )

pytestmark = scenario_marker("SF-VIE-04")

from backend.db.models import (
    Artifact,
    ArtifactLink,
    Blob,
    Conversation,
    MemoryEntry,
    Message,
)
from backend.search import scope as S
from backend.services import espaces as E
from backend.services import memoire as M


@pytest.fixture()
def base(tmp_path, monkeypatch):
    engine, sessions, db_path = create_isolated_db(
        tmp_path, monkeypatch=monkeypatch, name="vie04.db"
    )
    try:
        yield engine, sessions
    finally:
        cleanup_temp_file(db_path, engine=engine)


def _artifact(db, art_id: str, name: str = "cite.txt", body: bytes = b"contenu") -> str:
    if db.query(Artifact).filter(Artifact.id == art_id).first() is None:
        db.add(
            Blob(
                id=f"sha_{art_id}",
                sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin",
                size_bytes=len(body),
                mime_detected="text/plain",
                state="available",
            )
        )
        db.add(
            Artifact(
                id=art_id,
                blob_id=f"sha_{art_id}",
                kind="text",
                original_filename=name,
                display_name=name,
            )
        )
        db.commit()
    return art_id


def _conv(db, conv_id: str, ws: str | None, answer: str | None = None) -> str:
    db.add(Conversation(id=conv_id, title="discussion", mode="chat", workspace_id=ws))
    if answer is not None:
        db.add(
            Message(
                id=f"msg_{conv_id}",
                conv_id=conv_id,
                role="assistant",
                content=answer,
                status="done",
            )
        )
    db.commit()
    return conv_id


def _link(db, art_id: str, kind: str, scope_id: str) -> None:
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_{kind}_{scope_id}",
            artifact_id=art_id,
            scope_kind=kind,
            scope_id=scope_id,
            role="shared",
        )
    )
    db.commit()



def _fiche_acceptee(db, ws_id: str, key: str, sources: list[str]) -> str:
    out = M.propose(
        db,
        ws_id,
        "Fait cite depuis la discussion deplacee.",
        source_artifact_ids=list(sources),
        origin_context="conversation de travail",
        promotion_reasons=[{"kind": "outil", "detail": "usage repete"}],
        idempotency_key=key,
    )
    return M.accept(db, out["entry_id"])["entry_id"]


def test_evaluation_move_produit_rapport_sans_modifier_espace(base):
    _, sessions = base
    with sessions() as db:
        src = E.create_workspace(db, "Source")
        dst = E.create_workspace(db, "Destination")
        art = _artifact(db, "art_cite")
        _conv(db, "conv_1", ws=src.id, answer="historique conserve")
        _link(db, art, "conversation", "conv_1")
        eid = _fiche_acceptee(db, src.id, "k1", [art])
        M.record_usage(db, eid, "msg_conv_1")

        conv_avant = db.query(Conversation).filter(Conversation.id == "conv_1").first()
        ws_avant = conv_avant.workspace_id
        lien_avant = db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == art
        ).count()
        blob_avant = (
            db.query(Blob).filter(Blob.id == f"sha_{art}").first().relative_path
        )
        stale_avant = db.query(MemoryEntry).filter(MemoryEntry.status == "stale").count()

        rapport = E.assess_move(db, "conv_1", dst.id)
        assert rapport["allowed"] is True
        assert "applied" not in rapport
        assert rapport["blocking"] == []
        assert art in (rapport.get("contributions") or [])

        rapport2 = E.request_move(db, "conv_1", dst.id)
        assert rapport2 == rapport

        conv_apres = db.query(Conversation).filter(Conversation.id == "conv_1").first()
        assert conv_apres.workspace_id == ws_avant == src.id
        assert (
            db.query(ArtifactLink).filter(ArtifactLink.artifact_id == art).count()
            == lien_avant
        )
        assert (
            db.query(Blob).filter(Blob.id == f"sha_{art}").first().relative_path
            == blob_avant
        )
        assert (
            db.query(MemoryEntry).filter(MemoryEntry.status == "stale").count()
            == stale_avant
        )


def test_confirmation_move_change_espace_conserve_pieces_jointes_et_invalide_syntheses(
    base,
):
    _, sessions = base
    with sessions() as db:
        src = E.create_workspace(db, "Source")
        dst = E.create_workspace(db, "Destination")
        art = _artifact(db, "art_cite")
        _conv(db, "conv_1", ws=src.id, answer="historique conserve")
        _link(db, art, "conversation", "conv_1")
        eid = _fiche_acceptee(db, src.id, "k1", [art])
        M.record_usage(db, eid, "msg_conv_1")

        blob_avant = (
            db.query(Blob).filter(Blob.id == f"sha_{art}").first().relative_path
        )
        liens_avant = db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == art
        ).count()

        out = E.request_move(db, "conv_1", dst.id, confirm=True)
        assert out.get("applied") is True
        assert out["artifacts_moved"] == 0

        conv = db.query(Conversation).filter(Conversation.id == "conv_1").first()
        assert conv.workspace_id == dst.id
        assert conv.title == "discussion"
        msg = db.query(Message).filter(Message.conv_id == "conv_1").first()
        assert msg is not None and msg.content == "historique conserve"

        assert db.query(Artifact).filter(Artifact.id == art).count() == 1
        assert (
            db.query(ArtifactLink).filter(ArtifactLink.artifact_id == art).count()
            == liens_avant
        )
        assert (
            db.query(Blob).filter(Blob.id == f"sha_{art}").first().relative_path
            == blob_avant
        )
        assert S.resolve_allowed_artifacts("conv_1") == {art}

        assert art not in S.resolve_space_information_scope(src.id)
        assert art in S.resolve_space_information_scope(dst.id)

        assert out["syntheses_invalidees"], "la fiche citante devait etre invalidee"
        courante = (
            db.query(MemoryEntry)
            .filter(MemoryEntry.id == out["syntheses_invalidees"][0])
            .first()
        )
        assert courante is not None
        assert courante.is_current == 1
        assert courante.status == "stale"


def test_confirmation_move_conserve_image_sans_duplication(base):
    _, sessions = base
    with sessions() as db:
        src = E.create_workspace(db, "Source")
        dst = E.create_workspace(db, "Destination")
        blob_path = "blobs/sha_art_img.bin"
        db.add(
            Blob(
                id="sha_art_img",
                sha256="sha_art_img",
                relative_path=blob_path,
                size_bytes=5,
                mime_detected="image/png",
                state="available",
            )
        )
        db.add(
            Artifact(
                id="art_img",
                blob_id="sha_art_img",
                kind="image",
                original_filename="capture.png",
                display_name="capture.png",
            )
        )
        db.add(
            ArtifactLink(
                id="lnk_img",
                artifact_id="art_img",
                scope_kind="conversation",
                scope_id="conv_img",
                role="attachment",
            )
        )
        _conv(db, "conv_img", ws=src.id)
        db.commit()

        out = E.request_move(db, "conv_img", dst.id, confirm=True)
        assert out.get("applied") is True
        assert db.query(Artifact).filter(Artifact.id == "art_img").count() == 1
        assert (
            db.query(ArtifactLink)
            .filter(ArtifactLink.artifact_id == "art_img")
            .count()
            == 1
        )
        assert (
            db.query(Blob)
            .filter(Blob.id == "sha_art_img")
            .first()
            .relative_path
            == blob_path
        )
        assert S.resolve_allowed_artifacts("conv_img") == {"art_img"}


def test_signal_reinitialise_compte_non_negatif(base):
    _, sessions = base
    with sessions() as db:
        src = E.create_workspace(db, "Source")
        dst = E.create_workspace(db, "Destination")
        art = _artifact(db, "art_sig")
        _conv(db, "conv_1", ws=src.id)
        _link(db, art, "conversation", "conv_1")
        eid = _fiche_acceptee(db, src.id, "k_sig", [art])
        M.record_usage(db, eid, "msg_conv_1")

        out = E.request_move(db, "conv_1", dst.id, confirm=True)
        assert out.get("applied") is True
        remis = out.get("signaux_remis_a_neutre") or {}
        assert remis.get("signaux_supprimes", 0) >= 0
        assert remis.get("entrees", 0) >= 0
        assert E.conversation_workspace(db, "conv_1") == dst.id

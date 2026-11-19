from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    DocRef,
    Message,
    MessageAttachment,
    Observation,
)
from backend.inference import compaction as C
from backend.inference import loop as L
from backend.inference.brief import validate_submit_brief
from backend.search import chunks as chunking
from backend.search import docrefs


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'public_ids.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", factory)
    return factory


def _artifact(db, artifact_id: str, filename: str) -> None:
    db.add(Blob(
        id=f"blob_{artifact_id}",
        sha256=f"sha_{artifact_id}",
        relative_path=f"blobs/{artifact_id}.bin",
        size_bytes=1,
        mime_detected="text/plain",
    ))
    db.add(Artifact(
        id=artifact_id,
        blob_id=f"blob_{artifact_id}",
        kind="text",
        original_filename=filename,
        display_name=filename,
    ))


def _brief(**updates) -> dict:
    value = {"brief": "Fait vérifié. Provenance : observation de ce tour."}
    value.update(updates)
    return value


def test_doc_labels_are_conversation_local_and_scope_bound(session_factory):
    db = session_factory()
    try:
        db.add(Conversation(id="conv_a", title="a", mode="chat"))
        db.add(Conversation(id="conv_b", title="b", mode="chat"))
        _artifact(db, "art_shared", "shared.txt")
        _artifact(db, "art_a2", "a2.txt")
        db.add(ArtifactLink(id="lnk_a_shared", artifact_id="art_shared",
                            scope_kind="conversation", scope_id="conv_a"))
        db.add(ArtifactLink(id="lnk_b_shared", artifact_id="art_shared",
                            scope_kind="conversation", scope_id="conv_b"))
        db.add(ArtifactLink(id="lnk_a2", artifact_id="art_a2",
                            scope_kind="conversation", scope_id="conv_a"))
        db.commit()

        assert docrefs.allocate_doc_label(db, "conv_a", "art_shared") == "doc_1"
        assert docrefs.allocate_doc_label(db, "conv_b", "art_shared") == "doc_1"
        assert docrefs.allocate_doc_label(db, "conv_a", "art_a2") == "doc_2"
        assert docrefs.resolve_doc_label(db, "conv_a", "doc_1") == "art_shared"
        assert docrefs.resolve_doc_label(db, "conv_b", "doc_1") == "art_shared"
        assert docrefs.resolve_doc_label(db, "conv_b", "doc_2") is None
        assert docrefs.public_id_for_artifact(db, "conv_b", "art_a2") is None
        assert [row["public_id"] for row in docrefs.backfill_from_links(db, "conv_a")] == ["doc_1", "doc_2"]
        assert [row["public_id"] for row in docrefs.list_for_conv(db, "conv_b")] == ["doc_1"]
    finally:
        db.close()


def test_brief_rejects_technical_and_out_of_scope_public_ids():
    """La signature v1.2 ferme TOUTES les fenêtres d'un coup : seule la clé `brief`
    existe, donc un id technique n'a plus d'emplacement où passer pour « preuve »
    ou « fichier pertinent ». Le périmètre, lui, vit toujours dans le lecteur
    (`scope`), et l'interdit-id utilisateur dans le prompt générateur.
    """
    ok, code, _ = validate_submit_brief(
        _brief(artifact_ids_relevant=["art_secret"]),
    )
    assert (ok, code) == (False, "UNKNOWN_ARG")

    ok, code, _ = validate_submit_brief(
        _brief(artifact_ids_relevant=["doc_2"]),
    )
    assert (ok, code) == (False, "UNKNOWN_ARG")

    ok, code, oknormeds = validate_submit_brief(_brief())
    assert (ok, code) == (True, "ok")
    assert set(oknormeds) == {"brief"}


def test_compaction_uses_public_id_and_persisted_brief_json(session_factory):
    exact_json = '{\n  "language": "fr",\n  "facts": []\n}'
    db = session_factory()
    try:
        db.add(Conversation(id="conv_compact", title="c", mode="chat"))
        db.add(Message(
            id="m_brief",
            conv_id="conv_compact",
            role="assistant",
            content="réponse",
            brief="rendu non contractuel",
            brief_json=exact_json,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        db.close()

    assert C.load_recent_briefs("conv_compact") == [exact_json]
    prompt = C.build_compaction_prompt(
        [{"role": "user", "content": "ancien"}],
        briefs=[exact_json],
        file_refs=[{"id": "art_secret", "public_id": "doc_7", "name": "photo.png"}],
    )
    body = prompt.split("## Historique visible à compacter", 1)[1]
    assert exact_json in body
    assert "public_id: doc_7" in body
    assert "art_secret" not in body

    redacted_prompt = C.build_compaction_prompt(
        [{"role": "user", "content": "ancien art_secret et sti_internal"}],
        briefs=[],
        file_refs=[],
    )
    redacted_body = redacted_prompt.split("## Historique visible à compacter", 1)[1]
    assert "art_secret" not in redacted_body
    assert "sti_internal" not in redacted_body
    assert C._summary_has_only_allowed_public_ids("doc_7", [{"public_id": "doc_7"}]) is True
    assert C._summary_has_only_allowed_public_ids("doc_8", [{"public_id": "doc_7"}]) is False


def test_search_and_find_render_public_ids_only():
    from backend.tools.find_files import render_find_files
    from backend.tools.search_info import render_search_info

    search_text = render_search_info(
        [{"query": "x", "candidates": 1, "results": [{
            "rank": 1,
            "public_id": "doc_4",
            "artifact_id": "art_secret",
            "evidence_id": "doc_4:0",
            "name": "f.txt",
            "type": "text",
        }]}],
        1,
    )
    find_text = render_find_files(
        [{"rank": 1, "public_id": "doc_4", "artifact_id": "art_secret", "name": "f.txt"}],
        1,
        False,
        "f",
        None,
    )
    assert "doc_4" in search_text and "art_secret" not in search_text
    assert "doc_4" in find_text and "art_secret" not in find_text


# ---------- Un artefact retiré ne récupère jamais son doc_N ----------

def _seed_removed_artifact(factory, conv_id: str = "conv_rm", artifact_id: str = "art_rm") -> None:
    """Pièce jointe d'un tour précédent, mise à la corbeille (lien removed_at)."""
    db = factory()
    try:
        db.add(Conversation(id=conv_id, title="c", mode="chat"))
        _artifact(db, artifact_id, "note.txt")
        db.add(ArtifactLink(
            id=f"lnk_{artifact_id}", artifact_id=artifact_id,
            scope_kind="conversation", scope_id=conv_id))
        db.add(Message(id="m_rm", conv_id=conv_id, role="user", content="x", status="completed"))
        db.add(MessageAttachment(id="ma_rm", message_id="m_rm", artifact_id=artifact_id, ordinal=0))
        db.commit()
        # Retrait utilisateur : le lien sort du périmètre, la pièce jointe reste en base.
        db.query(ArtifactLink).filter(ArtifactLink.artifact_id == artifact_id).one().removed_at = \
            datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _trash_artifact(factory, artifact_id: str, conv_id: str) -> None:
    """Retrait utilisateur : le lien sort du périmètre, la pièce jointe reste en base."""
    db = factory()
    try:
        db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == artifact_id,
            ArtifactLink.scope_id == conv_id,
        ).one().removed_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def test_removed_artifact_never_regains_a_doc_label(session_factory):
    _seed_removed_artifact(session_factory)
    db = session_factory()
    try:
        with pytest.raises(ValueError):
            docrefs.allocate_doc_label(db, "conv_rm", "art_rm")
        assert docrefs.public_id_for_artifact(db, "conv_rm", "art_rm") is None
        assert docrefs.resolve_doc_label(db, "conv_rm", "doc_1") is None
        assert docrefs.list_for_conv(db, "conv_rm") == []
        assert docrefs.backfill_from_links(db, "conv_rm") == []
        assert db.query(DocRef).filter(DocRef.conv_id == "conv_rm").first() is None
        assert db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == "art_rm",
            ArtifactLink.removed_at.is_(None),
        ).first() is None
    finally:
        db.close()


def test_label_attribue_ne_revit_pas_apres_retrait(session_factory):
    """Un doc_N déjà attribué disparaît du périmètre au retrait, sans être ressuscité."""
    db = session_factory()
    try:
        db.add(Conversation(id="conv_lbl", title="c", mode="chat"))
        _artifact(db, "art_lbl", "note.txt")
        db.add(ArtifactLink(id="lnk_lbl", artifact_id="art_lbl",
                            scope_kind="conversation", scope_id="conv_lbl"))
        db.commit()
        assert docrefs.allocate_doc_label(db, "conv_lbl", "art_lbl") == "doc_1"
        db.commit()
    finally:
        db.close()
    _trash_artifact(session_factory, "art_lbl", "conv_lbl")
    db = session_factory()
    try:
        assert docrefs.resolve_doc_label(db, "conv_lbl", "doc_1") is None
        assert docrefs.list_for_conv(db, "conv_lbl") == []
        with pytest.raises(ValueError):
            docrefs.allocate_doc_label(db, "conv_lbl", "art_lbl")
        link = db.query(ArtifactLink).filter(ArtifactLink.artifact_id == "art_lbl").one()
        assert link.removed_at is not None  # le retrait n'est jamais annulé en lecture
    finally:
        db.close()


def test_registres_courant_et_historique_ignorent_les_artefacts_retires(session_factory):
    _seed_removed_artifact(session_factory)
    assert L.build_current_artifact_entries("conv_rm", ["art_rm"]) == []
    assert L.build_history_artifact_entries("conv_rm") == []
    # Le registre historique reste alimenté tant que le lien est actif.
    db = session_factory()
    try:
        db.query(ArtifactLink).filter(ArtifactLink.artifact_id == "art_rm").one().removed_at = None
        db.commit()
    finally:
        db.close()
    entries = L.build_history_artifact_entries("conv_rm")
    assert [entry["public_id"] for entry in entries] == ["doc_1"]
    assert L.build_current_artifact_entries("conv_rm", ["art_rm"])[0]["public_id"] == "doc_1"


def test_artefact_supprime_ne_rend_aucun_doc_label(session_factory):
    db = session_factory()
    try:
        db.add(Conversation(id="conv_del", title="c", mode="chat"))
        _artifact(db, "art_del", "vieux.txt")
        db.add(ArtifactLink(id="lnk_del", artifact_id="art_del",
                            scope_kind="conversation", scope_id="conv_del"))
        db.commit()
        db.query(Artifact).filter(Artifact.id == "art_del").one().deleted_at = \
            datetime.now(timezone.utc)
        db.commit()
        with pytest.raises(ValueError):
            docrefs.allocate_doc_label(db, "conv_del", "art_del")
        assert docrefs.public_id_for_artifact(db, "conv_del", "art_del") is None
    finally:
        db.close()
    assert L.build_current_artifact_entries("conv_del", ["art_del"]) == []


# ---------- Une preuve doit exister : fragment ingéré ou observation persistée ----------

def _seed_chunks(db, artifact_id: str, texts: list[str]) -> None:
    for ordinal, text in enumerate(texts):
        db.add(ArtifactChunk(id=f"chk_{artifact_id}_{ordinal}", artifact_id=artifact_id,
                             card_revision=1, ordinal=ordinal, text=text, context=""))


def test_chunk_evidence_points_at_persisted_fragments(session_factory):
    db = session_factory()
    try:
        db.add(Conversation(id="conv_chk", title="c", mode="chat"))
        _artifact(db, "art_chk", "rapport.txt")
        db.add(ArtifactLink(id="lnk_chk", artifact_id="art_chk",
                            scope_kind="conversation", scope_id="conv_chk"))
        _seed_chunks(db, "art_chk", [
            "Le CA du T2 s'élève à 45 000 euros selon le rapport annuel.",
            "Le budget de l'année suivante reste à confirmer.",
        ])
        db.commit()
        assert chunking.persisted_ordinals(db, "art_chk") == {0, 1}
        assert chunking.ordinal_of_quote(db, "art_chk", "45 000 euros") == 0
        # Citation tronquée à l'ingestion : la sonde longue échoue, la courte retrouve le fragment.
        assert chunking.ordinal_of_quote(db, "art_chk", "budget de l'année suivante") == 1
        # Aucun fragment ne contient cette phrase → aucune preuve, jamais un rang deviné.
        assert chunking.ordinal_of_quote(db, "art_chk", "chiffre d'affaires du T3") is None
        assert chunking.ordinal_of_quote(db, "art_chk", "45 000") is None  # trop court pour prouver
    finally:
        db.close()


def test_ask_files_binds_answer_to_persisted_chunk(session_factory):
    from backend.tools.ask_files import _answer_one, _chunk_evidence_id

    class _Art:
        original_filename = "rapport.txt"
        display_name = "rapport.txt"

    db = session_factory()
    try:
        db.add(Conversation(id="conv_ask", title="c", mode="chat"))
        _artifact(db, "art_ask", "rapport.txt")
        db.add(ArtifactLink(id="lnk_ask", artifact_id="art_ask",
                            scope_kind="conversation", scope_id="conv_ask"))
        _seed_chunks(db, "art_ask", [
            "Le CA du T2 s'élève à 45 000 euros selon le rapport annuel.",
            "Le budget de l'année suivante reste à confirmer.",
        ])
        db.commit()
        text = "Le CA du T2 s'élève à 45 000 euros selon le rapport annuel. Autre phrase sans rapport."
        unproven = _answer_one("rapport annuel ?", "doc_1", _Art(), text, "text")
        assert "45 000" in unproven["answer"] and unproven["evidence_id"] is None
        assert _chunk_evidence_id(db, "doc_1", "art_ask", str(unproven["answer"])) == "doc_1:0"
        assert _chunk_evidence_id(db, "doc_1", "art_ask", "phrase absente de tout fragment") is None
    finally:
        db.close()


def test_une_citation_reveille_le_fragment_persiste_pas_une_invention(session_factory):
    """La règle de la preuve persistée vit à la SOURCE (`search/chunks`) : un rang
    sans fragment n'existe pour personne. Depuis la signature v1.2 (`{"brief"}`),
    le handoff du brief ne conduit plus ce contrôle — sa fenêtre est fermée, et
    c'est le rang ÉMIS par l'outil (test_ask_files_binds_answer_to_persisted_chunk
    ci-dessus) qui est déjà garanti persisté.
    """
    db = session_factory()
    try:
        db.add(Conversation(id="conv_ev", title="c", mode="chat"))
        _artifact(db, "art_ev", "rapport.txt")
        db.add(ArtifactLink(id="lnk_ev", artifact_id="art_ev",
                            scope_kind="conversation", scope_id="conv_ev"))
        _seed_chunks(db, "art_ev", ["Le CA du T2 s'élève à 45 000 euros."])
        db.commit()
        assert docrefs.allocate_doc_label(db, "conv_ev", "art_ev") == "doc_1"
        db.commit()
    finally:
        db.close()
    from backend.search import chunks as _chunks

    db = session_factory()
    try:
        # Le fragment réellement ingéré se résout ; le rang inventé n'existe pas.
        assert _chunks.ordinal_of_quote(db, "art_ev", "45 000 euros") == 0
        assert _chunks.persisted_ordinals(db, "art_ev") == {0}
        # Un doc_N:k d'un autre périmètre ne résout rien non plus.
        assert _chunks.ordinal_of_quote(db, "art_ev", "budget de l'an prochain") is None
    finally:
        db.close()
    # La fenêtre v1.2 : impossible de déposer la citation au schéma du handoff.
    ok, code, _ = validate_submit_brief(
        _brief(facts=[{"evidence_ids": ["doc_1:7"]}]))
    assert (ok, code) == (False, "UNKNOWN_ARG")


def test_search_info_evidence_falls_back_on_source_observation(session_factory):
    db = session_factory()
    try:
        db.add(Conversation(id="conv_src", title="c", mode="chat"))
        _artifact(db, "art_src", "photo.png")
        db.add(ArtifactLink(id="lnk_src", artifact_id="art_src",
                            scope_kind="conversation", scope_id="conv_src"))
        db.add(Observation(id="obs_src", artifact_id="art_src", tool_name="vision",
                           status="completed", payload_ref="papillon sur une fleur"))
        db.commit()
        assert docrefs.allocate_doc_label(db, "conv_src", "art_src") == "doc_1"
        db.commit()
    finally:
        db.close()
    from backend.db.models import ArtifactCard
    from backend.db.database import SessionLocal
    db = SessionLocal()
    try:
        db.add(ArtifactCard(id="card_src", artifact_id="art_src", revision=1, is_current=1,
                            title="photo.png", summary="Papillon macro", source_observation_id="obs_src"))
        db.commit()
        # Aucun fragment ingéré : la preuve est l'observation dont la fiche est issue.
        assert L._persisted_evidence_id(db, "art_src", "doc_1", "Papillon macro") == "obs_src"
    finally:
        db.close()
    assert L._persisted_evidence_id(None, "art_src", "doc_1", "Papillon macro") is None


def test_compaction_rejects_invented_observation_ids(session_factory):
    db = session_factory()
    try:
        db.add(Conversation(id="conv_inv", title="c", mode="chat"))
        _artifact(db, "art_inv", "photo.png")
        db.add(Observation(id="obs_reel", artifact_id="art_inv", tool_name="vision",
                           status="completed", payload_ref="papillon"))
        db.commit()
    finally:
        db.close()
    assert C._summary_has_only_persisted_observations("preuve : obs_reel") is True
    assert C._summary_has_only_persisted_observations("preuve : obs_reel:1") is True
    assert C._summary_has_only_persisted_observations("preuve : obs_fabrique") is False
    assert C._summary_has_only_persisted_observations("aucune preuve citée") is True

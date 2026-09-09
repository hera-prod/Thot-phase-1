"""Tests robustes du pipeline média — sans modèle, sans GPU.

Verrouille les régressions vues en prod (logs q-r.md) :
- KeyError 'id' dans format_attachment_registry (toast 'Erreur' avec message "'id'")
- LLM 8B qui émet [vision(...)] textuel au lieu de tool_call → routage déterministe
- get_source/read_text qui ignoraient les art_xxx (table Source seule)
- Follow-ups sans nouveaux fichiers qui perdaient les outils (registre vide)
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    Message,
    MessageAttachment,
)
from backend.inference import loop as L


# ---------- Helpers ----------

def _entry(art_id="art_abc123def456", kind="image", name="photo.png", available=True, first="current"):
    return {
        "id": art_id,
        "ordinal": 1,
        "index": 1,
        "kind": kind,
        "name": name,
        "path": "blobs/ab/ab.bin",
        "first_seen_in_message": first,
        "message_index": 1,
        "available": available,
        "previous_analyses": [],
    }


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    """Vraie DB SQLite temporaire + data_dir temporaire, branchées via monkeypatch."""
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    return tmp_path, data_dir, TestSession


def _seed_conv_with_artifact(TestSession, data_dir, conv_id="conv1", art_id="art_abc123def456", kind="image"):
    blob_file = data_dir / "blobs" / "ab" / "ab.bin"
    blob_file.write_bytes(b"fake-bytes")
    db = TestSession()
    try:
        db.add(Conversation(id=conv_id, title="t", mode="chat"))
        db.add(Blob(id="shaX", sha256="shaX", relative_path="blobs/ab/ab.bin", size_bytes=10, mime_detected="image/png"))
        db.add(Artifact(id=art_id, blob_id="shaX", kind=kind, original_filename="photo.png", display_name="photo.png"))
        db.add(ArtifactLink(id="lnk1", artifact_id=art_id, scope_kind="conversation", scope_id=conv_id))
        msg = Message(id="m1", conv_id=conv_id, role="user", content="hello", status="completed")
        db.add(msg)
        db.add(MessageAttachment(id="ma1", message_id="m1", artifact_id=art_id, ordinal=0))
        db.commit()
    finally:
        db.close()


# ---------- Routage déterministe (pur) ----------

def test_plan_image_vers_vision():
    plan = L.plan_media_tools("Décris cette image", [_entry(kind="image")])
    assert plan == [{"tool": "vision", "artifact_id": "art_abc123def456", "name": "photo.png"}]


def test_plan_pdf_vers_doc_ocr():
    plan = L.plan_media_tools("Décris", [_entry(kind="pdf", name="doc.pdf")])
    assert len(plan) == 1 and plan[0]["tool"] == "doc_ocr"


def test_plan_image_avec_demande_texte_vers_doc_ocr():
    plan = L.plan_media_tools("Extrais le texte de l'image", [_entry(kind="image")])
    assert plan[0]["tool"] == "doc_ocr"


def test_plan_ignore_indisponibles_et_vide():
    assert L.plan_media_tools("Décris", [_entry(available=False)]) == []
    assert L.plan_media_tools("Décris", []) == []


def test_pick_reinspection_derniere_par_defaut():
    h = [_entry("art_1", name="a.png", first="m1"), _entry("art_2", name="b.png", first="m2")]
    assert L.pick_reinspection_entry("Et là ?", h)["id"] == "art_2"


def test_pick_reinspection_ordinaux():
    h = [_entry("art_1", first="m1"), _entry("art_2", first="m2")]
    assert L.pick_reinspection_entry("la première image", h)["id"] == "art_1"
    assert L.pick_reinspection_entry("la deuxième image", h)["id"] == "art_2"


def test_pick_reinspection_vide():
    assert L.pick_reinspection_entry("cette image", []) is None


# ---------- Registre : non-régression KeyError 'id' ----------

def test_format_registry_artifact_schema_sans_keyerror():
    out = L.format_attachment_registry([_entry()])
    assert "art_abc123def456" in out
    assert "artifact_id" in out


def test_format_registry_vide():
    assert L.format_attachment_registry([]) == "Aucun fichier dans cette conversation."


# ---------- Historique artefacts (follow-ups) ----------

def test_history_entries_remonte_artefacts_conv(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_conv_with_artifact(TestSession, data_dir)
    entries = L.build_history_artifact_entries("conv1")
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == "art_abc123def456"
    assert e["available"] is True
    assert e["message_index"] == 1
    # Schéma compatible format_attachment_registry (pas de KeyError)
    L.format_attachment_registry(entries)


def test_history_entries_exclut_ids_courants(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_conv_with_artifact(TestSession, data_dir)
    assert L.build_history_artifact_entries("conv1", exclude_ids={"art_abc123def456"}) == []


def test_history_entries_conv_inconnue_vide(tmp_repo):
    _, _, _ = tmp_repo
    assert L.build_history_artifact_entries("conv-inexistante") == []


# ---------- get_source / read_text résolvent art_xxx ----------

def test_get_source_resout_artifact(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_conv_with_artifact(TestSession, data_dir)
    res = L._execute_get_source({"args": {"source_id": "art_abc123def456"}})
    assert res["ok"] is True
    assert "working_path" in res["result"]


def test_get_source_introuvable(tmp_repo):
    _, _, _ = tmp_repo
    res = L._execute_get_source({"args": {"source_id": "art_inexistant"}})
    assert res["ok"] is False


def test_read_text_resout_artifact(tmp_repo):
    tmp_path, data_dir, TestSession = tmp_repo
    (data_dir / "blobs" / "ab" / "ab.bin").write_text("contenu texte")
    db = TestSession()
    try:
        db.add(Conversation(id="c2", title="t", mode="chat"))
        db.add(Blob(id="shaT", sha256="shaT", relative_path="blobs/ab/ab.bin", size_bytes=13, mime_detected="text/plain"))
        db.add(Artifact(id="art_txt000001", blob_id="shaT", kind="text", original_filename="note.txt", display_name="note.txt"))
        db.commit()
    finally:
        db.close()
    res = L._execute_read_text({"args": {"source_id": "art_txt000001"}})
    assert res["ok"] is True
    assert "contenu texte" in res["result"]


# ---------- Exécuteurs sans modèle (chemins d'échec) ----------

def test_execute_vision_artifact_inconnu_sans_manager():
    res = L._execute_vision(
        manager=None,
        tool_call={"args": {"artifact_id": "art_inexistant", "query": "Décris"}},
        image_paths=[],
        request_id="req-test",
        attachment_ids=[],
    )
    assert res["ok"] is False
    assert "introuvable" in res["result"]


def test_execute_doc_ocr_artifact_inconnu_sans_manager():
    res = L._execute_doc_ocr(
        manager=None,
        tool_call={"args": {"artifact_id": "art_inexistant"}},
        request_id="req-test",
    )
    assert res["ok"] is False


def test_execute_vision_hallucination_fallback_sans_manager(tmp_repo, tmp_path):
    """Si le LLM hallucine un image_path, fallback sur le vrai fichier (valid_paths[0])."""
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG" + b"\x00" * 100)
    # Pas de manager : on vérifie seulement que la résolution ne crash pas avant le use_model.
    # Ici le fichier existe donc ça ira jusqu'au use_model → on s'arrête avant via artifact inconnu.
    res = L._execute_vision(
        manager=None,
        tool_call={"args": {"artifact_id": "art_inexistant", "image_path": "/tmp/nimporte_quoi.png"}},
        image_paths=[str(real)],
        request_id="req-test",
        attachment_ids=[],
    )
    # valid_paths=[real] existe mais requested hallucine → fallback strict tente real → use_model(None) lève → erreur encapsulée
    assert res["ok"] is False
    assert "result" in res and "latency_ms" in res


# ---------- Détection d'intention ----------

def test_asks_for_text_extraction():
    assert L.asks_for_text_extraction("Transcris ce document") is True
    assert L.asks_for_text_extraction("Qu'en penses-tu ?") is False


def test_requires_media_reinspection():
    assert L.requires_media_reinspection("Et cette image, elle montre quoi ?") is True
    assert L.requires_media_reinspection("Bonjour") is False


def test_resolve_reinspection_tool():
    assert L.resolve_reinspection_tool("Lis le texte du panneau") == "doc_ocr"
    assert L.resolve_reinspection_tool("Que vois-tu ?") == "vision"

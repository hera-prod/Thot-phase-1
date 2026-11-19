"""Tests trace orchestrateur — persistance bulle UI (thinking/plan/brief + toolcalls).

Sans modèle : les helpers de trace n'ont besoin que de la DB.
Couvre : migration colonnes, écriture ToolCall pendant le run, backfill msg_id,
contrat de retour (collected, trace), bornes.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Artifact, Base, Blob, Conversation, Message, MessageAttachment, Run, Source, ToolCall
from backend.inference import loop as L


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts, _migrate_search_columns
    _migrate_search_columns(engine)
    _ensure_fts(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    return TestSession


def _cols(engine, table):
    from sqlalchemy import text as _text
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(_text(f"PRAGMA table_info({table})")).fetchall()}


def test_migration_colonnes_trace(tmp_repo, tmp_path):
    from sqlalchemy import create_engine as _ce
    engine = _ce(f"sqlite:///{tmp_path}/test.db")
    assert {"orchestrator_thinking", "brief", "brief_json", "plan_json"} <= _cols(engine, "messages")
    assert "run_id" in _cols(engine, "toolcalls")


def test_toolcall_attempt_and_ordinal_are_stable(tmp_repo):
    L._begin_toolcall("run-stable", "call-stable", "find_files", {"query": "x"}, 3)
    L._persist_toolcall("run-stable", "call-stable", "find_files", {"query": "x"}, {"ok": True, "result": "ok", "latency_ms": 1})
    db = tmp_repo()
    try:
        row = db.query(ToolCall).filter(ToolCall.run_id == "run-stable").one()
        assert row.attempt == 1
        assert row.ordinal == 3
        assert row.status == "completed"
    finally:
        db.close()


def test_persist_toolcall_pendant_run(tmp_repo):
    TestSession = tmp_repo
    L._persist_toolcall("run1", "call1", "vision",
                        {"artifact_id": "art_x", "query": "quoi ?"},
                        {"ok": True, "result": "un chat roux", "latency_ms": 120})
    db = TestSession()
    try:
        rows = db.query(ToolCall).filter(ToolCall.run_id == "run1").all()
        assert len(rows) == 1
        assert rows[0].msg_id is None  # backfill par main.py AVANT terminal
        assert rows[0].tool == "vision" and rows[0].ok is True
        # Les arguments persistés passent par le même sanitizeur que l'emit live :
        # un art_… stocké brut réapparaîtrait en clair au rechargement (m14).
        assert rows[0].args["artifact_id"] == "[identifiant interne retiré]"
    finally:
        db.close()


def test_emit_plan_retient_dernier(tmp_repo, capsys):
    trace = L._empty_trace()
    L._emit_plan({"request_id": "r", "intent": "general", "reason": "x",
                  "attachment_kind": None, "selected_tool": None,
                  "selected_model": None}, trace)
    assert trace["plan"]["intent"] == "general"
    out = capsys.readouterr().out
    assert "execution_plan" in out  # toujours émis vers l'UI en direct


def test_append_thinking_borne(tmp_repo):
    trace = L._empty_trace()
    L._append_orch_thinking(trace, "")
    assert trace["orchestrator_thinking"] == ""
    L._append_orch_thinking(trace, "décision : vision")
    L._append_orch_thinking(trace, "brief prêt")
    assert "vision" in trace["orchestrator_thinking"]
    L._append_orch_thinking(trace, "y" * 9000)
    assert len(trace["orchestrator_thinking"]) <= L.TRACE_THINKING_CHARS


def test_persist_assistant_trace_backfill(tmp_repo):
    TestSession = tmp_repo
    db = TestSession()
    try:
        db.add(Conversation(id="conv1", title="t", mode="chat"))
        db.commit()
    finally:
        db.close()
    L._persist_toolcall("run9", "call9", "doc_ocr", {"artifact_id": "art_y"},
                        {"ok": True, "result": "texte", "latency_ms": 5})
    from backend.main import _persist_assistant_trace
    db = TestSession()
    try:
        _persist_assistant_trace(db, "conv1", "voici la réponse", "completed", "run9",
                                 {"orchestrator_thinking": "contenus itérations",
                                  "brief": "brief exact",
                                  "brief_json": "{\"facts\": []}",
                                  "plan": {"intent": "media_available", "reason": "r"}})
        db.commit()
        msg = db.query(Message).filter(Message.run_id == "run9",
                                       Message.role == "assistant").first()
        assert msg is not None and msg.brief == "brief exact"
        assert msg.orchestrator_thinking == "contenus itérations"
        assert msg.brief_json == '{"facts": []}'
        assert json.loads(msg.plan_json)["intent"] == "media_available"
        tc = db.query(ToolCall).filter(ToolCall.run_id == "run9").first()
        assert tc.msg_id == msg.id  # backfill AVANT terminal
    finally:
        db.close()


def test_persist_generated_image_on_assistant_message(tmp_repo):
    TestSession = tmp_repo
    db = TestSession()
    try:
        db.add(Conversation(id="conv-img", title="t", mode="chat"))
        db.add(Run(id="run-img", conv_id="conv-img", request_id="run-img", kind="chat", status="running", runtime_id="llama_cpp", runtime_fingerprint="llama_cpp:LFM"))
        db.add(Blob(id="blob-img", sha256="ab" * 32, relative_path="blobs/ab/x.bin", size_bytes=3, mime_detected="image/png"))
        db.add(Artifact(id="art-img", blob_id="blob-img", kind="image", original_filename="image.png", display_name="Image"))
        db.commit()
    finally:
        db.close()
    from backend.main import _persist_assistant_trace
    db = TestSession()
    try:
        _persist_assistant_trace(db, "conv-img", "image créée", "completed", "run-img", {"image_artifact_id": "art-img"})
        db.commit()
        run = db.query(Run).filter(Run.id == "run-img").one()
        attachment = db.query(MessageAttachment).filter(MessageAttachment.artifact_id == "art-img").one()
        assert run.status == "completed"
        assert run.output_artifact_id == "art-img"
        assert attachment.message_id == db.query(Message).filter(Message.run_id == "run-img", Message.role == "assistant").one().id
    finally:
        db.close()


def test_contrat_retour_tuple(tmp_repo):
    # Chemin sans modèle : message vide → (collected, trace)
    collected, trace = L.run_agent_loop(None, conv_id="c", request_id="r",
                                        message="  ",
                                        image_paths=None, history_shared=[])
    assert collected == "" and trace == L._empty_trace()


# ---------- m14 :vivant et rechargement affichent les MÊMES arguments ----------


def test_persist_toolcall_sanitise_les_arguments(tmp_repo):
    """Le chemin live et la ligne rechargée doivent afficher la même chose.

    Live : emit("tool_call", sanitize_tool_arguments(args)) → masqué.
    Reload : l'UI relit ToolCall.args verbatim (ToolCallDisplay lit args.query).
    Sans sanitisation à l'écriture, le même tour s'affichait masqué puis en clair.
    """
    TestSession = tmp_repo
    L._persist_toolcall("run-san", "call-san", "vision",
                        {"artifact_id": "art_x",
                         "query": "que montre art_x ?"},
                        {"ok": True, "result": "un chat roux", "latency_ms": 7})
    db = TestSession()
    try:
        row = db.query(ToolCall).filter(ToolCall.run_id == "run-san").one()
        stored = row.args["query"]
        assert "art_x" not in stored
        assert stored == L.sanitize_tool_arguments(
            {"artifact_id": "art_x", "query": "que montre art_x ?"})["query"]
        assert row.args["query"] == "que montre [identifiant interne retiré] ?"
        # Identique au contenu que l'emit live produit pour la même entrée.
        assert row.args == L.sanitize_tool_arguments(
            {"artifact_id": "art_x", "query": "que montre art_x ?"})
    finally:
        db.close()


def test_begin_toolcall_sanitise_les_arguments(tmp_repo):
    """Le même contrat tient sur la ligne « running », écrite par _begin_toolcall.

    Une ligne demeurée « running » (crash, annulation) est relue telle quelle :
    elle ne doit pas être la seule à garder l'identifiant en clair.
    """
    TestSession = tmp_repo
    L._begin_toolcall("run-begin", "call-begin", "doc_ocr", {"artifact_id": "sti_z"})
    db = TestSession()
    try:
        row = db.query(ToolCall).filter(ToolCall.run_id == "run-begin").one()
        assert row.status == "running"
        assert "sti_z" not in str(row.args)
    finally:
        db.close()


def test_repli_cache_media_survit_a_la_sanitisation(tmp_repo):
    """Le seul relecteur de ToolCall.args reste exact après masquage.

    build_attachment_registry retombe sur un ToolCall d'un AUTRE message quand la
    source n'a pas le sien ; le Criterion est `s.sha256 in str(args) or
    s.path in str(args)`. Un sha256 est hexadécimal et un chemin persistant vaut
    media/{sha256}.ext : ni l'un ni l'autre n'est exprimable en art_…/sti_…, donc
    le masque ne peut pas casser ce repli. PÉPIN : le ToolCall doit avoir un
    msg_id DIFFÉRENT de la source, sinon le repli n'est jamais emprunté.
    """
    TestSession = tmp_repo
    sha = "9f" * 32
    db = TestSession()
    try:
        db.add(Conversation(id="conv-reg", title="t", mode="chat"))
        db.add(Message(id="msg-src", conv_id="conv-reg", role="user", content="voici"))
        db.add(Message(id="msg-tool", conv_id="conv-reg", role="assistant", content="vu"))
        db.add(Source(id="src-1", msg_id="msg-src", conversation_id="conv-reg",
                      source_type="attachment", path=f"media/{sha}.png",
                      media_kind="image", sha256=sha, relative_path=f"media/{sha}.png"))
        db.commit()
    finally:
        db.close()

    L._persist_toolcall("run-reg", "call-reg", "vision",
                        {"artifact_id": "art_q", "query": f"lis {sha} et media/{sha}.png"},
                        {"ok": True, "result": "texte lu", "latency_ms": 3})
    db = TestSession()
    try:
        tc = db.query(ToolCall).filter(ToolCall.run_id == "run-reg").one()
        tc.msg_id = "msg-tool"
        db.commit()
        registry = L.build_attachment_registry("conv-reg")
        entry = next(e for e in registry if e.get("id") == "src-1")
        # Le repli a bien emprunté la ligne d'un autre message, arguments masqués.
        assert [a["tool"] for a in entry.get("previous_analyses", [])] == ["vision"]
    finally:
        db.close()

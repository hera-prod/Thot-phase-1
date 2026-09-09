"""Tests T-SRCH — moteur de recherche documentaire (annexe 11, Lot A).

Sans modèle, sans GPU : DB SQLite temporaire + FTS5 réel.
Couvre T-SRCH-01..07, 09, 10, 15, 17 (+ curseur, pagination, get_source limitée).
Lot B : 08 (volume/pagination profonde), 11 (Evidence), 12 (injection),
        13/14 (corbeille/GC), 16 (hybride).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    Message,
    MessageAttachment,
    Observation,
)
from backend.inference import loop as L
from backend.search import cards as C
from backend.search import fts as F
from backend.search import observations as O
from backend.search import scope as S


# ---------- Fixture ----------

@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    """Vraie DB temporaire + data_dir temporaire, branchées via monkeypatch."""
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    # Migration colonnes annexe 11 + FTS5 + tables chunks/vecteurs sur le moteur de test
    from backend.db.database import _ensure_fts, _migrate_search_columns
    from backend.search import chunks as _chkT
    _migrate_search_columns(engine)
    _ensure_fts(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    _s = TestSession()
    try:
        _chkT.ensure_chunk_tables(_s)
        _s.commit()
    finally:
        _s.close()
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    return tmp_path, data_dir, TestSession


def _seed_artifact(TestSession, data_dir, conv_id="conv1", art_id="art_facture001",
                   kind="pdf", name="Facture_EDF_juin_2026.pdf", with_bytes=True):
    """Un artefact lié à une conversation (sans fiche — self-healing testé)."""
    if with_bytes:
        (data_dir / "blobs" / "ab" / "ab.bin").write_bytes(b"%PDF-fake-facture")
    db = TestSession()
    try:
        if db.get(Conversation, conv_id) is None:
            db.add(Conversation(id=conv_id, title="t", mode="chat"))
        sha = f"sha-{art_id}"
        db.add(Blob(id=sha, sha256=sha, relative_path="blobs/ab/ab.bin",
                    size_bytes=17, mime_detected="application/pdf", state="available"))
        db.add(Artifact(id=art_id, blob_id=sha, kind=kind,
                        original_filename=name, display_name=name))
        db.add(ArtifactLink(id=f"lnk-{art_id}", artifact_id=art_id,
                            scope_kind="conversation", scope_id=conv_id, role="attachment"))
        # Le vrai flux (main.py) attache l'artefact au message d'envoi
        msg_id = f"msg-{art_id}"
        if db.get(Message, msg_id) is None:
            db.add(Message(id=msg_id, conv_id=conv_id, role="user",
                           content="voici un fichier", run_id=f"run-{art_id}", status="done"))
        db.add(MessageAttachment(id=f"ma-{art_id}", message_id=msg_id,
                                 artifact_id=art_id, ordinal=1))
        db.commit()
    finally:
        db.close()
    return art_id


# ---------- T-SRCH-01 : fiche minimale à l'import ----------

def test_01_fiche_minimale_import(tmp_repo):
    _, _, TestSession = tmp_repo
    art_id = _seed_artifact(TestSession, tmp_repo[1])
    db = TestSession()
    try:
        art = db.query(Artifact).filter(Artifact.id == art_id).first()
        card = C.ensure_card_on_import(db, art)
        db.commit()
        assert card.revision == 1
        assert card.analysis_status == "pending"
        assert "facture" in (card.keywords_json or "").lower()
        # Indexée par nom : retrouvable sans accent ni analyse
        F.ensure_fts(db)
        page = F.search(db, query="facture", scope_kind="conversation",
                        scope_id="conv1", allowed_ids={art_id})
        assert page["ok"] and page["returned_count"] == 1
        assert page["results"][0]["artifact_id"] == art_id
    finally:
        db.close()


# ---------- T-SRCH-02 : OCR → observation → fiche N+1 ----------

def test_02_observation_fiche_suivante(tmp_repo):
    _, _, TestSession = tmp_repo
    art_id = _seed_artifact(TestSession, tmp_repo[1])
    obs = O.record_tool_observation(art_id, "doc_ocr", "montant facture",
                                    "Montant total : 84,20 EUR. Échéance 15/07/2026.",
                                    True, run_id="run1", tool_call_id="call1",
                                    model_id="GLM-OCR")
    assert obs is not None and obs.status == "completed"
    db = TestSession()
    try:
        stored = db.query(Observation).filter(Observation.artifact_id == art_id).all()
        assert len(stored) == 1
        assert stored[0].run_id == "run1"
        card = C.get_current_card(db, art_id)
        assert card.revision == 2 and card.analysis_status == "analysed"
        assert card.source_observation_id == stored[0].id
        # L'extrait OCR est cherchable
        page = F.search(db, query="84,20", scope_kind="conversation",
                        scope_id="conv1", allowed_ids={art_id})
        assert page["ok"] and page["returned_count"] == 1
    finally:
        db.close()


# ---------- T-SRCH-03 : historique des fiches conservé ----------

def test_03_historique_fiches(tmp_repo):
    _, _, TestSession = tmp_repo
    art_id = _seed_artifact(TestSession, tmp_repo[1])
    O.record_tool_observation(art_id, "doc_ocr", "q1", "texte partiel…", True)
    O.record_tool_observation(art_id, "vision", "q2", "photo floue", False)
    db = TestSession()
    try:
        cards = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == art_id).all()
        assert len(cards) == 3  # rev1 import + 2 révisions
        current = [c for c in cards if int(c.is_current or 0) == 1]
        assert len(current) == 1 and current[0].revision == 3
        assert current[0].analysis_status == "failed"
    finally:
        db.close()


# ---------- T-SRCH-04 : insensible aux accents ----------

def test_04_sans_accents(tmp_repo):
    _, _, TestSession = tmp_repo
    art_id = _seed_artifact(TestSession, tmp_repo[1], name="Facture_électricité_juin.pdf")
    db = TestSession()
    try:
        art = db.query(Artifact).filter(Artifact.id == art_id).first()
        C.ensure_card_on_import(db, art)
        db.commit()
        page = F.search(db, query="electricite", scope_kind="conversation",
                        scope_id="conv1", allowed_ids={art_id})
        assert page["ok"] and page["returned_count"] == 1
    finally:
        db.close()


# ---------- T-SRCH-05 : filtre types ----------

def test_05_filtre_types(tmp_repo):
    tmp_path, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir, art_id="art_pdf001", kind="pdf", name="devis.pdf")
    _seed_artifact(TestSession, data_dir, conv_id="conv1", art_id="art_img001",
                   kind="image", name="devis.png", with_bytes=False)
    (data_dir / "blobs" / "ab" / "ab.bin").write_bytes(b"x")
    db = TestSession()
    try:
        for aid in ("art_pdf001", "art_img001"):
            art = db.query(Artifact).filter(Artifact.id == aid).first()
            C.ensure_card_on_import(db, art)
        db.commit()
        page = F.search(db, query="devis", scope_kind="conversation", scope_id="conv1",
                        allowed_ids={"art_pdf001", "art_img001"}, types=["pdf"])
        assert page["ok"]
        assert {r["artifact_id"] for r in page["results"]} == {"art_pdf001"}
    finally:
        db.close()


# ---------- T-SRCH-06 : scope strict entre conversations ----------

def test_06_scope_strict(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir, conv_id="conv1", art_id="art_a001", name="rapport.pdf")
    _seed_artifact(TestSession, data_dir, conv_id="conv2", art_id="art_b001", name="rapport.pdf")
    db = TestSession()
    try:
        for aid in ("art_a001", "art_b001"):
            C.ensure_card_on_import(db, db.query(Artifact).filter(Artifact.id == aid).first())
        db.commit()
        allowed = S.resolve_allowed_artifacts("conv1")
        assert allowed == {"art_a001"}
        page = F.search(db, query="rapport", scope_kind="conversation", scope_id="conv1",
                        allowed_ids=allowed)
        assert {r["artifact_id"] for r in page["results"]} == {"art_a001"}
    finally:
        db.close()


# ---------- T-SRCH-07 : id deviné hors scope ----------

def test_07_id_devine_refuse(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir, conv_id="conv2", art_id="art_secret001",
                   name="secret.pdf")
    # get_source mono-fichier hors conv → refus sans révéler l'existence
    res = L._execute_get_source({"args": {"source_id": "art_secret001", "reason": "lire"}}, conv_id="conv1")
    assert not res["ok"] and "ARTIFACT_NOT_IN_SCOPE" in res["result"]
    assert "secret" not in res["result"].lower() or "non accessible" in res["result"].lower()
    # search_documents ne voit rien non plus
    res2 = L._execute_search_documents({"args": {"query": "secret", "purpose": "t"}}, conv_id="conv1")
    assert res2["ok"] and '"returned_count": 0' in res2["result"]


# ---------- T-SRCH-09 : zéro résultat = périmètre + pistes ----------

def test_09_zero_resultat_borne(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir)
    db = TestSession()
    try:
        C.ensure_card_on_import(db, db.query(Artifact).first())
        db.commit()
        page = F.search(db, query="zxy introuvable", scope_kind="conversation",
                        scope_id="conv1", allowed_ids={"art_facture001"})
        assert page["ok"] and page["results"] == []
        assert "suggestions" in page and len(page["suggestions"]) >= 1
        assert "searched_count" in page
    finally:
        db.close()


# ---------- T-SRCH-10 : raisons de match ----------

def test_10_raisons_match(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir, art_id="art_001", name="Facture_EDF.pdf")
    _seed_artifact(TestSession, data_dir, art_id="art_002", name="Facture_GDF.pdf")
    _seed_artifact(TestSession, data_dir, art_id="art_003", name="Photo_plage.png",
                   kind="image")
    db = TestSession()
    try:
        for aid in ("art_001", "art_002", "art_003"):
            C.ensure_card_on_import(db, db.query(Artifact).filter(Artifact.id == aid).first())
        db.commit()
        page = F.search(db, query="facture", scope_kind="conversation", scope_id="conv1",
                        allowed_ids={"art_001", "art_002", "art_003"})
        assert page["ok"] and page["returned_count"] == 2
        for r in page["results"]:
            assert r["match_reasons"], "chaque candidat explique son match"
            assert r["card"]["revision"] >= 1
    finally:
        db.close()


# ---------- Pagination : curseur stable + mismatch ----------

def test_pagination_curseur(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    for i in range(3):
        _seed_artifact(TestSession, data_dir, art_id=f"art_p{i}", name=f"doc_{i}.pdf")
    db = TestSession()
    try:
        for i in range(3):
            C.ensure_card_on_import(db, db.query(Artifact).filter(Artifact.id == f"art_p{i}").first())
        db.commit()
        ids = {f"art_p{i}" for i in range(3)}
        p1 = F.search(db, query="doc", scope_kind="conversation", scope_id="conv1",
                      allowed_ids=ids, limit=2)
        assert p1["returned_count"] == 2 and p1["next_cursor"]
        p2 = F.search(db, query="doc", scope_kind="conversation", scope_id="conv1",
                      allowed_ids=ids, limit=2, cursor=p1["next_cursor"])
        assert p2["returned_count"] == 1 and p2["next_cursor"] is None
        assert {r["artifact_id"] for r in p2["results"]} not in (
            {r["artifact_id"] for r in p1["results"]},)
        bad = F.search(db, query="autre", scope_kind="conversation", scope_id="conv1",
                       allowed_ids=ids, cursor=p1["next_cursor"])
        assert not bad["ok"] and bad["code"] == "SEARCH_CURSOR_MISMATCH"
    finally:
        db.close()


# ---------- get_source Lot A : sans chemin, list-all borné ----------

def test_get_source_sans_chemin(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir)
    res = L._execute_get_source({"args": {"source_id": "art_facture001", "reason": "fiche"}}, conv_id="conv1")
    assert res["ok"]
    assert "working_path" not in res["result"]
    assert "artifact_id" in res["result"]


def test_get_source_list_borne(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    for i in range(25):
        _seed_artifact(TestSession, data_dir, art_id=f"art_l{i:02d}", name=f"f{i}.pdf")
    res = L._execute_get_source({"args": {"reason": "liste"}}, conv_id="conv1")
    assert res["ok"]
    assert "search_documents" in res["result"]  # renvoi vers la recherche au-delà de 20


# ---------- T-SRCH-15 : utile sans embeddings ----------

def test_15_sans_embeddings(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_artifact(TestSession, data_dir, kind="text", name="notes_mobilenet.md")
    db = TestSession()
    try:
        C.ensure_card_on_import(db, db.query(Artifact).first())
        db.commit()
        page = F.search(db, query="mobilenet", scope_kind="conversation",
                        scope_id="conv1", allowed_ids={"art_facture001"})
        assert page["ok"] and page["returned_count"] == 1
    finally:
        db.close()


# ---------- T-SRCH-17 : parité prompt/schémas ----------

def test_17_parite_recherche():
    from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
    from backend.tools.search_documents import SearchDocumentsArgs, SearchDocumentsTool
    assert "search_documents" in SYSTEM_ORCHESTRATOR
    assert "get_source" in SYSTEM_ORCHESTRATOR
    assert "artifact_id" in SYSTEM_ORCHESTRATOR
    assert "n'invente JAMAIS" in SYSTEM_ORCHESTRATOR
    schema = SearchDocumentsArgs.model_json_schema()["properties"]
    assert schema["query"]["maxLength"] == 300
    assert schema["limit"]["maximum"] == 20
    assert "artifact_id" in SearchDocumentsTool().description


# ---------- Empreinte déterministe (réutilisation) ----------

def test_fingerprint_deterministe():
    h1 = O.fingerprint("shaX", "vision", "VL", "p", "q")
    h2 = O.fingerprint("shaX", "vision", "VL", "p", "q")
    h3 = O.fingerprint("shaY", "vision", "VL", "p", "q")
    assert h1 == h2 and h1 != h3

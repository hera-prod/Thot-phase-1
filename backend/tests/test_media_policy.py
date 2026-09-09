"""Tests de la stratégie toolcalls média (media_policy.py) — pur, sans modèle.

Couvre les cas réels du log q-r.md qui ne relançaient jamais vision :
- T2 "quelle est la couleur de cheveux de la personne au fond ?" (couche C)
- T4 "Revoit l'image." (couche B)
"""
from __future__ import annotations

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
from backend.inference import media_policy as P
from backend.inference import loop as L


# ---------- Couche A : référence explicite ----------

@pytest.mark.parametrize("msg", [
    "Que vois-tu dans cette image ?",
    "Décris-moi dans l'image ce qu'il y a",
    "Sur l'image, qui est au fond ?",
    "Montre la première image",
    "Et celle-là ?",
])
def test_couche_a_reference_explicite(msg):
    assert P.has_explicit_reference(msg) is True
    assert P.needs_reinspection(msg) is True


# ---------- Couche B : verbes de réexamen ----------

@pytest.mark.parametrize("msg", [
    "Revoit l'image.",          # q-r.md T4 — ne matchait PAS l'ancienne liste
    "Revois la photo stp",
    "Regarde à nouveau le fond",
    "Relis le document",
])
def test_couche_b_verbe_reexamen(msg):
    assert P.has_revisit_verb(msg) is True
    assert P.needs_reinspection(msg) is True


# ---------- Couche C : détail visuel ----------

@pytest.mark.parametrize("msg", [
    "quelle est la couleur de cheveux de la personne au fond ?",  # q-r.md T2
    "Les cheveux de la personne au fond sur l'escalier ?",        # q-r.md T3
    "Que tient l'homme au premier plan ?",
    "Combien de personnes sont à gauche ?",
    "Où est le chat ?",
])
def test_couche_c_detail_visuel(msg):
    assert P.asks_visual_detail(msg) is True
    assert P.needs_reinspection(msg) is True


# ---------- Négatifs ----------

@pytest.mark.parametrize("msg", [
    "Bonjour",
    "Résume notre conversation",
    "Quelle heure est-il ?",
    "",
])
def test_pas_de_reinspection(msg):
    assert P.needs_reinspection(msg) is False


# ---------- Compat loop.py (wrappers) ----------

def test_wrappers_loop_deleguent():
    assert L.requires_media_reinspection("Revoit l'image.") is True
    assert L.requires_media_reinspection("Bonjour") is False
    assert L.resolve_reinspection_tool("Lis le panneau") == "doc_ocr"
    assert L.resolve_reinspection_tool("Que vois-tu ?") == "vision"
    h = [{"id": "a1", "available": True}, {"id": "a2", "available": True}]
    assert L.pick_reinspection_entry("première image", h)["id"] == "a1"
    assert L.pick_reinspection_entry("Et là ?", h)["id"] == "a2"


def test_resolve_tool_for():
    assert P.resolve_tool_for("pdf", "Décris") == "doc_ocr"
    assert P.resolve_tool_for("image", "Décris") == "vision"
    assert P.resolve_tool_for("image", "Extrais le texte", lambda m: True) == "doc_ocr"


# ---------- get_source list-all (R3) ----------

@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    (data_dir / "blobs" / "ab" / "ab.bin").write_bytes(b"x")
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    db = TestSession()
    try:
        db.add(Conversation(id="c1", title="t", mode="chat"))
        db.add(Blob(id="s1", sha256="s1", relative_path="blobs/ab/ab.bin", size_bytes=1, mime_detected="image/png"))
        db.add(Artifact(id="art_list000001", blob_id="s1", kind="image", original_filename="a.png", display_name="a.png"))
        db.add(ArtifactLink(id="l1", artifact_id="art_list000001", scope_kind="conversation", scope_id="c1"))
        db.add(Message(id="m1", conv_id="c1", role="user", content="hi", status="completed"))
        db.add(MessageAttachment(id="ma1", message_id="m1", artifact_id="art_list000001", ordinal=0))
        db.commit()
    finally:
        db.close()
    return TestSession


def test_get_source_list_all(tmp_repo):
    res = L._execute_get_source({"args": {"reason": "voir les fichiers"}}, conv_id="c1")
    assert res["ok"] is True
    assert "art_list000001" in res["result"]


def test_get_source_list_all_sans_conv():
    res = L._execute_get_source({"args": {"reason": "x"}})
    assert res["ok"] is False


def test_get_source_par_id_toujours_ok(tmp_repo):
    res = L._execute_get_source({"args": {"source_id": "art_list000001", "reason": "x"}}, conv_id="c1")
    assert res["ok"] is True


# ---------- Cadrage requête VLM (pas de contexte LLM, pas de déixis) ----------

@pytest.mark.parametrize("raw,expected", [
    # q-r.md : la question brute envoyée au VLM perdait le modèle
    ("Et sur la première image peux-tu me dire la couleur du parasol sur le verre à droite ?",
     "La couleur du parasol sur le verre à droite ?"),
    ("Peux-tu revoir la seconde image et me dire la couleur des cheveux de la jeune fille derière dans les escaliers.",
     "La couleur des cheveux de la jeune fille derière dans les escaliers."),
    ("Est-ce que tu arriverais à lire ce qu'il y a d'écrit en japonais sur cette image ?",
     "Lire ce qu'il y a d'écrit en japonais ?"),
    ("Revoit l'image.", "Décris cette image en détail."),
    ("Et cette image ?", "Décris cette image en détail."),
    ("Décris moi cette image.", "Décris moi."),
    ("", "Décris cette image en détail."),
])
def test_clean_media_query(raw, expected):
    assert P.clean_media_query(raw) == expected


def test_clean_media_query_garde_question_simple():
    assert P.clean_media_query("Quelle est la couleur du comptoir ?") == "Quelle est la couleur du comptoir ?"


# ---------- Routage OCR élargi (q-r.md : japonais partait en vision) ----------

@pytest.mark.parametrize("msg", [
    "Est-ce que tu arriverais à lire ce qu'il y a d'écrit en japonais sur cette image ?",
    "Transcris ce document",
    "Lis le panneau",
])
def test_ocr_request_detectee(msg):
    assert L.asks_for_text_extraction(msg) is True


@pytest.mark.parametrize("msg", [
    "Qu'en penses-tu ?",
    "Que vois-tu ?",
    "Décris moi cette image.",
])
def test_ocr_request_negative(msg):
    assert L.asks_for_text_extraction(msg) is False


def test_plan_ocr_pour_lecture_japonais():
    from backend.inference.loop import plan_media_tools
    e = {"id": "art_x", "kind": "image", "name": "a.png", "available": True}
    plan = plan_media_tools("lire ce qu'il y a d'écrit en japonais", [e])
    assert plan and plan[0]["tool"] == "doc_ocr"


# ---------- decide_reinspection : scénario q-r.md « seconde image » ----------

def test_decide_seconde_image_sans_nouveau_fichier():
    from backend.inference.media_policy import decide_reinspection
    from backend.inference.loop import asks_for_text_extraction
    hist = [
        {"id": "art_1", "kind": "image", "name": "a.png", "available": True},
        {"id": "art_2", "kind": "image", "name": "b.png", "available": True},
    ]
    dec = decide_reinspection(
        "Peux-tu revoir la seconde image et me dire la couleur des cheveux ?",
        [], hist, asks_for_text_extraction,
    )
    assert dec is not None
    assert dec["artifact_id"] == "art_2"
    assert dec["tool"] == "vision"


def test_decide_rien_si_nouveau_fichier_ou_sans_historique():
    from backend.inference.media_policy import decide_reinspection
    from backend.inference.loop import asks_for_text_extraction
    cur = [{"id": "art_9", "kind": "image", "name": "n.png", "available": True}]
    assert decide_reinspection("Et là ?", cur, [], asks_for_text_extraction) is None
    assert decide_reinspection("Et là ?", [], [], asks_for_text_extraction) is None
    assert decide_reinspection("Bonjour", [], [{"id": "a", "available": True}], asks_for_text_extraction) is None

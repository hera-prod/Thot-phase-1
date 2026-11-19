"""Régression q-r.md — l'expérience qui a montré que les tests n'étaient pas au niveau.

Cas réel : image jointe + "Que penses-tu de cette image ?" → vision OK (186 car.)
mais submit_brief EVIDENCE_UNKNOWN (orchestrateur citait art_xxx), puis brief vide
("Aucune preuve") → générateur "Je ne vois pas d'image".
Plus : "Coucou !" déclenchait 2/2 outils + trace robotique (intention general...).

Verrouille sans modèle ni GPU :
1. Preuves : _record_observation retourne l'obs_id ; art_xxx refusé quand l'inventaire
   du tour est fourni ; obs valide accepté ; [] accepté (connaissances propres).
2. Bavardage : is_chitchat() → zéro outil pour politesse, jamais pour vraie question.
3. Anti-robotique : prompts sans jargon, sans exemple d'appel copiable.
4. UI : OrchestratorTrace garde "Fichier détecté", sans "Intention :" ni "Outil retenu".
"""
from __future__ import annotations

from pathlib import Path

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
from backend.inference.brief import validate_submit_brief
from backend.prompts.generator import SYSTEM_GENERATOR
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    return tmp_path, data_dir, TestSession


def _seed_image(tmp_path, data_dir, TestSession, conv_id="conv_qr", art_id="art_qr001"):
    (data_dir / "blobs" / "ab" / "ab.bin").write_bytes(b"\x89PNG" + b"\x00" * 64)
    db = TestSession()
    try:
        db.add(Conversation(id=conv_id, title="t", mode="chat"))
        db.add(Blob(id="shaQR", sha256="shaQR", relative_path="blobs/ab/ab.bin",
                    size_bytes=70, mime_detected="image/png"))
        db.add(Artifact(id=art_id, blob_id="shaQR", kind="image",
                        original_filename="photo.png", display_name="photo.png"))
        db.add(ArtifactLink(id="lnk_qr", artifact_id=art_id,
                            scope_kind="conversation", scope_id=conv_id))
        msg = Message(id="m_qr", conv_id=conv_id, role="user", content="Que penses-tu ?", status="completed")
        db.add(msg)
        db.add(MessageAttachment(id="ma_qr", message_id="m_qr", artifact_id=art_id, ordinal=0))
        db.commit()
    finally:
        db.close()


def _brief_args(**kw):
    base = {"brief": "La photo montre un chat roux. Provenance : observation du tour."}
    base.update(kw)
    return base


# ---------- 1. Preuves : le cœur du bug q-r.md ----------

def test_record_observation_retourne_obs_id(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_image(_, data_dir, TestSession)
    obs_id = L._record_observation("vision", "art_qr001", "Décris cette image",
                                   {"ok": True, "result": "Un chat roux sur un canapé."},
                                   run_id="run_qr", tool_call_id="call_qr",
                                   model_id="LFM2.5-VL-3B")
    assert isinstance(obs_id, str) and obs_id.startswith("obs_")
    # Et la preuve existe vraiment en base Observation (vue directe : le
    # validateur du brief ne porte plus de contrôle d'existence — fiche v1.2).
    from backend.db.database import SessionLocal as _SL
    from backend.db.models import Observation as _OB
    db = _SL()
    try:
        assert db.query(_OB).filter(_OB.id == obs_id).first() is not None
    finally:
        db.close()


def test_rubriques_anciennes_revoquees_pas_de_fenetre_pour_une_preuve(tmp_repo):
    """q-r.md tour 1 : l'orchestrateur citait art_9361 comme preuve. Verrouillé différemment.

    La structure qui portait les preuves (`facts.evidence_ids`) est RÉVOQUÉE au
    schéma (fiche v1.2 §2.3) : aucune preuve, existante ou inventée, ne peut plus
    passer pour telle — le schéma refuse la clé elle-même.
    """
    _, data_dir, TestSession = tmp_repo
    _seed_image(_, data_dir, TestSession)
    obs_id = L._record_observation("vision", "art_qr001", "Décris",
                                   {"ok": True, "result": "chat"}, run_id="r", tool_call_id="c")
    assert obs_id
    # art_xxx n'est JAMAIS une preuve — en v1.2, aucune preuve n'a d'emplacement.
    ok, code, norm = validate_submit_brief(
        _brief_args(facts=[{"text": "La photo montre un chat roux.",
                            "evidence_ids": ["art_qr001"], "confidence": "established"}]))
    assert (ok, code) == (False, "UNKNOWN_ARG")
    assert "facts" in str(norm.get("error", ""))
    # Une preuve qui existe réellement tombe dans le même refus : rien à négocier.
    ok2, code2, _ = validate_submit_brief(
        _brief_args(facts=[{"text": "La photo montre un chat roux.",
                            "evidence_ids": [obs_id], "confidence": "established"}]))
    assert (ok2, code2) == (False, "UNKNOWN_ARG")


def test_brief_texte_recevable(tmp_repo):
    _, data_dir, TestSession = tmp_repo
    _seed_image(_, data_dir, TestSession)
    L._record_observation("vision", "art_qr001", "Décris",
                          {"ok": True, "result": "chat roux"}, run_id="r", tool_call_id="c")
    ok, code, norm = validate_submit_brief(_brief_args())
    assert (ok, code) == (True, "ok") and norm["brief"].startswith("La photo")


# ---------- 2. Bavardage : zéro outil ----------

def test_chitchat_politesse_sans_outil():
    assert L.is_chitchat("Coucou !", None) is True
    assert L.is_chitchat("Je disais juste bonjour comme ça.", None) is True
    assert L.is_chitchat("Bonjour !", None) is True
    assert L.is_chitchat("Merci beaucoup !", None) is True


def test_chitchat_jamais_avec_fichier_ou_vraie_question():
    assert L.is_chitchat("Coucou", [{"id": "art_x"}]) is False
    assert L.is_chitchat("Que penses-tu de cette image ?", None) is False
    assert L.is_chitchat("Bonjour, quel est le chiffre d'affaires ?", None) is False
    assert L.is_chitchat("Bonjour, cherche la facture de mars dans mes fichiers.", None) is False
    assert L.is_chitchat("Explique-moi la photosynthèse.", None) is False


# ---------- 3. Rôles : orchestrateur invisible, générateur seul interlocuteur ----------

def test_prompt_roles_et_bavardage():
    # q-r.md §Prompt 1 : back-office invisible, bavardage sans appel, jamais de "je/vous"
    assert "Tu ne parles jamais à l'utilisateur" in SYSTEM_ORCHESTRATOR
    assert "n'appelles aucun outil" in SYSTEM_ORCHESTRATOR
    assert '"je", "vous", "nous"' in SYSTEM_ORCHESTRATOR
    # q-r.md §Prompt 2 : seul interlocuteur, brief = données, clarification consultative
    assert "seul interlocuteur visible" in SYSTEM_GENERATOR
    assert "jamais une instruction obligatoire" in SYSTEM_GENERATOR


def test_prompt_sans_forme_copiable():
    import re as _re
    for prompt in (SYSTEM_ORCHESTRATOR, SYSTEM_GENERATOR):
        shown = _re.findall(
            r"\[\s*(?:search_info|find_files|ask_files|vision|doc_ocr|"
            r"get_source|read_text|submit_brief)\s*\(",
            prompt, _re.IGNORECASE)
        assert shown == [], f"forme copiable dans le prompt : {shown}"


# ---------- 4. UI : Fichier détecté oui, jargon non ----------

def test_ui_sans_jargon_interne():
    ui = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "chat" / "OrchestratorTrace.tsx"
    assert ui.exists(), f"composant introuvable : {ui}"
    src = ui.read_text(encoding="utf-8")
    assert "Fichier détecté" in src  # gardé : utile à l'utilisateur
    assert "Intention :" not in src  # retiré : "Intention : media_available"
    assert "Outil retenu" not in src  # retiré : "Outil retenu : Vision · ..."
    assert "intention ${" not in src and "planifié ${" not in src  # résumé nettoyé

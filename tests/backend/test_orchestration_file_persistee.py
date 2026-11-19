"""File d'attente persistée — orchestration.md §2.1 et LIM-011 (tâche 5.2).

D'OÙ VIENT CE FICHIER : « Un seul tour à la fois et file des demandes… elle
entre dans une file d'attente » (orchestration.md §2.1) — en théorie. En
pratique, la ligne Run n'était écrite que PAR `_ensure_chat_run` à l'entrée du
tour, donc l'existence d'un run en file n'était JAMAIS persistée : un crash
entre la validation et l'exécution laissait le tour muet, et l'état visible
par l'interface (depuis `runs.status`, chemin existant, aucun nouveau verbe)
ne montrait rien.

CE QUE LES TESTS VERROUILLENT :

  1. un Run soumis est écrit `queued` AVANT l'exécution, et son état survit à
     un redémarrage (lecture persistante via une session NEUVE).
  2. au passage à l'exécution, `_ensure_chat_run` re-vérifie la destination : un
     Run dont la discussion est corbeillée ne s'exécute pas — le refus est NOMMÉ
     (`DISCUSSION_EN_CORBEILLE`), pas une panne générique.

LIM-011 ne tranche NI l'ordre NI les priorités NI les bornes (orchestration.md
§2.1 : « l'ordre exact de la file, les priorités, les bornes et les actions
d'annulation restent à spécifier ») : ces tests ne testent RIEN sur la
politique d'ordonnancement.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import models as _models
from backend.db.models import Base, Conversation, Run

CONV = "conv_file_persistee"

# Empreinte de runtime cohérente avec `runtime_id` : la re-vérification d'identité
# (RUNTIME_MISMATCH si divergent) doit rester levée même APRES la re-vérification
# corbeille qui précède.
RUNTIME_ID = "llama_cpp"
RUNTIME_FP = "llama_cpp:LFM2.5-8B-simule"


@pytest.fixture()
def fileue(tmp_path, monkeypatch):
    """Une base en fichier : le redémarrage est une session neuve sur la même base."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'file.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", session)
    return SimpleNamespace(
        engine=engine, sessions=session, fichier=tmp_path / "file.db"
    )


def _admission(
    fileue, run_id: str, conv_id: str = CONV, fingerprint: str = "c11-fp"
) -> None:
    """La conversation cible, puis l'admission du Run en file (code REAL prod)."""
    from backend.main import persistance_queued

    with fileue.sessions() as db:
        db.add(Conversation(id=conv_id, title="essai", mode="chat"))
        db.commit()
    persistance_queued(run_id, conv_id, kind="chat", fingerprint=fingerprint)


def _lire_run(fileue, run_id: str) -> Run | None:
    with fileue.sessions() as db:
        ligne = db.query(Run).filter(Run.id == run_id).first()
        if ligne is None:
            return None
        # Détacher consciemment : lecture à froid, identique à un redémarrage.
        db.expunge(ligne)
        return ligne


# ---------- 1. l'admission écrit `queued`, AVANT l'exécution ----------


def test_admission_persiste_le_run_en_queued_avant_exécution(fileue):
    run_id = "req_file_1"
    _admission(fileue, run_id)
    ligne = _lire_run(fileue, run_id)
    assert ligne is not None, "le Run n'a pas été persisté à l'admission"
    assert ligne.status == "queued", (
        f"état d'admission attendu `queued`, lu `{ligne.status}`"
    )
    assert ligne.kind == "chat"
    assert ligne.fingerprint == "c11-fp"
    assert ligne.conv_id == CONV


def test_un_redemarrage_retient_le_run_en_queued(fileue):
    """Le redémarrage est simulé par une session NEUVE : même base, autre lecture.

    orchestration.md §2.1 : « cette file est une responsabilité applicative, pas
    un outil ni une liste improvisée par le LLM » — la persistance est en base,
    pas en mémoire de process.
    """
    run_id = "req_file_2"
    _admission(fileue, run_id)
    # Session séparée (comme un redémarrage) : relecture persistante.
    with fileue.sessions() as db:
        ligne = db.query(Run).filter(Run.id == run_id).first()
        assert ligne is not None, (
            "le Run est sorti de la base à la création d'une session"
        )
        assert ligne.status == "queued"


# ---------- 2. le passage à l'exécution re-vérifie la destination ----------


def test_le_passage_a_l_execution_transitionne_le_run_file(
    fileue,
):
    run_id = "req_file_3"
    _admission(fileue, run_id)
    from backend.inference.loop import _ensure_chat_run

    _ensure_chat_run(
        run_id,
        CONV,
        runtime_id=RUNTIME_ID,
        runtime_fingerprint=RUNTIME_FP,
        request_fingerprint="c11-fp",
    )
    ligne = _lire_run(fileue, run_id)
    assert ligne is not None, "le Run disparu au passage à l'exécution"
    assert ligne.status == "running", "le Run passé à l'exécution reste `queued`"
    assert ligne.runtime_id == RUNTIME_ID, "l'empreinte d'exécution n'a pas été notée"


def test_un_run_file_sur_discussion_corbeille_ne_s_execute_pas(fileue):
    """Discussion corbeillée PENDANT la file : le refus est nommé au passage."""
    from sqlalchemy import text

    run_id = "req_file_4"
    _admission(fileue, run_id)
    with fileue.sessions() as db:
        db.execute(
            text(
                "UPDATE conversations SET deleted_at = CURRENT_TIMESTAMP WHERE id = :c"
            ),
            {"c": CONV},
        )
        db.commit()

    from backend.inference.loop import _ensure_chat_run

    with pytest.raises(RuntimeError) as refusee:
        _ensure_chat_run(
            run_id,
            CONV,
            runtime_id=RUNTIME_ID,
            runtime_fingerprint=RUNTIME_FP,
            request_fingerprint="c11-fp",
        )
    assert "DISCUSSION_EN_CORBEILLE" in str(refusee.value), (
        f"refus attendu DISCUSSION_EN_CORBEILLE, lu : {refusee.value}"
    )
    # Le run ne poursuit pas : il reste `queued` (non `running`) après le refus.
    ligne = _lire_run(fileue, run_id)
    assert ligne is not None, "le Run refusé a disparu de la base"
    assert ligne.status == "queued", (
        "un Run refusé au passage ne doit pas passer `running`"
    )

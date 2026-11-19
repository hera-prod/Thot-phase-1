"""Le planificateur d'entretien mémoire : candidats, boucle et arrêt.

Sans LLM : les seuils viennent de `memoire_auto`, la sélection est une requête,
et le thread ne fait qu'appeler le chemin déterministe.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base, Workspace
from backend.services import activity
from backend.services import memoire_auto as A
from backend.services import memoire_scheduler as S


@pytest.fixture(autouse=True)
def _verrou_propre():
    A._DERNIER_ETAT.clear()
    yield
    with activity._OWNER_LOCK:
        activity._OWNER = None
    activity.TURN_ACTIVE.clear()
    try:
        activity._RESERVATION_LOCK.release()
    except RuntimeError:
        pass
    A._DERNIER_ETAT.clear()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_scheduler.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()
        # Ne jamais laisser un thread de test vivant entre deux tests.
        S.arreter_planificateur_memoire()
        S.arreter_planificateur_documentaire()


def _espace(
    db,
    workspace_id: str,
    *,
    tours: int,
    activite: datetime,
    state: str = "active",
) -> str:
    db.add(
        Workspace(
            id=workspace_id,
            name=workspace_id,
            state=state,
            auto_memory_turn_count=tours,
            last_activity_at=activite,
            updated_at=activite,
        )
    )
    db.commit()
    return workspace_id


def test_candidats_exige_cinq_tours_et_six_minutes_d_inactivite(db):
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    du = _espace(
        db,
        "ws_du",
        tours=A.SEUIL_TOURS_MEMOIRE,
        activite=maintenant - timedelta(minutes=6),
    )
    _espace(
        db,
        "ws_recent",
        tours=A.SEUIL_TOURS_MEMOIRE,
        activite=maintenant - timedelta(seconds=10),
    )
    _espace(
        db,
        "ws_peu_de_tours",
        tours=A.SEUIL_TOURS_MEMOIRE - 1,
        activite=maintenant - timedelta(minutes=6),
    )
    _espace(
        db,
        "ws_corbeille",
        tours=A.SEUIL_TOURS_MEMOIRE,
        activite=maintenant - timedelta(minutes=6),
        state="trashed",
    )

    assert S._candidats(db, maintenant) == [du]


def test_entretenir_sans_message_remet_le_compteur_a_zero(db):
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    workspace_id = _espace(
        db,
        "ws_sans_message",
        tours=A.SEUIL_TOURS_MEMOIRE,
        activite=maintenant - timedelta(minutes=6),
    )

    out = A.entretenir_memoire(db, workspace_id, maintenant=maintenant)

    assert out["statut"] == "pas_de_nouveaux_messages"
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    assert ws.auto_memory_turn_count == 0


def test_chat_actif_produit_memory_deferred_sans_lever(db, monkeypatch):
    """Le planificateur n'ouvre plus de porte à part : le wrapper dit « différé »."""
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    workspace_id = _espace(
        db,
        "ws_chat_actif",
        tours=A.SEUIL_TOURS_MEMOIRE,
        activite=maintenant - timedelta(minutes=6),
    )
    events: list[tuple[str, dict]] = []

    def fake_emit(event, payload, **kwargs):
        events.append((event, payload))
        return True

    monkeypatch.setattr("backend.inference.ipc.emit", fake_emit)
    chat = activity.try_reserver("chat", "chat:1")
    try:
        S._entretenir_un_espace(workspace_id)
    finally:
        activity.liberer(chat)

    assert [event for event, _ in events] == ["memory_deferred"]
    assert events[0][1]["raison"] == "verrou_occupe"
    assert events[0][1]["bloque_par"] == "chat"


def test_entretenir_un_espace_passe_par_le_wrapper(db, monkeypatch):
    """Le planificateur ne contourne jamais le wrapper d'entretien."""
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    workspace_id = _espace(
        db,
        "ws_wrapper",
        tours=A.SEUIL_TOURS_MEMOIRE,
        activite=maintenant - timedelta(minutes=6),
    )
    appels: list[tuple[str, dict]] = []

    def faux_wrapper(db_arg, ws, **kwargs):
        appels.append((ws, kwargs))
        return {"ok": True, "statut": "effectue"}

    monkeypatch.setattr(A, "executer_entretien_memoire", faux_wrapper)

    S._entretenir_un_espace(workspace_id)

    assert appels == [(workspace_id, {"force": False, "mode": "auto"})]


def test_demarrage_et_arret_idempotents_avec_desactivation(monkeypatch):
    monkeypatch.setenv("THOT_DISABLE_MEMORY_SCHEDULER", "1")

    assert S.demarrer_planificateur_memoire()["statut"] == "desactive"
    assert S.demarrer_planificateur_memoire()["statut"] == "desactive"
    assert S.arreter_planificateur_memoire()["statut"] == "arrete"
    assert S.arreter_planificateur_memoire()["statut"] == "arrete"


def test_demarrage_reel_ne_cree_pas_deux_boucles(monkeypatch):
    monkeypatch.setenv("THOT_DISABLE_MEMORY_SCHEDULER", "0")

    assert S.demarrer_planificateur_memoire()["statut"] == "demarre"
    assert S.demarrer_planificateur_memoire()["statut"] == "deja_demarre"
    assert S.arreter_planificateur_memoire()["statut"] == "arrete"

    assert S._thread is None
    assert S._stop_event.is_set() is True


# ---------------------------------------------------------------------------
# Réveil immédiat de la boucle documentaire
# ---------------------------------------------------------------------------


def _attendre(predicat, timeout_s: float = 5.0) -> bool:
    """Attente bornée sans jamais dormir 300 s dans les tests."""
    fin = time.monotonic() + timeout_s
    while time.monotonic() < fin:
        if predicat():
            return True
        time.sleep(0.01)
    return False


def test_reveiller_extraction_documentaire_pose_l_evenement():
    S._reveil_doc.clear()
    S.reveiller_extraction_documentaire()
    assert S._reveil_doc.is_set()
    S._reveil_doc.clear()


def test_boucle_documentaire_traite_sur_evenement_sans_attendre_le_backfill(
    monkeypatch,
):
    """Un enqueue réveille une passe en < 10 s, pas après 300 s."""
    passes: list[int] = []
    monkeypatch.setattr(S, "_backfiller_documents", lambda: None)
    monkeypatch.setattr(S, "_executer_documents", lambda: passes.append(1))
    monkeypatch.setattr(S, "_INTERVALLE_BACKFILL_DOC_S", 300.0)
    monkeypatch.setattr(S, "_DEBOUNCE_DOC_S", 0.01)
    S._reveil_doc.clear()
    stop = threading.Event()
    thread = threading.Thread(target=S._boucle_documentaire, args=(stop,), daemon=True)
    thread.start()
    try:
        # La passe initiale de la boucle n'est pas le sujet.
        assert _attendre(lambda: bool(passes))
        passes.clear()
        debut = time.monotonic()
        S.reveiller_extraction_documentaire()
        assert _attendre(lambda: bool(passes))
        assert time.monotonic() - debut < 10.0
    finally:
        stop.set()
        S._reveil_doc.set()
        thread.join(5)
        S._reveil_doc.clear()


def test_cinq_enqueues_ne_declenchent_qu_une_passe(monkeypatch):
    """L'Event ne mémorise qu'un bit : une rafale = une passe coalescée."""
    passes: list[int] = []
    monkeypatch.setattr(S, "_backfiller_documents", lambda: None)
    monkeypatch.setattr(S, "_executer_documents", lambda: passes.append(1))
    monkeypatch.setattr(S, "_INTERVALLE_BACKFILL_DOC_S", 300.0)
    monkeypatch.setattr(S, "_DEBOUNCE_DOC_S", 0.01)
    S._reveil_doc.clear()
    stop = threading.Event()
    thread = threading.Thread(target=S._boucle_documentaire, args=(stop,), daemon=True)
    thread.start()
    try:
        assert _attendre(lambda: bool(passes))
        passes.clear()
        for _ in range(5):
            S.reveiller_extraction_documentaire()
        assert _attendre(lambda: bool(passes))
        # Laisse le temps à un éventuel second tour (indésirable) d'arriver.
        time.sleep(0.1)
        assert len(passes) == 1
    finally:
        stop.set()
        S._reveil_doc.set()
        thread.join(5)
        S._reveil_doc.clear()


class _FauxEvenement:
    """Event minimal : rend le timeout observé, sans attendre réellement."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.wait_timeouts: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) >= 2:
            self.stop.set()
        return False

    def set(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def is_set(self) -> bool:
        return False


def test_sans_evenement_le_backfill_300_s_fonctionne(monkeypatch):
    """Le filet périodique reste : timeout 300 s puis backfill."""
    backfills: list[int] = []
    passes: list[int] = []
    monkeypatch.setattr(S, "_backfiller_documents", lambda: backfills.append(1))
    monkeypatch.setattr(S, "_executer_documents", lambda: passes.append(1))
    stop = threading.Event()
    faux = _FauxEvenement(stop)
    monkeypatch.setattr(S, "_reveil_doc", faux)

    S._boucle_documentaire(stop)

    assert faux.wait_timeouts[0] == 300.0
    # Une passe initiale + une passe après expiration du timeout.
    assert len(backfills) == 2
    assert len(passes) == 2


def test_arret_planificateur_documentaire_reveille_le_wait_300(monkeypatch):
    """L'arrêt ne doit pas attendre 300 s : il pose le réveil et joint le thread."""
    passes: list[int] = []
    monkeypatch.setattr(S, "_backfiller_documents", lambda: None)
    monkeypatch.setattr(S, "_executer_documents", lambda: passes.append(1))
    monkeypatch.setattr(S, "_INTERVALLE_BACKFILL_DOC_S", 300.0)
    monkeypatch.setattr(S, "_DEBOUNCE_DOC_S", 0.01)
    monkeypatch.delenv("THOT_DISABLE_MEMORY_DOC_SCHEDULER", raising=False)

    assert S.demarrer_planificateur_documentaire()["statut"] == "demarre"
    try:
        # La passe initiale atteste que la boucle est bien lancée.
        assert _attendre(lambda: bool(passes))
        debut = time.monotonic()
        out = S.arreter_planificateur_documentaire()
        ecoule = time.monotonic() - debut
    finally:
        S.arreter_planificateur_documentaire()
        S._reveil_doc.clear()

    assert out["statut"] == "arrete"
    assert ecoule < 2.0
    assert S._thread_doc is None


def test_execution_documentaire_single_flight(monkeypatch):
    """Deux déclenchements concurrents ne lancent qu'UNE passe."""
    from backend.services import memoire_documentaire as D

    entre = threading.Event()
    libere = threading.Event()
    appels: list[int] = []

    def faux_lot():
        appels.append(1)
        entre.set()
        libere.wait(2)

    monkeypatch.setattr(D, "executer_lot_documentaire", faux_lot)
    # Un test précédent ne doit pas laisser le verrou pris.
    try:
        S._execution_doc_lock.release()
    except RuntimeError:
        pass

    premier = threading.Thread(target=S._executer_documents, daemon=True)
    premier.start()
    try:
        assert entre.wait(2)
        second = threading.Thread(target=S._executer_documents, daemon=True)
        second.start()
        second.join(1)
        assert not second.is_alive()
        assert appels == [1]
    finally:
        libere.set()
        premier.join(2)
        try:
            S._execution_doc_lock.release()
        except RuntimeError:
            pass

    assert appels == [1]

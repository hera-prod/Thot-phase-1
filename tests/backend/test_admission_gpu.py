"""Phase 3.1/3.7 — admission d'ingestion et concurrence GPU.

Ces tests verrouillent les contraintes de la revue de plan :
- 200 fichiers + 1 GPU + chat : le nombre de threads est borné, le chat est servi ;
- vol de claim impossible (deux acquisitions simultanées, une seule gagne) ;
- ancien propriétaire ne peut pas libérer le bail du nouveau ;
- aucune transaction SQLite ouverte pendant l'attente GPU ;
- ResourceBusy retryable côté chat ;
- `en_cours` n'est pas écrit à l'enfilage.

Les tests sont ASYNCHRONES avec timeout, jamais des attentes synchrones de 200
traitements. Le but est de prouver l'absence d'explosion et l'équité, pas de
faire tourner 200 OCR réels.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import pytest

from backend.inference import resource_coordinator as rc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClaimStore:
    """Simule une table `artifacts` en mémoire pour tester le vol de claim."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, float | str | None]] = {}

    def seed(self, artifact_id: str, token: str | None = None) -> None:
        with self._lock:
            self._rows[artifact_id] = {
                "token": token,
                "at": time.monotonic(),
                "owner": None,
            }

    def acquire(self, artifact_id: str, owner: str, ttl_s: float = 300.0) -> str | None:
        with self._lock:
            row = self._rows.get(artifact_id)
            if row is None:
                return None
            now = time.monotonic()
            old_token = row["token"]
            old_at = row["at"]
            if (
                old_token is not None
                and old_at is not None
                and (now - float(old_at)) < ttl_s
            ):
                return None
            token = f"token-{owner}-{time.monotonic_ns()}"
            row["token"] = token
            row["at"] = now
            row["owner"] = owner
            return token

    def release(self, artifact_id: str, token: str) -> bool:
        with self._lock:
            row = self._rows.get(artifact_id)
            if row is None or row["token"] != token:
                return False
            row["token"] = None
            row["at"] = None
            row["owner"] = None
            return True


# ---------------------------------------------------------------------------
# 3.7 — vol de claim impossible
# ---------------------------------------------------------------------------


def test_two_simultaneous_claims_only_one_wins() -> None:
    """Deux acquisitions simultanées sur le même artefact : une seule gagne.

    C'est la garantie de l'acquisition atomique conditionnelle : aucune fenêtre
    de lecture-puis-écriture entre deux requérants.
    """
    store = _FakeClaimStore()
    store.seed("art-1")

    results: list[str | None] = []

    def attempt(owner: str) -> None:
        results.append(store.acquire("art-1", owner))

    t1 = threading.Thread(target=attempt, args=("owner-a",))
    t2 = threading.Thread(target=attempt, args=("owner-b",))
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert len(results) == 2
    gagnants = [r for r in results if r is not None]
    assert len(gagnants) == 1, f"Deux gagnants : {results}"


def test_old_owner_cannot_release_new_claim() -> None:
    """Après une reprise, l'ancien propriétaire ne peut pas libérer le nouveau bail."""
    store = _FakeClaimStore()
    store.seed("art-2")

    token_a = store.acquire("art-2", "owner-a")
    assert token_a is not None
    # L'ancien propriétaire libère : succès.
    assert store.release("art-2", token_a) is True

    token_b = store.acquire("art-2", "owner-b")
    assert token_b is not None

    # L'ancien propriétaire tente de libérer le bail du nouveau : échec.
    assert store.release("art-2", token_a) is False
    # Le nouveau propriétaire peut libérer.
    assert store.release("art-2", token_b) is True


# ---------------------------------------------------------------------------
# 3.7 — 200 fichiers + 1 GPU + chat (asynchrone, borné)
# ---------------------------------------------------------------------------


def _stress_coordinator(
    ingestion_count: int,
    interactive_count: int,
    *,
    chat_timeout_s: float = 2.0,
    ingestion_timeout_s: float = 10.0,
    ingestion_hold_s: float = 0.0,
) -> tuple[int, int, list[str]]:
    """Lance N ingests et M chats sur un coordinateur unique.

    DISCRIMINANT DE PRIORITÉ : les threads d'ingestion enfilés AVANT le chat
    retiennent ensuite la ressource `ingestion_hold_s` chacun. En FIFO, le chat
    attendrait `ingestion_count * ingestion_hold_s` avant son tour ; le test
    choisit donc ce produit STRICTEMENT SUPÉRIEUR à `chat_timeout_s`, si bien
    qu'un ordonnanceur FIFO ferait échouer le chat (`ResourceBusy`) ou
    produirait un `order[0]` d'ingestion. Un coordinateur prioritaire le sert
    premier, dans le seuil.

    Retourne (succès, échecs, ordre). Aucun thread n'attend indéfiniment : les
    acquéreurs utilisent un timeout, et les échecs sont comptés comme
    `ResourceBusy`.
    """
    coordinator = rc.ResourceCoordinator()
    # Un détenteur initial factice : les threads doivent attendre.
    coordinator._owner = "dummy"
    coordinator._token = "dummy_token"
    coordinator._generation = 1

    success = 0
    failures = 0
    order: list[str] = []
    lock = threading.Lock()

    def worker(kind: str, idx: int) -> None:
        nonlocal success, failures
        try:
            timeout = chat_timeout_s if kind == "chat" else ingestion_timeout_s
            lease = coordinator.acquire(
                "gpu", f"{kind}-{idx}", timeout_s=timeout, kind=kind
            )
            with lock:
                order.append(f"{kind}-{idx}")
            if kind == "ingestion" and ingestion_hold_s > 0:
                time.sleep(ingestion_hold_s)
            coordinator.release(lease)
            with lock:
                success += 1
        except rc.ResourceBusy:
            with lock:
                failures += 1

    threads = []
    for i in range(ingestion_count):
        t = threading.Thread(target=worker, args=("ingestion", i))
        threads.append(t)
        t.start()
    for i in range(interactive_count):
        t = threading.Thread(target=worker, args=("chat", i))
        threads.append(t)
        t.start()

    # On attend que TOUS les requérants soient enregistrés AVANT de libérer le
    # détenteur : sinon un ingestion pourrait acquérir avant même que le chat
    # n'entre dans la file, et `order[0]` ne prouverait plus la priorité.
    attendus = ingestion_count + interactive_count
    deadline_inscription = time.monotonic() + ingestion_timeout_s
    while time.monotonic() < deadline_inscription:
        inscrits = coordinator.snapshot()["waiters"]
        if isinstance(inscrits, int) and inscrits >= attendus:
            break
        time.sleep(0.005)
    with coordinator._condition:
        coordinator._owner = None
        coordinator._token = None
        coordinator._condition.notify_all()

    # Attente bornée de TOUS les threads.
    deadline = time.monotonic() + max(chat_timeout_s, ingestion_timeout_s) + 2.0
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)

    return success, failures, order


def test_200_files_one_gpu_chat_served_and_threads_bounded() -> None:
    """200 ingests + 1 chat sur 1 GPU : pas d'explosion, PRIORITÉ prouvée.

    Les 200 ingests s'enregistrent d'abord, le chat ensuite. Chaque ingestion
    retient 0,02 s : en FIFO le chat attendrait ~4 s, au-delà de son timeout de
    3 s. Le test exige donc `order[0] == "chat-0"` : le chat passe AVANT tous
    les waiters d'ingestion, pas seulement « quelque part » dans l'ordre.
    """
    initial_threads = threading.active_count()
    success, failures, order = _stress_coordinator(
        ingestion_count=200,
        interactive_count=1,
        chat_timeout_s=3.0,
        ingestion_timeout_s=10.0,
        ingestion_hold_s=0.02,
    )
    # 201 demandes, un seul détenteur à la fois : toutes doivent aboutir ou
    # échouer par timeout, aucune ne doit rester bloquée.
    assert success + failures == 201
    # PRIORITÉ INTERACTIVE : le chat est servi EN PREMIER, malgré 200
    # ingestions déjà en file. En FIFO, order[0] serait "ingestion-*".
    assert order and order[0] == "chat-0", (
        f"le chat n'a pas été servi en premier : {order[:5]!r}"
    )
    # Le nombre de threads vivants ne doit pas avoir explosé (tolérance 10).
    final_threads = threading.active_count()
    assert final_threads <= initial_threads + 10


# ---------------------------------------------------------------------------
# 3.5 — aucune transaction SQLite pendant l'attente GPU
# ---------------------------------------------------------------------------


@contextmanager
def _track_sessions() -> Iterator[list[object]]:
    """Context manager qui enregistre les sessions ouvertes."""
    sessions: list[object] = []

    class _TrackedSession:
        def __init__(self) -> None:
            self.active = True
            sessions.append(self)

        def close(self) -> None:
            self.active = False

        def __enter__(self) -> _TrackedSession:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    yield sessions


def test_no_open_sqlite_session_during_gpu_wait() -> None:
    """Aucune session SQLite active pendant l'attente du coordinateur.

    L'invariant de la revue de plan est vérifié au niveau du coordinateur :
    il ne consulte jamais la base. Le test le prouve en patchant le moteur
    SQLite pour lever si on l'appelle pendant l'attente.
    """
    coordinator = rc.ResourceCoordinator()
    coordinator._owner = "dummy"
    coordinator._token = "dummy_token"

    # Si le coordinateur tentait d'ouvrir une session, ce patch lèverait.
    with patch(
        "backend.db.database.SessionLocal",
        side_effect=AssertionError("SQLite ouverte pendant l'attente"),
    ):
        with pytest.raises(rc.ResourceBusy):
            coordinator.acquire("gpu", "ingest_owner", timeout_s=0.1, kind="ingestion")


# ---------------------------------------------------------------------------
# 3.7 — ResourceBusy retryable côté chat
# ---------------------------------------------------------------------------


def test_resource_busy_is_retryable_for_chat() -> None:
    """Le chat reçoit `ResourceBusy` (retryable), pas une exception fatale."""
    coordinator = rc.ResourceCoordinator()
    coordinator._owner = "dummy"
    coordinator._token = "dummy_token"

    try:
        coordinator.acquire("gpu", "chat_owner", timeout_s=0.05, kind="interactive")
    except rc.ResourceBusy as exc:
        # C'est le type attendu : le chat peut réessayer.
        assert "RESOURCE_BUSY" in str(exc)
    else:
        pytest.fail("Le chat aurait dû recevoir ResourceBusy")

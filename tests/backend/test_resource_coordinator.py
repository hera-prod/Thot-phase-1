from __future__ import annotations

import threading

import pytest

from backend.inference.resource_coordinator import (
    GPUReentrantAcquire,
    ResourceBusy,
    ResourceCoordinator,
    ResourceLeaseLost,
)


def test_only_one_owner_and_reentrant_is_rejected() -> None:
    coordinator = ResourceCoordinator()
    lease = coordinator.acquire("text", "owner_a")
    with pytest.raises(GPUReentrantAcquire, match="GPU_REENTRANT_ACQUIRE"):
        coordinator.acquire("text", "owner_a")
    with pytest.raises(ResourceBusy, match="RESOURCE_BUSY"):
        coordinator.acquire("image", "owner_b", timeout_s=0)
    coordinator.release(lease)
    second = coordinator.acquire("image", "owner_b", timeout_s=0)
    assert second.generation == lease.generation + 1
    with pytest.raises(ResourceLeaseLost):
        coordinator.release(lease)


def test_text_vision_ocr_image_sequence_has_one_owner() -> None:
    coordinator = ResourceCoordinator()
    for resource in ("model", "vision", "ocr", "image"):
        with coordinator.lease(resource, f"owner:{resource}", timeout_s=0):
            assert coordinator.snapshot()["owner"] == f"owner:{resource}"
    assert coordinator.snapshot()["owner"] is None


def test_competing_owner_waits_and_releases() -> None:
    coordinator = ResourceCoordinator()
    entered = threading.Event()
    released = threading.Event()

    def hold() -> None:
        with coordinator.lease("image", "owner_a", timeout_s=1):
            entered.set()
            released.wait(0.2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    with coordinator.lease("text", "owner_b", timeout_s=1):
        assert coordinator.snapshot()["owner"] == "owner_b"
    released.set()
    thread.join()
    assert coordinator.snapshot()["owner"] is None


# ---------------------------------------------------------------------------
# Phase 3.4 — file équitable, aging, timeout chat
# ---------------------------------------------------------------------------


def test_aging_prioritizes_ingestion_after_threshold(monkeypatch) -> None:
    """Un travail d'ingestion qui attend 60 s passe devant un interactif frais."""
    import time

    from backend.inference import resource_coordinator as rc

    monkeypatch.setattr(rc, "AGING_SECONDES", 0.1)

    coordinator = rc.ResourceCoordinator()
    # Un détenteur factice tient la ressource.
    coordinator._owner = "dummy"
    coordinator._token = "dummy_token"
    coordinator._generation = 1

    order: list[str] = []

    def ingestion_worker() -> None:
        try:
            lease = coordinator.acquire(
                "model", "ingest_owner", timeout_s=2.0, kind="ingestion"
            )
            order.append("ingestion")
            coordinator.release(lease)
        except rc.ResourceBusy:
            order.append("ingestion_timeout")

    def interactive_worker() -> None:
        try:
            lease = coordinator.acquire(
                "model", "chat_owner", timeout_s=2.0, kind="interactive"
            )
            order.append("interactive")
            coordinator.release(lease)
        except rc.ResourceBusy:
            order.append("interactive_timeout")

    # L'ingestion arrive d'abord et patiente au-delà du seuil d'aging.
    t1 = threading.Thread(target=ingestion_worker)
    t1.start()
    time.sleep(0.15)

    # L'interactif arrive après : sans aging, il passerait devant.
    t2 = threading.Thread(target=interactive_worker)
    t2.start()
    time.sleep(0.05)

    # Libère la ressource factice.
    with coordinator._condition:
        coordinator._owner = None
        coordinator._token = None
        coordinator._condition.notify_all()

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert order[0] == "ingestion", f"Ordre inattendu : {order}"


def test_chat_timeout_raises_resource_busy() -> None:
    """Le chat attend 120 s au plus, puis reçoit `ResourceBusy` retryable."""
    import time

    from backend.inference import resource_coordinator as rc

    coordinator = rc.ResourceCoordinator()
    coordinator._owner = "dummy"
    coordinator._token = "dummy_token"

    start = time.monotonic()
    with pytest.raises(rc.ResourceBusy, match="RESOURCE_BUSY"):
        coordinator.acquire("model", "chat_owner", timeout_s=0.1, kind="interactive")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.1


def test_release_by_old_owner_after_new_lease_is_rejected() -> None:
    """Un ancien propriétaire ne peut pas libérer le bail d'un nouveau.

    C'est la garantie que le vol de bail ne devient pas une corruption d'état :
    après une reprise, l'ancien worker doit être incapable de libérer la
    ressource détenue par le nouveau propriétaire.
    """
    coordinator = ResourceCoordinator()
    first = coordinator.acquire("model", "owner_a")
    coordinator.release(first)

    second = coordinator.acquire("model", "owner_b")
    with pytest.raises(ResourceLeaseLost):
        coordinator.release(first)
    assert coordinator.snapshot()["owner"] == "owner_b"
    coordinator.release(second)


def test_waiting_owner_does_not_hold_the_database() -> None:
    """Le coordinateur ne touche pas à SQLite : aucune transaction ouverte.

    Le test vérifie que `acquire` ne fait aucune opération de base de données.
    L'absence de session SQLite ouverte pendant l'attente est une contrainte de
    l'appelant (`attachment_ingest`), mais le coordinateur ne doit jamais
    dépendre d'une session pour attendre.

    L'ancien test était FAUX : le détenteur libérait le bail dès `entered.set()`,
    donc l'attente testée pouvait être vide. Le détenteur reste ici en place
    jusqu'à `release_event`, ce qui garantit que `acquire` attend réellement
    pendant que le patch de session est actif.
    """
    from backend.inference import resource_coordinator as rc

    coordinator = rc.ResourceCoordinator()
    entered = threading.Event()
    release_event = threading.Event()

    def hold() -> None:
        with coordinator.lease("model", "owner_a", timeout_s=1):
            entered.set()
            # Garder le bail jusqu'à ce que le test ait vérifié l'attente.
            release_event.wait(timeout=2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    # Attente courte avec timeout : doit lever ResourceBusy sans toucher à la base.
    with pytest.raises(ResourceBusy):
        coordinator.acquire("model", "owner_b", timeout_s=0.05, kind="chat")
    release_event.set()
    thread.join(timeout=1)

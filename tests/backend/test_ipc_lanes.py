"""La voie de lecture ne doit jamais attendre le worker de travail.

Le bug que ce fichier verrouille : un `image.generate` tient le worker IPC unique
pendant toute la diffusion GPU. Revenir en arrière pendant une génération suffisait
alors à faire attendre — puis paraître vides — discussions, galerie et historique.

Ces tests sont volontairement sans GPU et sans base : ils n'exercent que le routage
des files de `stdin_loop`, pas les handlers réels.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from backend.inference import ipc
from backend.inference.ipc import (
    CONTROL_PRIORITIES,
    PROTOCOL_METHODS,
    READ_ONLY_METHODS,
)


def test_les_lectures_sont_des_methodes_du_protocole() -> None:
    """Une lecture que le protocole refuse à l'entrée ne serait jamais servie."""
    inconnues = sorted(READ_ONLY_METHODS - PROTOCOL_METHODS)
    assert inconnues == [], (
        "Ces méthodes de lecture sont absentes de PROTOCOL_METHODS et seraient "
        "refusées avant d'atteindre leur voie :\n"
        + "\n".join(f"  - {methode}" for methode in inconnues)
    )


def test_les_lectures_ne_sont_pas_des_controles() -> None:
    """Une lecture est servie par le pool, jamais détournée vers le contrôle."""
    partagees = sorted(READ_ONLY_METHODS & set(CONTROL_PRIORITIES))
    assert partagees == [], (
        "Ces méthodes sont à la fois lecture et contrôle : le routage deviendrait "
        "ambigu et le test de concurrence ne prouverait plus rien.\n"
        + "\n".join(f"  - {methode}" for methode in partagees)
    )


def test_les_lectures_de_navigation_sont_couvertes() -> None:
    """Les écrans qui paraissaient vides ont chacun leur lecture dans la voie."""
    attendues = {
        "list.conversations",
        "list.messages",
        "image.list",
        "get.attachment_bytes",
        "espace.conversations",
    }
    manquantes = sorted(attendues - READ_ONLY_METHODS)
    assert manquantes == [], (
        "Ces lectures de navigation resteraient bloquées derrière un image.generate :\n"
        + "\n".join(f"  - {methode}" for methode in manquantes)
    )


def test_les_ecritures_ne_sont_pas_des_lectures() -> None:
    """Une écriture routée comme lecture perdrait l'ordre garanti par le worker."""
    ecritures = {
        "save.upload",
        "stage.paths",
        "attachments.prepare",
        "image.delete",
        "rename.conversation",
        "espace.create",
    }
    intrus = sorted(ecritures & READ_ONLY_METHODS)
    assert intrus == [], (
        "Ces écritures passeraient par la voie de lecture et échapperaient à "
        "l'ordre du worker :\n" + "\n".join(f"  - {methode}" for methode in intrus)
    )


# Garde-fou anti-blocage, pas une durée de travail : l'ordonnanceur peut tarder à
# céder la main au worker ou au pool de lecture quand la suite complète tourne sous
# charge. On attend des événements de synchronisation ; ce délai ne fait qu'empêcher
# un test cassé de figer la suite.
_WAIT_TIMEOUT_S = 30.0


def test_une_lecture_est_servie_pendant_qu_un_travail_est_bloque(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrence réelle : le worker est retenu, la lecture revient quand même."""
    heavy_started = threading.Event()
    heavy_finished = threading.Event()
    read_done = threading.Event()
    release = threading.Event()

    def handler(item: dict) -> None:
        method = item["method"]
        if method == "image.generate":
            heavy_started.set()
            # Strictement plus long que les attentes du test : le travail lourd ne
            # peut pas se terminer tout seul avant que la lecture soit observée.
            release.wait(timeout=_WAIT_TIMEOUT_S * 2)
            heavy_finished.set()
        elif method == "list.conversations":
            read_done.set()

    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, encoding="utf-8")
    monkeypatch.setattr(ipc.sys, "stdin", reader)

    loop = threading.Thread(
        target=lambda: ipc.stdin_loop(
            handler, read_pool_size=1, on_shutdown=lambda: None
        ),
        name="test-ipc-lanes",
        daemon=True,
    )

    def send(request_id: str, method: str) -> None:
        line = json.dumps(
            {
                "protocol_version": 1,
                "request_id": request_id,
                "method": method,
                "params": {},
            }
        )
        os.write(write_fd, (line + "\n").encode("utf-8"))

    try:
        loop.start()
        send("req-heavy", "image.generate")
        assert heavy_started.wait(timeout=_WAIT_TIMEOUT_S), (
            "le worker n'a pas démarré le travail lourd"
        )

        send("req-read", "list.conversations")
        assert read_done.wait(timeout=_WAIT_TIMEOUT_S), (
            "la lecture est restée bloquée derrière le travail lourd : "
            "la voie de lecture n'est pas indépendante"
        )
        assert not heavy_finished.is_set(), (
            "le travail lourd s'est terminé avant la lecture : le test ne prouve rien"
        )
    finally:
        # Ne jamais laisser un thread retenu si une assertion a échoué.
        release.set()
        os.close(write_fd)
        loop.join(timeout=10)
        reader.close()

    assert not loop.is_alive(), "stdin_loop n'a pas rendu la main après EOF"
    assert heavy_finished.is_set()


def test_la_file_de_lecture_pleine_refuse_proprement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saturer la voie de lecture doit produire un refus, pas un blocage."""
    recus: list[tuple[str, dict]] = []
    refus_vu = threading.Event()

    def capturer(event: str, payload: dict, **_kwargs) -> bool:
        recus.append((event, payload))
        if event == "error" and payload.get("code") == "queue_full":
            refus_vu.set()
        return True

    monkeypatch.setattr(ipc, "emit", capturer)

    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, encoding="utf-8")
    monkeypatch.setattr(ipc.sys, "stdin", reader)

    loop = threading.Thread(
        target=lambda: ipc.stdin_loop(
            lambda _item: None,
            read_queue_max=1,
            read_pool_size=0,
            on_shutdown=lambda: None,
        ),
        name="test-ipc-read-full",
        daemon=True,
    )

    def send(request_id: str, method: str) -> None:
        line = json.dumps(
            {
                "protocol_version": 1,
                "request_id": request_id,
                "method": method,
                "params": {},
            }
        )
        os.write(write_fd, (line + "\n").encode("utf-8"))

    try:
        loop.start()
        send("req-read-1", "list.conversations")
        send("req-read-2", "list.messages")
        assert refus_vu.wait(timeout=2), (
            "la file de lecture pleine n'a émis aucun refus : la requête serait "
            "restée silencieusement perdue"
        )
    finally:
        os.close(write_fd)
        loop.join(timeout=10)
        reader.close()

    assert not loop.is_alive(), "stdin_loop n'a pas rendu la main après EOF"
    refus = [
        payload
        for event, payload in recus
        if event == "error" and payload.get("code") == "queue_full"
    ]
    assert any(
        payload.get("request_id") == "req-read-2" and payload.get("retryable") is True
        for payload in refus
    ), "le second request_id n'a pas reçu un refus récupérable"

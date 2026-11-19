"""Le GC de démarrage ne bloque pas la boucle stdin et isole ses échecs.

Phase 4 : `main` exécutait corbeille + GC + un commit par artefact AVANT
`stdin_loop`, sans borne. Ces tests verrouillent le lancement asynchrone et
l'indépendance des deux tâches (une corbeille en échec ne saute pas le GC).
"""

from __future__ import annotations

import threading
from contextlib import nullcontext

from backend import main as backend_main


def test_le_lancement_du_gc_rend_la_main_immediatement(monkeypatch):
    demarre = threading.Event()
    libere = threading.Event()
    termine = threading.Event()

    def _cible():
        demarre.set()
        libere.wait(timeout=5)
        termine.set()

    monkeypatch.setattr(backend_main, "_gc_et_entretien_demarrage", _cible)
    fil = backend_main.demarrer_gc_demarrage()

    assert demarre.wait(timeout=2), "la cible doit démarrer"
    assert not termine.is_set(), "le lanceur ne doit pas attendre la cible"
    assert fil.daemon is True

    libere.set()
    fil.join(timeout=2)
    assert termine.is_set()


def test_une_corbeille_en_echec_ne_saute_pas_le_gc(monkeypatch):
    from backend.db import database as database_module
    from backend.ipc import attachments as attachments_module
    from backend.search import trash as trash_module
    from backend.services import corbeille as corbeille_module
    from backend.services import depot_integrite as integrite_module

    appels: dict[str, int] = {"purge": 0, "orphelins": 0}

    class _FakeSession:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    session = _FakeSession()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(database_module, "_signaler_depot_degrade", lambda: None)
    monkeypatch.setattr(
        corbeille_module,
        "balayer_corbeille",
        lambda _db: (_ for _ in ()).throw(RuntimeError("corbeille cassée")),
    )

    def _purge(_db, **_kwargs):
        appels["purge"] += 1
        return {"links": 0, "artifacts": 0, "blobs": 0}

    monkeypatch.setattr(trash_module, "purge", _purge)
    monkeypatch.setattr(integrite_module, "verrou_gc", lambda *_a, **_k: nullcontext())

    def _orphelins(_db):
        appels["orphelins"] += 1
        return {"purged": 0, "examined": 0, "failed": 0, "errors": []}

    monkeypatch.setattr(integrite_module, "purger_orphelins_blobs_expires", _orphelins)
    for nom in (
        "purger_staging_expires",
        "purger_temporaires_temp_expires",
        "purger_temporaires_blobs_expires",
    ):
        monkeypatch.setattr(attachments_module, nom, lambda *a, **k: 0)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    backend_main._gc_et_entretien_demarrage()

    assert appels["purge"] == 1, "le GC doit courir malgré l'échec de corbeille"
    assert appels["orphelins"] == 1
    assert session.rollbacks >= 1, "l'échec de corbeille doit être annulé"
    assert session.closed is True


def test_le_verrou_gc_est_reentrant_dans_le_meme_processus(tmp_path):
    from backend.services import depot_integrite as integrite_module

    with integrite_module.verrou_gc(tmp_path):
        with integrite_module.verrou_gc(tmp_path):
            pass


def test_le_cli_gc_sort_non_nul_et_dit_que_la_portee_est_globale(monkeypatch, capsys):
    from backend.db import database as database_module
    from backend.search import trash as trash_module
    from backend.services import depot_integrite as integrite_module

    class _FakeSession:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(database_module, "init_db", lambda: None)
    monkeypatch.setattr(database_module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(integrite_module, "verrou_gc", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(
        trash_module,
        "purge",
        lambda _db, **_k: {
            "links": 0,
            "links_repares": 0,
            "artifacts": 1,
            "blobs": 0,
            "blobs_failed": 1,
            "artifacts_failed": 2,
            "chunks": 0,
            "index_chunks": 0,
            "vecteurs": 0,
            "index": 0,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        integrite_module,
        "purger_orphelins_blobs_expires",
        lambda _db, **_k: {"purged": 0, "examined": 0, "failed": 0, "errors": []},
    )

    code = integrite_module._main(["--gc", "--espace", "ws_quelconque"])

    assert code == 1, "un GC avec échecs ne doit pas sortir 0"
    sortie = capsys.readouterr().out
    assert "--gc est global" in sortie
    assert "échec" in sortie


def test_le_sidecar_demarre_quand_le_verrou_de_vie_est_indisponible(
    monkeypatch, capsys
):
    """Sans `flock` (Windows sans fallback), le sidecar démarre quand même.

    Le verrou est une protection de concurrence, pas une condition d'existence :
    un RuntimeError à l'acquisition doit être journalisé, jamais fatal.
    """
    from backend.services import depot_integrite as integrite_module

    def _refus():
        raise RuntimeError("fcntl indisponible sur cette plateforme")

    monkeypatch.setattr(integrite_module, "acquerir_verrou_gc_vie", _refus)

    assert backend_main.acquerir_verrou_gc_sidecar() is False
    assert "verrou de vie indisponible" in capsys.readouterr().err


def test_le_sidecar_garde_le_verrou_de_vie_quand_il_existe(monkeypatch):
    from backend.services import depot_integrite as integrite_module

    appels = []
    monkeypatch.setattr(
        integrite_module,
        "acquerir_verrou_gc_vie",
        lambda: appels.append("verrou"),
    )

    assert backend_main.acquerir_verrou_gc_sidecar() is True
    assert appels == ["verrou"]


def test_le_cli_gc_refuse_de_tourner_sans_verrou(monkeypatch, capsys):
    """Le CLI ne supprime QUE sous verrou : son échec reste fail-closed."""
    from backend.services import depot_integrite as integrite_module

    def _sans_verrou(*_args, **_kwargs):
        raise RuntimeError("verrou indisponible")

    monkeypatch.setattr(integrite_module, "verrou_gc", _sans_verrou)

    code = integrite_module._main(["--gc"])

    assert code == 1
    sortie = capsys.readouterr().out
    assert "GC impossible" in sortie
    assert "verrou indisponible" in sortie


def test_le_cli_gc_sort_zero_sans_echec(monkeypatch):
    from backend.db import database as database_module
    from backend.search import trash as trash_module
    from backend.services import depot_integrite as integrite_module

    class _FakeSession:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(database_module, "init_db", lambda: None)
    monkeypatch.setattr(database_module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(integrite_module, "verrou_gc", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(
        trash_module,
        "purge",
        lambda _db, **_k: {
            "links": 0,
            "links_repares": 0,
            "artifacts": 0,
            "blobs": 0,
            "blobs_failed": 0,
            "artifacts_failed": 0,
            "chunks": 0,
            "index_chunks": 0,
            "vecteurs": 0,
            "index": 0,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        integrite_module,
        "purger_orphelins_blobs_expires",
        lambda _db, **_k: {"purged": 0, "examined": 0, "failed": 0, "errors": []},
    )

    assert integrite_module._main(["--gc"]) == 0

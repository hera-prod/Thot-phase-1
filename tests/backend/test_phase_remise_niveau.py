"""Remise à niveau Phases 1-5 : verrous purs (sans modèle, sans GPU, <1s).

Couvre ce que la remise à niveau a tranché, pour qu'on ne régresse plus :
- annulation coopérative C09 (flags, q-r.md : stop local seul avant)
- empreinte C11 (idempotence fingerprint)
- MAX_ITER canonique = 16 (arbitrage Phase 4 : code+defaults+tests vs README 5)
- distribution.json valide (Phase 5)
"""

import json
from pathlib import Path

import backend.main as M
from backend.inference import loop as L
from backend.config import defaults as D

ROOT = Path(__file__).resolve().parents[2]


def test_cancel_flag_identite_par_run():
    f1 = M.get_cancel_flag("run-a")
    f2 = M.get_cancel_flag("run-a")
    f3 = M.get_cancel_flag("run-b")
    assert f1 is f2
    assert f1 is not f3
    M.clear_cancel_flag("run-a")
    M.clear_cancel_flag("run-b")


def test_cancel_flag_set_puis_clear():
    flag = M.get_cancel_flag("run-cancel-test")
    assert not flag.is_set()
    flag.set()
    assert flag.is_set()
    M.clear_cancel_flag("run-cancel-test")
    assert not M.get_cancel_flag("run-cancel-test").is_set()
    M.clear_cancel_flag("run-cancel-test")


def test_fingerprint_c11_deterministe_et_scope_conv():
    base = {"message": "Bonjour", "mode": "chat", "conv_id": "c1"}
    h1 = M._canonical_fingerprint("chat.send", dict(base))
    h2 = M._canonical_fingerprint("chat.send", dict(base))
    assert h1 == h2
    assert len(h1) == 16 and all(c in "0123456789abcdef" for c in h1)
    # conv différente → empreinte différente (périmètre acteur C11)
    h3 = M._canonical_fingerprint("chat.send", {**base, "conv_id": "c2"})
    assert h3 != h1
    # méthode différente → différente
    h4 = M._canonical_fingerprint("cancel", dict(base))
    assert h4 != h1


def test_max_iter_canonique_16():
    # Arbitrage Phase 4 : loop + defaults font foi (README disait 5 à tort)
    assert L.MAX_ITER == 16
    assert D.MAX_ITER == 16
    assert L.MAX_ITER == D.MAX_ITER


def test_distribution_json_valide():
    dist = json.loads((ROOT / "distribution.json").read_text(encoding="utf-8"))
    assert dist["product"] == "Thot Phase 1"
    assert dist["models_included"] is False
    # pas de checksums dans le dépôt : renseignés à la release
    assert "checksums" not in dist
    assert "release" in dist["checksums_note"].lower()
    # date YYYY-MM-DD parseable
    y, m, d = dist["build_date"].split("-")
    assert len(y) == 4 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31
    # layout annoncé = forme validée (data/ est git-ignoré, absent d'un checkout
    # neuf) : le set des derniers segments doit valoir exactement l'arborescence
    # créée par backend/ipc/paths_bridge.py::data_dirs — ni plus, ni moins.
    parts = [p.strip() for p in dist["data_layout"].split(",")]
    assert all(part.startswith("data/") for part in parts), parts
    segments = [part.split("/", 1)[1] for part in parts]
    assert len(set(segments)) == len(segments), parts
    assert set(segments) == {"database", "blobs", "logs", "temp", "backups"}, parts

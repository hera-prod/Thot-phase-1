"""Chaîne UI↔backend Images : le run studio DOIT toujours se terminer par un événement
terminal corrélé request_id (run_completed OU run_failed), jamais de pendaison silencieuse.

Régression couverte : l'UI restait bloquée sur « Chargement de l'encodeur » alors que le
run était terminé côté backend — aucun test ne verrouillait la séquence d'événements
émise par main.handle(image.generate) jusqu'au terminal.

Niveau testé : vrai dispatch main.handle + vrai service + vrai sous-processus spike
(stub imprimant le protocole STAGE réel) + vraie publication DB. Seuls le poids du
modèle (stub), la disponibilité (mock is_available) et le manager (None = pas
d'admission) sont doublés.
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import textwrap
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main as _main
from backend.db.models import Artifact, Base, Blob, Run
from backend.services import flux_spike as _fx
from backend.services import image_service as _svc

_STUB = textwrap.dedent("""\
    import argparse, os, sys, time
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    with open(args.prompt_file, encoding="utf-8") as stream:
        prompt = stream.read()
    if not prompt:
        raise SystemExit(4)
    mode = os.environ.get("THOT_TEST_SPIKE_MODE", "ok")
    stages = [
        ("demarrage", "Préparation"),
        ("texte-chargement", "Chargement de l'encodeur texte"),
        ("texte-inference", "Encodage du prompt"),
        ("texte-liberation", "Libération de l'encodeur"),
        ("transformer-chargement", "Chargement du transformer"),
        ("transformer-assemblage", "Assemblage des poids"),
        ("vae-chargement", "Chargement du VAE"),
    ]
    for stage_id, label in stages:
        print(f"STAGE: {stage_id} :: {label}", flush=True)
    if mode == "hang":
        time.sleep(30)
    for step in range(1, 5):
        print(f"STAGE: diffusion :: Diffusion — pas {step}/4", flush=True)
    print("STAGE: vae-inference :: Décodage de l'image (VAE)", flush=True)
    print("STAGE: sauvegarde :: Sauvegarde", flush=True)
    if mode == "crash":
        sys.exit(3)
    from PIL import Image
    size = (8, 8) if mode == "mismatch" else (args.width, args.height)
    Image.new("RGB", size, (9, 9, 9)).save(args.output, format="PNG")
    print("saved " + args.output, flush=True)
    print("STEPS_EXECUTED: 4", flush=True)
    """)


@pytest.fixture()
def chain(tmp_path, monkeypatch):
    db_file = tmp_path / "chain.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    # Le service découpe sa disponibilité en deux temps (installation, puis VRAM
    # APRÈS déchargement) ; `is_available` reste l'entrée du statut UI.
    monkeypatch.setattr(_svc, "is_available", lambda: (True, "stub"))
    monkeypatch.setattr(_svc, "check_install", lambda: (True, "stub"))
    monkeypatch.setattr(_svc, "check_vram", lambda: (True, "stub"))
    monkeypatch.setattr(_main, "manager", None)
    stub = tmp_path / "stub_spike.py"
    stub.write_text(_STUB, encoding="utf-8")
    monkeypatch.setattr(
        _fx,
        "_spike_cmd",
        lambda prompt_file, seed, width, height, out, **_kw: (
            [
                sys.executable,
                str(stub),
                "--prompt-file",
                prompt_file,
                "--width",
                str(width),
                "--height",
                str(height),
                "--seed",
                str(seed),
                "--output",
                out,
            ],
            str(tmp_path),
        ),
    )
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        _main, "emit", lambda event, payload, **kw: emitted.append((event, payload))
    )
    monkeypatch.setenv("THOT_TEST_SPIKE_MODE", "ok")
    return tmp_path, data_dir, TestSession, emitted


def _generate(
    emitted, rid="req-chain-1", prompt="un phare breton au coucher du soleil"
):
    _main.handle(
        {
            "request_id": rid,
            "method": "image.generate",
            "params": {
                "prompt": prompt,
                "aspect_ratio": "1:1",
                "reference_ids": [],
                "seed": 11,
                "idempotency_key": f"key-{rid}",
            },
            "protocol_version": 1,
        }
    )
    return emitted


def _payloads(emitted, event):
    return [p for e, p in emitted if e == event]


def test_method_image_status_reste_valide_apres_migration_evenements():
    from backend.inference.ipc import _validate_request

    valid, error_code, normalized = _validate_request(
        {
            "id": "status-protocol-1",
            "method": "image.status",
            "params": {},
        }
    )
    assert valid is True
    assert error_code is None
    assert normalized["request_id"] == "status-protocol-1"
    assert normalized["method"] == "image.status"


def test_dispatch_image_status_emet_image_status(monkeypatch):
    from backend.services import flux_spike as fx

    events = []
    monkeypatch.setattr(fx, "backend_enabled", lambda: True)
    monkeypatch.setattr(
        fx, "probe", lambda: (True, {"cuda": True, "free_vram_gb": 10.0})
    )
    monkeypatch.setattr(
        "backend.services.image_service.is_available", lambda: (True, "ok")
    )
    monkeypatch.setattr(
        _main, "emit", lambda event, payload, **_: events.append((event, payload))
    )
    from backend.inference.ipc import _validate_request

    valid, error_code, normalized = _validate_request(
        {
            "id": "status-dispatch-1",
            "method": "image.status",
            "params": {},
            "protocol_version": 1,
        }
    )
    assert valid is True and error_code is None
    _main.handle(normalized)
    assert events[0][0] == "image_status"
    assert events[0][1]["request_id"] == "status-dispatch-1"


def test_probe_sans_torch_rapide():
    """Régression incident 23/09 : probe() importait torch → contexte CUDA dans le
    sidecar → worker unique pendu → image.status sans réponse (UI « Interrogation… »).
    Sonde = fichiers + nvidia-smi, jamais torch, réponse en quelques secondes."""
    if shutil.which("nvidia-smi") is None:
        pytest.skip("pas de GPU/nvidia-smi")
    code = (
        "import sys, time, json; t0=time.time();"
        "from backend.services.flux_spike import probe;"
        "ok,d=probe();"
        "print(json.dumps({'ok':ok,'cuda':d.get('cuda'),'free':d.get('free_vram_gb'),"
        "'total':d.get('total_vram_gb'),'torch':'torch' in sys.modules,"
        "'dur':round(time.time()-t0,1)}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_fx.REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    import json as _js

    res = _js.loads(proc.stdout.strip().splitlines()[-1])
    assert res["torch"] is False
    assert res["cuda"] is True
    assert res["free"] and res["total"]
    assert res["dur"] < 20


def test_succes_sequence_terminale_correelee(chain):
    _, data_dir, TestSession, emitted = chain
    _generate(emitted)
    kinds = [e for e, _ in emitted]
    assert kinds[0] == "run_started"
    assert "run_stage" in kinds
    stage_ids = [p.get("stage") for p in _payloads(emitted, "run_stage")]
    required_stages = [
        "production",
        "demarrage",
        "texte-chargement",
        "texte-inference",
        "texte-liberation",
        "transformer-chargement",
        "transformer-assemblage",
        "vae-chargement",
        "diffusion",
        "vae-inference",
        "sauvegarde",
        "publication",
        "transfert",
    ]
    positions = [stage_ids.index(stage) for stage in required_stages]
    assert positions == sorted(positions)
    diffusion_steps = [
        p.get("step")
        for p in _payloads(emitted, "run_stage")
        if p.get("stage") == "diffusion"
    ]
    assert list(dict.fromkeys(diffusion_steps)) == [1, 2, 3, 4]
    # Terminal unique, corrélé, après persistance (C08).
    assert kinds[-2] == "artifact_created"
    assert kinds[-1] == "run_completed"
    assert "run_failed" not in kinds
    for e, p in emitted:
        if "request_id" in p:
            assert p["request_id"] == "req-chain-1", e
    art_id = _payloads(emitted, "artifact_created")[0]["artifact_id"]
    assert _payloads(emitted, "run_completed")[0]["artifact_id"] == art_id
    db = TestSession()
    try:
        assert (
            db.query(Run).filter(Run.id == "req-chain-1").first().status == "completed"
        )
        art = db.query(Artifact).filter(Artifact.id == art_id).first()
        assert art is not None and art.kind == "image"
        blob = db.query(Blob).filter(Blob.sha256 == art.blob_id).first()
        assert (data_dir / blob.relative_path).exists()
    finally:
        db.close()


def test_replay_effecte_sans_seed_conserve_la_valeur(chain):
    _, _, TestSession, emitted = chain
    request = {
        "prompt": "prompt sans seed",
        "aspect_ratio": "16:9",
        "reference_ids": [],
        "idempotency_key": "key-no-seed",
    }
    _main.handle(
        {
            "request_id": "req-seed-a",
            "method": "image.generate",
            "params": request,
            "protocol_version": 1,
        }
    )
    emitted.clear()
    _main.handle(
        {
            "request_id": "req-seed-b",
            "method": "image.generate",
            "params": request,
            "protocol_version": 1,
        }
    )
    assert [event for event, _ in emitted] == ["artifact_created", "run_completed"]
    assert emitted[1][1]["deduplicated"] is True
    from backend.db.models import ArtifactCard

    db = TestSession()
    try:
        cards = db.query(ArtifactCard).all()
        provenance = json.loads(cards[0].summary)
        assert 0 <= provenance["seed"] <= (1 << 32) - 1
    finally:
        db.close()


def test_replay_completed_rejoue_artefact_avant_terminal(chain):
    _, _, TestSession, emitted = chain
    _generate(emitted)
    artifact_id = _payloads(emitted, "artifact_created")[0]["artifact_id"]
    emitted.clear()
    _generate(emitted)
    assert [event for event, _ in emitted] == ["artifact_created", "run_completed"]
    assert _payloads(emitted, "artifact_created")[0]["artifact_id"] == artifact_id
    assert _payloads(emitted, "run_completed")[0]["artifact_id"] == artifact_id
    assert _payloads(emitted, "run_completed")[0]["deduplicated"] is True


def test_meme_request_id_parametres_differents_conflit(chain):
    _, _, TestSession, emitted = chain
    _generate(emitted, rid="req-conflict")
    emitted.clear()
    _main.handle(
        {
            "request_id": "req-conflict",
            "method": "image.generate",
            "params": {
                "prompt": "autre prompt",
                "aspect_ratio": "1:1",
                "reference_ids": [],
                "seed": 11,
                "idempotency_key": "key-req-conflict",
            },
            "protocol_version": 1,
        }
    )
    errors = _payloads(emitted, "error")
    assert errors and errors[0]["code"] == "REQUEST_CONFLICT"
    assert not _payloads(emitted, "run_started")
    assert not _payloads(emitted, "run_completed")
    assert not _payloads(emitted, "run_failed")


def test_execution_non_terminale_dupliquee_refusee(chain):
    _, _, TestSession, emitted = chain
    params = {
        "prompt": "un phare breton au coucher du soleil",
        "aspect_ratio": "1:1",
        "reference_ids": [],
        "seed": 11,
        "idempotency_key": "key-busy",
    }
    fingerprint = _main._image_request_fingerprint("image.generate", params, 1)
    db = TestSession()
    try:
        db.add(
            Run(
                id="req-busy",
                request_id="req-busy",
                kind="production",
                status="running",
                fingerprint=fingerprint,
                seq=0,
            )
        )
        db.commit()
    finally:
        db.close()
    _main.handle(
        {
            "request_id": "req-busy",
            "method": "image.generate",
            "params": params,
            "protocol_version": 1,
        }
    )
    errors = _payloads(emitted, "error")
    assert errors and errors[0]["code"] == "REQUEST_IN_PROGRESS"
    assert not _payloads(emitted, "run_started")
    assert not _payloads(emitted, "run_completed")


def test_png_incompatible_terminal_failed(chain, monkeypatch):
    _, _, TestSession, emitted = chain
    monkeypatch.setenv("THOT_TEST_SPIKE_MODE", "mismatch")
    _generate(emitted, rid="req-mismatch")
    failed = _payloads(emitted, "run_failed")
    assert failed and failed[0]["code"] == "DIMENSIONS_MISMATCH"
    assert not _payloads(emitted, "artifact_created")
    assert not _payloads(emitted, "run_completed")
    db = TestSession()
    try:
        assert db.query(Run).filter(Run.id == "req-mismatch").first().status == "failed"
    finally:
        db.close()


def test_cancel_prioritaire_pendant_handler_lourd(monkeypatch):
    from backend.inference import ipc

    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()

    def handler(item):
        if item.get("method") == "chat.send":
            started.set()
            release.wait(3)
        elif item.get("method") == "cancel":
            cancelled.set()

    lines = [
        {
            "request_id": "heavy",
            "method": "chat.send",
            "params": {"message": "long", "mode": "chat"},
        },
        {
            "request_id": "cancel-heavy",
            "method": "cancel",
            "params": {"run_id": "heavy"},
        },
    ]
    stream = io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n")
    monkeypatch.setattr(ipc.sys, "stdin", stream)
    thread = threading.Thread(target=ipc.stdin_loop, args=(handler,), daemon=True)
    thread.start()
    assert started.wait(1)
    assert cancelled.wait(1)
    release.set()
    thread.join(3)
    assert not thread.is_alive()


class _ScriptedStdin:
    """stdin scripté ligne par ligne, comme un vrai tuyau : le test décide du moment où le
    lecteur peut avancer, donc aucune course sur l'état des files.

    `close()` ferme le flux (EOF) : observer l'EOF garantit que toutes les lignes
    poussées ont été lues, donc effectivement enfilées.
    """

    def __init__(self) -> None:
        self._lines: "queue.Queue[str]" = queue.Queue()

    def push(self, *requests: dict) -> None:
        for request in requests:
            self._lines.put(json.dumps(request) + "\n")

    def close(self) -> None:
        self._lines.put("")

    def readline(self, size: int = -1) -> str:
        """Accepte la borne imposée par `_lire_ligne_bornee`, comme un vrai tuyau.

        Les lignes scriptées sont bien plus courtes que `size` : les rendre entières
        reproduit fidèlement `TextIOWrapper.readline`, sans état résiduel à gérer.
        """
        try:
            return self._lines.get(timeout=15)
        except queue.Empty:
            return ""


def test_image_status_passe_devant_un_run_image_long(monkeypatch):
    """Régression 25/09 : `image.status` était enfilé comme un travail ordinaire, donc
    derrière un `image.generate` — la page Images restait sur « Interrogation… » jusqu'à
    la fin de la diffusion (jusqu'à 660 s). Le diagnostic passe par le chemin de contrôle,
    donc pendant le run, pas après."""
    from backend.inference import ipc

    run_started = threading.Event()
    release_run = threading.Event()
    status_served = threading.Event()
    handled: list[str] = []

    def handler(item):
        method = item.get("method")
        if method == "image.generate":
            run_started.set()
            release_run.wait(5)
        if method == "image.status":
            status_served.set()
        handled.append(method)

    lines = [
        {
            "request_id": "run-long",
            "method": "image.generate",
            "params": {"prompt": "un phare"},
        },
        {"request_id": "status-long", "method": "image.status", "params": {}},
    ]
    monkeypatch.setattr(
        ipc.sys,
        "stdin",
        io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n"),
    )
    thread = threading.Thread(target=ipc.stdin_loop, args=(handler,), daemon=True)
    thread.start()
    try:
        assert run_started.wait(2)
        assert status_served.wait(2)
        # Le run long tient encore le worker : le diagnostic a déjà été servi.
        assert handled == ["image.status"]
    finally:
        release_run.set()
    thread.join(10)
    assert not thread.is_alive()
    assert handled == ["image.status", "image.generate"]


def test_image_status_serti_meme_file_de_travail_pleine(monkeypatch):
    """Le chemin de contrôle n'est pas borné : `image.status` reste servi alors que la
    file de travail refuse ses travaux (`queue_full` toujours émis pour elle)."""
    from backend.inference import ipc

    run_started = threading.Event()
    release_run = threading.Event()
    status_served = threading.Event()
    handled: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ipc, "emit", lambda event, payload, **kw: events.append((event, payload))
    )

    def handler(item):
        if item.get("method") == "image.generate" and not run_started.is_set():
            run_started.set()
            release_run.wait(5)
        if item.get("method") == "image.status":
            status_served.set()
        handled.append(item.get("request_id"))

    stream = _ScriptedStdin()
    monkeypatch.setattr(ipc.sys, "stdin", stream)
    shutdown = threading.Event()
    thread = threading.Thread(
        target=ipc.stdin_loop,
        args=(handler,),
        kwargs={"max_queue": 1, "on_shutdown": shutdown.set},
        daemon=True,
    )
    thread.start()
    try:
        stream.push(
            {
                "request_id": "run-1",
                "method": "image.generate",
                "params": {"prompt": "un phare"},
            }
        )
        assert run_started.wait(2)  # run-1 sorti de la file, worker occupé
        # File de travail de taille 1 : run-2 la remplit, run-3 est refusé.
        stream.push(
            {
                "request_id": "run-2",
                "method": "image.generate",
                "params": {"prompt": "une dune"},
            },
            {
                "request_id": "run-3",
                "method": "image.generate",
                "params": {"prompt": "un phare"},
            },
            {"request_id": "status-full", "method": "image.status", "params": {}},
        )
        stream.close()
        assert shutdown.wait(2)
        assert status_served.wait(2)
        assert handled == ["status-full"]
        full = [
            payload
            for event, payload in events
            if event == "error" and payload.get("code") == "queue_full"
        ]
        assert [payload["request_id"] for payload in full] == ["run-3"]
    finally:
        release_run.set()
    thread.join(10)
    assert not thread.is_alive()
    assert handled == ["status-full", "run-1", "run-2"]


def test_cancel_reste_prioritaire_derriere_un_status_attente(monkeypatch):
    """Ordre du chemin de contrôle : les diagnostics gardent leur ordre d'arrivée, un
    cancel en attente passe devant eux, et l'EOF reste traité en dernier."""
    from backend.inference import ipc

    first_status_entered = threading.Event()
    release_first_status = threading.Event()
    handled: list[str] = []

    def handler(item):
        method = item.get("method")
        if method == "image.status" and not first_status_entered.is_set():
            first_status_entered.set()
            release_first_status.wait(5)
        handled.append(item.get("request_id"))

    stream = _ScriptedStdin()
    monkeypatch.setattr(ipc.sys, "stdin", stream)
    shutdown = threading.Event()
    thread = threading.Thread(
        target=ipc.stdin_loop,
        args=(handler,),
        kwargs={"on_shutdown": shutdown.set},
        daemon=True,
    )
    thread.start()
    try:
        stream.push({"request_id": "status-a", "method": "image.status", "params": {}})
        assert first_status_entered.wait(2)  # le chemin de contrôle est occupé
        stream.push(
            {"request_id": "status-b", "method": "image.status", "params": {}},
            {
                "request_id": "cancel-1",
                "method": "cancel",
                "params": {"run_id": "run-1"},
            },
            {"request_id": "status-c", "method": "image.status", "params": {}},
        )
        stream.close()
        assert shutdown.wait(2)  # tout le script est enfilé, cancel compris
        release_first_status.set()
        thread.join(10)
        assert not thread.is_alive()
    finally:
        release_first_status.set()
    assert handled == ["status-a", "cancel-1", "status-b", "status-c"]


def test_crash_spike_terminal_failed_correele(chain, monkeypatch):
    _, _, TestSession, emitted = chain
    monkeypatch.setenv("THOT_TEST_SPIKE_MODE", "crash")
    _generate(emitted, rid="req-chain-crash")
    kinds = [e for e, _ in emitted]
    assert "run_failed" in kinds
    assert "run_completed" not in kinds and "artifact_created" not in kinds
    failed = _payloads(emitted, "run_failed")[0]
    assert failed["request_id"] == "req-chain-crash"
    # Le code public suit la grille du contrat commun : un crash image non
    # attribué plus précisément est un INTERNAL_ERROR (aucun code image_failed
    # dans la grille ni dans les fiches).
    assert failed["code"] == "INTERNAL_ERROR"
    db = TestSession()
    try:
        assert (
            db.query(Run).filter(Run.id == "req-chain-crash").first().status == "failed"
        )
    finally:
        db.close()


def test_spike_silencieux_timeout_terminal(chain, monkeypatch):
    _, _, TestSession, emitted = chain
    monkeypatch.setenv("THOT_TEST_SPIKE_MODE", "hang")
    # run_t2i fige RUN_TIMEOUT_S en défaut à la def : on rabaisse le défaut réel
    # (pas de contournement — c'est exactement le timeout de production, raccourci).
    defaults = _fx.run_t2i.__defaults__
    assert defaults is not None and len(defaults) == 4
    monkeypatch.setattr(
        _fx.run_t2i, "__defaults__", (defaults[0], 2, defaults[2], defaults[3])
    )
    _generate(emitted, rid="req-chain-timeout")
    failed = _payloads(emitted, "run_failed")
    assert failed and failed[0]["request_id"] == "req-chain-timeout"
    assert "TIMEOUT" in failed[0]["message"]
    assert not _payloads(emitted, "run_completed")
    # Verrou libéré : un run suivant peut partir (pas de BUSY fantôme).
    assert _fx._lock.acquire(blocking=False)
    _fx._lock.release()
    db = TestSession()
    try:
        assert (
            db.query(Run).filter(Run.id == "req-chain-timeout").first().status
            == "failed"
        )
    finally:
        db.close()

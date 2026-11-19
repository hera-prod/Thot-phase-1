from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import torch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main as backend_main
from backend.db import database as database_module
from backend.db.models import ArtifactCard, Base, Run
from backend.services import flux_spike as flux
from backend.services import image_service as image


@pytest.fixture()
def image_recovery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    # Le service découpe sa disponibilité en deux temps (installation, puis VRAM
    # APRÈS déchargement) ; `is_available` reste l'entrée du statut UI.
    monkeypatch.setattr(image, "is_available", lambda: (True, "stub"))
    monkeypatch.setattr(image, "check_install", lambda: (True, "stub"))
    monkeypatch.setattr(image, "check_vram", lambda: (True, "stub"))
    monkeypatch.setattr(backend_main, "manager", None)
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        backend_main,
        "emit",
        lambda event, payload, **kwargs: emitted.append((event, payload)),
    )
    monkeypatch.setattr(
        flux,
        "run_t2i",
        lambda prompt, seed, **kwargs: (
            _mini_png((kwargs["width"], kwargs["height"])),
            {
                "width": kwargs["width"],
                "height": kwargs["height"],
                "seed": seed,
                "backend": flux.BACKEND_NAME,
            },
        ),
    )
    return tmp_path, data_dir, session_factory, emitted


def _mini_png(size=(4, 4)) -> bytes:
    import io

    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", size, (7, 8, 9)).save(stream, format="PNG")
    return stream.getvalue()


def _image_request(request_id: str, prompt: str = "un phare") -> dict:
    return {
        "request_id": request_id,
        "method": "image.generate",
        "params": {
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "reference_ids": [],
            "seed": 11,
            "idempotency_key": request_id,
        },
        "protocol_version": 1,
    }


def _payloads(emitted, event):
    return [payload for name, payload in emitted if name == event]


def test_init_db_marks_only_active_production_runs_interrupted(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'startup.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = session_factory()
    try:
        for index, status in enumerate(
            ("queued", "running", "cancelling", "", None, "completed")
        ):
            db.add(
                Run(
                    id=f"run-{index}",
                    request_id=f"request-{index}",
                    kind="production",
                    status=status,
                )
            )
        db.add(
            Run(
                id="chat-running",
                request_id="chat-running",
                kind="chat",
                status="running",
            )
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(database_module, "engine", engine)
    database_module.init_db()
    db = session_factory()
    try:
        statuses = {row.id: row.status for row in db.query(Run).all()}
    finally:
        db.close()
    assert [statuses[f"run-{index}"] for index in range(5)] == ["interrupted"] * 5
    assert statuses["run-5"] == "completed"
    assert statuses["chat-running"] == "running"


def test_interrupted_before_publication_reuses_run_and_regenerates(
    image_recovery, monkeypatch
):
    _, _, sessions, emitted = image_recovery
    request = _image_request("before-publication")
    fingerprint = backend_main._image_request_fingerprint(
        "image.generate", request["params"], 1
    )
    db = sessions()
    try:
        db.add(
            Run(
                id="before-publication",
                request_id="before-publication",
                kind="production",
                status="interrupted",
                fingerprint=fingerprint,
            )
        )
        db.commit()
    finally:
        db.close()
    backend_main.handle(request)
    assert [name for name, _ in emitted][:2] == ["run_started", "run_stage"]
    assert _payloads(emitted, "run_completed")
    db = sessions()
    try:
        rows = db.query(Run).filter(Run.request_id == "before-publication").all()
        assert len(rows) == 1
        assert rows[0].id == "before-publication"
        assert rows[0].status == "completed"
    finally:
        db.close()


def test_interrupted_after_publication_replays_output(image_recovery, monkeypatch):
    _, _, sessions, emitted = image_recovery
    request = _image_request("after-publication")
    backend_main.handle(request)
    artifact_id = _payloads(emitted, "artifact_created")[0]["artifact_id"]
    db = sessions()
    try:
        run = db.query(Run).filter(Run.id == "after-publication").one()
        run.status = "interrupted"
        db.commit()
    finally:
        db.close()
    emitted.clear()
    monkeypatch.setattr(
        image,
        "generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("regeneration")),
    )
    backend_main.handle(request)
    assert [name for name, _ in emitted] == ["artifact_created", "run_completed"]
    assert _payloads(emitted, "run_completed")[0]["artifact_id"] == artifact_id
    db = sessions()
    try:
        run = db.query(Run).filter(Run.id == "after-publication").one()
        assert run.status == "completed"
        assert run.output_artifact_id == artifact_id
    finally:
        db.close()


def test_interrupted_finds_published_artifact_by_exact_json_run_id(
    image_recovery, monkeypatch
):
    _, _, sessions, emitted = image_recovery
    request = _image_request("card-recovery")
    fingerprint = backend_main._image_request_fingerprint(
        "image.generate", request["params"], 1
    )
    db = sessions()
    try:
        published = flux.publish_generated(
            db,
            _mini_png(),
            {
                "prompt": "un phare",
                "width": 4,
                "height": 4,
                "seed": 11,
                "run_id": "card-recovery",
            },
        )
        db.add(
            Run(
                id="card-recovery",
                request_id="card-recovery",
                kind="production",
                status="interrupted",
                fingerprint=fingerprint,
            )
        )
        db.commit()
    finally:
        db.close()
    emitted.clear()
    monkeypatch.setattr(
        image,
        "generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("regeneration")),
    )
    backend_main.handle(request)
    assert [name for name, _ in emitted] == ["artifact_created", "run_completed"]
    assert (
        _payloads(emitted, "run_completed")[0]["artifact_id"]
        == published["artifact_id"]
    )
    db = sessions()
    try:
        run = db.query(Run).filter(Run.id == "card-recovery").one()
        assert run.output_artifact_id == published["artifact_id"]
        card = (
            db.query(ArtifactCard)
            .filter(ArtifactCard.artifact_id == published["artifact_id"])
            .one()
        )
        assert json.loads(card.summary)["run_id"] == "card-recovery"
    finally:
        db.close()


def test_publication_run_id_match_does_not_use_substring(image_recovery, monkeypatch):
    _, _, sessions, emitted = image_recovery
    request = _image_request("exact-run")
    fingerprint = backend_main._image_request_fingerprint(
        "image.generate", request["params"], 1
    )
    db = sessions()
    try:
        flux.publish_generated(
            db,
            _mini_png(),
            {
                "prompt": "un phare",
                "width": 4,
                "height": 4,
                "seed": 11,
                "run_id": "exact-run-extra",
            },
        )
        db.add(
            Run(
                id="exact-run",
                request_id="exact-run",
                kind="production",
                status="interrupted",
                fingerprint=fingerprint,
            )
        )
        db.commit()
    finally:
        db.close()
    backend_main.handle(request)
    assert _payloads(emitted, "run_started")
    assert _payloads(emitted, "run_completed")


def test_cancel_before_publication_prevents_publication(image_recovery, monkeypatch):
    import threading

    _, _, sessions, _ = image_recovery
    flag = threading.Event()
    monkeypatch.setattr(
        flux,
        "run_t2i",
        lambda prompt, seed, **kwargs: (
            flag.set()
            or (
                _mini_png((kwargs["width"], kwargs["height"])),
                {
                    "width": kwargs["width"],
                    "height": kwargs["height"],
                    "seed": seed,
                    "backend": flux.BACKEND_NAME,
                },
            )
        ),
    )
    monkeypatch.setattr(
        flux,
        "publish_generated",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("published")),
    )
    with pytest.raises(RuntimeError, match=r"\[CANCELLED\]"):
        image.generate(
            None,
            "cancel-before",
            "un phare",
            "1:1",
            [],
            11,
            cancel_flag=flag,
            db_session_factory=sessions,
        )


def test_publication_wins_after_boundary(image_recovery, monkeypatch):
    import threading

    _, _, sessions, _ = image_recovery
    flag = threading.Event()
    monkeypatch.setattr(
        flux,
        "publish_generated",
        lambda *args, **kwargs: flag.set() or {"artifact_id": "art-published"},
    )
    result = image.generate(
        None,
        "cancel-after",
        "un phare",
        "1:1",
        [],
        11,
        cancel_flag=flag,
        db_session_factory=sessions,
    )
    assert result["artifact_id"] == "art-published"
    assert flag.is_set()


def test_main_keeps_published_image_completed_when_cancel_arrives(
    image_recovery, monkeypatch
):
    _, _, sessions, emitted = image_recovery

    def generate(manager, run_id, *args, **kwargs):
        kwargs["cancel_flag"].set()
        artifact_id = "art-main-published"
        state_db = sessions()
        try:
            kwargs["before_commit"](state_db, artifact_id)
            state_db.commit()
        finally:
            state_db.close()
        return {"artifact_id": artifact_id}

    monkeypatch.setattr(image, "generate", generate)
    backend_main.handle(_image_request("main-cancel-after"))
    assert _payloads(emitted, "run_completed")
    assert not _payloads(emitted, "run_cancelled")
    db = sessions()
    try:
        assert (
            db.query(Run).filter(Run.id == "main-cancel-after").one().status
            == "completed"
        )
    finally:
        db.close()


def test_chat_trace_publication_prevents_cancelled_terminal(
    image_recovery, monkeypatch
):
    _, _, _, emitted = image_recovery

    def run_loop(manager, **kwargs):
        kwargs["cancel_flag"].set()
        return "image publiée", {"image_published": True}

    monkeypatch.setattr("backend.inference.loop.run_agent_loop", run_loop)
    backend_main.handle(
        {
            "request_id": "chat-publication",
            "method": "chat.send",
            "params": {
                "message": "génère une image",
                "mode": "chat",
                "conv_id": "chat-publication",
            },
            "protocol_version": 1,
        }
    )
    assert _payloads(emitted, "run_completed")
    assert not _payloads(emitted, "run_cancelled")
    assert _payloads(emitted, "answer")[0]["text"] == "image publiée"
    event_names = [name for name, _ in emitted]
    assert event_names.index("answer") < event_names.index("run_completed")


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG est Linux")
def test_parent_guard_stops_child_when_parent_exits():
    child_code = (
        "import os,time;from backend.image_engine.runtime.parent_guard import arm_parent_guard;"
        "arm_parent_guard();print(os.getpid(),flush=True);time.sleep(30)"
    )
    parent_code = (
        "import os,subprocess,sys;"
        f"child_code={child_code!r};"
        "p=subprocess.Popen([sys.executable,'-u','-c',child_code],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL);"
        "print(p.stdout.readline().decode().strip(),flush=True);os._exit(0)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-u", "-c", parent_code],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    stdout, _ = parent.communicate(timeout=5)
    child_line = stdout.strip()
    assert child_line.isdigit()
    child_pid = int(child_line)
    deadline = time.monotonic() + 3
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert not alive


def test_dequant_failure_is_explicit_without_random_fallback(monkeypatch):
    import gguf
    from backend.image_engine.loaders import dequant_gpu

    qtype = gguf.GGMLQuantizationType.Q8_0
    monkeypatch.setitem(
        dequant_gpu.DEQUANT_GPU,
        qtype,
        lambda *args: (_ for _ in ()).throw(RuntimeError("gpu down")),
    )
    monkeypatch.setattr(
        "gguf.quants.dequantize",
        lambda *args: (_ for _ in ()).throw(RuntimeError("cpu down")),
    )
    monkeypatch.setattr(
        torch,
        "randn",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("random fallback")
        ),
    )
    with pytest.raises(RuntimeError, match=r"\[DEQUANT_FAILED\]"):
        dequant_gpu.dequantize_gpu(
            torch.zeros(34, dtype=torch.uint8), qtype, (32, 1), dtype=torch.float32
        )


def test_worker_failure_sanitizes_saved_and_local_paths(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    fake = tmp_path / "worker.py"
    fake.write_text(
        textwrap.dedent(
            f"""
            import argparse, sys
            ap=argparse.ArgumentParser()
            ap.add_argument('--prompt-file', required=True)
            ap.add_argument('--width', type=int, required=True)
            ap.add_argument('--height', type=int, required=True)
            ap.add_argument('--seed', type=int, required=True)
            ap.add_argument('--output', required=True)
            a=ap.parse_args()
            print('saved ' + a.output, flush=True)
            print('worker failed at {flux.REPO_ROOT}/models/flux.gguf', flush=True)
            sys.exit(3)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        flux,
        "_spike_cmd",
        lambda prompt_file, seed, width, height, out, **_kw: (
            [
                sys.executable,
                str(fake),
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
            None,
        ),
    )
    with pytest.raises(RuntimeError) as error:
        flux.run_t2i("prompt", seed=1, width=16, height=16, timeout_s=10)
    message = str(error.value)
    assert "saved" not in message
    assert str(flux.REPO_ROOT) not in message
    assert "[chemin local]" in message


def test_qwen_embedding_uses_gguf_only_and_rejects_out_of_range(monkeypatch, tmp_path):
    import numpy as np
    import gguf
    from backend.image_engine.models.qwen3_forward import Qwen3NativeEncoder

    class Lazy:
        qtype = gguf.GGMLQuantizationType.F32
        data = np.zeros((3, 4), dtype=np.float32).tobytes()
        shape_pt = (3, 4)

    class Loader:
        def get_lazy(self, name):
            assert name == "token_embd.weight"
            return Lazy()

    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))
    encoder = Qwen3NativeEncoder(
        Loader(), n_layers=0, device="cpu", dtype=torch.float32
    )
    assert tuple(encoder._embed_matrix().shape) == (3, 4)
    fitted = encoder._fit_input_ids(torch.tensor([[1, 2]], dtype=torch.long), 3)
    assert fitted.tolist() == [[1, 2]]
    with pytest.raises(RuntimeError, match=r"\[TOKEN_OUT_OF_RANGE\]"):
        encoder._fit_input_ids(torch.tensor([[3]], dtype=torch.long), 3)

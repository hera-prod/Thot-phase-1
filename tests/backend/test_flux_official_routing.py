"""Routage officiel/custom du backend FLUX — sans GPU, sans diffusers chargé.

Phase A « interdire le fallback silencieux » : demander la voie officielle sans
`Flux2KleinInpaintPipeline` doit ÉCHOUER, pas glisser vers le subprocess custom
en prétendant ensuite que l'officiel a tourné.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from backend.services import flux_spike as fx


class _UnavailableOfficial:
    """Sosie de `official_inpaint` dont le pipeline n'est pas installé."""

    @staticmethod
    def is_available() -> bool:
        return False


def _png_512() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (512, 512), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeProcess:
    """Processus minimal : stdout sur un vrai pipe, sortie déjà écrite."""

    def __init__(self, stdout, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.pid = None  # aucun groupe de processus à signaler

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _fake_popen_factory(recorded: list[list[str]]):
    def fake_popen(command, *args, **kwargs):
        recorded.append(list(command))
        output_path = command[command.index("--output") + 1]
        with open(output_path, "wb") as stream:
            stream.write(_png_512())
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"STEPS_EXECUTED: 4\n")
        finally:
            os.close(write_fd)
        return _FakeProcess(os.fdopen(read_fd, "rb"))

    return fake_popen


@pytest.fixture(autouse=True)
def _clean_routing_state(monkeypatch):
    monkeypatch.setattr(fx, "_cleanup_blocked", None)
    monkeypatch.delenv("THOT_USE_OFFICIAL_INPAINT", raising=False)
    # Prêt du cache paresseux d'import pour que chaque test reparte propre.
    monkeypatch.setattr(fx, "_OFFICIAL_INPAINT_MODULE", None)
    monkeypatch.setattr(fx, "_OFFICIAL_INPAINT_ATTEMPTED", False, raising=False)


def _run_locked():
    return fx._run_t2i_locked(
        "un prompt",
        7,
        512,
        512,
        30.0,
        None,
        None,
        None,
        init_image="/tmp/init.png",
        mask="/tmp/mask.png",
        mode="inpaint",
        strength=0.8,
    )


@pytest.mark.parametrize(
    "official",
    [None, _UnavailableOfficial()],
    ids=["module_absent", "pipeline_indisponible"],
)
def test_official_requested_but_unavailable_raises_without_custom(
    monkeypatch, official
):
    monkeypatch.setenv("THOT_USE_OFFICIAL_INPAINT", "1")
    monkeypatch.setattr(fx, "_official_inpaint_module", lambda: official)

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("le chemin custom ne doit pas être lancé")

    monkeypatch.setattr(fx.subprocess, "Popen", forbidden_popen)

    with pytest.raises(RuntimeError) as excinfo:
        _run_locked()

    message = str(excinfo.value)
    assert "[OFFICIAL_INPAINT_CLASS_UNAVAILABLE]" in message
    assert "THOT_USE_OFFICIAL_INPAINT=0" in message
    assert "aucun fallback custom" in message


def test_official_not_requested_reaches_custom_and_reports_custom(monkeypatch):
    monkeypatch.setenv("THOT_USE_OFFICIAL_INPAINT", "0")
    monkeypatch.setattr(fx, "_official_inpaint_module", lambda: None)
    recorded: list[list[str]] = []
    monkeypatch.setattr(fx.subprocess, "Popen", _fake_popen_factory(recorded))

    png_bytes, stats = _run_locked()

    assert len(recorded) == 1
    assert png_bytes == _png_512()
    assert stats["backend_requested"] == "custom"
    assert stats["backend_effective"] == fx.BACKEND_NAME
    assert stats["fallback_used"] is False
    assert stats["steps_executed"] == 4


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off"])
def test_official_inpaint_requested_false_values(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("THOT_USE_OFFICIAL_INPAINT", raising=False)
    else:
        monkeypatch.setenv("THOT_USE_OFFICIAL_INPAINT", value)
    assert fx.official_inpaint_requested() is False


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_official_inpaint_requested_true_values(monkeypatch, value):
    monkeypatch.setenv("THOT_USE_OFFICIAL_INPAINT", value)
    assert fx.official_inpaint_requested() is True

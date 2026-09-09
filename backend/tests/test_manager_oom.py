"""Robustesse chargement modèle en VRAM basse — sans GPU (Llama faké).

Non-régression du log q-r.md : après plusieurs allers 8B<->VL (~4.6 Go libres),
les 4 stratégies échouaient avec 'Failed to load model from file', jamais reconnu
comme OOM → aucun repli en offload partiel → run mort.
"""
from __future__ import annotations

import pytest

from backend.inference import manager as M


@pytest.fixture()
def mgr(monkeypatch):
    m = M.ModelManager()
    # Pas d'attente réelle en test
    monkeypatch.setattr(m, "_wait_for_vram_release", lambda timeout_s=0: None)
    return m


class FakeLlama:
    seen_kwargs: list[dict] = []

    def __init__(self, **kwargs):
        FakeLlama.seen_kwargs.append(kwargs)


# ---------- _looks_like_oom ----------

def test_oom_explicite_reconnu(mgr):
    assert mgr._looks_like_oom(RuntimeError("CUDA out of memory")) is True
    assert mgr._looks_like_oom(RuntimeError("cublas alloc failed")) is True


def test_failed_to_load_vram_basse_est_oom(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    assert mgr._looks_like_oom(ValueError("Failed to load model from file: x.gguf")) is True


def test_failed_to_load_vram_haute_n_est_pas_oom(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)
    assert mgr._looks_like_oom(ValueError("Failed to load model from file: x.gguf")) is False


def test_erreur_neutre_n_est_pas_oom(mgr):
    assert mgr._looks_like_oom(ValueError("No such file")) is False


# ---------- n_ctx suit la VRAM courante (pas le cache détecteur) ----------

def test_n_ctx_reduit_quand_vram_basse(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 4096)
    assert FakeLlama.seen_kwargs, "Llama aurait dû être instancié"
    # 4600 Mo < 12 Go → min(32768, max(4096, 4096+1024)) = 5120
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 5120


def test_n_ctx_plein_quand_vram_haute(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 13 * 1024)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 4096)
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 131072


def test_offload_partiel_apres_failed_to_load_vram_basse(mgr, monkeypatch):
    """2e stratégie : Failed-to-load + VRAM basse → tente n_gpu_layers réduit."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    calls: list[dict] = []

    class FlakyLlama:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ValueError("Failed to load model from file: x.gguf")

    monkeypatch.setattr(M, "Llama", FlakyLlama)
    llm = mgr._load_model("LFM2.5-8B", 4096)
    assert isinstance(llm, FlakyLlama)
    assert len(calls) >= 2
    # Le repli partiel force n_gpu_layers explicite (réduit)
    assert calls[1].get("n_gpu_layers") is not None


# ---------- Prompt générateur : priorité observation courante ----------

def test_generator_priorite_observation_courante():
    from backend.prompts.generator import SYSTEM_GENERATOR
    assert "TOUJOURS" in SYSTEM_GENERATOR
    assert "observation d'outil du tour courant" in SYSTEM_GENERATOR


# ---------- sans_mmproj sauté pour vision (q-r.md : "mmproj_path requis" masquait tout) ----------

def test_vision_sans_sans_mmproj(mgr, monkeypatch):
    """GLM-OCR : 3 tentatives max, jamais sans_mmproj, erreur réelle surfacée."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)
    calls: list[dict] = []

    class AlwaysFail:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise ValueError("Failed to load model from file: x.gguf")

    monkeypatch.setattr(M, "Llama", AlwaysFail)
    with pytest.raises(RuntimeError, match="Impossible de charger GLM-OCR"):
        mgr._load_model("GLM-OCR", 1024)
    # nominal + apres_gc_force + flash_off = 3 instanciations, pas de 4e sans mmproj
    assert len(calls) == 3
    assert all("mmproj" in kw for kw in calls)


def test_llama_context_est_oom(mgr):
    assert mgr._looks_like_oom(ValueError("Failed to create llama_context")) is True


def test_repli_oom_reduit_n_ctx(mgr, monkeypatch):
    """Après un échec llama_context, le retry divise n_ctx par 2 (min 4096)."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    calls: list[dict] = []

    class FlakyLlama:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ValueError("Failed to create llama_context")

    monkeypatch.setattr(M, "Llama", FlakyLlama)
    llm = mgr._load_model("LFM2.5-VL-3B", 1024)
    assert isinstance(llm, FlakyLlama)
    assert len(calls) >= 2
    assert calls[0]["n_ctx"] == 32768
    assert calls[1]["n_ctx"] == 16384


def test_erreur_finale_montre_vraie_cause_pas_mmproj_path(mgr, monkeypatch):
    """L'erreur surfacée liste les vraies tentatives, pas le masque sans_mmproj."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)

    class AlwaysFail:
        def __init__(self, **kwargs):
            raise ValueError("Failed to load model from file: x.gguf")

    monkeypatch.setattr(M, "Llama", AlwaysFail)
    try:
        mgr._load_model("GLM-OCR", 1024)
        raise AssertionError("aurait dû échouer")
    except RuntimeError as e:
        assert "mmproj_path requis" not in str(e)
        assert "nominal" in str(e)


def test_texte_garde_sans_mmproj(mgr, monkeypatch):
    """8B texte : les 4 stratégies dont sans_mmproj (lui peut tourner sans projector)."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)
    calls: list[dict] = []

    class AlwaysFail:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise ValueError("Failed to load model from file: x.gguf")

    monkeypatch.setattr(M, "Llama", AlwaysFail)
    with pytest.raises(RuntimeError, match="Impossible de charger LFM2.5-8B"):
        mgr._load_model("LFM2.5-8B", 1024)
    assert len(calls) == 4

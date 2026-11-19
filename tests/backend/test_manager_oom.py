"""Robustesse chargement modèle en VRAM basse — sans GPU (Llama faké).

Non-régression du log q-r.md : après plusieurs allers 8B<->VL (~4.6 Go libres),
les 4 stratégies échouaient avec 'Failed to load model from file', jamais reconnu
comme OOM → aucun repli en offload partiel → run mort.
"""

from __future__ import annotations

from dataclasses import replace

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


# ---------------------------------------------------------------------------
# Substitut du projecteur vision (mmproj) pour les tests mockés
#
# Le vrai fichier mmproj pèse 856 Mo et est EXCLU de l'export (les poids ne sont
# pas transportés). Pour exécuter les tests de repli vision sans ce fichier, on
# crée un substitut minimal (quelques octets, pas de poids) sous `tmp_path` et on
# fait pointer `cfg.mmproj` vers ce substitut. Le vrai `_build_llm_kwargs` est
# conservé ; on ne double que `_build_vision_handler` (qui importe
# `llama_cpp.llama_chat_format` et tenterait d'instancier un vrai handler sur le
# fichier mmproj). Aucun modèle ni poids n'est importé ni lu.


@pytest.fixture()
def mmproj_substitut(tmp_path):
    """Fichier mmproj factice (quelques octets, sans poids) sous tmp_path."""
    chemin = tmp_path / "mmproj-substitut.gguf"
    chemin.write_bytes(b"\x00\x00mmproj-substitut")
    return chemin


class _HandlerFactice:
    """Chat_handler factice : aucun poids, aucun fichier lu."""


# ---------- _looks_like_oom ----------


def test_oom_explicite_reconnu(mgr):
    assert mgr._looks_like_oom(RuntimeError("CUDA out of memory")) is True
    assert mgr._looks_like_oom(RuntimeError("cublas alloc failed")) is True


def test_failed_to_load_vram_basse_est_oom(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    assert (
        mgr._looks_like_oom(ValueError("Failed to load model from file: x.gguf"))
        is True
    )


def test_failed_to_load_vram_haute_n_est_pas_oom(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)
    assert (
        mgr._looks_like_oom(ValueError("Failed to load model from file: x.gguf"))
        is False
    )


def test_erreur_neutre_n_est_pas_oom(mgr):
    assert mgr._looks_like_oom(ValueError("No such file")) is False


# ---------- n_ctx suit la VRAM courante (pas le cache détecteur) ----------


def test_n_ctx_reduit_quand_vram_basse(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 4096)
    assert FakeLlama.seen_kwargs, "Llama aurait dû être instancié"
    # 4600 Mo < 8 Go → plancher 8K (l'ancien « prompt + 1K » donnait 5120,
    # insuffisant : le prompt fixe orchestrateur + outils pèse déjà ~7,2K).
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 8192


def test_n_ctx_128k_des_8go(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9 * 1024)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 7000)
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 131072


def test_n_ctx_131072_a_la_limite_8go(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 8 * 1024)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 7000)
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 131072


def test_n_ctx_32768_entre_7_et_8_go(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 7 * 1024 + 512)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 7000)
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 32768


def test_n_ctx_plein_quand_vram_haute(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 13 * 1024)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()
    mgr._load_model("LFM2.5-8B", 4096)
    assert FakeLlama.seen_kwargs[0]["n_ctx"] == 131072


def test_context_window_suit_chargement_et_dechargement(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 13 * 1024)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    llm = mgr._load_model("LFM2.5-8B", 4096)
    mgr._current_model = llm
    mgr._current_name = "LFM2.5-8B"

    assert mgr.context_window("LFM2.5-8B") is not None
    assert mgr.context_window("autre") is None

    mgr._unload_current()
    assert mgr.context_window("LFM2.5-8B") is None


def test_rechargement_si_fenetre_insuffisante(mgr, monkeypatch):
    """La réutilisation rapide ne sert pas un modèle trop petit pour la demande."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    FakeLlama.seen_kwargs.clear()

    with mgr._use_model_uncoordinated("LFM2.5-8B", estimated_tokens=1000):
        assert mgr.context_window("LFM2.5-8B") == 8192
    assert len(FakeLlama.seen_kwargs) == 1

    # 10 000 + 512 > 8192 → la fenêtre est insuffisante : rechargement.
    with mgr._use_model_uncoordinated("LFM2.5-8B", estimated_tokens=10000):
        assert mgr.context_window("LFM2.5-8B") > 8192
    assert len(FakeLlama.seen_kwargs) == 2


def test_pas_de_rechargement_si_fenetre_ne_peut_pas_grandir(mgr, monkeypatch):
    """Si la VRAM ne permet pas une fenêtre plus grande, on ne recharge pas."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    monkeypatch.setattr(M, "Llama", FakeLlama)
    # Simulation : la VRAM plafonne la fenêtre cible à la valeur déjà allouée.
    monkeypatch.setattr(M, "resolve_n_ctx", lambda name, free, est: 8192)
    FakeLlama.seen_kwargs.clear()

    with mgr._use_model_uncoordinated("LFM2.5-8B", estimated_tokens=1000):
        assert mgr.context_window("LFM2.5-8B") == 8192
    assert len(FakeLlama.seen_kwargs) == 1

    with mgr._use_model_uncoordinated("LFM2.5-8B", estimated_tokens=10000):
        assert mgr.context_window("LFM2.5-8B") == 8192
    assert len(FakeLlama.seen_kwargs) == 1


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


# ---------- Prompt générateur : hiérarchie q-r.md (demande > brief-données) ----------


def test_generator_priorite_observation_courante():
    from backend.prompts.generator import SYSTEM_GENERATOR

    # q-r.md §Prompt 2 : la demande prime tout ; le brief prime l'historique sur les
    # faits ; la clarification est consultative, jamais une instruction obligatoire.
    assert "le message utilisateur prime tout" in SYSTEM_GENERATOR
    assert "le brief prime l'historique" in SYSTEM_GENERATOR
    assert "jamais une instruction obligatoire" in SYSTEM_GENERATOR
    assert "SEULE source" not in SYSTEM_GENERATOR


# ---------- sans_mmproj sauté pour vision (q-r.md : "mmproj_path requis" masquait tout) ----------


def test_vision_sans_sans_mmproj(mgr, monkeypatch, mmproj_substitut):
    """VL-3B : 3 tentatives max, jamais sans_mmproj, erreur réelle surfacée.
    (Autrefois : ce test visait GLM-OCR ; GGUF jamais livré — brouillon : PaddleOCR-VL
    ne passe plus par le manager ; on verrouille le comportement sur VL-3B, le seul
    vision llama.cpp restant.)"""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)
    # Pointer cfg.mmproj vers le substitut tmp_path (pas de poids, pas le vrai 856 Mo).
    cfg_vl = M.MODEL_CONFIGS["LFM2.5-VL-3B"]
    monkeypatch.setitem(
        M.MODEL_CONFIGS, "LFM2.5-VL-3B", replace(cfg_vl, mmproj=str(mmproj_substitut))
    )
    # Doubler uniquement _build_vision_handler : aucun import llama_cpp.llama_chat_format,
    # aucune lecture du fichier mmproj, aucun poids. Le vrai _build_llm_kwargs est conservé.
    monkeypatch.setattr(
        mgr, "_build_vision_handler", lambda cfg, mmproj_path=None: _HandlerFactice()
    )
    calls: list[dict] = []

    class AlwaysFail:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise ValueError("Failed to load model from file: x.gguf")

    monkeypatch.setattr(M, "Llama", AlwaysFail)
    with pytest.raises(RuntimeError, match="Impossible de charger LFM2.5-VL-3B"):
        mgr._load_model("LFM2.5-VL-3B", 1024)
    # nominal + apres_gc_force + flash_off = 3 instanciations, pas de 4e sans mmproj
    assert len(calls) == 3
    assert all(kw.get("mmproj") == str(mmproj_substitut) for kw in calls)


def test_llama_context_est_oom(mgr):
    assert mgr._looks_like_oom(ValueError("Failed to create llama_context")) is True


def test_repli_oom_reduit_n_ctx(mgr, monkeypatch, mmproj_substitut):
    """Après un échec llama_context, le retry divise n_ctx par 2 (min 4096)."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 4600)
    # Pointer cfg.mmproj vers le substitut tmp_path (pas de poids).
    cfg_vl = M.MODEL_CONFIGS["LFM2.5-VL-3B"]
    monkeypatch.setitem(
        M.MODEL_CONFIGS, "LFM2.5-VL-3B", replace(cfg_vl, mmproj=str(mmproj_substitut))
    )
    # Doubler uniquement _build_vision_handler (aucun import llama_cpp, aucun poids).
    monkeypatch.setattr(
        mgr, "_build_vision_handler", lambda cfg, mmproj_path=None: _HandlerFactice()
    )
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
    assert calls[0].get("mmproj") == str(mmproj_substitut)
    assert calls[1].get("mmproj") == str(mmproj_substitut)


def test_erreur_finale_montre_vraie_cause_pas_mmproj_path(
    mgr, monkeypatch, mmproj_substitut
):
    """L'erreur surfacée liste les vraies tentatives, pas le masque sans_mmproj."""
    monkeypatch.setattr(mgr, "_get_free_vram_mb", lambda: 9000)
    # Pointer cfg.mmproj vers le substitut tmp_path (pas de poids).
    cfg_vl = M.MODEL_CONFIGS["LFM2.5-VL-3B"]
    monkeypatch.setitem(
        M.MODEL_CONFIGS, "LFM2.5-VL-3B", replace(cfg_vl, mmproj=str(mmproj_substitut))
    )
    # Doubler uniquement _build_vision_handler (aucun import llama_cpp, aucun poids).
    monkeypatch.setattr(
        mgr, "_build_vision_handler", lambda cfg, mmproj_path=None: _HandlerFactice()
    )

    class AlwaysFail:
        def __init__(self, **kwargs):
            raise ValueError("Failed to load model from file: x.gguf")

    monkeypatch.setattr(M, "Llama", AlwaysFail)
    try:
        mgr._load_model("LFM2.5-VL-3B", 1024)
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


def test_paddleocr_ne_passe_jamais_par_llama(mgr):
    """Brouillon : PaddleOCR-VL-1.6 = worker transformers, fail loud via le manager."""
    with pytest.raises(ValueError, match="worker transformers"):
        mgr._load_model("PADDLEOCR-VL-1.6", 1024)

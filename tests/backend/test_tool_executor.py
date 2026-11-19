from __future__ import annotations

import threading
import time

from backend.inference.tool_calls import ToolCallRecord
from backend.inference.tool_executor import (
    TOOL_RESULT_MAX_CHARS,
    TOOL_TURN_MAX_CHARS,
    ToolExecutor,
)
from backend.tools.registry import get_default_registry


def test_executor_validates_before_callback() -> None:
    calls: list[str] = []
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: calls.append(name) or {"ok": True, "result": "ok", "latency_ms": 1},
        frozenset({"find_files"}),
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    assert result.ok is True
    assert calls == ["find_files"]


def test_executor_blocks_unknown_and_invalid_without_callback() -> None:
    calls: list[str] = []
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: calls.append(name) or {"ok": True, "result": "ok"},
        frozenset({"find_files"}),
    )
    unknown = executor.execute_call(ToolCallRecord("call_1", "delete_everything", {}, 0))
    invalid = executor.execute_call(ToolCallRecord("call_2", "find_files", {"unknown": 1}, 1))
    vide = executor.execute_call(ToolCallRecord("call_3", "find_files", {}, 2))
    assert unknown.result == "[UNKNOWN_TOOL]"
    # Le code MÉTIER doit survivre, et rester actionnable. Avant, tout était
    # noyé sous `[BAD_ARGS] <dump pydantic> errors.pydantic.dev…`, et le modèle
    # ne pouvait pas savoir qu'il fallait retirer la clé ou l'argument manquant
    # (q-r.md : un `[I2I_UNAVAILABLE]` jamais lu comme tel).
    assert invalid.result.startswith("[UNKNOWN_ARG]"), invalid.result
    assert "errors.pydantic.dev" not in invalid.result
    assert vide.result.startswith("[EMPTY_QUERY]"), vide.result
    assert "errors.pydantic.dev" not in vide.result
    assert calls == []


def test_executor_converts_callback_exception_to_result() -> None:
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: (_ for _ in ()).throw(RuntimeError("boom")),
        frozenset({"find_files"}),
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    assert result.ok is False
    assert "INTERNAL_ERROR" in result.result


def test_executor_marks_over_budget_result_as_timeout() -> None:
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: (time.sleep(0.01) or {"ok": True, "result": "late"}),
        frozenset({"find_files"}),
        timeout_s=0.001,
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    assert result.ok is False
    assert result.result == "[TIMEOUT] tool timeout"


def test_executor_coupe_chaque_resultat_a_son_plafond_unitaire() -> None:
    """Un outil qui rend 200 ko ne déborde pas le plafond unitaire."""
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: {"ok": True, "result": "x" * 200_000},
        frozenset({"find_files"}),
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    assert len(result.result) == TOOL_RESULT_MAX_CHARS


def test_executor_coupe_le_tour_et_le_dit() -> None:
    """Dix outils à 16 000 car. ne peuvent pas dépasser le plafond de tour.

    C'est le manque que le document des miroirs classe portage n°4 : le
    plafond unitaire existait, l'agrégé non. Sans ce test, le retour à la
    ligne `[:16000]` passerait pour un plafond.
    """
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: {"ok": True, "result": "x" * TOOL_RESULT_MAX_CHARS},
        frozenset({"find_files"}),
    )
    rendus = [
        executor.execute_call(ToolCallRecord(f"call_{i}", "find_files", {"query": f"q{i}"}, i))
        for i in range(10)
    ]
    # Ce qui entre dans la fenêtre, donc le CONTENU seul : un refus de budget
    # n'occupe pas de place, il refuse d'en occuper. Le compter ferait
    # dépasser un plafond qui, lui, est bien respecté.
    admis = [r for r in rendus if not r.result.startswith("[BUDGET_EXHAUSTED]")]
    assert sum(len(r.result) for r in admis) <= TOOL_TURN_MAX_CHARS
    # Trois résultats pleins, puis le tour est épuisé : le refus est explicite
    # et il ne se déguise pas en résultat tronqué.
    assert len(admis) == 3
    assert rendus[6].result == "[BUDGET_EXHAUSTED] plafond de tour atteint — résultat non rendu"
    for r in rendus[7:]:
        assert r.result == rendus[6].result


def test_executor_annonce_les_deux_nombres_de_la_coupe() -> None:
    """Une troncature muette ferait croire à une lecture intégrale."""
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: {"ok": True, "result": "x" * 5_000},
        frozenset({"find_files"}),
        turn_max_chars=2_000,
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    # 5 000 reçus, 2 000 de place, moins la marqueur elle-même : le second
    # nombre est celui du texte LIVRÉ, donc le total fait bien 2 000.
    assert "5000→19" in result.result
    assert len(result.result) == 2_000


def test_executor_ne_coupe_pas_ce_qui_tient() -> None:
    """Un résultat court passe intact : le plafond est une borne, pas un filtre."""
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: {"ok": True, "result": "court"},
        frozenset({"find_files"}),
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    assert result.result == "court"


def test_executor_waits_for_native_completion_after_timeout() -> None:
    completed = threading.Event()

    def slow_call(name, args, call):
        time.sleep(0.01)
        completed.set()
        return {"ok": True, "result": "late"}

    executor = ToolExecutor(
        get_default_registry(),
        slow_call,
        frozenset({"find_files"}),
        timeout_s=0.001,
    )
    result = executor.execute_call(ToolCallRecord("call_1", "find_files", {"query": "x"}, 0))
    assert result.result == "[TIMEOUT] tool timeout"
    assert completed.is_set()

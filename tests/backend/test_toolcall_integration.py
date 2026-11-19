from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base
from backend.inference.tool_calls import (
    append_canonical_tool_round,
    normalize_native_tool_calls,
    textual_tool_intent,
)
from backend.inference.tool_executor import ToolExecutor
from backend.inference.loop import (
    DISCOVERY_TOOL_NAMES,
    _aggregate_image_status,
    _discovery_records,
    _execute_find_files,
    _recover_named_tool_call,
    _with_image_status,
)
from backend.services.effect_ledger import claim_image_effect, mark_running, mark_uncertain
from backend.tools.base import ToolResult
from backend.tools.registry import get_default_registry


def test_two_calls_have_two_results_and_one_assistant_turn() -> None:
    records = normalize_native_tool_calls([
        {"id": "call_find", "function": {"name": "find_files", "arguments": '{"query":"x"}'}},
        {"id": "call_ask", "function": {"name": "ask_files", "arguments": '{"questions":["q"],"resource_ids":["doc_1"]}'}},
    ], run_id="run_int", round_index=0)
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: {"ok": True, "result": f"result:{name}", "latency_ms": 1},
        frozenset({"find_files", "ask_files"}),
    )
    executions = [executor.execute_call(record) for record in records]
    messages: list[dict] = []
    append_canonical_tool_round(
        messages,
        "raisonnement",
        records,
        {execution.call.id: execution.result for execution in executions},
    )
    assert [message["role"] for message in messages] == ["assistant", "tool", "tool"]
    assert [message["tool_call_id"] for message in messages[1:]] == ["call_find", "call_ask"]


def test_qr_textual_pseudo_call_only_selects_native_recovery_tool() -> None:
    content = (
        "<think>je génère via l'outil\n"
        '{\n  "name": "generate_image",\n  "arguments": {"prompt": "papillon photo réaliste en macro", "aspect_ratio": "1:1"}\n}\n'
        "</think>"
    )
    assert textual_tool_intent(content) == "generate_image"
    records = normalize_native_tool_calls([
        {"id": "native_generate", "function": {"name": "generate_image", "arguments": '{"prompt":"papillon","aspect_ratio":"1:1"}'}},
    ], run_id="run_qr", round_index=0)
    executor = ToolExecutor(
        get_default_registry(),
        lambda name, args, call: {"ok": True, "result": f"generated:{args['prompt']}", "latency_ms": 1},
        frozenset({"generate_image"}),
    )
    execution = executor.execute_call(records[0])
    assert execution.ok is True
    assert execution.call.provenance == "native"
    assert execution.arguments["aspect_ratio"] == "1:1"


def test_textual_intent_retries_with_named_native_call() -> None:
    class FakeRuntime:
        runtime_id = "llama_cpp"

        def complete_chat(self, request):
            assert request.tool_choice == {"type": "function", "function": {"name": "generate_image"}}
            return SimpleNamespace(raw={"choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "native_recovery",
                    "function": {"name": "generate_image", "arguments": '{"prompt":"papillon","aspect_ratio":"1:1"}'},
                }],
            }}]})

    records = _recover_named_tool_call(
        FakeRuntime(),
        [{"role": "user", "content": "génère un papillon"}],
        [{"type": "function", "function": {"name": "generate_image", "parameters": {"type": "object"}}}],
        "generate_image",
        run_id="run_qr",
        round_index=0,
        timeout_s=30.0,
    )
    assert len(records) == 1
    assert records[0].provenance == "native"
    assert records[0].name == "generate_image"


def test_image_publication_status_is_added_to_generator_brief() -> None:
    brief = _with_image_status({"brief": "Un chat tigré, lumière douce."}, {"published": True})
    assert brief is not None
    assert brief["brief"].startswith("Une image a été générée et publiée pour cette demande.")
    assert "Un chat tigré, lumière douce." in brief["brief"]


def test_failed_image_status_marks_even_an_empty_brief_incomplete() -> None:
    # Fiche §4.3 : une sortie dégradée est un état de traitement — jamais muet.
    brief = _with_image_status({}, {"published": False})
    assert brief is not None
    assert "La génération d’image n’a pas pu être terminée." in brief["brief"]
    assert "La couverture de cette demande est partielle." in brief["brief"]


def test_discovery_contract_excludes_submit_brief() -> None:
    assert "submit_brief" not in DISCOVERY_TOOL_NAMES
    assert DISCOVERY_TOOL_NAMES == frozenset({
        "search_info", "find_files", "ask_files", "generate_image", "web_research",
    })
    # m3 : `textual_tool_intent` n'acceptait qu'un nom appartenant à SON propre
    # littéral. L'annexe 10 exige « l'allowlist de la phase de découverte » :
    # deux listes égales aujourd'hui, sans rien qui les maintienne égales.
    # L'égalité est donc épinglée ICI, à côté du pin de DISCOVERY_TOOL_NAMES —
    # ajouter un outil de découverte casse ce test au lieu de diverger en silence.
    from backend.inference.tool_calls import _REASK_NAMED_TOOL_NAMES

    assert _REASK_NAMED_TOOL_NAMES == DISCOVERY_TOOL_NAMES
    records = normalize_native_tool_calls([
        {"id": "submit", "function": {"name": "submit_brief", "arguments": "{}"}},
        {"id": "find", "function": {"name": "find_files", "arguments": "{}"}},
    ], run_id="run_discovery", round_index=0)
    assert [record.name for record in _discovery_records(records)] == ["find_files"]


def test_reask_named_passe_le_budget_de_texte() -> None:
    """m1 : le re-ask natif forcé est un appel modèle de plus, pas gratuit.

    Sans `timeout_s`, `RuntimeRequest` prenait son défaut de 300 s : un tour image
    — le cas où un pseudo-appel textuel est ATTENDU — se voyait offrir un budget
    neuf au milieu d'un tour qui en avait déjà brûlé un.
    """
    seen: list[float] = []

    class FakeRuntime:
        runtime_id = "llama_cpp"

        def complete_chat(self, request):
            seen.append(request.timeout_s)
            return SimpleNamespace(raw={"choices": [{"message": {"content": "", "tool_calls": []}}]})

    _recover_named_tool_call(
        FakeRuntime(),
        [{"role": "user", "content": "génère un papillon"}],
        [{"type": "function", "function": {"name": "generate_image", "parameters": {"type": "object"}}}],
        "generate_image",
        run_id="run_qr",
        round_index=0,
        timeout_s=7.5,
    )
    assert seen == [7.5], "le re-ask doit hériter du budget restant, pas du défaut"



def test_image_status_aggregates_in_order_and_marks_failure_incomplete() -> None:
    status = _aggregate_image_status([
        {"tool": "generate_image", "ok": False, "result": "SERVICE_UNAVAILABLE"},
        {"tool": "generate_image", "ok": True, "artifact_id": "art_second", "result": "ok"},
    ])
    assert status is not None
    assert [attempt["ordinal"] for attempt in status["attempts"]] == [0, 1]
    brief = _with_image_status({"brief": "fait-0"}, status)
    assert brief is not None
    # La publication réelle ouvre le brief ; le texte soumis suit ; la tentative
    # échouée garde sa limite explicite et la couverture partielle est dite.
    assert brief["brief"].startswith("Une image a été générée et publiée pour cette demande.")
    assert "fait-0" in brief["brief"]
    assert "La génération d’image n’est pas disponible dans cet environnement." in brief["brief"]
    assert "La couverture de cette demande est partielle." in brief["brief"]


def test_discovery_forwards_recency_to_the_find_files_tool(monkeypatch) -> None:
    """`recency` atteint `FindFilesTool` — et rien d'autre n'est appelé.

    CE QUE CE TEST PINS À LA SUITE DE LA MIGRATION : `_execute_find_files` est un
    adaptateur, donc la seule chose qu'il doit transmettre sont les ARGUMENTS que
    l'orchestrateur a donnés. Avant, il les traduisait à la main en arguments de
    `search_documents` ; ce chemin n'existe plus, et un filtre qui serait perdu au
    passage ne se verrait nulle part ailleurs.

    On instrumente la CLASSE, pas une instance : l'adaptateur construit son propre
    outil, donc un patch d'instance ne le toucherait pas. Et on n'atteint surtout
    pas `execute` : sans base temporaire, il lirait la base réelle du projet.
    """
    from backend.tools.find_files import FindFilesTool

    vus: list[dict] = []

    async def _execute(self, args, ctx):  # noqa: ARG001 — signature de méthode
        vus.append(args.model_dump())
        return ToolResult(ok=True, content="{}", display_summary="[find_files] ok")

    monkeypatch.setattr(FindFilesTool, "execute", _execute)
    result = _execute_find_files({"args": {"query": "facture", "recency": "today"}}, conv_id="conv")

    assert result["ok"] is True
    assert vus[0]["recency"] == "today"
    assert vus[0]["query"] == "facture"


def test_cross_run_uncertain_effect_is_not_replayed() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    first = claim_image_effect(db, run_id="run_a", logical_action_id="image", output_index=0, fingerprint="fp")
    mark_running(db, first)
    mark_uncertain(db, first.effect_id, "crash")
    second = claim_image_effect(db, run_id="run_b", logical_action_id="image", output_index=0, fingerprint="fp")
    assert second.effect_id == first.effect_id
    assert second.status == "uncertain"

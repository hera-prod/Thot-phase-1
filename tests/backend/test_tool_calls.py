from __future__ import annotations

import json

import pytest

from backend.inference.tool_calls import (
    ToolCallParseError,
    append_canonical_tool_round,
    as_openai_tool_call,
    normalize_lfm25_content,
    normalize_named_tool_response,
    normalize_native_tool_calls,
    sanitize_provider_content,
    textual_tool_intent,
)


def test_normalizes_arguments_and_missing_id_deterministically() -> None:
    first = normalize_native_tool_calls(
        [{"function": {"name": "find_files", "arguments": '{"query":"rapport"}'}}],
        run_id="run_1",
        round_index=2,
    )
    second = normalize_native_tool_calls(
        [{"function": {"name": "find_files", "arguments": {"query": "rapport"}}}],
        run_id="run_1",
        round_index=2,
    )
    assert first[0].id == second[0].id
    assert first[0].ordinal == 0
    assert json.loads(as_openai_tool_call(first[0])["function"]["arguments"]) == {"query": "rapport"}


def test_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ToolCallParseError, match="dupliqué"):
        normalize_native_tool_calls([
            {"id": "same", "function": {"name": "find_files", "arguments": "{}"}},
            {"id": "same", "function": {"name": "ask_files", "arguments": "{}"}},
        ], run_id="run_1", round_index=0)


def test_rejects_non_object_arguments() -> None:
    with pytest.raises(ToolCallParseError, match="arguments invalides"):
        normalize_native_tool_calls([
            {"id": "call_1", "function": {"name": "find_files", "arguments": "[]"}},
        ], run_id="run_1", round_index=0)


def test_canonical_round_has_one_result_per_call_in_order() -> None:
    records = normalize_native_tool_calls([
        {"id": "call_1", "function": {"name": "find_files", "arguments": "{}"}},
        {"id": "call_2", "function": {"name": "ask_files", "arguments": "{}"}},
    ], run_id="run_1", round_index=0)
    messages: list[dict] = []
    append_canonical_tool_round(messages, "raisonnement", records, {
        "call_1": "résultat 1",
        "call_2": "résultat 2",
    })
    assert [message["role"] for message in messages] == ["assistant", "tool", "tool"]
    assert messages[0]["tool_calls"][1]["id"] == "call_2"


def test_native_lfm_wrapper_is_parsed_but_markerless_is_not() -> None:
    content = "<|tool_call_start|>[generate_image(prompt=\"un chat\", format=\"1:1\", seed=None)]<|tool_call_end|>"
    records = normalize_lfm25_content("<think>réfléchis</think>" + content, run_id="run_1", round_index=0)
    assert records[0].name == "generate_image"
    assert records[0].arguments == {"prompt": "un chat", "format": "1:1", "seed": None}
    assert normalize_lfm25_content("[generate_image(prompt=\"x\")]", run_id="run_1", round_index=0) == ()


def test_malformed_native_lfm_wrapper_is_rejected() -> None:
    with pytest.raises(ToolCallParseError, match="incomplet"):
        normalize_lfm25_content("<|tool_call_start|>[find_files(text=\"x\")]", run_id="run_1", round_index=0)
    with pytest.raises(ToolCallParseError, match="non littéral"):
        normalize_lfm25_content("<|tool_call_start|>[find_files(text=some_name)]<|tool_call_end|>", run_id="run_1", round_index=0)


def test_textual_intent_reads_a_bare_pseudo_call() -> None:
    content = '{"name":"generate_image","arguments":{"prompt":"papillon macro","format":"1:1"}}'
    assert textual_tool_intent(content) == "generate_image"


def test_textual_intent_sees_through_thinking_prose_and_fences() -> None:
    pseudo = '{\n  "name": "generate_image",\n  "arguments": {\n    "prompt": "papillon"\n  }\n}'
    assert textual_tool_intent(f"<think>je passe par un outil\n{pseudo}\n</think>") == "generate_image"
    assert textual_tool_intent(f"Voici mon plan :\n{pseudo}\nJe continue.") == "generate_image"
    assert textual_tool_intent(f"```json\n{pseudo}\n```") == "generate_image"


def test_textual_intent_refuses_two_distinct_names() -> None:
    content = (
        '{"name":"generate_image","arguments":{"prompt":"papillon"}} '
        '{"name":"find_files","arguments":{"query":"papillon"}}'
    )
    assert textual_tool_intent(content) is None


def test_textual_intent_accepts_one_name_repeated() -> None:
    content = (
        '{"name":"generate_image","arguments":{"prompt":"papillon"}} '
        '{"name":"generate_image","arguments":{"prompt":"papillon","format":"1:1"}}'
    )
    assert textual_tool_intent(content) == "generate_image"


def test_textual_intent_requires_exact_allowed_shape() -> None:
    assert textual_tool_intent('{"name":"generate_image","arguments":{"prompt":"<|im_start|>"}}') is None
    assert textual_tool_intent('{"name":"generate_image","arguments":{"opts":{"mode":"<|tool_call_start|>"}}}') is None
    assert textual_tool_intent('{"name":"delete_file","arguments":{}}') is None
    assert textual_tool_intent('{"tool":"generate_image","arguments":{}}') is None
    assert textual_tool_intent('{"name":"generate_image","arguments":{},"side_effect":true}') is None
    assert textual_tool_intent('{"name":"generate_image"}') is None
    assert textual_tool_intent('{"name":"generate_image","arguments":"papillon"}') is None
    assert textual_tool_intent("Voici le plan: {\"name\":\"generate_image\"}") is None
    assert textual_tool_intent('{"name":"generate_image","arguments":') is None
    assert textual_tool_intent("je ne peux pas générer d’image") is None


def test_textual_intent_refuses_oversized_content() -> None:
    content = "x" * 65536 + '{"name":"generate_image","arguments":{}}'
    assert textual_tool_intent(content) is None


def test_textual_intent_refuses_un_balayage_epuise() -> None:
    """Un nom trouvé n'est pas une unicité PROUVÉE.

    Le balayage s'arrête après `_TEXTUAL_INTENT_MAX_SCANS` `{` : le texte restant
    peut contenir un second pseudo-appel jamais lu. Retourner le premier nom
    revenait à contraindre le modèle vers un outil au hasard — et la docstring
    de la fonction affirmait le contraire.
    """
    from backend.inference.tool_calls import _TEXTUAL_INTENT_MAX_SCANS

    ask = '{"name":"ask_files","arguments":{"questions":["q"]}}'
    image = '{"name":"generate_image","arguments":{"prompt":"papillon"}}'
    # 63 `{` ratés, puis ask_files est lu au 64e scan : la limite tombe AVANT le
    # second objet. Un nom vu, l'autre jamais lu → ambiguïté non tranchée, et le
    # contenu lisible affirmait le contraire de ce que la fonction renvoyait.
    epuise = "{" * (_TEXTUAL_INTENT_MAX_SCANS - 1)
    assert textual_tool_intent(epuise + ask + image) is None
    # Sans texte derrière, le contenu a été ENTIÈREMENT lu : refuser ici serait
    # un refus de pure forme.
    assert textual_tool_intent(epuise + ask) == "ask_files"


def test_textual_intent_lit_tout_le_contenu() -> None:
    """Deux pseudo-appels distincts, même collés : le second ne doit pas être sauté.

    `raw_decode` renvoie une position absolue ; l'ancien `opening + end` passait
    par-dessus tout ce qui suivait, si bien qu'un contenu bruité de 60 `{` faisait
    disparaître le second nom — et l'ambiguïté que la fonction promet de refuser
    n'était jamais vue.
    """
    ask = '{"name":"ask_files","arguments":{"questions":["q"]}}'
    image = '{"name":"generate_image","arguments":{"prompt":"papillon"}}'
    assert textual_tool_intent(ask + image) is None
    assert textual_tool_intent("{" * 60 + ask + image) is None
    assert textual_tool_intent("{" * 8 + ask) == "ask_files", \
        "un contenu entièrement lu et un seul nom : rien à refuser"
    assert textual_tool_intent(ask + ask) == "ask_files", "le même nom répété reste univoque"



def test_textual_intent_returns_the_name_alone() -> None:
    intent = textual_tool_intent('prose {"name":"ask_files","arguments":{"questions":["q"]}} suite')
    assert intent == "ask_files"
    assert isinstance(intent, str) and not isinstance(intent, (dict, list, tuple))


def test_named_response_accepts_only_the_exact_native_function() -> None:
    message = {
        "tool_calls": [{
            "id": "native_exact",
            "function": {"name": "generate_image", "arguments": "{}"},
        }],
    }
    records = normalize_named_tool_response(
        message,
        expected_name="generate_image",
        run_id="run_strict",
        round_index=0,
    )
    assert len(records) == 1 and records[0].name == "generate_image"

    wrong = {
        "tool_calls": [{
            "id": "native_wrong",
            "function": {"name": "find_files", "arguments": "{}"},
        }],
    }
    assert normalize_named_tool_response(
        wrong,
        expected_name="generate_image",
        run_id="run_strict",
        round_index=0,
    ) == ()


def test_named_response_does_not_execute_free_json() -> None:
    message = {
        "content": '{"name":"generate_image","arguments":{"prompt":"x"}}',
    }
    assert normalize_named_tool_response(
        message,
        expected_name="generate_image",
        run_id="run_strict",
        round_index=0,
        allow_wrapper=True,
    ) == ()


def test_provider_sanitizer_removes_json_and_nested_pseudo_calls() -> None:
    value = 'avant {"name":"generate_image","arguments":{"prompt":"x"}} après'
    assert sanitize_provider_content(value) == "avant  après"
    value = 'avant [ask_files(questions=["q", "r"])] après'
    assert "ask_files" not in sanitize_provider_content(value)
    assert sanitize_provider_content('{"name":"generate_image","arguments":') == ""


def test_canonical_tool_arguments_are_sanitized_without_mutating_execution_record() -> None:
    record = normalize_native_tool_calls(
        [{"id": "call_1", "function": {
            "name": "generate_image",
            "arguments": {"prompt": '{"name":"generate_image","arguments":{}}'},
        }}],
        run_id="run_args",
        round_index=0,
    )[0]
    rendered = as_openai_tool_call(record)["function"]["arguments"]
    assert "generate_image" not in rendered
    assert record.arguments["prompt"].startswith('{"name"')

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest

from backend.inference.runtime import (
    RuntimeCapabilities,
    RuntimeProtocolError,
    RuntimeRequest,
    RuntimeResponse,
    LlamaCppRuntime,
    _estimated_tokens,
    CONTEXT_SAFETY_TOKENS,
    MIN_OUTPUT_TOKENS,
    normalize_chat_response,
    normalize_tool_calls,
    RuntimeModelAdapter,
)


def test_normalize_native_tool_call() -> None:
    calls = normalize_tool_calls(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "find_files", "arguments": '{"query":"rapport"}'},
            }
        ]
    )
    assert calls[0].id == "call_1"
    assert calls[0].name == "find_files"
    assert calls[0].arguments == {"query": "rapport"}


def test_normalize_rejects_duplicate_ids() -> None:
    with pytest.raises(RuntimeProtocolError, match="dupliqué"):
        normalize_tool_calls(
            [
                {"id": "same", "function": {"name": "find_files", "arguments": "{}"}},
                {"id": "same", "function": {"name": "ask_files", "arguments": "{}"}},
            ]
        )


def test_normalize_rejects_invalid_arguments() -> None:
    with pytest.raises(RuntimeProtocolError, match="arguments invalides"):
        normalize_tool_calls(
            [
                {
                    "id": "call_1",
                    "function": {"name": "find_files", "arguments": "not-json"},
                },
            ]
        )


def test_normalize_response_without_tool_call() -> None:
    response = normalize_chat_response(
        {"choices": [{"message": {"content": "Bonjour"}, "finish_reason": "stop"}]}
    )
    assert response.text == "Bonjour"
    assert response.tool_calls == ()


class FakeModel:
    def __init__(self) -> None:
        self.chat_request: dict | None = None
        self.text_request: dict | None = None

    def create_chat_completion(self, **kwargs):
        self.chat_request = kwargs
        return {
            "choices": [
                {
                    "message": {"content": "ok", "tool_calls": []},
                    "finish_reason": "stop",
                }
            ]
        }

    def create_completion(self, **kwargs):
        self.text_request = kwargs
        return {"choices": [{"text": "résumé"}]}


class FakeManager:
    def __init__(self) -> None:
        self.model = FakeModel()
        self.n_ctx: int | None = None
        self.load_kwargs: dict = {}

    @contextmanager
    def use_model(self, name, estimated_tokens=0, *, kind="interactive"):
        assert name == "LFM2.5-8B"
        self.load_kwargs = {"estimated_tokens": estimated_tokens, "kind": kind}
        yield self.model

    def context_window(self, name=None):
        return self.n_ctx


class FakeManagerSansFenetre:
    """Manager sans `context_window` : le runtime doit garder son comportement."""

    def __init__(self) -> None:
        self.model = FakeModel()

    @contextmanager
    def use_model(self, name, estimated_tokens=0, *, kind="interactive"):
        yield self.model


def test_llama_cpp_runtime_uses_both_provider_apis() -> None:
    manager = FakeManager()
    runtime = LlamaCppRuntime(manager)
    response = runtime.complete_chat(
        RuntimeRequest(
            messages=({"role": "user", "content": "Bonjour"},),
            tools=({"type": "function", "function": {"name": "find_files"}},),
            tool_choice="auto",
        )
    )
    assert response.text == "ok"
    assert manager.model.chat_request is not None
    assert manager.model.chat_request["tool_choice"] == "auto"
    assert runtime.complete_text("résume", max_tokens=32, temperature=0.2) == "résumé"
    assert manager.model.text_request is not None
    assert manager.model.text_request["max_tokens"] == 32


def test_llama_cpp_runtime_marks_slow_response_as_timeout() -> None:
    manager = FakeManager()
    original = manager.model.create_chat_completion

    def slow(**kwargs):
        time.sleep(0.01)
        return original(**kwargs)

    manager.model.create_chat_completion = slow
    runtime = LlamaCppRuntime(manager)
    with pytest.raises(RuntimeProtocolError, match="RUNTIME_TIMEOUT"):
        runtime.complete_chat(
            RuntimeRequest(
                messages=({"role": "user", "content": "Bonjour"},),
                timeout_s=0.001,
            )
        )


def test_runtime_adapter_propagates_request_timeout() -> None:
    class CapturingRuntime:
        runtime_id = "test"

        def __init__(self) -> None:
            self.request: RuntimeRequest | None = None

        @property
        def runtime_fingerprint(self) -> str:
            return "test:fingerprint"

        @property
        def capabilities(self) -> RuntimeCapabilities:
            return RuntimeCapabilities(True, True, False, False)

        def complete_chat(self, request: RuntimeRequest) -> RuntimeResponse:
            self.request = request
            return RuntimeResponse(
                text="ok",
                tool_calls=(),
                finish_reason="stop",
                raw={"choices": [{"message": {"content": "ok"}}]},
            )

        def complete_text(
            self, prompt: str, *, max_tokens: int, temperature: float
        ) -> str:
            return ""

    runtime = CapturingRuntime()
    adapter = RuntimeModelAdapter(runtime)
    adapter.create_chat_completion(
        messages=[{"role": "user", "content": "x"}],
        timeout_s=1.25,
    )
    assert runtime.request is not None
    assert runtime.request.timeout_s == 1.25


def test_llama_cpp_runtime_plafonne_max_tokens_a_la_fenetre() -> None:
    manager = FakeManager()
    manager.n_ctx = 2000
    runtime = LlamaCppRuntime(manager)
    request = RuntimeRequest(
        messages=({"role": "user", "content": "Bonjour"},),
        max_tokens=1500,
    )

    runtime.complete_chat(request)

    attendu = 2000 - _estimated_tokens(request) - 256
    assert manager.model.chat_request is not None
    assert manager.model.chat_request["max_tokens"] == attendu


def test_llama_cpp_runtime_refuse_prompt_plus_grand_que_fenetre() -> None:
    manager = FakeManager()
    manager.n_ctx = 1000
    runtime = LlamaCppRuntime(manager)

    with pytest.raises(RuntimeProtocolError, match="CONTEXT_WINDOW_TOO_SMALL"):
        runtime.complete_chat(
            RuntimeRequest(
                messages=({"role": "user", "content": "x" * 10000},),
                max_tokens=100,
            )
        )


def test_llama_cpp_runtime_sans_context_window_garde_max_tokens() -> None:
    manager = FakeManagerSansFenetre()
    runtime = LlamaCppRuntime(manager)

    runtime.complete_chat(
        RuntimeRequest(
            messages=({"role": "user", "content": "Bonjour"},),
            max_tokens=1500,
        )
    )

    assert manager.model.chat_request is not None
    assert manager.model.chat_request["max_tokens"] == 1500


def test_llama_cpp_runtime_refuse_place_insuffisante_pour_la_sortie() -> None:
    manager = FakeManager()
    request = RuntimeRequest(
        messages=({"role": "user", "content": "Bonjour"},),
        max_tokens=100,
    )
    manager.n_ctx = _estimated_tokens(request) + 256 + 32
    runtime = LlamaCppRuntime(manager)

    with pytest.raises(RuntimeProtocolError, match="CONTEXT_WINDOW_TOO_SMALL"):
        runtime.complete_chat(request)

    assert manager.model.chat_request is None


def test_llama_cpp_runtime_nominal_max_tokens_conserve() -> None:
    manager = FakeManager()
    manager.n_ctx = 10000
    runtime = LlamaCppRuntime(manager)
    request = RuntimeRequest(
        messages=({"role": "user", "content": "Bonjour"},),
        max_tokens=32,
    )

    runtime.complete_chat(request)

    assert manager.model.chat_request is not None
    assert manager.model.chat_request["max_tokens"] == 32

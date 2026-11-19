from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from backend.config.defaults import CHARS_PER_TOKEN

# Marge de sécurité entre le prompt estimé et la fenêtre : même avec un ratio
# prudent (3 caractères/token), le tokenizer réel n'est pas mesuré.
CONTEXT_SAFETY_TOKENS = 256
# Sortie minimale exigée pour qu'un appel ait du sens ; en dessous, refus.
MIN_OUTPUT_TOKENS = 64


class RuntimeProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class RuntimeCapabilities:
    native_tool_calls: bool
    native_timeout: bool
    streaming: bool
    parallel_tool_calls: bool


@dataclass(frozen=True)
class RuntimeRequest:
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    tool_choice: Any = None
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_s: float = 300.0
    # Nature du travail pour le coordinateur de ressources : « interactive »
    # (chat) attend jusqu'à 120 s, « ingestion » cède après 10 s et replanifie.
    kind: str = "interactive"


@dataclass(frozen=True)
class RuntimeResponse:
    text: str
    tool_calls: tuple[RuntimeToolCall, ...]
    finish_reason: str | None
    raw: Mapping[str, Any]


class ChatRuntime(Protocol):
    runtime_id: str

    @property
    def runtime_fingerprint(self) -> str: ...

    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    def complete_chat(self, request: RuntimeRequest) -> RuntimeResponse: ...

    def complete_text(
        self, prompt: str, *, max_tokens: int, temperature: float
    ) -> str: ...


def normalize_tool_calls(native: Any) -> tuple[RuntimeToolCall, ...]:
    if native is None:
        return ()
    if not isinstance(native, list):
        raise RuntimeProtocolError("tool_calls doit être une liste")
    calls: list[RuntimeToolCall] = []
    seen: set[str] = set()
    for index, raw in enumerate(native):
        if not isinstance(raw, Mapping):
            raise RuntimeProtocolError(f"tool_calls[{index}] invalide")
        call_id = str(raw.get("id") or "").strip()
        function = raw.get("function")
        if not isinstance(function, Mapping):
            raise RuntimeProtocolError(f"tool_calls[{index}].function manquant")
        name = str(function.get("name") or "").strip()
        if not call_id or not name:
            raise RuntimeProtocolError(f"tool_calls[{index}] incomplet")
        if call_id in seen:
            raise RuntimeProtocolError(f"tool_call_id dupliqué: {call_id}")
        seen.add(call_id)
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise RuntimeProtocolError(f"arguments invalides pour {name}") from exc
        if not isinstance(arguments, Mapping):
            raise RuntimeProtocolError(f"arguments invalides pour {name}")
        calls.append(RuntimeToolCall(call_id, name, dict(arguments)))
    return tuple(calls)


def normalize_chat_response(response: Any) -> RuntimeResponse:
    if not isinstance(response, Mapping):
        raise RuntimeProtocolError("réponse chat invalide")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeProtocolError("réponse chat sans choix")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise RuntimeProtocolError("choix chat invalide")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise RuntimeProtocolError("message chat manquant")
    return RuntimeResponse(
        text=str(message.get("content") or ""),
        tool_calls=normalize_tool_calls(message.get("tool_calls")),
        finish_reason=str(choice.get("finish_reason"))
        if choice.get("finish_reason") is not None
        else None,
        raw=response,
    )


def _jinja_tool_call(tool_call: Any) -> dict[str, Any]:
    """Un appel d'outil dont ``function.arguments`` est un objet, pas une chaîne."""
    function = tool_call.get("function") if isinstance(tool_call, Mapping) else None
    if not isinstance(function, Mapping):
        raise RuntimeProtocolError("tool_call.function absent pour le template Jinja")
    name = str(function.get("name") or "")
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise RuntimeProtocolError(
                f"arguments illisibles pour le template Jinja: {name}"
            ) from exc
    if not isinstance(arguments, Mapping):
        raise RuntimeProtocolError(
            f"arguments illisibles pour le template Jinja: {name}"
        )
    return {**tool_call, "function": {**function, "arguments": dict(arguments)}}


def to_jinja_tool_call_messages(
    messages: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Traduit la forme OpenAI des arguments d'outil vers la forme du template Jinja.

    Le record canonique et la charge utile OpenAI portent ``function.arguments``
    en chaîne JSON : c'est ce que la spec OpenAI, la trace, la persistance et le
    serveur llama attendent. Le template Jinja embarqué dans le GGUF veut l'inverse —
    sa macro ``render_tool_calls`` fait ``func_args.items()`` pour rendre
    ``nom(valeur, ...)`` — donc une chaîne y meurt sur
    ``'str object' has no attribute 'items'`` et le tour suivant du modèle ne part
    jamais : un tour à outil ne peut alors jamais produire une seconde décision.

    Traduire ici, au SEUL point où les messages entrent dans llama.cpp, laisse le
    contrat OpenAI intact partout ailleurs et ne touche qu'une copie : l'appelant
    conserve ses messages. Les messages sans appel d'outil traversent inchangés.
    """
    translated: list[dict[str, Any]] = []
    for message in messages:
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            translated.append(dict(message))
            continue
        translated.append(
            {**message, "tool_calls": [_jinja_tool_call(call) for call in tool_calls]}
        )
    return translated


def _estimated_tokens(request: RuntimeRequest) -> int:
    """Ce que le prompt va coûter, en tokens — schémas d'outils INCLUS.

    Mesuré le 28/09 : l'estimation ne comptait que le contenu des messages. Or
    les cinq définitions de découverte pèsent 5 585 octets de JSON, soit
    ~1 400 tokens qui n'étaient jamais comptés. Sur une machine de 8 Go,
    `resolve_n_ctx` dimensionne la fenêtre sur cette estimation, et le prompt
    réel la dépasse : llama.cpp échoue alors sur un `could not broadcast input
    array from shape (301,) into shape (280,)` — une erreur opaque, loin du
    « contexte dépassé » qu'elle signifie.

    Le ratio de 3 caractères par token (CHARS_PER_TOKEN) est volontairement
    prudent pour le français : sous-estimer ici coûte un prompt estimé trop
    court, donc un `max_tokens` trop généreux et un dépassement de fenêtre ;
    surestimer ne coûte qu'une fenêtre ou une compaction plus prudente.

    Le ``+ 1024`` de base couvre le template de chat et l'enveloppe des messages.
    La marge de sécurité ``CONTEXT_SAFETY_TOKENS`` n'est plus ajoutée ici : elle
    est appliquée une seule fois, au moment du plafonnement de ``max_tokens``.
    """
    contenu = sum(
        len(str(message.get("content") or "")) for message in request.messages
    )
    schemas = 0
    for outil in request.tools:
        try:
            schemas += len(json.dumps(dict(outil), ensure_ascii=False))
        except (TypeError, ValueError):
            schemas += (
                512  # un schéma illisible reste un schéma : on ne sous-estime pas
            )
    return (contenu + schemas) // CHARS_PER_TOKEN + 1024


def _window_for(manager: object, model_name: str) -> int | None:
    """Fenêtre réelle du modèle chargé, si le manager sait la donner.

    Les managers de test (et les runtimes alternatifs) ne l'exposent pas : dans
    ce cas on renvoie None et l'appelant garde son comportement historique, sans
    inventer une fenêtre qui n'a pas été allouée.
    """
    getter = getattr(manager, "context_window", None)
    if not callable(getter):
        return None
    fenetre: Any = getter(model_name)
    return int(fenetre) if fenetre is not None else None


def _file_identity(path: str) -> str:
    try:
        stat = os.stat(path)
        raw = f"{os.path.abspath(path)}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        raw = os.path.abspath(path)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class LlamaCppRuntime:
    runtime_id = "llama_cpp"

    def __init__(self, manager: Any, model_name: str = "LFM2.5-8B") -> None:
        self.manager = manager
        self.model_name = model_name

    @property
    def runtime_fingerprint(self) -> str:
        try:
            from backend.config.defaults import MODEL_CONFIGS

            model_path = MODEL_CONFIGS[self.model_name].path
        except Exception:
            model_path = self.model_name
        return (
            f"llama_cpp:{self.model_name}:{_file_identity(model_path)}:llama-cpp-python"
        )

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(True, True, True, False)

    def complete_chat(self, request: RuntimeRequest) -> RuntimeResponse:
        if not math.isfinite(request.timeout_s) or request.timeout_s <= 0:
            raise RuntimeProtocolError("RUNTIME_TIMEOUT")
        started = time.monotonic()
        est = _estimated_tokens(request)
        with self.manager.use_model(
            self.model_name,
            estimated_tokens=est,
            kind=request.kind,
        ) as model:
            max_tokens = request.max_tokens
            fenetre = _window_for(self.manager, self.model_name)
            if fenetre is not None:
                dispo = fenetre - est - CONTEXT_SAFETY_TOKENS
                if dispo < MIN_OUTPUT_TOKENS:
                    raise RuntimeProtocolError(
                        f"CONTEXT_WINDOW_TOO_SMALL: prompt≈{est} tokens, fenêtre {fenetre}, "
                        f"place restante {dispo} < {MIN_OUTPUT_TOKENS} tokens de sortie."
                    )
                max_tokens = min(request.max_tokens, dispo)
            response = model.create_chat_completion(
                messages=to_jinja_tool_call_messages(request.messages),
                tools=[dict(tool) for tool in request.tools] or None,
                tool_choice=request.tool_choice,
                temperature=request.temperature,
                max_tokens=max_tokens,
            )
        if time.monotonic() - started > request.timeout_s:
            raise RuntimeProtocolError("RUNTIME_TIMEOUT")
        return normalize_chat_response(response)

    def complete_text(self, prompt: str, *, max_tokens: int, temperature: float) -> str:
        estimated = len(prompt) // CHARS_PER_TOKEN + max_tokens + 1024
        with self.manager.use_model(
            self.model_name, estimated_tokens=estimated
        ) as model:
            effective_max = max_tokens
            fenetre = _window_for(self.manager, self.model_name)
            if fenetre is not None:
                dispo = fenetre - estimated - CONTEXT_SAFETY_TOKENS
                if dispo < MIN_OUTPUT_TOKENS:
                    raise RuntimeProtocolError(
                        "CONTEXT_WINDOW_TOO_SMALL: prompt de compaction trop grand "
                        f"pour la fenêtre {fenetre} (estimé {estimated}, "
                        f"place restante {dispo} < {MIN_OUTPUT_TOKENS})."
                    )
                effective_max = min(max_tokens, dispo)
            response = model.create_completion(
                prompt=prompt,
                max_tokens=effective_max,
                temperature=temperature,
            )
        choices = response.get("choices", []) if isinstance(response, Mapping) else []
        if not choices or not isinstance(choices[0], Mapping):
            raise RuntimeProtocolError("réponse texte vide")
        return str(choices[0].get("text") or "").strip()


class RuntimeModelAdapter:
    def __init__(self, runtime: ChatRuntime) -> None:
        self.runtime = runtime

    def create_completion(
        self, *, prompt: str, max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "text": self.runtime.complete_text(
                        prompt, max_tokens=max_tokens, temperature=temperature
                    )
                }
            ]
        }

    def create_chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        timeout_value = kwargs.get("timeout_s", 300.0)
        request = RuntimeRequest(
            messages=tuple(kwargs.get("messages") or ()),
            tools=tuple(kwargs.get("tools") or ()),
            tool_choice=kwargs.get("tool_choice"),
            temperature=float(kwargs.get("temperature", 0.0)),
            max_tokens=int(kwargs.get("max_tokens", 1024)),
            timeout_s=float(timeout_value if timeout_value is not None else 300.0),
        )
        return dict(self.runtime.complete_chat(request).raw)


def _manifest_runtime_default() -> str:
    candidates = (
        Path(__file__).resolve().parents[2] / "distribution.json",
        Path.cwd() / "distribution.json",
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            runtime = (
                payload.get("tool_runtime") if isinstance(payload, Mapping) else None
            )
            default = runtime.get("default") if isinstance(runtime, Mapping) else None
            if default in {"llama_cpp", "llama_server"}:
                return str(default)
        except (OSError, ValueError, TypeError):
            continue
    return "llama_cpp"


def build_runtime(manager: Any) -> ChatRuntime:
    choice = (
        os.environ.get("THOT_TOOL_RUNTIME", _manifest_runtime_default()).strip().lower()
    )
    if choice == "llama_cpp":
        return LlamaCppRuntime(manager)
    if choice == "llama_server":
        from backend.inference.llama_server import (
            LlamaServerProcess,
            LlamaServerRuntime,
            default_llama_server_config,
        )

        process = LlamaServerProcess(default_llama_server_config())
        process.start()
        return LlamaServerRuntime(process)
    raise RuntimeProtocolError(f"THOT_TOOL_RUNTIME inconnu: {choice}")

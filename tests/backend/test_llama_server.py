from __future__ import annotations

import pytest

from backend.inference.llama_server import (
    LlamaServerClient,
    LlamaServerConfig,
    LlamaServerError,
    LlamaServerProcess,
    LlamaServerRuntime,
)
from backend.inference.runtime import RuntimeRequest


class FakeClient(LlamaServerClient):
    def __init__(self, config: LlamaServerConfig) -> None:
        super().__init__(config)
        self.payloads = []

    def health(self) -> bool:
        return True

    def chat_completion(self, payload, timeout_s=None):
        self.payloads.append(dict(payload))
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}, "finish_reason": "stop"}]}


def test_command_is_local_and_tool_aware() -> None:
    config = LlamaServerConfig(binary="/opt/llama-server", model="/models/text.gguf")
    process = LlamaServerProcess(config, FakeClient(config))
    command = process.command()
    assert command[0] == "/opt/llama-server"
    assert "--jinja" in command
    assert "--flash-attn" in command
    assert "127.0.0.1" in command
    assert command[command.index("--host") + 1] == "127.0.0.1"


def test_runtime_sends_native_tool_contract() -> None:
    config = LlamaServerConfig(binary="/opt/llama-server", model="/models/text.gguf")
    client = FakeClient(config)
    runtime = LlamaServerRuntime(LlamaServerProcess(config, client))
    response = runtime.complete_chat(RuntimeRequest(
        messages=({"role": "user", "content": "salut"},),
        tools=({"type": "function", "function": {"name": "find_files"}},),
        tool_choice="auto",
    ))
    assert response.text == "ok"
    assert runtime.capabilities.native_tool_calls is True
    assert runtime.capabilities.native_timeout is True
    assert len(runtime.runtime_fingerprint) > 32
    assert client.payloads[0]["tool_choice"] == "auto"
    assert client.payloads[0]["parallel_tool_calls"] is False


def test_default_config_resolves_without_any_environment_variable(tmp_path, monkeypatch) -> None:
    """Le runtime se résout depuis le dossier de l'application, variables ou pas.

    AVANT : `default_llama_server_config()` levait « THOT_LLAMA_SERVER absent », donc
    une application sans cette variable ne démarrait pas, même avec son runtime posé
    à côté d'elle. Ce test ne peut plus être écrit comme il l'était — l'absence de la
    variable n'est plus une erreur — donc il vérifie l'inverse, qui est le contrat
    nouveau : le runtime est trouvé par ses racines.
    """
    from backend.inference.llama_server import default_llama_server_config

    monkeypatch.delenv("THOT_LLAMA_SERVER", raising=False)
    racine = tmp_path / "app"
    (racine / "bin").mkdir(parents=True)
    (racine / "portable.json").write_text("{}", encoding="utf-8")
    binaire = racine / "bin" / "llama-server"
    binaire.write_bytes(b"#!/bin/false\n")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))

    config = default_llama_server_config()

    assert config.binary == str(binaire)


def test_un_dead_override_ne_remplace_pas_le_fichier_present(tmp_path, monkeypatch) -> None:
    """Une surcharge MORTE ne gagne pas sur un runtime réellement présent.

    AVANT : `THOT_LLAMA_SERVER=<chemin mort>` suffisait à rendre l'application
    inutilisable, parce que la variable était la SEULE source et qu'il fallait en
    plus `THOT_LLAMA_SERVER_ALLOW_UNPACKAGED=1` pour qu'elle passe. Le couple était
    l'aveu écrit que le runtime ne venait pas de l'application.

    MAINTENANT : la variable est une surcharge. Posée et fausse, elle est consignée
    comme écart et la résolution continue sur le disque. Le test le prouve dans les
    deux sens : sans le fichier, l'erreur nomme la variable ; avec le fichier, le
    fichier gagne.
    """
    from backend.inference.llama_server import default_llama_server_config

    monkeypatch.setenv("THOT_LLAMA_SERVER", str(tmp_path / "absent" / "llama-server"))
    with pytest.raises(LlamaServerError) as echec:
        default_llama_server_config()
    assert "THOT_LLAMA_SERVER" in str(echec.value)

    racine = tmp_path / "app"
    (racine / "bin").mkdir(parents=True)
    (racine / "portable.json").write_text("{}", encoding="utf-8")
    binaire = racine / "bin" / "llama-server"
    binaire.write_bytes(b"#!/bin/false\n")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))

    assert default_llama_server_config().binary == str(binaire)


def test_start_rejects_missing_executable_and_model(tmp_path) -> None:
    config = LlamaServerConfig(
        binary=str(tmp_path / "missing-server"),
        model=str(tmp_path / "missing.gguf"),
    )
    with pytest.raises(LlamaServerError, match="executable invalide"):
        LlamaServerProcess(config, FakeClient(config)).start()

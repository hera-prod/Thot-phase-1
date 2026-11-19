from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.config.paths import (
    DependanceIntrouvable,
    resoudre_fichier_modele,
    resoudre_runtime_inference,
)
from backend.inference.runtime import (
    ChatRuntime,
    RuntimeCapabilities,
    RuntimeProtocolError,
    RuntimeRequest,
    RuntimeResponse,
    normalize_chat_response,
)


def _file_identity(path: str) -> str:
    try:
        stat = os.stat(path)
        raw = f"{os.path.abspath(path)}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        raw = os.path.abspath(path)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class LlamaServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlamaServerConfig:
    binary: str
    model: str
    host: str = "127.0.0.1"
    port: int = 8081
    ctx_size: int = 32768
    parallel: int = 1
    gpu_layers: int = 99
    flash_attn: bool = True
    threads: int = -1
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class LlamaServerClient:
    def __init__(self, config: LlamaServerConfig, timeout_s: float = 30.0) -> None:
        self.config = config
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None, timeout_s: float | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.config.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or self.timeout_s) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise LlamaServerError(f"llama-server HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LlamaServerError(f"llama-server inaccessible: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LlamaServerError("llama-server réponse JSON invalide") from exc

    def health(self) -> bool:
        try:
            payload = self._request("GET", "/health")
            return bool(isinstance(payload, Mapping) and payload.get("status") == "ok")
        except LlamaServerError:
            return False

    def props(self) -> Mapping[str, Any]:
        payload = self._request("GET", "/props")
        if not isinstance(payload, Mapping):
            raise LlamaServerError("llama-server /props invalide")
        return payload

    def chat_completion(self, payload: Mapping[str, Any], timeout_s: float | None = None) -> Mapping[str, Any]:
        response = self._request("POST", "/v1/chat/completions", payload, timeout_s=timeout_s)
        if not isinstance(response, Mapping):
            raise LlamaServerError("llama-server chat response invalide")
        return response

    def close(self) -> None:
        return None


class LlamaServerProcess:
    def __init__(self, config: LlamaServerConfig, client: LlamaServerClient | None = None) -> None:
        self.config = config
        self.client = client or LlamaServerClient(config)
        self.process: subprocess.Popen[bytes] | None = None
        self._lease = None

    def command(self) -> list[str]:
        command = [
            self.config.binary,
            "--model", self.config.model,
            "--host", self.config.host,
            "--port", str(self.config.port),
            "--ctx-size", str(self.config.ctx_size),
            "--parallel", str(self.config.parallel),
            "--n-gpu-layers", str(self.config.gpu_layers),
            "--threads", str(self.config.threads),
        ]
        if self.config.flash_attn:
            command.extend(["--flash-attn", "on"])
        command.append("--jinja")
        command.extend(self.config.extra_args)
        return command

    def _release_lease(self) -> None:
        if self._lease is None:
            return
        from backend.inference.resource_coordinator import get_resource_coordinator
        try:
            get_resource_coordinator().release(self._lease)
        except Exception:
            pass
        self._lease = None

    def start(self, timeout_s: float = 120.0) -> None:
        binary = Path(self.config.binary).expanduser().resolve()
        model = Path(self.config.model).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise LlamaServerError(f"llama-server executable invalide: {binary}")
        if not model.is_file():
            raise LlamaServerError(f"modèle llama-server introuvable: {model}")
        if self.process is not None and self.process.poll() is None and self.client.health():
            return
        if self.process is not None and self.process.poll() is None:
            self.stop()
        from backend.inference.resource_coordinator import get_resource_coordinator
        self._lease = get_resource_coordinator().acquire("model", "llama-server", timeout_s=30.0)
        try:
            self.process = subprocess.Popen(
                self.command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                process = self.process
                if process is None:
                    raise LlamaServerError("llama-server processus absent au startup")
                if process.poll() is not None:
                    stderr = b""
                    if process.stderr is not None:
                        stderr = process.stderr.read()[:2000]
                    self.stop()
                    raise LlamaServerError(
                        f"llama-server s'est arrêté au startup: "
                        f"{stderr.decode('utf-8', errors='replace')}"
                    )
                if self.client.health():
                    return
                time.sleep(0.2)
            self.stop()
            raise LlamaServerError("llama-server timeout au startup")
        except OSError as exc:
            self.stop()
            raise LlamaServerError(f"impossible de lancer llama-server: {exc}") from exc
        except Exception:
            self.stop()
            raise


    def stop(self, timeout_s: float = 10.0) -> None:
        process = self.process
        self.process = None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=timeout_s)
        finally:
            self._release_lease()


class LlamaServerRuntime(ChatRuntime):
    runtime_id = "llama_server"

    def __init__(self, process: LlamaServerProcess) -> None:
        self.process = process

    @property
    def runtime_fingerprint(self) -> str:
        command = self.process.command()
        command_id = hashlib.sha256(" ".join(command).encode("utf-8")).hexdigest()[:16]
        return f"llama_server:{_file_identity(self.process.config.binary)}:{_file_identity(self.process.config.model)}:{command_id}"

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(True, True, True, self.process.config.parallel > 1)

    @property
    def client(self) -> LlamaServerClient:
        return self.process.client

    def complete_chat(self, request: RuntimeRequest) -> RuntimeResponse:
        if not math.isfinite(request.timeout_s) or request.timeout_s <= 0:
            raise LlamaServerError("RUNTIME_TIMEOUT")
        payload: dict[str, Any] = {
            "model": self.process.config.model,
            "messages": [dict(message) for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
            payload["tool_choice"] = request.tool_choice or "auto"
            payload["parallel_tool_calls"] = self.process.config.parallel > 1
        response = self.client.chat_completion(payload, timeout_s=request.timeout_s)
        try:
            return normalize_chat_response(response)
        except RuntimeProtocolError as exc:
            raise LlamaServerError(str(exc)) from exc

    def complete_text(self, prompt: str, *, max_tokens: int, temperature: float) -> str:
        response = self.client.chat_completion({
            "model": self.process.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise LlamaServerError("llama-server réponse texte invalide")
        return str(choices[0].get("message", {}).get("content") or "").strip()


# ══════════════════════════════════════════════════════════════════════════════
# LA CONFIGURATION PAR DÉFAUT — le runtime et son modèle, résolus par l'APP
# ══════════════════════════════════════════════════════════════════════════════
#
# Le chemin du modèle suit EXACTEMENT la même règle que le binaire, et pour la
# même raison : `models/LLM/x.gguf` est une pièce de l'application, donc elle se
# cherche dans les racines de l'application. `Path.cwd()` était le point faible
# de ce module : lancé depuis un autre dossier — un raccourci, un double-clic, un
# `cron` — le modèle disparaissait alors que l'application était intacte.
NOM_MODELE = "LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf"
VARIABLE_MODELE = "THOT_LFM_MODEL"


def chemin_modele() -> Path:
    """Le modèle de discussion, résolu depuis le dossier de données.

    `THOT_LFM_MODEL` reste une SURCHARGE : posée, elle gagne, absente, elle n'est
    qu'un niveau sauté. Le défaut est cherché sous `models/` dans chaque racine de
    l'application, et son absence n'est pas signalée ici : `start()` la signalera
    au lancement, avec le chemin exact qu'il a cherché.
    """
    return resoudre_fichier_modele((NOM_MODELE,), surcharge=os.environ.get(VARIABLE_MODELE))


def default_llama_server_config(
    explicite: str | Path | None = None,
) -> LlamaServerConfig:
    """Construit la configuration du runtime, ou refuse de démarrer.

    Le runtime est résolu par `backend.config.paths.resoudre_executable()` : à côté
    de l'application, puis dans ses racines, puis — en développement seulement —
    dans le PATH. `THOT_LLAMA_SERVER` n'est plus une condition de démarrage, et
    `THOT_LLAMA_SERVER_ALLOW_UNPACKAGED` n'existe plus : elle ne faisait que
    décrire ce que fait désormais la structure. Une application portable qui
    exige une variable d'environnement pour trouver son moteur n'est pas
    portable, elle est configurée.

    Les refus de `paths` (`DependanceIntrouvable`) sont traduits en
    `LlamaServerError` pour que l'appelant qui ne connaît que ce module ait une
    seule exception à traiter. Le message est conservé tel quel : il porte les
    emplacements examinés, sans quoi le diagnostic redeviendrait « introuvable ».
    `ManifesteIncoherent`, lui, remonte tel quel : un manifeste incohérent est un
    défaut de CONSTRUCTION, pas une absence de machine, et l'aplatir dans
    `LlamaServerError` ferait croire qu'il suffit d'une autre variable.

    `explicite` est le niveau 1 de la hiérarchie (tests, outillage) : un chemin
    que l'appelant connaît et qui n'existe pas est un bug d'appelant, pas une
    absence à explorer ailleurs.
    """
    try:
        runtime = resoudre_runtime_inference(explicite)
    except DependanceIntrouvable as exc:
        raise LlamaServerError(str(exc)) from exc
    return LlamaServerConfig(binary=str(runtime.chemin), model=str(chemin_modele()))

"""
IPC — Communication JSON Lines entre Python et Rust (contrats C08/C09)
Ref : Depot_Github/documentation/contrats_transversaux.md C08 + pseudocode/backend_socle.md

Transport de contrôle non bloquant :
- stdin : lecteur dédié → file bornée → coordinateur (pas d'attente inference)
- stdout : writer unique sérialisé, seq croissant par run, flush contrôlé, logs sur stderr
- Validation : protocol_version, request_id, method, params ; idempotence downstream dans main.py
- Bornes : taille ligne, file pleine → erreur récupérable, contre-pression
"""

import os
import sys
import json
import time
import threading
import queue
from pathlib import Path

os.environ["PYTHONUNBUFFERED"] = "1"

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 256 * 1024  # 256 KB max par ligne JSON (borne C08)
MAX_QUEUE = 32  # file bornée C08
PROTOCOL_METHODS = {"chat.send", "cancel", "list_conversations", "get_conversation", "list_messages", "health", "resync"}

# Writer unique sérialisé
_writer_lock = threading.Lock()
_seq_lock = threading.Lock()
_seq_by_run: dict[str, int] = {}  # run_id -> seq courant
_global_seq = 0  # pour backend.ready sans run

def _next_seq(run_id: str | None) -> int:
    global _global_seq
    with _seq_lock:
        if run_id is None:
            _global_seq += 1
            return _global_seq
        cur = _seq_by_run.get(run_id, 0) + 1
        _seq_by_run[run_id] = cur
        return cur

def emit(event: str, payload: dict, *, run_id: str | None = None, conversation_id: str | None = None, seq: int | None = None):
    """
    Envoie un événement versionné vers Rust.
    Contrats C08 : {protocol_version, event, run_id, conversation_id, seq, payload}
    Compat : ancien frontend écoute `event` + `payload` (on garde les deux).
    Un seul writer sérialise stdout.
    """
    if seq is None and run_id is not None:
        seq = _next_seq(run_id)
    elif seq is None:
        seq = _next_seq(None)
    # Enveloppe versionnée + enveloppe legacy pour compat frontend
    envelope = {
        "event": event,
        "payload": payload,
        # C08 champs versionnés (nouveau)
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "seq": seq,
    }
    line = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n"
    with _writer_lock:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except BrokenPipeError:
            pass

def emit_legacy(event: str, payload: dict):
    """Compat : émet sans run_id/seq (pour backend_ready legacy)."""
    emit(event, payload, run_id=None, conversation_id=None)

def log(msg: str):
    """Log sur stderr (ne pollue pas stdout)."""
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except BrokenPipeError:
        pass

def _validate_request(obj: dict) -> tuple[bool, str | None, dict]:
    """
    Valide requête C08. Retourne (ok, error_code, normalized).
    Normalise : id -> request_id, method, params, protocol_version
    """
    if not isinstance(obj, dict):
        return False, "invalid_json", {}
    # Accepte legacy {id, method, params} et versionné {request_id, protocol_version}
    request_id = obj.get("request_id") or obj.get("id")
    method = obj.get("method")
    params = obj.get("params", {})
    proto = obj.get("protocol_version", 1)
    if proto != PROTOCOL_VERSION and proto != 1:
        # Version incompatible → refuser explicitement C18
        return False, "protocol_version_incompatible", {"request_id": request_id, "code": "protocol_version_incompatible", "message": f"protocol_version {proto} incompatible (attendu {PROTOCOL_VERSION})"}
    if not request_id or not isinstance(request_id, str):
        return False, "missing_request_id", {"code": "missing_request_id", "message": "request_id manquant"}
    if method not in PROTOCOL_METHODS:
        # chat.send legacy reste accepté, mais on normalise
        if method is None:
            method = "chat.send"
        elif method not in PROTOCOL_METHODS:
            return False, "unknown_method", {"request_id": request_id, "code": "unknown_method", "message": f"Unknown {method}"}
    if not isinstance(params, dict):
        return False, "invalid_params", {"request_id": request_id, "code": "invalid_params", "message": "params doit être un objet"}
    normalized = {"request_id": request_id, "method": method, "params": params, "protocol_version": proto, "raw": obj}
    return True, None, normalized

def stdin_loop(handler, *, max_queue: int = MAX_QUEUE):
    """
    Boucle non bloquante C08 :
    - Thread lecteur stdin → queue bornée
    - Thread worker unique pour les runs lourds (évite de bloquer le lecteur)
    - Main thread reste disponible pour annulation/statut (handler peut être appelé en worker)
    Simplification Phase 1 : handler est exécuté dans le thread worker (1 seul run lourd à la fois),
    le lecteur reste toujours disponible.
    Si file pleine → émet error backpressure.
    """
    q: queue.Queue = queue.Queue(maxsize=max_queue)
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                line = sys.stdin.readline()
                if not line:
                    # EOF → arrêt
                    log("[ipc] stdin EOF")
                    stop.set()
                    try:
                        q.put_nowait(None)  # sentinel
                    except queue.Full:
                        pass
                    break
                line = line.strip()
                if not line:
                    continue
                if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                    log(f"[ipc] ligne trop volumineuse ({len(line)} chars) → rejet")
                    # Essaie d'extraire request_id pour corréler l'erreur
                    try:
                        obj = json.loads(line[:2048])
                        rid = obj.get("request_id") or obj.get("id") or "unknown"
                    except Exception:
                        rid = "unknown"
                    emit("error", {"request_id": rid, "code": "message_too_large", "message": "Requête trop volumineuse", "retryable": False}, run_id=rid)
                    continue
                try:
                    req = json.loads(line)
                except Exception as e:
                    emit("error", {"code": "ipc_parse_error", "message": str(e), "retryable": False}, run_id=None)
                    log(f"[ipc] json parse error: {e} ligne={line[:300]}")
                    continue
                ok, err_code, norm = _validate_request(req)
                if not ok:
                    rid = norm.get("request_id") or req.get("id") or "unknown"
                    emit("error", {"request_id": rid, "code": err_code or "invalid_request", "message": norm.get("message", "Requête invalide"), "retryable": False}, run_id=rid)
                    continue
                # File pleine → backpressure C08
                try:
                    q.put(norm, block=False)
                except queue.Full:
                    rid = norm["request_id"]
                    emit("error", {"request_id": rid, "code": "queue_full", "message": "File d'attente pleine, réessayez", "retryable": True}, run_id=rid)
                    log(f"[ipc] queue full, rejet {rid}")
            except Exception as e:
                log(f"[ipc] reader exception: {e}")
                time.sleep(0.1)

    t_reader = threading.Thread(target=reader, name="ipc-reader", daemon=True)
    t_reader.start()

    # Worker unique pour les runs lourds (admission 1 run lourd C07)
    # Le handler décide s'il doit déléguer au worker ou répondre direct (cancel/health)
    # Simplification : tout passe par le worker, mais cancel est traité prioritairement
    def worker_loop():
        while True:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                if stop.is_set():
                    break
                continue
            if item is None:
                q.task_done()
                break
            try:
                # handler peut être synchrone bloquant (run_agent_loop) → c'est le worker qui bloque, pas le reader
                handler(item)
            except Exception as e:
                rid = item.get("request_id", "unknown") if isinstance(item, dict) else "unknown"
                emit("error", {"request_id": rid, "code": "handler_error", "message": str(e)[:500], "retryable": False}, run_id=rid)
                log(f"[ipc] handler error {rid}: {e}")
            finally:
                q.task_done()

    t_worker = threading.Thread(target=worker_loop, name="ipc-worker", daemon=True)
    t_worker.start()

    # Main thread attend l'arrêt (garde le process vivant, gère les signaux)
    try:
        while not stop.is_set():
            time.sleep(0.2)
            # Si worker meurt, relancer (sauf arrêt demandé)
            if not t_worker.is_alive() and not stop.is_set():
                log("[ipc] worker mort, relance")
                t_worker = threading.Thread(target=worker_loop, name="ipc-worker", daemon=True)
                t_worker.start()
        # EOF reçu : attendre vidage file (jusqu'à TOTAL_TIMEOUT)
        log("[ipc] attente vidage worker après EOF")
        t_worker.join(timeout=310)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        # Dernier join si pas déjà
        if t_worker.is_alive():
            t_worker.join(timeout=2)

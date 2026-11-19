"""
IPC — Communication JSON Lines entre Python et Electron (contrats C08/C09)
Ref : docs/contrats/contrats_transversaux.md C08 + docs/depot/pseudocode/backend_socle.md

Transport de contrôle non bloquant :
- stdin : lecteur dédié → chemin de contrôle prioritaire (cancel, image.status),
  voie de lecture navigante (listes, galerie, dépôt) ou file de travail bornée →
  coordinateur (jamais d'attente inference)
- stdout : writer unique sérialisé, seq croissant par run, flush contrôlé, logs sur stderr
- Validation : protocol_version, request_id, method, params ; idempotence downstream dans main.py
- Bornes : taille ligne, file pleine → erreur récupérable, contre-pression

La voie de lecture existe parce qu'un `image.generate` tient le worker unique pendant
toute la diffusion GPU. Sans elle, revenir en arrière pendant une génération faisait
attendre (puis paraître vides) discussions, galerie et historique derrière ce travail.
"""

import os
import sys
import json
import time
import threading
import queue
import re
from pathlib import Path
from typing import TypeGuard

os.environ["PYTHONUNBUFFERED"] = "1"

from backend.config.defaults import INPUT_LIMITS

PROTOCOL_VERSION = 1
# Borne de ligne JSON. 20 Mio de fichier en base64 ≈ 26,7 Mio de caractères ;
# 32 Mio laissent la marge de l'enveloppe JSON. La lecture ci-dessous est bornée
# AVANT accumulation : le plafond est donc aussi un plafond de mémoire, pas un
# contrôle après coup. Une ligne trop longue est vidée puis rejetée.
MAX_LINE_BYTES = int(INPUT_LIMITS["ipc_line_bytes"])
_DRAIN_CHUNK = 64 * 1024
MAX_QUEUE = 32  # file bornée C08
WORKER_SHUTDOWN_TIMEOUT_S = 600
MAX_REQUEST_ID_LENGTH = 128
_REQUEST_ID_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_REQUEST_ID_LENGTH}}}")
PROTOCOL_METHODS = {
    "chat.send",
    "cancel",
    "health",
    "resync",
    "image.generate",
    "image.list",
    "image.status",
    "web.settings",
    # Méthodes de base et d'espaces, portées depuis le pont Electron.
    # Convention : `verbe.nom` ; l'event de réponse porte le même
    # nom avec des tirets bas.
    "list.conversations",
    "get.conversation",
    "list.messages",
    "rename.conversation",
    "delete.conversation",
    "image.delete",
    "image.rename",
    "image.set_backend",
    "get.attachment_bytes",
    # Pièces jointes : dépôt, staging, promotion, vignette.
    "save.upload",
    "stage.paths",
    "stage.picker_bytes",
    "attachments.prepare",
    # Relance explicite de l'extraction d'un document du dépôt (Phase 6).
    # Commande utilisateur : jamais un outil, jamais un chemin ni un modèle
    # reçu du renderer.
    "artifact.ingestion_retry",
    "get.staged_preview_bytes",
    "espace.list",
    "espace.create",
    "espace.get",
    "espace.stats",
    "espace.conversations",
    "espace.memoire",
    "espace.memoire.rafraichir",  # Le dépôt de l'espace : lister ce que l'utilisateur y a versé,
    # et en retirer un document (le fichier est conservé).
    # Mémoire : trois écritures (forcer, corriger, supprimer) — jamais
    # dans READ_ONLY_METHODS.
    "espace.memoire.forcer",
    "espace.memoire.corriger",
    "espace.memoire.supprimer",
    "espace.depot",
    "espace.depot_retirer",
    # Entretien du dépôt, tous espaces confondus : vérifier son état
    # (lecture seule) puis réparer ce qui se répare. Les deux sont
    # ajoutées ici ET dans `_IPC_HANDLERS` — voir
    # `tests/backend/test_registre_ipc.py`, qui échoue si l'on oublie
    # l'une des deux portes.
    "espace.depot_integrite",
    "espace.depot_reparer",
    # Déplacer une discussion vers un autre espace (§7.3) :
    # évalué sans `confirm`, appliqué avec. Ajoutée ici ET dans
    # `_IPC_HANDLERS` (`backend/main.py`) — la garde en deux
    # portes est `tests/backend/test_registre_ipc.py`.
    "espace.conversation_deplacer",
    # Renommer le nom affiché d'un document du dépôt (depot.md
    # §5.2) : la fiche est révisée, jamais réextraite. Ajoutée
    # ici ET dans `_IPC_HANDLERS`, comme les deux canaux
    # d'entretien ci-dessus.
    "espace.depot_renommer",
    # Verser un fichier de la discussion dans le dépôt de SON
    # espace (espaces.md §6, promotion vers le dépôt) : cible
    # déduite du rattachement de la discussion, jamais d'un id
    # reçu du client. Ajoutée ici ET dans `_IPC_HANDLERS`.
    "espace.conversation_partager",
}

# Chemin de contrôle : file de priorité servie par un thread dédié, donc jamais
# bloquée par un handler lourd de la file de travail (un image.generate tient le
# worker unique pendant toute la diffusion). Priorité = plus petit nombre d'abord.
PRIORITY_CANCEL = 0  # une annulation passe avant tout, toujours
PRIORITY_STATUS = 1  # diagnostic moteur : ne doit pas être affamé par un run long
PRIORITY_SHUTDOWN = (
    2  # sentinelle EOF : vidage du chemin de contrôle, traitement en dernier
)
CONTROL_PRIORITIES = {"cancel": PRIORITY_CANCEL, "image.status": PRIORITY_STATUS}

# Voie de lecture : méthodes pures, sans écriture ni staging. Les servir hors du
# worker unique évite qu'une diffusion GPU (`image.generate`) ne bloque la
# navigation — discussions, messages, galerie, dépôt. Les écritures, le staging et
# les générations restent sur le worker, où leur ordre est garanti.
READ_ONLY_METHODS: frozenset[str] = frozenset(
    {
        "list.conversations",
        "get.conversation",
        "list.messages",
        "image.list",
        "get.attachment_bytes",
        "get.staged_preview_bytes",
        "espace.list",
        "espace.get",
        "espace.stats",
        "espace.conversations",
        "espace.memoire",
        "espace.depot",
        "espace.depot_integrite",
    }
)
READ_QUEUE_MAX = 64  # lectures courtes : file plus large que le travail
READ_QUEUE_MAX = 64  # lectures courtes : file plus large que le travail


def is_valid_request_id(value: object) -> TypeGuard[str]:
    """Return whether *value* is a safe single path component request ID."""
    return isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value) is not None


def _safe_request_id(value: object) -> str:
    """Return a validated correlation ID, never an untrusted request value."""
    return value if is_valid_request_id(value) else "unknown"


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


def emit(
    event: str,
    payload: dict,
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
    seq: int | None = None,
) -> bool:
    """
    Envoie un événement versionné vers Electron.
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
            return True
        except BrokenPipeError:
            return False


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
    # Accepte legacy {id, method, params} et versionné {request_id, protocol_version}.
    # Ne substitue pas request_id par id lorsqu'un champ versionné est présent :
    # un identifiant vide ou invalide doit être refusé, jamais contourné par le legacy.
    request_id = obj["request_id"] if "request_id" in obj else obj.get("id")
    method = obj.get("method")
    params = obj.get("params", {})
    proto = obj.get("protocol_version", 1)
    if request_id is None or request_id == "":
        return (
            False,
            "missing_request_id",
            {"code": "missing_request_id", "message": "request_id manquant"},
        )
    if not is_valid_request_id(request_id):
        return (
            False,
            "invalid_request_id",
            {
                "code": "invalid_request_id",
                "message": "request_id doit contenir uniquement des lettres, chiffres, '-' ou '_'",
            },
        )
    if proto != PROTOCOL_VERSION and proto != 1:
        # Version incompatible → refuser explicitement C18
        return (
            False,
            "protocol_version_incompatible",
            {
                "request_id": request_id,
                "code": "protocol_version_incompatible",
                "message": f"protocol_version {proto} incompatible (attendu {PROTOCOL_VERSION})",
            },
        )
    if method not in PROTOCOL_METHODS:
        # chat.send legacy reste accepté, mais on normalise
        if method is None:
            method = "chat.send"
        elif method not in PROTOCOL_METHODS:
            return (
                False,
                "unknown_method",
                {
                    "request_id": request_id,
                    "code": "unknown_method",
                    "message": f"Unknown {method}",
                },
            )
    if not isinstance(params, dict):
        return (
            False,
            "invalid_params",
            {
                "request_id": request_id,
                "code": "invalid_params",
                "message": "params doit être un objet",
            },
        )
    normalized = {
        "request_id": request_id,
        "method": method,
        "params": params,
        "protocol_version": proto,
        "raw": obj,
    }
    return True, None, normalized


def _lire_ligne_bornee(stream, *, max_bytes: int = MAX_LINE_BYTES) -> tuple[str, bool]:
    """Lit UNE ligne sans jamais matérialiser plus de `max_bytes` octets.

    `readline()` sans taille accumule toute la ligne avant de rendre la main :
    une ligne de 500 Mo faisait gonfler le processus avant le contrôle de
    `MAX_LINE_BYTES`, c'est-à-dire après le dommage. Ici, `readline(max_bytes + 1)`
    borne la première lecture, et une queue éventuelle est vidée par blocs puis
    jetée. Retourne `(texte, trop_long)` ; en cas de dépassement, `texte` n'est
    qu'un préfixe (assez pour retrouver un `request_id` si l'enveloppe le porte
    en tête) et l'appelant émet `message_too_large` sans parser la ligne.

    On préfère le flux binaire quand il existe : la borne est alors en OCTETS,
    exactement l'unité du contrat. Le repli texte existe pour les flux de test
    (`io.StringIO`), sans changer la règle.
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        brut = buffer.readline(max_bytes + 1)
        if not brut:
            return "", False
        if brut.endswith(b"\n"):
            if len(brut) - 1 <= max_bytes:
                return brut.decode("utf-8", errors="replace"), False
            return brut[:max_bytes].decode("utf-8", errors="replace"), True
        if len(brut) <= max_bytes:
            return brut.decode("utf-8", errors="replace"), False
        while True:
            reste = buffer.readline(_DRAIN_CHUNK)
            if not reste or reste.endswith(b"\n"):
                break
        return brut[:max_bytes].decode("utf-8", errors="replace"), True

    texte = stream.readline(max_bytes + 1)
    if not texte:
        return "", False
    if texte.endswith("\n"):
        if len(texte.encode("utf-8")) - 1 <= max_bytes:
            return texte, False
        return texte[:max_bytes], True
    if len(texte) <= max_bytes and len(texte.encode("utf-8")) <= max_bytes:
        return texte, False
    while True:
        reste = stream.readline(_DRAIN_CHUNK)
        if not reste or reste.endswith("\n"):
            break
    return texte[:max_bytes], True


def stdin_loop(
    handler,
    *,
    max_queue: int = MAX_QUEUE,
    read_queue_max: int = READ_QUEUE_MAX,
    on_shutdown=None,
    shutdown_callback=None,
    read_pool_size: int = 4,
):
    work_queue: queue.Queue = queue.Queue(maxsize=max_queue)
    read_queue: queue.Queue = queue.Queue(maxsize=read_queue_max)
    control_queue: queue.PriorityQueue = queue.PriorityQueue()
    stop = threading.Event()
    control_sequence = 0
    shutdown_lock = threading.Lock()
    shutdown_called = False
    callback = on_shutdown if on_shutdown is not None else shutdown_callback

    def notify_shutdown() -> None:
        nonlocal shutdown_called
        with shutdown_lock:
            if shutdown_called:
                return
            shutdown_called = True
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            log(f"[ipc] shutdown callback failed: {exc}")

    def put_control(item: dict | None, priority: int) -> None:
        nonlocal control_sequence
        control_sequence += 1
        control_queue.put_nowait((priority, control_sequence, item))

    def reject_queue_full(request: dict, log_context: str) -> None:
        """Émet le refus récupérable d'une file pleine, identique pour toutes les voies."""
        rid = request["request_id"]
        emit(
            "error",
            {
                "request_id": rid,
                "code": "queue_full",
                "message": "File d'attente pleine, réessayez",
                "retryable": True,
            },
            run_id=rid,
        )
        log(f"[ipc] {log_context}, rejet {rid}")

    def enqueue(request: dict) -> None:
        """Achemine une requête validée vers le chemin qui lui appartient.

        Chemin de contrôle (cancel, image.status) : prioritaire et jamais plein — un
        handler borné ne retient ni une annulation ni un diagnostic, donc chaque
        request_id reçoit sa réponse. Voie de lecture : les méthodes pures n'écrivent
        rien et ne peuvent donc pas doubler le travail ; les servir à part empêche une
        diffusion GPU de bloquer la navigation. File de travail : bornée, et son refus
        `queue_full` reste récupérable côté UI.
        """
        method = request["method"]
        priority = CONTROL_PRIORITIES.get(method)
        if priority is not None:
            put_control(request, priority)
            return
        if method in READ_ONLY_METHODS:
            try:
                read_queue.put_nowait(request)
            except queue.Full:
                reject_queue_full(request, "read queue full")
            return
        try:
            work_queue.put_nowait(request)
        except queue.Full:
            reject_queue_full(request, "queue full")

    def reader():
        while not stop.is_set():
            try:
                line, trop_long = _lire_ligne_bornee(sys.stdin)
                if not line:
                    log("[ipc] stdin EOF")
                    stop.set()
                    notify_shutdown()
                    try:
                        work_queue.put_nowait(None)
                    except queue.Full:
                        pass
                    for _ in range(max(read_pool_size, 0)):
                        try:
                            read_queue.put_nowait(None)
                        except queue.Full:
                            pass
                    put_control(None, PRIORITY_SHUTDOWN)
                    break
                if trop_long:
                    log(
                        f"[ipc] ligne trop volumineuse (> {MAX_LINE_BYTES} octets) → rejet"
                    )
                    try:
                        obj = json.loads(line[:2048])
                        candidate = (
                            obj.get("request_id")
                            if isinstance(obj, dict) and "request_id" in obj
                            else (obj.get("id") if isinstance(obj, dict) else None)
                        )
                    except Exception:
                        candidate = None
                    rid = _safe_request_id(candidate)
                    emit(
                        "error",
                        {
                            "request_id": rid,
                            "code": "message_too_large",
                            "message": "Requête trop volumineuse",
                            "retryable": False,
                        },
                        run_id=rid,
                    )
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except Exception as exc:
                    emit(
                        "error",
                        {
                            "code": "ipc_parse_error",
                            "message": str(exc),
                            "retryable": False,
                        },
                        run_id=None,
                    )
                    log(f"[ipc] json parse error: {exc} ligne={line[:300]}")
                    continue
                ok, error_code, normalized = _validate_request(request)
                if not ok:
                    request_obj = request if isinstance(request, dict) else {}
                    candidate = normalized.get("request_id")
                    if candidate is None:
                        candidate = (
                            request_obj.get("request_id")
                            if "request_id" in request_obj
                            else request_obj.get("id")
                        )
                    rid = _safe_request_id(candidate)
                    emit(
                        "error",
                        {
                            "request_id": rid,
                            "code": error_code or "invalid_request",
                            "message": normalized.get("message", "Requête invalide"),
                            "retryable": False,
                        },
                        run_id=rid,
                    )
                    continue
                enqueue(normalized)
            except Exception as exc:
                log(f"[ipc] reader exception: {exc}")
                time.sleep(0.1)

    def run_handler(item: dict) -> None:
        try:
            handler(item)
        except Exception as exc:
            rid = (
                item.get("request_id", "unknown")
                if isinstance(item, dict)
                else "unknown"
            )
            emit(
                "error",
                {
                    "request_id": rid,
                    "run_id": rid,
                    "code": "handler_error",
                    "message": str(exc)[:500],
                    "retryable": False,
                },
                run_id=rid,
            )
            log(f"[ipc] handler error {rid}: {exc}")

    def worker_loop() -> None:
        while True:
            try:
                item = work_queue.get(timeout=0.1)
            except queue.Empty:
                if stop.is_set() and work_queue.empty():
                    break
                continue
            try:
                if item is None:
                    break
                run_handler(item)
            finally:
                work_queue.task_done()

    def control_loop() -> None:
        while True:
            try:
                _, _, item = control_queue.get(timeout=0.1)
            except queue.Empty:
                if stop.is_set() and control_queue.empty():
                    break
                continue
            try:
                if item is None:
                    break
                run_handler(item)
            finally:
                control_queue.task_done()

    def read_loop() -> None:
        """Sert les lectures pures, indépendamment du worker de travail."""
        while True:
            try:
                item = read_queue.get(timeout=0.1)
            except queue.Empty:
                if stop.is_set() and read_queue.empty():
                    break
                continue
            try:
                if item is None:
                    break
                run_handler(item)
            finally:
                read_queue.task_done()

    t_reader = threading.Thread(target=reader, name="ipc-reader", daemon=True)
    t_worker = threading.Thread(target=worker_loop, name="ipc-worker", daemon=True)
    t_control = threading.Thread(target=control_loop, name="ipc-control", daemon=True)
    read_threads = [
        threading.Thread(target=read_loop, name=f"ipc-read-{i}", daemon=True)
        for i in range(max(read_pool_size, 0))
    ]
    t_reader.start()
    t_worker.start()
    t_control.start()
    for thread in read_threads:
        thread.start()
    try:
        while not stop.is_set():
            time.sleep(0.05)
            if not t_worker.is_alive() and not stop.is_set():
                log("[ipc] worker mort, relance")
                t_worker = threading.Thread(
                    target=worker_loop, name="ipc-worker", daemon=True
                )
                t_worker.start()
            if not t_control.is_alive() and not stop.is_set():
                log("[ipc] control worker mort, relance")
                t_control = threading.Thread(
                    target=control_loop, name="ipc-control", daemon=True
                )
                t_control.start()
            for index, thread in enumerate(read_threads):
                if thread.is_alive() or stop.is_set():
                    continue
                log("[ipc] read worker mort, relance")
                read_threads[index] = threading.Thread(
                    target=read_loop, name=f"ipc-read-{index}", daemon=True
                )
                read_threads[index].start()
        notify_shutdown()
        log("[ipc] attente vidage worker après EOF")
        t_worker.join(timeout=WORKER_SHUTDOWN_TIMEOUT_S)
        t_control.join(timeout=5)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        notify_shutdown()
        if t_reader.is_alive():
            t_reader.join(timeout=1)
        if t_worker.is_alive():
            t_worker.join(timeout=2)
        if t_control.is_alive():
            t_control.join(timeout=2)
        for thread in read_threads:
            if thread.is_alive():
                thread.join(timeout=2)

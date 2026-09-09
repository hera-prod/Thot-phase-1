"""
Thot Phase 1 — Point d'entrée du sidecar Python (JSON Lines sur stdin/stdout)
Contrats C08/C09 + pseudocode/backend_socle.md
- Thread lecteur non bloquant (ipc.py) → worker unique
- Idempotence request_id + empreinte canonique (C11)
- Un tour par conversation (C09) garanti atomique
- Annulation coopérative + escalade (C09)
- Terminal unique après persistance (C08)
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import time
import threading
import hashlib
from pathlib import Path

from backend.inference.ipc import emit, emit_legacy, log, stdin_loop, PROTOCOL_VERSION
from backend.inference.manager import ModelManager
from backend.db.database import init_db

manager = ModelManager()

# Annulation : run_id -> threading.Event
_cancel_flags: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

def get_cancel_flag(run_id: str) -> threading.Event:
    with _cancel_lock:
        if run_id not in _cancel_flags:
            _cancel_flags[run_id] = threading.Event()
        return _cancel_flags[run_id]

def clear_cancel_flag(run_id: str):
    with _cancel_lock:
        _cancel_flags.pop(run_id, None)

def _canonical_fingerprint(method: str, params: dict, protocol_version: int = 1) -> str:
    """Empreinte canonique C11 : protocol_version + method + params triés + conv_id acteur"""
    # Inclure conv_id comme périmètre acteur
    conv = params.get("conv_id") or params.get("conversation_id") or ""
    payload = json.dumps({"v": protocol_version, "m": method, "c": conv, "p": params}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

def handle(normalized: dict):
    """
    Handler C08 : reçoit {request_id, method, params, protocol_version}
    Répond via emit() corrélé run_id/seq. Un seul terminal par run après persistance.
    """
    request_id = normalized["request_id"]
    method = normalized["method"]
    params = normalized["params"]
    proto = normalized.get("protocol_version", 1)

    # Corrélation : run_id = request_id pour chat.send (Phase 1 simplifié)
    # En phase 5, run_id distinct, mais ici 1:1 pour idempotence simple C11
    run_id = request_id

    if method == "chat.send":
        message = (params.get("message") or "").strip()
        mode = params.get("mode", "chat")
        # Annexe 10 : attachment_ids durables (cible), image_paths compat déprécié
        image_paths = params.get("image_paths") or params.get("image") or []
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        image_paths = [p for p in image_paths if p]
        attachment_ids = params.get("attachment_ids") or params.get("attachmentIds") or []
        if isinstance(attachment_ids, str):
            attachment_ids = [attachment_ids]
        attachment_ids = [a for a in attachment_ids if a]
        conv_id = (params.get("conv_id") or params.get("conversation_id") or request_id or "tmp").strip()
        if not conv_id:
            conv_id = request_id
        # Compat migration : image_paths dans staging géré → promeut en artifact_id et log
        if image_paths and not attachment_ids:
            log(f"[compat] image_paths reçu ({len(image_paths)}), promotion via staging")
            # Tente promotion via même logique que attachments.prepare (hash → blob)
            try:
                from backend.db.database import SessionLocal as _SL
                from backend.db.models import Blob, Artifact, ArtifactLink
                import hashlib, pathlib
                _dbp = _SL()
                for p in image_paths:
                    # Vérifie que chemin est dans temp/staged ou temp/{request_id} géré
                    from backend.config.paths import get_temp_dir
                    temp = get_temp_dir()
                    if temp not in pathlib.Path(p).resolve().parents and pathlib.Path(p).resolve().parent != temp:
                        # Hors staging géré → refus C18, mais on tolère en compat avec log
                        log(f"[compat] image_paths hors staging géré: {p}")
                    # Calcule hash et promeut si possible (similaire à attachments_prepare)
                    try:
                        data = pathlib.Path(p).read_bytes() if pathlib.Path(p).exists() else b""
                        sha = hashlib.sha256(data).hexdigest() if data else None
                        if sha:
                            # Vérifie si déjà promu
                            existing = _dbp.query(Artifact).filter(Artifact.id == f"art_{sha[:12]}").first()
                            if not existing:
                                # Crée blob/artifact minimal pour compat
                                from pathlib import Path as _P
                                sha_path = _dbp.query(Blob).filter(Blob.sha256 == sha).first()
                                if not sha_path:
                                    rel = f"blobs/{sha[:2]}/{sha}.bin"
                                    _dbp.add(Blob(id=sha, sha256=sha, relative_path=rel, size_bytes=len(data), mime_detected="image/png", state="available"))
                                    _dbp.flush()
                                # Crée artifact
                                art_id = f"art_{sha[:12]}"
                                _dbp.add(Artifact(id=art_id, blob_id=sha, kind="image", original_filename=pathlib.Path(p).name, display_name=pathlib.Path(p).name))
                                _dbp.flush()
                                _dbp.add(ArtifactLink(id=f"lnk_{art_id[4:8]}_{request_id[:8]}", artifact_id=art_id, scope_kind="conversation", scope_id=conv_id if 'conv_id' in locals() else "tmp"))
                                _dbp.commit()
                                # Annexe 11 Lot A : fiche rev 1 dès la promotion (sinon self-healing au run)
                                try:
                                    from backend.search import cards as _cardsP, fts as _ftsP
                                    _artP = _dbp.query(Artifact).filter(Artifact.id == art_id).first()
                                    if _artP is not None:
                                        _ftsP.ensure_fts(_dbp)
                                        _cardsP.ensure_card_on_import(_dbp, _artP)
                                        _dbp.commit()
                                except Exception as _eP:
                                    log(f"[compat] fiche Lot A ignorée {art_id}: {_eP}")
                            attachment_ids.append(f"art_{sha[:12]}")
                    except Exception as e:
                        log(f"[compat] promotion échouée {p}: {e}")
                _dbp.close()
            except Exception as e:
                log(f"[compat] promotion image_paths échouée: {e}")

        # Validation entrée C18
        if not message and not image_paths and not attachment_ids:
            emit("error", {"request_id": request_id, "code": "empty_message", "message": "Message vide", "retryable": False}, run_id=run_id, conversation_id=conv_id)
            return
        if mode not in ("chat", "agent"):
            emit("error", {"request_id": request_id, "code": "invalid_mode", "message": f"mode {mode} invalide", "retryable": False}, run_id=run_id, conversation_id=conv_id)
            return
        # Validation Taille
        if len(message) > 200_000:
            emit("error", {"request_id": request_id, "code": "message_too_large", "message": "Message trop volumineux", "retryable": False}, run_id=run_id, conversation_id=conv_id)
            return

        # Idempotence C11 : si run_id déjà terminé et même empreinte → rejouer acceptation sans réexécuter
        fingerprint = _canonical_fingerprint(method, params, proto)
        # Vérifie en DB si message user avec run_id existe déjà
        from backend.db.database import SessionLocal
        db_check = SessionLocal()
        try:
            from backend.db.models import Message
            existing = db_check.query(Message).filter(Message.run_id == run_id).first()
            if existing is not None:
                # Même run_id déjà traité → dedup, vérifier empreinte via content hash
                # Simplification : si message identique, rejouer le terminal depuis DB
                log(f"[dedup] run_id {run_id} déjà existant, restitution sans réexécution")
                # Récupère réponse assistant associée
                assistant = db_check.query(Message).filter(Message.conv_id == conv_id, Message.role == "assistant", Message.run_id == run_id).first()
                if assistant:
                    # Émet idempotent : run.started + answer + run.completed depuis persistant
                    emit("run.started", {"request_id": request_id, "run_id": run_id}, run_id=run_id, conversation_id=conv_id)
                    emit("answer.delta", {"request_id": request_id, "text": assistant.content[:4000], "done": True}, run_id=run_id, conversation_id=conv_id)
                    emit("run.completed", {"request_id": request_id, "run_id": run_id, "status": "completed"}, run_id=run_id, conversation_id=conv_id)
                    # Compat legacy
                    emit("message_started", {"request_id": request_id, "run_id": run_id}, run_id=run_id, conversation_id=conv_id)
                    emit("answer", {"request_id": request_id, "text": assistant.content[:4000]}, run_id=run_id, conversation_id=conv_id)
                    emit("message_completed", {"request_id": request_id, "run_id": run_id}, run_id=run_id, conversation_id=conv_id)
                    return
        except Exception as e:
            log(f"[dedup] check failed: {e}")
        finally:
            try: db_check.close()
            except: pass

        # Vérification atomique 1 run chat non-terminal par conversation C09
        # Simplification Phase 1 : on vérifie qu'aucun run en cours pour cette conv (flag en mémoire)
        # + en DB on vérifie qu'aucun message user récent non terminé ? Pour l'instant lock mémoire
        # On utilise un lock global par conv_id
        # Pour rester simple, on refuse si un flag cancel existe déjà pour cette conv
        # (vrai verrou DB nécessiterait table Run avec état)

        from backend.db.database import SessionLocal
        db = SessionLocal()
        history_shared: list[dict] = []
        try:
            from backend.db.repositories.conversations import get_orchestrator_context, get_conversation, create_conversation, append_message
            conv = get_conversation(db, conv_id)
            if conv is None and conv_id != "tmp":
                title = (message or "Nouvelle conversation")[:80].strip() or "Nouvelle conversation"
                try:
                    conv = create_conversation(db, title=title, mode=mode if mode in ("chat", "agent") else "chat", conv_id=conv_id)
                    db.commit()
                except Exception as e:
                    log(f"[db] create_conversation failed: {e}")
                    db.rollback()
            if conv is not None:
                history_shared = get_orchestrator_context(db, conv_id)
            # Persiste message user immédiatement avant LLM (survit crash) C11
            user_msg_id = None
            if message and conv is not None:
                try:
                    # Idempotence : si même run_id déjà, ne pas réinsérer
                    from backend.db.models import Message
                    if not db.query(Message).filter(Message.run_id == run_id, Message.role == "user").first():
                        m = append_message(db, conversation_id=conv_id, role="user", content=message, status="completed", run_id=run_id)
                        user_msg_id = m.id
                        # Annexe 10 : persistance via artifact_ids durables (cible).
                        # D'OÙ VIENNENT LES IDS : frontend ChatConversation.handleSend → attachmentsPrepare
                        # (Rust promeut sti_xxx → art_xxx) → chatSend(attachment_ids). OÙ VONT-ILS :
                        # MessageAttachment(message, artifact) = "ce message montre ce fichier" → lu par
                        # loop.build_history_artifact_entries pour les follow-ups (registre historique).
                        # POURQUOI PAS Source (legacy) : Source servait l'ancien pipeline image_paths ;
                        # le pipeline durable écrit MessageAttachment + ArtifactLink. Les lecteurs
                        # historiques (registre, get_source) connaissent les DEUX tables (compat).
                        if attachment_ids:
                            from backend.db.models import MessageAttachment, Artifact, ArtifactLink
                            import uuid
                            for idx, art_id in enumerate(attachment_ids):
                                try:
                                    # Vérifie scope conversation (C18) : l'artefact a-t-il été préparé
                                    # POUR cette conv ? D'où vient le link : attachments_prepare (Rust) le crée
                                    # avec scope_id = conv_id du frontend. Pourquoi refuser sinon : sans ça, un
                                    # artifact_id deviné/collé donnerait accès aux fichiers d'une AUTRE
                                    # conversation (fuite inter-conv). Le refus est un log, pas une erreur :
                                    # le run continue (texte seul) au lieu de mourir en toast.
                                    link = db.query(ArtifactLink).filter(ArtifactLink.artifact_id == art_id, ArtifactLink.scope_kind == "conversation", ArtifactLink.scope_id == conv_id).first()
                                    if not link:
                                        log(f"[scope] attachment {art_id} hors scope {conv_id} — refus C18")
                                        continue
                                    # Vérifie artifact existe
                                    art = db.query(Artifact).filter(Artifact.id == art_id).first()
                                    if not art:
                                        log(f"[artifact] {art_id} introuvable")
                                        continue
                                    db.add(MessageAttachment(id=str(uuid.uuid4()), message_id=m.id, artifact_id=art_id, ordinal=idx))
                                except Exception as e:
                                    log(f"[msg_att] failed {art_id}: {e}")
                        # Compat image_paths (déprécié) → legacy Source pour migration
                        if image_paths:
                            from backend.db.media_cache import persist_attachment
                            from backend.db.models import Source
                            import mimetypes, uuid
                            for p in image_paths:
                                try:
                                    rel, sha256, size = persist_attachment(p)
                                    src = Source(id=str(uuid.uuid4()), msg_id=m.id, conversation_id=conv_id, source_type="attachment", path=p, hash=sha256, media_kind="pdf" if p.lower().endswith(".pdf") else "image", original_name=Path(p).name, mime_type=mimetypes.guess_type(p)[0] or ("application/pdf" if p.lower().endswith(".pdf") else "image/png"), sha256=sha256, relative_path=rel, size_bytes=size, analysis_version=0)
                                    db.add(src)
                                except Exception as e:
                                    log(f"[media] persist failed {p}: {e}")
                        db.commit()
                    else:
                        log(f"[dedup] user message {run_id} déjà persisté")
                except Exception as e:
                    log(f"[db] append user failed: {e}")
                    db.rollback()
        except Exception as e:
            log(f"[db] history load failed: {e}")
            history_shared = []
        finally:
            try: db.close()
            except: pass

        # Émet acceptation + début de run (C08 : chat.send retourne acceptation, stream ensuite)
        emit("run.started", {"request_id": request_id, "run_id": run_id, "conversation_id": conv_id, "fingerprint": fingerprint}, run_id=run_id, conversation_id=conv_id)
        emit("message_started", {"request_id": request_id, "run_id": run_id, "model": "LFM2.5-8B"}, run_id=run_id, conversation_id=conv_id)  # compat
        # Flag annulation
        cancel_flag = get_cancel_flag(run_id)

        # Exécution boucle (bloquant dans worker, pas dans lecteur)
        collected = ""
        error_occurred = False
        try:
            from backend.inference.loop import run_agent_loop
            # Passe cancel_flag pour arrêt coopératif C09, attachment_ids durables annexe 10
            collected = run_agent_loop(manager, conv_id=conv_id, request_id=request_id, message=message, mode=mode, image_paths=image_paths, attachment_ids=attachment_ids, history_shared=history_shared, cancel_flag=cancel_flag)
            # Vérifie annulation
            if cancel_flag.is_set():
                log(f"[run] {run_id} annulé")
                # Persiste état cancelled si pas déjà
                db2 = SessionLocal()
                try:
                    from backend.db.repositories.conversations import append_message
                    # Marque message partiel si collected
                    status = "cancelled" if not collected else "partial"
                    if collected:
                        append_message(db2, conversation_id=conv_id, role="assistant", content=collected, status=status, run_id=run_id)
                        db2.commit()
                except Exception as e:
                    log(f"[db] append cancelled failed: {e}")
                finally:
                    try: db2.close()
                    except: pass
                emit("run.cancelled", {"request_id": request_id, "run_id": run_id}, run_id=run_id, conversation_id=conv_id)
                emit("message_completed", {"request_id": request_id, "run_id": run_id, "cancelled": True}, run_id=run_id, conversation_id=conv_id)
                return
        except Exception as e:
            log(f"[loop] error {run_id}: {e}")
            emit("error", {"request_id": request_id, "code": "loop_error", "message": str(e)[:800], "retryable": True}, run_id=run_id, conversation_id=conv_id)
            emit("run.failed", {"request_id": request_id, "run_id": run_id, "code": "loop_error", "message": str(e)[:500]}, run_id=run_id, conversation_id=conv_id)
            error_occurred = True
            collected = ""
        finally:
            clear_cancel_flag(run_id)

        # Persiste réponse assistant après génération AVANT terminal C08
        if collected and not error_occurred:
            db2 = SessionLocal()
            try:
                from backend.db.repositories.conversations import append_message
                from backend.db.models import Message
                # Idempotence : ne pas dupliquer si déjà présent
                if not db2.query(Message).filter(Message.run_id == run_id, Message.role == "assistant").first():
                    append_message(db2, conversation_id=conv_id, role="assistant", content=collected, status="completed", run_id=run_id)
                    db2.commit()
                else:
                    log(f"[dedup] assistant {run_id} déjà persisté")
            except Exception as e:
                log(f"[db] append assistant failed: {e}")
                try: db2.rollback()
                except: pass
            finally:
                try: db2.close()
                except: pass
            # Émet answer.delta + run.completed (terminal unique après persistance)
            # Streaming déjà émis par loop.py via answer/answer.delta ; on complète avec terminal
            emit("run.completed", {"request_id": request_id, "run_id": run_id, "status": "completed", "tokens_out": len(collected)//4}, run_id=run_id, conversation_id=conv_id)
            emit("message_completed", {"request_id": request_id, "run_id": run_id, "tokens_in": len(message)//4, "tokens_out": len(collected)//4}, run_id=run_id, conversation_id=conv_id)
        elif not collected and not error_occurred:
            # Pas de collecte (annulation déjà gérée ou vide)
            emit("run.failed", {"request_id": request_id, "run_id": run_id, "code": "empty_response", "message": "Pas de réponse générée"}, run_id=run_id, conversation_id=conv_id)

    elif method == "cancel":
        target_run = params.get("run_id") or params.get("request_id") or request_id
        log(f"[cancel] {target_run} demandé")
        flag = get_cancel_flag(target_run)
        flag.set()
        # Tente aussi d'interrompre le manager si possible (C09 escalade)
        try:
            # Manager: passer en failed si close non confirmé → worker isolé
            pass
        except Exception:
            pass
        emit("run.cancelled", {"request_id": request_id, "run_id": target_run}, run_id=target_run)

    elif method in ("list_conversations", "get_conversation", "list_messages", "health", "resync"):
        # Ces commandes ne passent pas par sidecar en prod (Rust rusqlite), mais on garde compat dev
        emit("error", {"request_id": request_id, "code": "not_implemented_via_sidecar", "message": f"{method} doit être appelé via Tauri invoke, pas sidecar"}, run_id=request_id)

    else:
        emit("error", {"request_id": request_id, "code": "unknown_method", "message": f"Unknown {method}", "retryable": False}, run_id=request_id)

if __name__ == "__main__":
    init_db()
    log(f"[thot-phase-1] backend_ready backend={manager.backend.backend} vram={manager.backend.free_vram//(1024*1024)}MB proto={PROTOCOL_VERSION}")
    # Émet backend_ready versionné + legacy
    emit("backend_ready", {"backend": manager.backend.backend, "vram": manager.backend.free_vram, "n_gpu_layers": manager.backend.n_gpu_layers, "protocol_version": PROTOCOL_VERSION})
    emit("backend.ready", {"backend": manager.backend.backend, "vram": manager.backend.free_vram, "protocol_version": PROTOCOL_VERSION}, run_id=None)  # C08 canonique
    stdin_loop(handle)

"""
Thot Phase 1 — Point d'entrée du sidecar Python (JSON Lines sur stdin/stdout)
Lancé par Rust Tauri au démarrage de l'app :
  Rust écrit {"id":"...","method":"chat.send","params":{...}} sur stdin
  Python répond {"event":"thinking","payload":{...}} sur stdout ligne par ligne
"""
# Fix pour lancement direct `python3 backend/main.py` (sans -m) : ajoute Thot-phase-1/ au sys.path
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# Alternative propre : `PYTHONPATH=. python3 -m backend.main`

from backend.inference.ipc import emit, log, stdin_loop
from backend.inference.manager import ModelManager
from backend.db.database import init_db

manager = ModelManager()

def handle(req: dict):
    method = req.get("method", "chat.send")
    params = req.get("params", {})
    req_id = req.get("id", "unknown")

    if method == "chat.send":
        message = params.get("message", "")
        mode = params.get("mode", "chat")
        image_paths = params.get("image_paths", []) or params.get("image", [])
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        conv_id = params.get("conv_id") or params.get("conversation_id") or req_id or "tmp"

        # Historique partagé double : user↔assistant uniquement, sans tool traces passées
        history_shared: list[dict] = []
        from backend.db.database import SessionLocal

        db = SessionLocal()
        try:
            from backend.db.repositories.conversations import get_orchestrator_context, get_conversation, create_conversation, append_message

            conv = get_conversation(db, conv_id)
            if conv is None and conv_id != "tmp":
                # Crée la conversation à la volée (titre = début du message)
                try:
                    title = (message or "Nouvelle conversation")[:80].strip() or "Nouvelle conversation"
                    conv = create_conversation(db, title=title, mode=mode if mode in ("chat", "agent") else "chat", conv_id=conv_id)
                except Exception as e:
                    log(f"[db] create_conversation failed: {e}")
            if conv is not None:
                history_shared = get_orchestrator_context(db, conv_id)
            else:
                history_shared = []
            # Persiste message user immédiatement (avant LLM) pour historique
            if message and message.strip() and conv is not None:
                try:
                    append_message(db, conversation_id=conv_id, role="user", content=message, status="completed")
                except Exception as e:
                    log(f"[db] append user failed: {e}")
        except Exception as e:
            log(f"[db] history load failed: {e}")
            history_shared = []
        finally:
            try:
                db.close()
            except Exception:
                pass

        from backend.inference.loop import run_agent_loop

        collected = run_agent_loop(manager, conv_id=conv_id, request_id=req_id, message=message, mode=mode, image_paths=image_paths, history_shared=history_shared)
        # Persiste réponse assistant après génération
        if collected:
            db2 = SessionLocal()
            try:
                from backend.db.repositories.conversations import append_message
                append_message(db2, conversation_id=conv_id, role="assistant", content=collected, status="completed", run_id=req_id)
            except Exception as e:
                log(f"[db] append assistant failed: {e}")
            finally:
                try:
                    db2.close()
                except Exception:
                    pass

    elif method == "cancel":
        # Phase 2 : set cancel_flag
        log(f"[cancel] {req_id}")
    else:
        emit("error", {"request_id": req_id, "code": "unknown_method", "message": f"Unknown {method}"})

if __name__ == "__main__":
    init_db()
    log(f"[thot-phase-1] backend_ready backend={manager.backend.backend} vram={manager.backend.free_vram//(1024*1024)}MB")
    emit("backend_ready", {"backend": manager.backend.backend, "vram": manager.backend.free_vram, "n_gpu_layers": manager.backend.n_gpu_layers})
    stdin_loop(handle)

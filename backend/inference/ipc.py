"""
IPC — Communication JSON Lines entre Python et Rust
Notes.md § Architecture IPC pour Tauri

Principe :
- Python lit les requêtes sur stdin (une ligne = un JSON)
- Python répond sur stdout (une ligne = un JSON {"event":...,"payload":...})
- Les logs vont sur stderr (pour ne pas polluer stdout réservé à l'IPC)
- PYTHONUNBUFFERED=1 + flush() obligatoire sinon les events sont retardés
"""
import os
import sys
import json

os.environ["PYTHONUNBUFFERED"] = "1"


def emit(event: str, payload: dict):
    """
    Envoie un événement vers Rust (puis vers le frontend via Tauri emit).
    Exemple : emit("answer", {"request_id": "...", "text": "Bonjour"})
    """
    sys.stdout.write(json.dumps({"event": event, "payload": payload}, separators=(",", ":")) + "\n")
    sys.stdout.flush()  # indispensable : sans flush, Rust ne reçoit rien (buffer)


def log(msg: str):
    """Écrit un log sur stderr (visible dans les logs Rust, ne pollue pas stdout)."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def stdin_loop(handler):
    """Boucle principale : lit stdin ligne par ligne et appelle handler(req) pour chaque requête."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)  # ex: {"id":"uuid","method":"chat.send","params":{...}}
            handler(req)
        except Exception as e:
            emit("error", {"message": str(e), "code": "ipc_parse_error"})
            log(f"[ipc] erreur : {e} ligne={line[:200]}")

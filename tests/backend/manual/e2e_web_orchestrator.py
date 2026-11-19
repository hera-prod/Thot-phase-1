"""E2E réel : l'orchestrateur sait-il appeler `web_research` et en tirer une source ?

Tour de chat complet, LFM2.5-8B (LlamaCppRuntime) réel, réseau réel. Aucun mock,
aucune diffusion. Pour chaque formulation on rapporte :
  * les tool calls réellement émis (natif / pseudo-appel textuel / rien) ;
  * si une source a été lue, son identifiant public et son nombre d'octets ;
  * la réponse finale VERBATIM ;
  * l'état SQL réel (toolcalls, web_snapshots, messages).

Trois formulations **positives** (une par classe de source) et deux **négatives**
(une question de connaissance propre, une question sur un fait changeant que
l'outil ne couvre pas). Les négatives sont les plus importantes : un outil de
recherche qui répond « oui » à tout est pire qu'un outil absent.

Usage : THOT_E2E_DIR=/tmp/... python3 tests/backend/manual/e2e_web_orchestrator.py <scenario>...

Un data dir FRAIS par famille, comme le harnais image : un effet réutilisé
transforme une lecture en rejeu, et le rejeu masque ce qu'on mesure. Le data dir
est lu au IMPORT (THOT_DB_PATH), donc une famille = un processus.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Les trois positives couvrent les trois ways d'utiliser l'outil ; les deux
# négatives sont les pièges classiques d'un filtre de mots-clés.
SCENARIOS = {
    # --- Positives
    "definition": "Qu'est-ce que le théorème de Pythagore ?",
    "scientifique": "Peux-tu me dire quels articles parlent de spectroscopie des atmosphères d'exoplanètes ?",
    "source-uri": "Va lire https://fr.wikipedia.org/wiki/Espace_tangent et résume-moi l'idée.",
    # --- Négatives
    "propre": "Explique-moi en deux phrases ce qu'est une boucle for en Python.",
    "actu": "Quel est le cours de l'euro aujourd'hui ?",
}


def _seed_conversation(conv_id: str, title: str) -> None:
    from backend.db.database import SessionLocal
    from backend.db.models import Conversation

    db = SessionLocal()
    try:
        if db.query(Conversation).filter(Conversation.id == conv_id).first() is None:
            db.add(Conversation(id=conv_id, title=title[:80], mode="chat"))
            db.commit()
    finally:
        db.close()


def _persist_assistant_message(conv_id: str, request_id: str, answer: str, trace: dict) -> None:
    """Écrit le message assistant comme le fait le sidecar en production."""
    from backend.db.database import SessionLocal
    from backend.main import _persist_assistant_trace

    db = SessionLocal()
    try:
        _persist_assistant_trace(db, conv_id, answer, "completed", request_id, trace)
        db.commit()
    finally:
        db.close()


def _sql_evidence(conv_id: str, run_id: str) -> dict:
    from sqlalchemy import text

    from backend.db.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT tool_call_id, tool, ok, provenance, substr(args,1,400) "
                "FROM toolcalls WHERE run_id = :run ORDER BY id"
            ),
            {"run": run_id},
        ).fetchall()
        snaps = db.execute(
            text(
                "SELECT public_id, requested_url, status, content_sha256, length(text) AS chars, "
                "extractor_version, conv_id, reused_count FROM web_snapshots ORDER BY fetched_at"
            )
        ).fetchall()
        return {
            "toolcalls": [dict(r._mapping) for r in rows],
            "web_snapshots": [dict(r._mapping) for r in snaps],
        }
    finally:
        db.close()


def main() -> None:
    which = sys.argv[1:] or list(SCENARIOS)
    fresh = Path(os.environ["THOT_E2E_DIR"])
    fresh.mkdir(parents=True, exist_ok=True)
    family = fresh.name

    from backend.db.database import init_db

    init_db()
    from backend.inference import loop as L
    from backend.inference.manager import ModelManager
    from backend.inference.runtime import LlamaCppRuntime

    manager = ModelManager()
    runtime = LlamaCppRuntime(manager)
    # Journal brut : ce que le 8B a RÉELLEMENT répondu à chaque appel. Sans ça,
    # « la récupération n'a pas démarré » serait invérifiable.
    raw_calls: list[dict] = []
    _complete_chat = runtime.complete_chat

    def _recording_complete_chat(request):
        index = len(raw_calls)
        response = _complete_chat(request)
        try:
            raw_calls.append({
                "index": index,
                "n_tools_offered": len(getattr(request, "tools", ()) or ()),
                "forced_named": (request.tool_choice or {}).get("function", {}).get("name")
                if isinstance(request.tool_choice, dict) else None,
                "message": response.raw["choices"][0]["message"],
            })
        except Exception as exc:  # pragma: no cover - journal best-effort
            raw_calls.append({"index": index, "error": str(exc)})
        return response

    runtime.complete_chat = _recording_complete_chat  # type: ignore[method-assign]
    report: dict[str, dict] = {}
    print(f"[harness] data_dir={fresh}", flush=True)

    for name in which:
        message = SCENARIOS[name]
        raw_calls.clear()
        events: list[tuple[str, dict]] = []
        L.emit = lambda event, payload, **kw: events.append((event, payload))  # type: ignore[assignment]
        conv_id = f"conv_web_{family}_{name}"
        request_id = f"req_web_{family}_{name}"
        _seed_conversation(conv_id, message)
        started = time.perf_counter()
        collected, trace = L.run_agent_loop(
            manager, conv_id=conv_id, request_id=request_id, message=message,
            image_paths=None, history_shared=[], runtime=runtime,
        )
        wall = time.perf_counter() - started
        _persist_assistant_message(conv_id, request_id, collected, trace)
        calls = [p for e, p in events if e == "tool_call"]
        results = [p for e, p in events if e == "tool_result"]
        errors = [(p.get("code"), str(p.get("message"))[:200]) for e, p in events if e == "error"]
        web = [r for r in results if r.get("tool_call_id") in
               {c["tool_call_id"] for c in calls if c["tool"] == "web_research"}]
        report[name] = {
            "conversation_id": conv_id,
            "request_id": request_id,
            "message": message,
            "wall_s": round(wall, 1),
            "model_call_count": len(raw_calls),
            "llm_error": errors,
            "tool_calls": [{"id": c["tool_call_id"], "tool": c["tool"],
                            "args": c.get("arguments")} for c in calls],
            "web_results": [{"ok": r.get("ok"), "result": str(r.get("result"))[:2000]} for r in web],
            "answer": collected,
            "raw_model_turns": list(raw_calls),
            "sql": _sql_evidence(conv_id, request_id),
        }
        print(json.dumps({name: report[name]}, ensure_ascii=False, indent=2), flush=True)

    (fresh / "e2e_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT_PATH", fresh / "e2e_report.json", flush=True)


if __name__ == "__main__":
    main()

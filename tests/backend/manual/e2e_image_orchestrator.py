"""E2E réel : l'orchestrateur est-il le SEUL point de décision de generate_image ?

Tour de chat complet, LFM2.5-8B (LlamaCppRuntime) + FLUX.2-klein 9B réels, Phase 0
sans admission déterministe. Pour chaque formulation on rapporte :
  * les tool calls réellement émis (natif / pseudo-appel textuel / rien),
  * si une diffusion a tourné, le wall time, les run_stage,
  * la réponse finale VERBATIM,
  * l'état SQL réel (toolcalls, artifact_links, message_attachments).

Usage : THOT_E2E_DIR=/tmp/... python3 tests/backend/manual/e2e_image_orchestrator.py <scenario>...

Un data dir FRAIS par famille de scénarios : un effet publié réutilisé transforme
silencieusement une diffusion en rejeu, et le rejeu masque précisément ce qu'on
mesure. Le data dir est lu au IMPORT (THOT_DB_PATH), donc une famille = un
processus.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# q-r.md:8 — LE cas de référence, verbatim.
REFERENCE_PROMPT = "Génère moi une image de papillon photo réaliste en macro"

SCENARIOS = {
    # --- Famille 1 : la référence, puis la même phrase dans une AUTRE conversation.
    "q-r": REFERENCE_PROMPT,
    "repete": REFERENCE_PROMPT,
    # --- Famille 2 : taux de coopération (le point de décision sous contrainte).
    "imperatif": "Génère une photo d'un vieux château sous la pluie",
    "souhait": "Je voudrais une photo de papillon en macro",
    "indirect": "Rends-moi un portrait de chat",
    "format": "Génère une image d'un marché de fruits, format 16:9",
    "awkward": "Montre-moi une image d'un chat qui lit",
    # --- Famille 3 : contrôles négatifs (aucun filtre mots-clés : c'est le modèle qui décide).
    "neg-capitale": "Quelle est la capitale de l'Italie ?",
    "neg-tableau": "Fais un tableau visuel des ventes du trimestre",
    # --- Famille 4 : moteur coupé (processus SANS THOT_IMAGE_BACKEND).
    "moteur-off": REFERENCE_PROMPT,
}

DEFAULT_ORDER = list(SCENARIOS)


def _seed_conversation(conv_id: str, title: str) -> None:
    """Le run E2E écrit en base comme le chat réel : la conversation doit exister."""
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
    """Écrit le message assistant EXACTEMENT comme le fait le sidecar en production.

    Sans cette étape, `message_attachments` resterait vide et la « vérité DB » que
    le balayage de véracité (scénario 7) interroge ne contiendrait jamais l'image :
    le harness mesurerait un pipeline amputé de sa dernière étape.
    """
    from backend.db.database import SessionLocal
    from backend.main import _persist_assistant_trace

    db = SessionLocal()
    try:
        _persist_assistant_trace(db, conv_id, answer, "completed", request_id, trace)
        db.commit()
    finally:
        db.close()


def _sql_evidence(conv_id: str, run_id: str) -> dict:
    """L'état réel en base — la seule source de vérité du balayage de véracité."""
    from sqlalchemy import text

    from backend.db.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT tool_call_id, tool, ok, run_id, provenance, substr(args,1,600) "
                "FROM toolcalls WHERE run_id = :run ORDER BY id"
            ),
            {"run": run_id},
        ).fetchall()
        links = db.execute(
            text(
                "SELECT artifact_id, scope_kind, scope_id, role, removed_at "
                "FROM artifact_links WHERE scope_id = :conv ORDER BY id"
            ),
            {"conv": conv_id},
        ).fetchall()
        attach = db.execute(
            text(
                "SELECT ma.message_id, ma.artifact_id, ma.ordinal, m.role, m.run_id "
                "FROM message_attachments ma JOIN messages m ON m.id = ma.message_id "
                "WHERE m.run_id = :run ORDER BY ma.ordinal"
            ),
            {"run": run_id},
        ).fetchall()
        effects = db.execute(
            text(
                "SELECT id, run_id, fingerprint, status, output_artifact_id, attempt, error "
                "FROM image_effects ORDER BY created_at, id"
            )
        ).fetchall()
        return {
            "toolcalls": [dict(r._mapping) for r in rows],
            "artifact_links_scoped_conversation": [dict(r._mapping) for r in links],
            "message_attachments": [dict(r._mapping) for r in attach],
            "image_effects": [dict(r._mapping) for r in effects],
        }
    finally:
        db.close()


def main() -> None:
    which = sys.argv[1:] or DEFAULT_ORDER
    fresh = Path(os.environ["THOT_E2E_DIR"])
    fresh.mkdir(parents=True, exist_ok=True)
    family = fresh.name

    from backend.db.database import init_db

    init_db()
    from backend.inference import loop as L
    from backend.inference.manager import ModelManager
    from backend.inference.runtime import LlamaCppRuntime
    from backend.services import flux_spike

    manager = ModelManager()
    runtime = LlamaCppRuntime(manager)
    # Journal brut : ce que le 8B a RÉELLEMENT répondu à chaque appel (natif, texte,
    # ou rien). Sans ça, « le recovery n'a pas démarré » est invérifiable.
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
    real_backend_enabled = flux_spike.backend_enabled
    report: dict[str, dict] = {}
    print(f"[harness] data_dir={fresh} image_backend_enabled={real_backend_enabled}", flush=True)

    for name in which:
        message = SCENARIOS[name]
        # « Moteur coupé » = l'opt-in du moteur image est faux. Même code de production,
        # seule la décision « le moteur existe-t-il » est forcée à non.
        flux_spike.backend_enabled = (lambda: False) if name == "moteur-off" else real_backend_enabled
        # COPIE : `raw_calls.clear()` réutilise la MÊME liste, donc un rapport qui la
        # référencerait verrait le dernier scénario dans chaque dump. `list(...)`
        # copie les bindings ; chaque dict reste un objet distinct construit par le
        # recorder, donc rien n'est partagé non plus.
        del raw_calls[:]
        events: list[tuple[str, dict]] = []
        L.emit = lambda event, payload, **kw: events.append((event, payload))  # type: ignore[assignment]
        conv_id = f"conv_e2e_{family}_{name}"
        request_id = f"req_e2e_{family}_{name}"
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
        stages = [(p.get("stage"), p.get("status"), p.get("step"), p.get("total"))
                  for e, p in events if e == "run_stage"]
        errors = [(p.get("code"), str(p.get("message"))[:300]) for e, p in events if e == "error"]
        image_calls = [c for c in calls if c["tool"] == "generate_image"]
        diffusion = [r for r in results if r["tool_call_id"] in {c["tool_call_id"] for c in image_calls}]
        report[name] = {
            "conversation_id": conv_id,
            "request_id": request_id,
            "message": message,
            "wall_s": round(wall, 1),
            "model_call_count": len(raw_calls),
            "llm_error": errors,
            "tool_calls": [{"id": c["tool_call_id"], "tool": c["tool"],
                            "args": c.get("arguments")} for c in calls],
            "tool_results": [{"tool_call_id": r["tool_call_id"], "ok": r.get("ok"),
                              "result": str(r.get("result"))[:300]} for r in results],
            "image_tool_results": [{"tool_call_id": r["tool_call_id"], "ok": r.get("ok"),
                                    "result": str(r.get("result"))[:300]} for r in diffusion],
            "run_stage": stages,
            "image_published": bool(trace.get("image_published")),
            "image_artifact_id": trace.get("image_artifact_id"),
            "orchestrator_thinking": str(trace.get("orchestrator_thinking") or "")[:1500],
            "brief": str(trace.get("brief") or "")[:900],
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

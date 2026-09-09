# backend/search/observations.py — Écriture systématique des observations (annexe 11, Lot A)
#
# D'OÙ VIENT CE FICHIER : l'annexe 11 exige une Observation persistée après CHAQUE
# Vision/OCR/lecture — avant, les résultats ne vivaient qu'en mémoire du tour.
# OÙ VA QUOI : record_tool_observation() est appelée par loop.py après chaque exécution
# (Phase 0 déterministe + Phase 1 orchestrateur), puis revise_card_after_observation().
# POURQUOI JAMAIS BLOQUANT : la persistance est un audit/enrichissement ; un échec DB
# ne doit jamais casser le tour → try/except total + session courte dédiée.
from __future__ import annotations

import hashlib
import json
import uuid

EXCERPT_CHARS = 4000   # extrait structuré pour le prompt (result_json)
PAYLOAD_CHARS = 16000  # référence bornée hors prompt (payload_ref)


def fingerprint(blob_sha: str | None, tool: str, model: str | None,
                params: str, query: str | None) -> str:
    """Empreinte d'entrée : blob + outil + modèle + paramètres + query (annexe 11)."""
    raw = "|".join([blob_sha or "", tool or "", model or "", params or "", query or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record_tool_observation(artifact_id: str, tool_name: str, query: str | None,
                            result_text: str, ok: bool, *,
                            run_id: str | None = None,
                            tool_call_id: str | None = None,
                            model_id: str | None = None,
                            latency_ms: int = 0) -> object | None:
    """Persiste une Observation immuable + révise la fiche. Ne lève jamais.

    Retourne l'observation créée (ou None si persistance impossible / sans artefact).
    """
    if not artifact_id:
        return None
    try:
        from backend.db.database import SessionLocal as _SL
        from backend.db.models import Artifact as _Art, Observation as _Obs
        db = _SL()
        try:
            art = db.query(_Art).filter(_Art.id == artifact_id).first()
            if art is None:
                return None
            blob_sha = None
            try:
                from backend.db.models import Blob as _Blob
                blob = db.query(_Blob).filter(_Blob.id == art.blob_id).first()
                blob_sha = blob.sha256 if blob else None
            except Exception:
                pass
            status = "completed" if ok else "failed"
            full = result_text or ""
            excerpt = full[:EXCERPT_CHARS]
            # La fiche d'import (rev 1) existe toujours avant la révision N+1 :
            # une observation prouve que le fichier est arrivé, même sans import formel.
            try:
                from backend.search import cards as _cards0
                _cards0.ensure_card_on_import(db, art)
            except Exception:
                pass
            obs = _Obs(
                id=f"obs_{uuid.uuid4().hex[:12]}",
                artifact_id=artifact_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_version="phase1",
                model_id=model_id,
                input_fingerprint=fingerprint(blob_sha, tool_name, model_id, str(latency_ms), query),
                query=(query or "")[:500],
                status=status,
                result_json=json.dumps(
                    {"excerpt": excerpt, "truncated": len(full) > EXCERPT_CHARS,
                     "chars_total": len(full)}, ensure_ascii=False),
                payload_ref=full[:PAYLOAD_CHARS],
            )
            db.add(obs)
            db.flush()
            try:
                from backend.search import cards as _cards
                _cards.revise_card_after_observation(db, art, obs, excerpt=excerpt)
            except Exception:
                pass
            db.commit()
            return obs
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        return None

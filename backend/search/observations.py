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

    Idempotent par empreinte (blob+outil+modèle+query) : un doublon enrichit le
    contexte run si fourni au lieu de dupliquer la ligne (exécuteur + hook loop).
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
            payload = full[:PAYLOAD_CHARS]
            fp = fingerprint(blob_sha, tool_name, model_id, "", query)
            # Idempotence : même analyse déjà persistée → enrichit le contexte, pas de doublon
            existing = db.query(_Obs).filter(_Obs.input_fingerprint == fp).first()
            if existing is not None:
                touched = False
                if run_id and not existing.run_id:
                    existing.run_id = run_id
                    touched = True
                if tool_call_id and not existing.tool_call_id:
                    existing.tool_call_id = tool_call_id
                    touched = True
                if touched:
                    db.commit()
                return existing
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
                input_fingerprint=fp,
                query=(query or "")[:500],
                status=status,
                result_json=json.dumps(
                    {"excerpt": excerpt, "truncated": len(full) > EXCERPT_CHARS,
                     "chars_total": len(full)}, ensure_ascii=False),
                payload_ref=payload,
            )
            db.add(obs)
            db.flush()
            try:
                from backend.search import cards as _cards
                _cards.revise_card_after_observation(db, art, obs, excerpt=excerpt)
            except Exception:
                pass
            # Lot C : ingestion chunks bornée (texte/OCR ; vision seulement si description riche).
            # Basse priorité conceptuelle : borné, jamais bloquant, même session.
            try:
                from backend.search import chunks as _chk
                from backend.db.models import ArtifactLink as _LinkC
                _kind = (art.kind or "").lower()
                _src = payload if _kind in ("text", "pdf") else (excerpt if len(excerpt) >= 200 else "")
                if _src and len(_src) >= 200:
                    _links = (db.query(_LinkC)
                              .filter(_LinkC.artifact_id == artifact_id,
                                      _LinkC.removed_at.is_(None)).all())
                    _scopes = [(l.scope_kind, l.scope_id) for l in _links]
                    if _scopes:
                        _chk.ensure_chunk_tables(db)
                        _chk.ensure_chunks_for_artifact(db, art, _src[:16000], _scopes)
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

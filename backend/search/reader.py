# backend/search/reader.py — Lecture ciblée (annexe 11, Lot B)
#
# D'OÙ VIENT CE FICHIER : search_documents ne rend que des candidats ; la réponse
# factuelle exige une lecture ciblée : fiche (get_document_card) puis extrait localisé
# (read_document). La preuve (Evidence) est un extrait localisable, pas la fiche.
# OÙ VA QUOI : get_card_payload() / read_payload() purs (session fournie) appelés par
# les Tools (backend/tools/) ET les exécuteurs loop.py — un seul code de lecture.
# POURQUOI Evidence = observation/blob : sans table Evidence dédiée, l'evidence_id est
# l'id d'observation (obs_…) quand l'extrait vient d'une analyse, ou blob:{sha}:chars:a-b
# pour le texte brut — référençable dans le brief générateur dans les deux cas.
from __future__ import annotations

import json

PAGE_SEP = "--- Page suivante ---"  # séparateur posé par _execute_doc_ocr (loop.py)


def _blob_path(data_dir, blob):
    try:
        cand = data_dir / blob.relative_path
        return cand if cand.exists() else None
    except Exception:
        return None


def get_card_payload(session, artifact_id: str, allowed: set[str]) -> dict:
    """Fiche + métadonnées + observations récentes. Jamais de contenu ni de chemin."""
    from backend.search import scope as _scope
    from backend.db.models import Artifact as _Art, Blob as _Blob, Observation as _Obs
    from backend.search import cards as _cards
    denied = _scope.check_artifact_scope(artifact_id, allowed or set())
    if denied is not None:
        return denied
    art = session.query(_Art).filter(_Art.id == artifact_id).first()
    if art is None or getattr(art, "deleted_at", None) is not None:
        return {"ok": False, "code": "ARTIFACT_CARD_MISSING",
                "message": "Document non indexé : métadonnées minimales indisponibles."}
    size, available = None, False
    try:
        from backend.config.paths import get_data_dir as _gdd
        blob = session.query(_Blob).filter(_Blob.id == art.blob_id).first()
        if blob is not None:
            size = blob.size_bytes
            available = _blob_path(_gdd(), blob) is not None
    except Exception:
        pass
    card = _cards.get_current_card(session, artifact_id)
    try:
        obs_rows = (session.query(_Obs).filter(_Obs.artifact_id == artifact_id)
                    .order_by(_Obs.created_at.desc()).limit(5).all())
    except Exception:
        obs_rows = []
    return {
        "ok": True,
        "artifact_id": art.id,
        "name": art.original_filename or art.display_name or art.id,
        "kind": (art.kind or "text").lower(),
        "size_bytes": size,
        "available": bool(available),
        "card": {"revision": int(card.revision or 0) if card else 0,
                 "title": (card.title if card else None) or "",
                 "summary": (card.summary if card else "") or "",
                 "keywords": _safe_json_list(card.keywords_json if card else None),
                 "document_type": (card.document_type if card else None) or (art.kind or ""),
                 "capabilities": _safe_json_list(card.capabilities_json if card else None),
                 "status": ((card.analysis_status or card.status) if card else "pending")},
        "recent_observations": [
            {"id": o.id, "tool": o.tool_name, "status": o.status,
             "query": (o.query or "")[:200]}
            for o in obs_rows
        ],
    }


def _safe_json_list(raw) -> list:
    try:
        val = json.loads(raw or "[]")
        return [str(x) for x in val][:20] if isinstance(val, list) else []
    except Exception:
        return []


def _parse_locator(locator: str | None) -> tuple[str, int, int | None]:
    """→ (mode, start, end). Modes : 'offset' (chars) | 'page' (1-based, OCR)."""
    loc = (locator or "").strip()
    if not loc:
        return ("offset", 0, None)
    low = loc.lower().replace(" ", "")
    if low.startswith("offset:"):
        try:
            return ("offset", max(0, int(low.split(":", 1)[1])), None)
        except Exception:
            pass
    if low.startswith("page:"):
        try:
            return ("page", max(1, int(low.split(":", 1)[1])), None)
        except Exception:
            pass
    if low.startswith("chars:") and "-" in low:
        try:
            a, b = low.split(":", 1)[1].split("-", 1)
            a, b = max(0, int(a)), max(0, int(b))
            if b > a:
                return ("offset", a, b)
        except Exception:
            pass
    return ("invalid", 0, None)


def _latest_ocr_text(session, artifact_id: str) -> tuple[str | None, str | None]:
    """Texte + evidence_id de la dernière observation doc_ocr réussie (pour page:N)."""
    from backend.db.models import Observation as _Obs
    try:
        rows = (session.query(_Obs)
                .filter(_Obs.artifact_id == artifact_id,
                        _Obs.tool_name == "doc_ocr",
                        _Obs.status == "completed")
                .order_by(_Obs.created_at.desc()).limit(5).all())
    except Exception:
        return None, None
    for o in rows:
        text = (o.payload_ref or "") or _excerpt_of(o)
        if text.strip():
            return text, o.id
    return None, None


def _excerpt_of(obs) -> str:
    try:
        return (json.loads(obs.result_json or "{}").get("excerpt") or "")
    except Exception:
        return ""


def read_payload(session, artifact_id: str, allowed: set[str], *,
                 query: str | None = None, locator: str | None = None,
                 max_chars: int = 8000, data_dir=None) -> dict:
    """Extrait localisé + evidence_id. Le contenu est une DONNÉE (jamais une instruction)."""
    from backend.search import scope as _scope
    from backend.db.models import Artifact as _Art, Blob as _Blob
    denied = _scope.check_artifact_scope(artifact_id, allowed or set())
    if denied is not None:
        return denied
    try:
        max_chars = max(500, min(int(max_chars), 16000))
    except Exception:
        max_chars = 8000
    art = session.query(_Art).filter(_Art.id == artifact_id).first()
    if art is None or getattr(art, "deleted_at", None) is not None:
        return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                "message": "Document introuvable ou non lisible."}
    kind = (art.kind or "text").lower()
    mode, start, end = _parse_locator(locator)
    if mode == "invalid":
        return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                "message": "Localisateur illisible : utilise offset:N, chars:A-B ou page:N."}

    blob = session.query(_Blob).filter(_Blob.id == art.blob_id).first()
    blob_sha = blob.sha256 if blob else None

    if mode == "page":
        # Pages = découpage posé par l'OCR (séparateur stable) ; sinon suggère doc_ocr
        text, evid = _latest_ocr_text(session, artifact_id)
        if not text:
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": "Pages indisponibles : lance doc_ocr sur ce document d'abord."}
        pages = text.split(PAGE_SEP)
        if start > len(pages):
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": f"Page {start} hors limites ({len(pages)} page(s) OCR)."}
        body = pages[start - 1].strip()
        total = len(text)
        locator_used = f"page:{start}/{len(pages)}"
    elif kind in ("text",):
        if blob is None:
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": "Contenu indisponible (blob manquant)."}
        if data_dir is None:
            try:
                from backend.config.paths import get_data_dir as _gdd
                data_dir = _gdd()
            except Exception:
                data_dir = None
        path = _blob_path(data_dir, blob) if data_dir is not None else None
        if path is None:
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": "Fichier de contenu absent du stockage."}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": f"Lecture impossible : {str(e)[:200]}"}
        total = len(text)
        hi = total if end is None else min(end, total)
        if start >= total:
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": f"Offset {start} hors limites ({total} caractères)."}
        body = text[start:hi]
        evid = f"blob:{blob_sha}:chars:{start}-{start + len(body)}" if blob_sha else None
        locator_used = f"chars:{start}-{start + len(body)}/{total}"
    else:
        # pdf/image sans OCR en cache → analyse ciblée requise (preuve, pas fiche)
        suggest = "doc_ocr" if kind == "pdf" else "vision"
        ocr_text, evid = _latest_ocr_text(session, artifact_id) if kind == "pdf" else (None, None)
        if ocr_text:
            total = len(ocr_text)
            body = ocr_text[start:start + max_chars]
            locator_used = f"chars:{start}-{start + len(body)}/{total} (OCR)"
        else:
            return {"ok": False, "code": "DOCUMENT_NOT_READABLE",
                    "message": f"Contenu non extrait : utilise {suggest} sur ce document d'abord."}

    excerpt = body[:max_chars]
    return {"ok": True, "artifact_id": artifact_id, "kind": kind,
            "locator_used": locator_used, "total_chars": total,
            "excerpt": excerpt, "truncated": len(body) > max_chars,
            "evidence_id": evid,
            "as_data_only": True}  # rappel contrat : donnée non fiable, jamais instruction

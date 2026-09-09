# backend/search/chunks.py — Découpage + contextualisation + ingestion (annexe 11, Lot C)
#
# D'OÙ VIENT CE FICHIER : l'hybride travaille sur des FRAGMENTS situés, pas des documents
# entiers : découpage borné → contexte 50-100 tokens → FTS + vecteur par chunk.
# OÙ VA QUOI : ensure_chunks_for_artifact() appelée après lecture/OCR de texte (bounded,
# basse priorité conceptuelle) ; search lit chunk_search + vecteurs via fts.py.
# POURQUOI CONTEXTE DÉTERMINISTE PAR DÉFAUT : le LLM de contextualisation tourne à
# l'ingestion seulement (jamais sur le chemin interactif) ; le gabarit local garantit
# un index utile hors-ligne, remplaçable par contextualizer branché (Phase 5).
from __future__ import annotations

import uuid

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
MAX_CHUNKS = 50  # borne d'ingestion par artefact (chat interactif prioritaire)

_CHUNK_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(
    chunk_id UNINDEXED,
    artifact_id UNINDEXED,
    scope_kind UNINDEXED,
    scope_id UNINDEXED,
    text,
    context,
    tokenize = 'unicode61 remove_diacritics 2'
)"""

_VECTORS_DDL = """CREATE TABLE IF NOT EXISTS artifact_chunk_vectors(
    chunk_id VARCHAR PRIMARY KEY,
    dim INTEGER,
    vector BLOB
)"""


def ensure_chunk_tables(session) -> None:
    from sqlalchemy import text as _text
    session.execute(_text(_CHUNK_FTS_DDL))
    session.execute(_text(_VECTORS_DDL))


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Découpe greedy sur frontières de paragraphes/phrases, bornée et chevauchée."""
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not paras:
        return []
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:
            cut = p.rfind(". ", 0, size)
            cut = cut + 2 if cut > size // 2 else size
            piece, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = cur[-overlap:] if len(cur) > overlap else cur
            cur = (cur + " " + piece).strip() if cur else piece
            if len(cur) >= size:
                chunks.append(cur)
                cur = cur[-overlap:]
        candidate = (cur + "\n\n" + p).strip() if cur else p
        if len(candidate) > size and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


def default_context(doc_title: str, ordinal: int, total: int, chunk: str) -> str:
    """Contexte déterministe (gabarit local) : titre + position + amorce factuelle."""
    head = chunk.strip().split("\n", 1)[0][:120]
    return (f"Document « {doc_title or '?'} », fragment {ordinal + 1}/{total}. "
            f"Amorce : {head}".strip())


def ensure_chunks_for_artifact(session, artifact, text: str,
                               scope_links: list[tuple[str, str]],
                               contextualizer=None) -> int:
    """Découpe + contexte + index FTS chunks + vecteurs. Retourne le nb de chunks. Borné."""
    from backend.db.models import ArtifactChunk as _Chk
    from backend.search import vectors as _vec
    from sqlalchemy import text as _text
    body = (text or "").strip()
    if not body:
        return 0
    # Réingestion propre : remplace les chunks de la même révision de fiche
    try:
        current_rev = 0
        from backend.search import cards as _cards
        card = _cards.get_current_card(session, artifact.id)
        current_rev = int(card.revision or 0) if card else 0
    except Exception:
        current_rev = 0
    try:
        old = session.query(_Chk).filter(_Chk.artifact_id == artifact.id).all()
        old_ids = [c.id for c in old]
        for c in old:
            session.delete(c)
        session.flush()
        if old_ids:
            for cid in old_ids:
                try:
                    session.execute(_text("DELETE FROM chunk_search WHERE chunk_id = :c"), {"c": cid})
                    session.execute(_text("DELETE FROM artifact_chunk_vectors WHERE chunk_id = :c"), {"c": cid})
                except Exception:
                    pass
            session.flush()
    except Exception:
        pass
    pieces = chunk_text(body)[:MAX_CHUNKS]
    title = artifact.original_filename or artifact.display_name or artifact.id
    try:
        ensure_chunk_tables(session)
    except Exception:
        return 0
    for i, piece in enumerate(pieces):
        cid = f"chk_{uuid.uuid4().hex[:12]}"
        try:
            ctx = contextualizer(title, i, len(pieces), piece) if contextualizer else default_context(title, i, len(pieces), piece)
        except Exception:
            ctx = default_context(title, i, len(pieces), piece)
        session.add(_Chk(id=cid, artifact_id=artifact.id, card_revision=current_rev,
                         ordinal=i, text=piece[:4000], context=(ctx or "")[:500]))
        session.flush()
        vec = _vec.pack_vector(_vec.hash_embedding((ctx or "") + "\n" + piece))
        try:
            session.execute(_text("INSERT INTO artifact_chunk_vectors(chunk_id, dim, vector) VALUES (:c, :d, :v)"),
                            {"c": cid, "d": _vec.DIM, "v": vec})
        except Exception:
            pass
        for sk, sid in scope_links:
            try:
                session.execute(
                    _text("""INSERT INTO chunk_search(chunk_id, artifact_id, scope_kind, scope_id, text, context)
                             VALUES (:c, :a, :sk, :sid, :t, :x)"""),
                    {"c": cid, "a": artifact.id, "sk": sk, "sid": sid, "t": piece[:4000], "x": (ctx or "")[:500]})
            except Exception:
                pass
    session.flush()
    return len(pieces)


def remove_chunks_for_artifact(session, artifact_id: str) -> None:
    """Retire chunks + index + vecteurs (purge GC)."""
    from backend.db.models import ArtifactChunk as _Chk
    from sqlalchemy import text as _text
    try:
        ids = [c.id for c in session.query(_Chk).filter(_Chk.artifact_id == artifact_id).all()]
        for c in session.query(_Chk).filter(_Chk.artifact_id == artifact_id).all():
            session.delete(c)
        session.flush()
        session.execute(_text("DELETE FROM chunk_search WHERE artifact_id = :a"), {"a": artifact_id})
        for cid in ids:
            try:
                session.execute(_text("DELETE FROM artifact_chunk_vectors WHERE chunk_id = :c"), {"c": cid})
            except Exception:
                pass
        session.flush()
    except Exception:
        pass


def best_source_text(session, artifact) -> str:
    """Meilleur texte indexable : blob texte (≤200k) sinon dernière observation OCR/lecture."""
    try:
        if (artifact.kind or "").lower() == "text":
            from backend.db.models import Blob as _Blob
            from backend.config.paths import get_data_dir as _gdd
            blob = session.query(_Blob).filter(_Blob.id == artifact.blob_id).first()
            if blob is not None and (blob.size_bytes or 0) <= 200_000:
                cand = _gdd() / blob.relative_path
                if cand.exists():
                    return cand.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        from backend.db.models import Observation as _Obs
        rows = (session.query(_Obs)
                .filter(_Obs.artifact_id == artifact.id, _Obs.status == "completed",
                        _Obs.tool_name.in_(["doc_ocr", "read_document", "read_text"]))
                .order_by(_Obs.created_at.desc()).limit(3).all())
        for o in rows:
            if (o.payload_ref or "").strip():
                return o.payload_ref
    except Exception:
        pass
    return ""

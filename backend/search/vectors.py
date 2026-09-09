# backend/search/vectors.py — Vecteurs locaux + fusion RRF (annexe 11, Lot C)
#
# D'OÙ VIENT CE FICHIER : quand FTS5 seul devient insuffisant, même API search_documents,
# couches internes en plus (BM25 top-50 + vecteurs top-50 → RRF → top-20 → rerank → top-5).
# OÙ VA QUOI : hash_embedding() à l'ingestion (chunks.py), search_vectors() + rrf_fuse()
# au retrieval (fts.py). Aucun modèle externe, aucun réseau : hashing trick déterministe.
# POURQUOI PAS D'EMBEDDINGS NEURONAUX : machine à VRAM limitée, hors-ligne d'abord ;
# le point de branchement embed() est prêt pour un modèle local (Phase 5) sans changer l'API.
from __future__ import annotations

import hashlib
import math
import struct

DIM = 256  # dimension fixe des vecteurs locaux (1 Ko/chunk en float32)
RRF_K = 60  # constante Reciprocal Rank Fusion (Elastic)


def hash_embedding(text: str, dim: int = DIM) -> list[float]:
    """Embedding déterministe (hashing trick signé + normalisation L2).

    Même texte → même vecteur, pour toujours. Faible sémantique (sac de tokens),
    mais orthogonal à BM25 : capte le recouvrement lexical que le ranking exact rate.
    """
    try:
        from backend.search.fts import normalize_query as _norm
        tokens = _norm(text)
    except Exception:
        tokens = (text or "").lower().split()
    vec = [0.0] * dim
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = -1.0 if (h[4] & 1) else 1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes, dim: int = DIM) -> list[float]:
    try:
        if len(blob) != dim * 4:
            return [0.0] * dim
        return list(struct.unpack(f"<{dim}f", blob))
    except Exception:
        return [0.0] * dim


def cosine(a: list[float], b: list[float]) -> float:
    try:
        return sum(x * y for x, y in zip(a, b))
    except Exception:
        return 0.0


def search_vectors(session, query: str, allowed_ids: set[str], top_n: int = 50) -> list[tuple[str, float]]:
    """Top chunks par cosinus, restreints aux artefacts autorisés. → [(artifact_id, score)]."""
    q = hash_embedding(query)
    try:
        from sqlalchemy import text as _text
        rows = session.execute(
            _text("SELECT chunk_id, vector FROM artifact_chunk_vectors")).fetchall()
    except Exception:
        return []
    chunk_art: dict[str, str] = {}
    try:
        from backend.db.models import ArtifactChunk as _Chk
        for c in session.query(_Chk).all():
            chunk_art[c.id] = c.artifact_id
    except Exception:
        return []
    best: dict[str, float] = {}
    for chunk_id, blob in rows:
        aid = chunk_art.get(chunk_id)
        if not aid or aid not in (allowed_ids or set()):
            continue
        try:
            s = cosine(q, unpack_vector(bytes(blob)))
        except Exception:
            continue
        if s > (best.get(aid) or 0.0):
            best[aid] = s
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:top_n]


def rrf_fuse(ranked_lists: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Fusion Reciprocal Rank Fusion : score = Σ 1/(k + rang). Ordre décroissant."""
    fused: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, aid in enumerate(lst, start=1):
            fused[aid] = fused.get(aid, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

# backend/search/fts.py — Index lexical FTS5 + ranking déterministe (annexe 11, Lot A)
#
# D'OÙ VIENT CE FICHIER : annexe 11 §Index FTS5 + §Étapes 1-7 — la Phase 1 doit retrouver
# un document SANS embeddings : SQLite FTS5 + filtres + BM25 suffisent.
# OÙ VA QUOI : index_card_row() appelé par cards.py à chaque fiche active ; search()
# appelé par l'exécuteur search_documents (loop.py) et le Tool (tools/).
# POURQUOI PAS D'EMBEDDINGS ICI : Lot C uniquement, sans changer l'API — ce module
# reste l'unique point de retrieval lexical.
from __future__ import annotations

import base64
import hashlib
import json
import unicodedata

# Colonnes FTS indexées (ordre fixe — highlight() s'y réfère par index)
# 0 artifact_id, 1 scope_kind, 2 scope_id, 3 kind, 4 card_revision (UNINDEXED),
# 5 filename, 6 title, 7 summary, 8 keywords, 9 entities, 10 extracted_preview
_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS artifact_search USING fts5(
    artifact_id UNINDEXED,
    scope_kind UNINDEXED,
    scope_id UNINDEXED,
    kind UNINDEXED,
    card_revision UNINDEXED,
    filename,
    title,
    summary,
    keywords,
    entities,
    extracted_preview,
    tokenize = 'unicode61 remove_diacritics 2'
)"""

MAX_CANDIDATES = 50   # récupération large et rapide (rerank Lot C plus tard)
MAX_LIMIT = 20        # borne tool call (annexe 11)
MAX_QUERY = 300

# Filtres de type acceptés → kinds internes (le kind Rust est image|pdf|text)
_TYPE_TO_KINDS: dict[str, set[str]] = {
    "image": {"image"},
    "pdf": {"pdf"},
    "text": {"text"},
    "markdown": {"text"},
    "csv": {"text"},
    "docx": {"text"},
}

# Statuts de fiche : filtre "unanalysed" = pending (jamais passé au modèle)
_STATUS_ALIASES = {"unanalysed": "pending"}


def ensure_fts(session) -> None:
    """Crée la table FTS5 sur la connexion de la session (idempotent)."""
    from sqlalchemy import text as _text
    session.execute(_text(_FTS_DDL))


def normalize_query(query: str) -> list[str]:
    """Normalise SANS dénaturer : NFC, minuscules, diacritiques retirés, tokens non vides."""
    q = unicodedata.normalize("NFC", query or "").lower()
    q = "".join(c for c in unicodedata.normalize("NFD", q) if not unicodedata.combining(c))
    return [t for t in q.split() if t]


def _fts_and(tokens: list[str]) -> str:
    """Requête FTS5 : tokens guillemettés en AND implicite (termes simples, sans syntaxe)."""
    return " ".join('"' + t.replace('"', "") + '"' for t in tokens)


def index_card_row(session, artifact, card, scope_kind: str, scope_id: str,
                   extracted_preview: str = "") -> None:
    """(Ré)indexe la fiche active pour UN lien de scope. Idempotent par suppression préalable."""
    from sqlalchemy import text as _text
    import json as _json
    try:
        keywords = " ".join(_json.loads(card.keywords_json or "[]"))
    except Exception:
        keywords = ""
    try:
        entities = " ".join(_json.loads(card.entities_json or "[]"))
    except Exception:
        entities = ""
    session.execute(
        _text("DELETE FROM artifact_search WHERE artifact_id = :a AND scope_kind = :sk AND scope_id = :sid"),
        {"a": artifact.id, "sk": scope_kind, "sid": scope_id},
    )
    session.execute(
        _text("""INSERT INTO artifact_search
                 (artifact_id, scope_kind, scope_id, kind, card_revision,
                  filename, title, summary, keywords, entities, extracted_preview)
                 VALUES (:a, :sk, :sid, :k, :rev, :fn, :t, :s, :kw, :en, :ex)"""),
        {"a": artifact.id, "sk": scope_kind, "sid": scope_id,
         "k": (artifact.kind or "text"),
         "rev": int(card.revision or 0),
         "fn": artifact.original_filename or artifact.display_name or "",
         "t": card.title or "", "s": card.summary or "",
         "kw": keywords, "en": entities,
         "ex": (extracted_preview or "")[:2000]},
    )


def remove_artifact(session, artifact_id: str) -> None:
    """Retire un artefact de l'index (purge GC — éligibilité, pas suppression aveugle)."""
    from sqlalchemy import text as _text
    session.execute(_text("DELETE FROM artifact_search WHERE artifact_id = :a"), {"a": artifact_id})


def _cursor_encode(fingerprint: str, offset: int) -> str:
    raw = json.dumps({"qh": fingerprint, "off": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _cursor_decode(cursor: str) -> dict | None:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        return None


def _request_fingerprint(query: str, scope_kind: str, scope_id: str,
                         types: list[str] | None, status: str) -> str:
    raw = "|".join([query, scope_kind, scope_id, ",".join(sorted(types or [])), status or "any"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _current_cards_map(session, artifact_ids: list[str]) -> dict:
    """artifact_id → fiche active (is_current=1, repli legacy : révision max)."""
    from backend.db.models import ArtifactCard as _Card
    out: dict = {}
    if not artifact_ids:
        return out
    rows = session.query(_Card).filter(_Card.artifact_id.in_(artifact_ids)).all()
    best: dict = {}
    for c in rows:
        cur = best.get(c.artifact_id)
        key = (1 if int(c.is_current or 0) == 1 else 0, int(c.revision or 0))
        if cur is None or key > cur[0]:
            best[c.artifact_id] = (key, c)
    return {aid: c for aid, (_, c) in best.items()}


def _score_row(tokens: list[str], full: str, row: dict, age_days: float | None) -> tuple[float, list[str]]:
    """Score transparent annexe 11 : 4 filename, 3 title, 2 kw/entities, 1.5 summary, 1 extrait."""
    def _hit(weight: float, text: str) -> tuple[float, bool, bool]:
        t = (text or "").lower()
        if not t:
            return 0.0, False, False
        if full and full in t:
            return weight, True, True
        hits = sum(1 for tok in tokens if tok in t)
        if hits >= len(tokens):
            return round(weight * 0.75, 3), True, False
        if hits:
            return round(weight * 0.4, 3), True, False
        return 0.0, False, False

    score, reasons = 0.0, []
    s, any_h, _ = _hit(4.0, row.get("filename") or "")
    if any_h:
        score += s
        reasons.append("filename")
    s, any_h, _ = _hit(3.0, row.get("title") or "")
    if any_h:
        score += s
        reasons.append("title")
    s1, h1, _ = _hit(2.0, row.get("keywords") or "")
    s2, h2, _ = _hit(2.0, row.get("entities") or "")
    if h1 or h2:
        score += max(s1, s2)
        reasons.append("keyword: " + (", ".join(tokens)))
    s, any_h, _ = _hit(1.5, row.get("summary") or "")
    if any_h:
        score += s
        reasons.append("summary")
    s, any_h, _ = _hit(1.0, row.get("extracted_preview") or "")
    if any_h:
        score += s
        reasons.append("extrait")
    if age_days is not None and age_days >= 0:
        score += round(0.2 * max(0.0, 1.0 - age_days / 90.0), 3)  # récence = départage, jamais vérité
    score += -float(row.get("bm25") or 0.0) * 1e-6  # BM25 FTS5 : départage résiduel
    return round(score, 4), reasons


def _hybrid_rerank(session, query_str: str, tokens: list[str], full_norm: str,
                   ranked: list[dict], allowed_ids: set[str],
                   scope_kind: str, scope_id: str) -> list[dict]:
    """RRF(doc-FTS, chunk-FTS, vecteurs) + blend rerank local. Sans chunks indexés : no-op."""
    from sqlalchemy import text as _text
    try:
        n = session.execute(
            _text("SELECT COUNT(*) FROM chunk_search WHERE scope_kind = :sk AND scope_id = :sid"),
            {"sk": scope_kind, "sid": scope_id}).scalar()
    except Exception:
        return ranked
    if not n:
        return ranked
    match_q = _fts_and(tokens)
    chunk_order: list[str] = []
    try:
        rows = session.execute(
            _text("""SELECT artifact_id, bm25(chunk_search) AS r FROM chunk_search
                     WHERE scope_kind = :sk AND scope_id = :sid AND chunk_search MATCH :q
                     ORDER BY bm25(chunk_search) LIMIT 50"""),
            {"sk": scope_kind, "sid": scope_id, "q": match_q}).mappings().all()
        seen: set[str] = set()
        for r in rows:
            aid = r["artifact_id"]
            if aid in (allowed_ids or set()) and aid not in seen:
                seen.add(aid)
                chunk_order.append(aid)
    except Exception:
        pass
    vec_order: list[str] = []
    try:
        from backend.search import vectors as _vec
        vec_order = [aid for aid, _ in _vec.search_vectors(session, query_str, allowed_ids or set(), 50)]
    except Exception:
        pass
    signals = [[d["aid"] for d in ranked]]
    if chunk_order:
        signals.append(chunk_order)
    if vec_order:
        signals.append(vec_order)
    if len(signals) < 2:
        return ranked
    try:
        from backend.search import vectors as _vec2
        fused = _vec2.rrf_fuse(signals)
    except Exception:
        return ranked
    by_aid = {d["aid"]: d for d in ranked}
    # Artefacts apportés par chunks/vecteurs seuls : entrée calculée depuis la fiche
    try:
        from backend.db.models import Artifact as _ArtH
        missing = [aid for aid, _ in fused if aid not in by_aid]
        if missing:
            arts = {a.id: a for a in session.query(_ArtH).filter(_ArtH.id.in_(missing)).all()}
            cards = _current_cards_map(session, missing)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            for aid in missing:
                art, card = arts.get(aid), cards.get(aid)
                if art is None or card is None:
                    continue
                try:
                    import json as _jh
                    kw = " ".join(_jh.loads(card.keywords_json or "[]"))
                except Exception:
                    kw = ""
                row = {"filename": art.original_filename or "", "title": card.title or "",
                       "summary": card.summary or "", "keywords": kw, "entities": "",
                       "extracted_preview": "", "bm25": 0.0}
                score, reasons = _score_row(tokens, full_norm, row, None)
                by_aid[aid] = {"aid": aid, "art": art, "card": card,
                               "score": score, "reasons": reasons or ["hybride"]}
    except Exception:
        pass
    fused_ids = [aid for aid, _ in fused if aid in by_aid]
    fused_ids += [d["aid"] for d in ranked if d["aid"] not in set(fused_ids)]
    # Blend : 0.6 RRF normalisé + 0.4 score lexical normalisé (déterministe)
    rrf = {aid: s for aid, s in fused}
    rmax = max(rrf.values()) if rrf else 1.0
    smax = max((by_aid[a]["score"] for a in fused_ids), default=0.0) or 1.0
    blended = sorted(
        fused_ids,
        key=lambda a: (0.6 * (rrf.get(a, 0.0) / rmax) + 0.4 * (by_aid[a]["score"] / smax)),
        reverse=True,
    )
    return [by_aid[a] for a in blended]


def search(session, *, query: str, scope_kind: str, scope_id: str,
           allowed_ids: set[str], types: list[str] | None = None,
           status: str = "any", limit: int = 10,
           cursor: str | None = None) -> dict:
    """Recherche bornée et paginée. Ne voit QUE les ids autorisés (scope.py en amont).

    Retourne {"ok": True, page…} ou {"ok": False, "code": …, "message": …}.
    """
    from sqlalchemy import text as _text
    from datetime import datetime, timezone
    original = (query or "").strip()
    if not original or len(original) > MAX_QUERY:
        return {"ok": False, "code": "SEARCH_QUERY_INVALID",
                "message": "Requête absente ou trop longue (300 caractères max)."}
    if not scope_kind or not scope_id:
        return {"ok": False, "code": "SEARCH_SCOPE_INVALID",
                "message": "Périmètre de recherche absent ou incompatible."}
    tokens = normalize_query(original)
    if not tokens:
        return {"ok": False, "code": "SEARCH_QUERY_INVALID",
                "message": "Requête illisible après normalisation."}
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except Exception:
        limit = 10

    fp = _request_fingerprint(original, scope_kind, scope_id, types, status or "any")
    offset = 0
    if cursor:
        dec = _cursor_decode(cursor)
        if not dec or dec.get("qh") != fp:
            return {"ok": False, "code": "SEARCH_CURSOR_MISMATCH",
                    "message": "Curseur d'une autre recherche : relance sans curseur."}
        try:
            offset = max(0, int(dec.get("off", 0)))
        except Exception:
            offset = 0

    kinds: set[str] | None = None
    if types:
        kinds = set()
        for t in types:
            kinds.update(_TYPE_TO_KINDS.get(str(t).lower(), set()))
    status_norm = _STATUS_ALIASES.get((status or "any").lower(), (status or "any").lower())

    match_q = _fts_and(tokens)
    kind_clause, params = "", {"sk": scope_kind, "sid": scope_id, "q": match_q}
    if kinds:
        placeholders = ",".join(f":k{i}" for i in range(len(kinds)))
        kind_clause = f" AND kind IN ({placeholders})"
        for i, k in enumerate(sorted(kinds)):
            params[f"k{i}"] = k
    try:
        rows = session.execute(
            _text(f"""SELECT artifact_id, scope_kind, scope_id, kind, card_revision,
                             filename, title, summary, keywords, entities, extracted_preview,
                             bm25(artifact_search) AS bm25
                      FROM artifact_search
                      WHERE scope_kind = :sk AND scope_id = :sid{kind_clause}
                        AND artifact_search MATCH :q
                      ORDER BY bm25(artifact_search)
                      LIMIT {MAX_CANDIDATES}"""),
            params,
        ).mappings().all()
    except Exception as e:
        return {"ok": False, "code": "SEARCH_QUERY_INVALID",
                "message": f"Requête FTS rejetée : {str(e)[:200]}"}
    # Repli OR si le AND strict ne donne rien (requête multi-termes)
    if not rows and len(tokens) > 1:
        or_q = " OR ".join('"' + t.replace('"', "") + '"' for t in tokens)
        try:
            rows = session.execute(
                _text(f"""SELECT artifact_id, scope_kind, scope_id, kind, card_revision,
                                 filename, title, summary, keywords, entities, extracted_preview,
                                 bm25(artifact_search) AS bm25
                          FROM artifact_search
                          WHERE scope_kind = :sk AND scope_id = :sid{kind_clause}
                            AND artifact_search MATCH :q
                          ORDER BY bm25(artifact_search)
                          LIMIT {MAX_CANDIDATES}"""),
                {**params, "q": or_q},
            ).mappings().all()
        except Exception:
            rows = []

    # Sécurité + fraîcheur : filtre allowed, révision active, statut, artefact vivant
    cand_ids = sorted({r["artifact_id"] for r in rows} & set(allowed_ids or set()))
    cards = _current_cards_map(session, cand_ids)
    from backend.db.models import Artifact as _Art
    arts = {a.id: a for a in session.query(_Art).filter(_Art.id.in_(cand_ids)).all()} if cand_ids else {}
    now = datetime.now(timezone.utc)
    full_norm = " ".join(tokens)

    ranked: list[dict] = []
    for r in rows:
        aid = r["artifact_id"]
        if aid not in allowed_ids or aid not in cards or aid not in arts:
            continue
        card = cards[aid]
        if int(r["card_revision"] or 0) != int(card.revision or 0):
            continue  # ligne FTS d'une révision dépassée
        if status_norm != "any" and (card.analysis_status or card.status or "") != status_norm:
            continue
        art = arts[aid]
        if getattr(art, "deleted_at", None) is not None:
            continue
        age = None
        try:
            created = art.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = (now - created).total_seconds() / 86400.0
        except Exception:
            pass
        score, reasons = _score_row(tokens, full_norm, dict(r), age)
        ranked.append({"aid": aid, "art": art, "card": card, "score": score, "reasons": reasons})
    ranked.sort(key=lambda d: d["score"], reverse=True)
    # Lot C : fusion hybride SANS changer l'API — FTS doc + FTS chunks + vecteurs → RRF → rerank.
    # Filtres de droits AVANT tout : seuls les allowed participent aux 3 listes (T-SRCH-16).
    try:
        ranked = _hybrid_rerank(session, original, tokens, full_norm, ranked,
                                set(allowed_ids or set()), scope_kind, scope_id)
    except Exception:
        pass

    page = ranked[offset:offset + limit]
    results = []
    for d in page:
        art, card = d["art"], d["card"]
        kind = (art.kind or "text").lower()
        if kind == "image":
            actions = ["get_document_card", "vision", "doc_ocr"]
        elif kind == "pdf":
            actions = ["get_document_card", "read_document", "doc_ocr"]
        else:
            actions = ["get_document_card", "read_document"]
        try:
            size = None
            from backend.db.models import Blob as _Blob
            blob = session.query(_Blob).filter(_Blob.id == art.blob_id).first()
            size = blob.size_bytes if blob else None
        except Exception:
            size = None
        results.append({
            "artifact_id": art.id,
            "name": art.original_filename or art.display_name or art.id,
            "kind": kind,
            "size_bytes": size,
            "score": d["score"],
            "match_reasons": d["reasons"],
            "card": {"revision": int(card.revision or 0),
                     "summary": card.summary or "",
                     "status": card.analysis_status or card.status or "pending"},
            "available_actions": actions,
        })

    next_cursor = None
    if offset + limit < len(ranked):
        next_cursor = _cursor_encode(fp, offset + limit)
    out: dict = {
        "ok": True,
        "query": original,
        "scope": scope_kind,
        "filters": {"types": types or None, "status": status or "any"},
        "results": results,
        "next_cursor": next_cursor,
        "searched_count": len(allowed_ids or set()),
        "returned_count": len(results),
    }
    if not results:
        # Absence bornée : périmètre + pistes, jamais "le fichier n'existe pas" (T-SRCH-09)
        out["suggestions"] = [
            "retirer le filtre de type",
            "chercher avec un seul mot-clé",
            "ouvrir la liste récente (get_source)",
        ]
    return out

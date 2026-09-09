# backend/search/cards.py — Fiches ArtifactCard versionnées (annexe 11, Lot A)
#
# D'OÙ VIENT CE FICHIER : la fiche facilite la DÉCOUVERTE (« probablement une facture »),
# elle n'est jamais une preuve primaire. Créée minimale à l'import (sans LLM), révisée
# après chaque observation. Immuable par révision : N+1 + bascule is_current.
# OÙ VA QUOI : ensure_card_on_import() appelée en paresseux (self-healing — les Artifact
# sont créés par Rust, Python assure la fiche au 1er contact) ; revise_card_after_observation()
# appelée par observations.record_tool_observation(). fts.index_card() réindexe la fiche active.
# POURQUOI PARESSEUX ET PAS À L'IMPORT RUST : le pont Rust écrit Artifact/Link en rusqlite
# direct ; plutôt que de dupliquer la logique fiche en Rust, Python la garantit à l'usage.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

# Capacités devinées par kind (sans modèle — affinées après observation)
KIND_CAPABILITIES: dict[str, list[str]] = {
    "image": ["vision", "ocr"],
    "pdf": ["ocr", "text_search"],
    "text": ["text_search"],
}


def _keywords_for(filename: str) -> list[str]:
    """Mots-clés initiaux depuis le nom de fichier (sans LLM)."""
    stem = (filename or "").rsplit(".", 1)[0]
    parts = [p.strip().lower() for p in stem.replace("-", "_").replace(" ", "_").split("_")]
    seen: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return seen[:12]


def get_current_card(db, artifact_id: str):
    """Fiche active (is_current=1), repli legacy : la plus récente sans flag."""
    from backend.db.models import ArtifactCard as _Card
    card = (
        db.query(_Card)
        .filter(_Card.artifact_id == artifact_id, _Card.is_current == 1)
        .order_by(_Card.revision.desc())
        .first()
    )
    if card is None:
        card = (
            db.query(_Card)
            .filter(_Card.artifact_id == artifact_id)
            .order_by(_Card.revision.desc())
            .first()
        )
    return card


def ensure_card_on_import(db, artifact) -> object:
    """Garantit une fiche rev 1 (pending) pour un artefact qui n'en a pas.

    `db` = session SQLAlchemy ouverte par l'appelant (il committe).
    `artifact` = ligne Artifact (id, kind, original_filename, display_name).
    Retourne la fiche active (existante ou créée).
    """
    from backend.db.models import ArtifactCard as _Card
    existing = get_current_card(db, artifact.id)
    if existing is not None:
        return existing
    name = artifact.original_filename or artifact.display_name or artifact.id
    kind = (artifact.kind or "text").lower()
    caps = KIND_CAPABILITIES.get(kind, ["text_search"])
    card = _Card(
        id=f"card_{uuid.uuid4().hex[:12]}",
        artifact_id=artifact.id,
        revision=1,
        is_current=1,
        title=name,
        summary="Fichier importé ; contenu non encore analysé.",
        keywords_json=json.dumps(_keywords_for(name), ensure_ascii=False),
        entities_json=json.dumps([], ensure_ascii=False),
        detected_language=None,
        document_type=kind,
        capabilities=",".join(caps),
        capabilities_json=json.dumps(caps, ensure_ascii=False),
        quality=None,
        status="pending",
        analysis_status="pending",
        source_observation_id=None,
    )
    db.add(card)
    db.flush()
    # Indexe la fiche active (une ligne FTS par lien actif — scope.py donne les liens)
    try:
        from backend.search import fts as _fts
        from backend.db.models import ArtifactLink as _Link
        links = (
            db.query(_Link)
            .filter(_Link.artifact_id == artifact.id, _Link.removed_at.is_(None))
            .all()
        )
        for link in links:
            _fts.index_card_row(db, artifact, card, link.scope_kind, link.scope_id,
                                extracted_preview="")
    except Exception:
        pass
    return card


def revise_card_after_observation(db, artifact, observation, excerpt: str = "") -> object:
    """Crée la fiche N+1 après une observation (jamais d'écrasement).

    Résumé prudent : incertitudes explicites (« OCR partiel », « probablement… »).
    Bascule is_current, réindexe, conserve l'historique (T-SRCH-03).
    """
    from backend.db.models import ArtifactCard as _Card
    current = get_current_card(db, artifact.id)
    prev_rev = int(current.revision or 0) if current is not None else 0
    tool = (observation.tool_name or "").lower()
    status = (observation.status or "completed").lower()
    name = artifact.original_filename or artifact.display_name or artifact.id
    kind = (artifact.kind or "text").lower()

    if status == "failed":
        new_status, summary = "failed", f"Analyse {tool} impossible sur ce fichier pour l'instant."
    elif status == "partial":
        new_status, summary = "partial", f"Analyse {tool} partielle : résultat incomplet, à compléter si besoin."
    elif tool in ("doc_ocr",):
        new_status, summary = "analysed", "Texte extrait par OCR ; extrait indexé pour la recherche."
    elif tool in ("vision",):
        new_status, summary = "analysed", "Image décrite par analyse visuelle ; description indexée."
    elif tool in ("read_text",):
        new_status, summary = "analysed", "Contenu texte lu ; extrait indexé pour la recherche."
    else:
        new_status, summary = "indexed", f"Fichier vu via {tool or 'outil'} ; fiche indexée."

    # Mots-clés : conserve les précédents + stem du nom (sans LLM)
    try:
        prev_kw = json.loads((current.keywords_json if current else None) or "[]")
    except Exception:
        prev_kw = []
    keywords: list[str] = []
    for k in list(prev_kw) + _keywords_for(name):
        kl = str(k).lower()
        if kl and kl not in keywords:
            keywords.append(kl)
    caps = KIND_CAPABILITIES.get(kind, ["text_search"])

    now = datetime.now(timezone.utc)
    if current is not None:
        current.is_current = 0
        current.superseded_at = now
        try:
            current.status = current.analysis_status or current.status
        except Exception:
            pass
    card = _Card(
        id=f"card_{uuid.uuid4().hex[:12]}",
        artifact_id=artifact.id,
        revision=prev_rev + 1,
        is_current=1,
        title=(current.title if current and current.title else name),
        summary=summary,
        keywords_json=json.dumps(keywords[:20], ensure_ascii=False),
        entities_json=(current.entities_json if current else None) or json.dumps([], ensure_ascii=False),
        detected_language=(current.detected_language if current else None),
        document_type=kind,
        capabilities=",".join(caps),
        capabilities_json=json.dumps(caps, ensure_ascii=False),
        quality=(current.quality if current else None),
        status=new_status,
        analysis_status=new_status,
        source_observation_id=observation.id,
    )
    db.add(card)
    db.flush()
    try:
        from backend.search import fts as _fts
        from backend.db.models import ArtifactLink as _Link
        links = (
            db.query(_Link)
            .filter(_Link.artifact_id == artifact.id, _Link.removed_at.is_(None))
            .all()
        )
        for link in links:
            _fts.index_card_row(db, artifact, card, link.scope_kind, link.scope_id,
                                extracted_preview=(excerpt or "")[:2000])
    except Exception:
        pass
    return card

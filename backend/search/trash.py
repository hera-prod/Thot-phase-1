# backend/search/trash.py — Corbeille 7 jours + purge GC (annexe 11, Lot B)
#
# D'OÙ VIENT CE FICHIER : T-SRCH-13/14 — un fichier supprimé reste trouvable/restaurable
# 7 jours dans le périmètre autorisé, puis index + blob éligibles au GC sécurisé.
# OÙ VA QUOI : move_to_trash()/restore() posent/retirent removed_at sur les liens du scope
# (± lignes FTS) ; purge() supprime les liens échus puis GC les artefacts sans lien actif
# (cartes, lignes FTS, blob si orphelin + fichier). scope.py exclut déjà removed_at.
# POURQUOI removed_at ET PAS DELETE : la restauration exige l'historique des liens.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TRASH_DAYS = 7


def _now():
    return datetime.now(timezone.utc)


def move_to_trash(session, artifact_id: str, scope_kind: str, scope_id: str) -> int:
    """Corbeille : marque les liens actifs + retire les lignes FTS du scope. Retourne n."""
    from backend.db.models import ArtifactLink as _Link
    from backend.search import fts as _fts
    from sqlalchemy import text as _text
    now = _now()
    rows = (
        session.query(_Link)
        .filter(_Link.artifact_id == artifact_id, _Link.scope_kind == scope_kind,
                _Link.scope_id == scope_id, _Link.removed_at.is_(None))
        .all()
    )
    for link in rows:
        link.removed_at = now
    session.execute(
        _text("DELETE FROM artifact_search WHERE artifact_id = :a AND scope_kind = :sk AND scope_id = :sid"),
        {"a": artifact_id, "sk": scope_kind, "sid": scope_id},
    )
    session.flush()
    return len(rows)


def restore(session, artifact_id: str, scope_kind: str, scope_id: str) -> int:
    """Restauration : réactive les liens + réindexe la fiche active. Retourne n."""
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link
    from backend.search import cards as _cards, fts as _fts
    rows = (
        session.query(_Link)
        .filter(_Link.artifact_id == artifact_id, _Link.scope_kind == scope_kind,
                _Link.scope_id == scope_id, _Link.removed_at.is_not(None))
        .all()
    )
    for link in rows:
        link.removed_at = None
    session.flush()
    art = session.query(_Art).filter(_Art.id == artifact_id).first()
    if art is not None:
        try:
            card = _cards.get_current_card(session, artifact_id)
            if card is None:
                card = _cards.ensure_card_on_import(session, art)
            _fts.ensure_fts(session)
            _fts.index_card_row(session, art, card, scope_kind, scope_id,
                                extracted_preview=_cards._text_preview(session, art))
        except Exception:
            pass
    session.flush()
    return len(rows)


def list_trash(session, scope_kind: str, scope_id: str) -> list[dict]:
    """Contenu de la corbeille du scope (restorable avant échéance)."""
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link
    rows = (
        session.query(_Link, _Art)
        .join(_Art, _Link.artifact_id == _Art.id)
        .filter(_Link.scope_kind == scope_kind, _Link.scope_id == scope_id,
                _Link.removed_at.is_not(None))
        .order_by(_Link.removed_at.desc())
        .all()
    )
    out = []
    for link, art in rows:
        try:
            removed = link.removed_at
            if removed is not None and removed.tzinfo is None:
                removed = removed.replace(tzinfo=timezone.utc)
            eligible = (removed + timedelta(days=TRASH_DAYS)) if removed else None
        except Exception:
            eligible = None
        out.append({"artifact_id": art.id,
                    "name": art.original_filename or art.display_name or art.id,
                    "removed_at": link.removed_at.isoformat() if link.removed_at else None,
                    "purge_eligible_at": eligible.isoformat() if eligible else None})
    return out


def purge_eligible(session, days: int = TRASH_DAYS) -> list:
    """Liens en corbeille depuis plus de `days` jours."""
    from backend.db.models import ArtifactLink as _Link
    cutoff = _now() - timedelta(days=days)
    return session.query(_Link).filter(_Link.removed_at.is_not(None),
                                       _Link.removed_at < cutoff).all()


def purge(session, days: int = TRASH_DAYS) -> dict:
    """Purge : supprime les liens échus puis GC artefacts sans lien actif.

    GC par artefact : lignes FTS + cartes + Artifact, et blob (+fichier) si orphelin.
    Les observations suivent en CASCADE. Retourne le compte rendu.
    """
    from backend.db.models import (Artifact as _Art, ArtifactCard as _Card,
                                   ArtifactLink as _Link, Blob as _Blob)
    from backend.search import fts as _fts
    report = {"links": 0, "artifacts": 0, "blobs": 0}
    for link in purge_eligible(session, days):
        session.delete(link)
        report["links"] += 1
    session.flush()
    orphans = (
        session.query(_Art)
        .outerjoin(_Link, (_Link.artifact_id == _Art.id) & (_Link.removed_at.is_(None)))
        .filter(_Link.id.is_(None))
        .all()
    )
    for art in orphans:
        try:
            _fts.remove_artifact(session, art.id)
        except Exception:
            pass
        try:
            from backend.search import chunks as _chk
            _chk.remove_chunks_for_artifact(session, art.id)
        except Exception:
            pass
        try:
            for card in session.query(_Card).filter(_Card.artifact_id == art.id).all():
                session.delete(card)
        except Exception:
            pass
        blob_id = art.blob_id
        try:
            from backend.db.models import MessageAttachment as _MA
            for ma in session.query(_MA).filter(_MA.artifact_id == art.id).all():
                session.delete(ma)
        except Exception:
            pass
        session.delete(art)
        session.flush()
        report["artifacts"] += 1
        try:
            still_used = session.query(_Art).filter(_Art.blob_id == blob_id).count()
            if not still_used:
                blob = session.query(_Blob).filter(_Blob.id == blob_id).first()
                if blob is not None:
                    try:
                        from backend.config.paths import get_data_dir as _gdd
                        cand = _gdd() / blob.relative_path
                        if cand.exists():
                            cand.unlink()
                    except Exception:
                        pass
                    session.delete(blob)
                    report["blobs"] += 1
        except Exception:
            pass
    session.flush()
    return report

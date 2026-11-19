"""Gestion du claim d'ingestion tokenisé (Phase 3, tâche 3.2).

Ce module fournit les opérations atomiques sur le claim d'un artefact :
- acquérir un bail (acquisition conditionnelle avec TTL),
- libérer un bail (libération conditionnelle au token),
- battre le cœur (heartbeat),
- vérifier la détention.

Le claim est la barrière d'exclusion mutuelle entre processus. Il doit être
acquis et COMMITÉ avant l'appel au coordinateur GPU, car aucune transaction
SQLite ne doit rester ouverte pendant une attente de 120 s.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from backend.db.models import Artifact as _Art
from backend.ingestion.etat import utc_naif

# TTL du claim : 5 minutes. Un worker vivant le renouvelle toutes les 60 s.
TTL_SECONDES = 300
INTERVALLE_BATTEMENT_S = 60


def acquérir_bail(
    session,
    artifact_id: str,
    owner: str,
    *,
    maintenant: datetime | None = None,
) -> str | None:
    """Acquiert atomiquement le claim. Rend le token ou None si déjà détenu.

    L'UPDATE est conditionnel : ``WHERE id=? AND (claim_token IS NULL OR
    claim_at < now - TTL)``. Un seul écrivain SQLite gagne. Aucune lecture
    préalable n'est faite, donc aucune fenêtre de course.

    L'appelant doit COMMITER cette session avant de commencer un travail long.
    """
    seuil = utc_naif(maintenant) - timedelta(seconds=TTL_SECONDES)
    token = uuid.uuid4().hex
    lignes = (
        session.query(_Art)
        .filter(
            _Art.id == artifact_id,
            or_(
                _Art.ingest_claim_token.is_(None),
                _Art.ingest_claim_at.is_(None),
                _Art.ingest_claim_at < seuil,
            ),
        )
        .update(
            {
                "ingest_claim_token": token,
                "ingest_claim_at": utc_naif(maintenant),
                "ingest_claim_owner": owner,
            },
            synchronize_session=False,
        )
    )
    session.commit()
    if not lignes:
        return None
    return token


def libérer_bail(session, artifact_id: str, token: str) -> bool:
    """Libère le claim SEULEMENT si le token est encore le propriétaire.

    Rend True si la libération a eu lieu. Un worker expiré reçoit False :
    il ne doit JAMAIS libérer le bail d'un nouveau propriétaire.
    """
    if not artifact_id or not token:
        return False
    lignes = (
        session.query(_Art)
        .filter(
            _Art.id == artifact_id,
            _Art.ingest_claim_token == token,
        )
        .update(
            {
                "ingest_claim_token": None,
                "ingest_claim_at": None,
                "ingest_claim_owner": None,
            },
            synchronize_session=False,
        )
    )
    session.commit()
    return bool(lignes)


def battre_bail(session, artifact_id: str, token: str) -> bool:
    """Renouvelle le TTL et le heartbeat SI le token est détenu.

    Rend False si le claim est perdu : signal d'arrêt immédiat pour le worker.
    """
    if not artifact_id or not token:
        return False
    lignes = (
        session.query(_Art)
        .filter(
            _Art.id == artifact_id,
            _Art.ingest_claim_token == token,
        )
        .update(
            {
                "ingest_claim_at": utc_naif(),
                "ingest_heartbeat_at": utc_naif(),
            },
            synchronize_session=False,
        )
    )
    session.commit()
    return bool(lignes)


def bail_actif(
    session, artifact_id: str, *, maintenant: datetime | None = None
) -> bool:
    """Un claim FRAIS existe-t-il pour cet artefact ? Lecture seule.

    Sert au single-flight des commandes utilisateur : un autre processus peut
    travailler alors que les ensembles locaux de ce processus sont vides. Un
    claim expiré n'est PAS actif — il est ré-acquérable, et le refuser
    empêcherait toute relance après un crash.
    """
    if not artifact_id:
        return False
    seuil = utc_naif(maintenant) - timedelta(seconds=TTL_SECONDES)
    return (
        session.query(_Art.id)
        .filter(
            _Art.id == artifact_id,
            _Art.ingest_claim_token.isnot(None),
            _Art.ingest_claim_at.isnot(None),
            _Art.ingest_claim_at >= seuil,
        )
        .first()
        is not None
    )


def bail_detenu(session, artifact_id: str, token: str) -> bool:
    """Le token est-il encore le détenteur du claim ? Lecture seule."""
    if not artifact_id or not token:
        return False
    return (
        session.query(_Art.id)
        .filter(_Art.id == artifact_id, _Art.ingest_claim_token == token)
        .first()
        is not None
    )


__all__ = [
    "INTERVALLE_BATTEMENT_S",
    "TTL_SECONDES",
    "acquérir_bail",
    "bail_actif",
    "bail_detenu",
    "battre_bail",
    "libérer_bail",
]

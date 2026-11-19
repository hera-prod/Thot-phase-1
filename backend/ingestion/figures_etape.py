"""Étape B — réclamations, reprises et écritures de la description des figures.

CE MODULE POSSÈDE LE VOCABULAIRE D'ÉTAT de la file `artifact_figures` :

    pending ──claim──> running ──succès──> done
                           │
                           ├── erreur réelle ──> pending (backoff 1/5/15 min)
                           │                         └── 3ᵉ échec ──> limite
                           ├── différé (chat/GPU) ──> pending (sans consommer d'essai)
                           └── représentation périmée ──> superseded

`attempts` compte les EXÉCUTIONS RÉELLES, incrémentées au claim : un processus
tué pendant le travail a bien consommé une tentative. Les différés sont
anti-comptés, parce qu'ils n'ont pas appelé le modèle.

TOUTES LES ÉCRITURES SONT CONDITIONNÉES AU `claim_token`. Un worker dont le
claim a expiré ne doit jamais écrire le résultat d'un autre : chaque UPDATE
porte `id = figure AND claim_token = token AND status = 'running'`.

DÉTERMINISME DU CROP : il est recalculé depuis le PDF au DPI de rendu qui a
produit les bbox de l'étape A (`gate.PDF_RENDER_DPI_CONFIG`, 200 ppp). La
résolution n'est pas un détail : un autre DPI recadrerait une autre zone. Le
crop est persisté sous `derived/figures/<aa>/<sha>.png`, et son empreinte est
la clé de l'observation d'audit.
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_

from backend.db.models import Artifact, ArtifactFigure, ArtifactRepresentation
from backend.ingestion import figures_store
from backend.ingestion.etat import utc_naif

try:
    from backend.config.defaults import FIGURES_LIMITS as _LIMITES

    TTL_CLAIM_S = int(_LIMITES.get("claim_ttl_s", 300))
    HEARTBEAT_S = int(_LIMITES.get("heartbeat_s", 60))
    MAX_ESSAIS = int(_LIMITES.get("max_attempts", 3))
    BACKOFF_MINUTES = tuple(_LIMITES.get("backoff_minutes", (1, 5, 15)))
    SECONDES_DIFFERE = int(_LIMITES.get("defer_seconds", 60))
except Exception:  # pragma: no cover — import sans config (tests purs)
    TTL_CLAIM_S = 300
    HEARTBEAT_S = 60
    MAX_ESSAIS = 3
    BACKOFF_MINUTES = (1, 5, 15)
    SECONDES_DIFFERE = 60


@dataclass(frozen=True)
class FigureClaim:
    """Instantané d'une figure réclamée. Aucun objet ORM ne traverse le worker."""

    figure_id: str
    artifact_id: str
    representation_id: str
    page: int
    region: int
    label: str | None
    score: float | None
    bbox_json: str | None
    attempts: int
    claim_token: str
    epuise: bool = False


def _maintenant() -> datetime:
    return utc_naif()


def _condition_claimable(maintenant: datetime):
    """Prédicat SQL d'une ligne réclamable : pending due, ou running expirée."""
    echeance = maintenant - timedelta(seconds=TTL_CLAIM_S)
    pending_due = and_(
        ArtifactFigure.status == figures_store.STATUT_PENDING,
        or_(
            ArtifactFigure.next_attempt_at.is_(None),
            ArtifactFigure.next_attempt_at <= maintenant,
        ),
    )
    running_expiree = and_(
        ArtifactFigure.status == figures_store.STATUT_RUNNING,
        or_(
            ArtifactFigure.claimed_at.is_(None),
            ArtifactFigure.claimed_at < echeance,
        ),
    )
    return or_(pending_due, running_expiree)


def _snapshot(ligne: ArtifactFigure, token: str, *, epuise: bool) -> FigureClaim:
    return FigureClaim(
        figure_id=ligne.id,
        artifact_id=ligne.artifact_id,
        representation_id=ligne.representation_id,
        page=int(ligne.page),
        region=int(ligne.region),
        label=ligne.label,
        score=ligne.score,
        bbox_json=ligne.bbox_json,
        attempts=int(ligne.attempts or 0),
        claim_token=token,
        epuise=epuise,
    )


def reclamer_figure(
    session, *, maintenant: datetime | None = None
) -> FigureClaim | None:
    """Réclame ATOMIQUEMENT une figure pending ou running expirée.

    La sélection liste des candidats, puis chaque candidat est acquis par un
    UPDATE conditionnel : deux workers peuvent lister la même ligne, un seul
    voit `rowcount == 1`. Le perdant passe au candidat suivant sans écrire.

    Une ligne déjà au plafond d'essais est réclamée UNE dernière fois avec
    `epuise=True`, sans incrémenter `attempts` : le worker la clôt en `limite`
    (et peut ainsi remplacer son placeholder), au lieu de la laisser pending
    pour toujours après un crash.
    """
    instant = _maintenant() if maintenant is None else utc_naif(maintenant)
    candidats = (
        session.query(ArtifactFigure.id)
        .filter(_condition_claimable(instant))
        .order_by(ArtifactFigure.created_at.asc(), ArtifactFigure.id.asc())
        .limit(32)
        .all()
    )
    gagne: FigureClaim | None = None
    for (figure_id,) in candidats:
        ligne = session.get(ArtifactFigure, str(figure_id))
        if ligne is None:
            continue
        epuise = int(ligne.attempts or 0) >= MAX_ESSAIS
        token = f"figclaim_{uuid.uuid4().hex}"
        changement: dict[str, Any] = {
            "status": figures_store.STATUT_RUNNING,
            "claim_token": token,
            "claimed_at": instant,
            "heartbeat_at": instant,
            "updated_at": instant,
        }
        if not epuise:
            changement["attempts"] = ArtifactFigure.attempts + 1
        lignes = (
            session.query(ArtifactFigure)
            .filter(
                ArtifactFigure.id == str(figure_id),
                _condition_claimable(instant),
            )
            .update(changement, synchronize_session="fetch")
        )
        if not lignes:
            session.rollback()
            continue
        figures_store.recalculer_etat_figures(session, ligne.artifact_id, commit=False)
        session.commit()
        # La ligne est rafraîchie après le commit (expire_on_commit peut être vrai
        # dans un sessionmaker de test).
        ligne = session.get(ArtifactFigure, str(figure_id))
        if ligne is None:  # pragma: no cover — supprimée entre claim et lecture
            continue
        gagne = _snapshot(ligne, token, epuise=epuise)
        break
    else:
        session.commit()
    return gagne


def battre_coeur_figure(
    session,
    figure_id: str,
    claim_token: str,
    *,
    maintenant: datetime | None = None,
) -> bool:
    """Renouvelle le claim d'une figure. `False` = bail perdu, arrêt immédiat."""
    instant = _maintenant() if maintenant is None else utc_naif(maintenant)
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure_id,
            ArtifactFigure.claim_token == claim_token,
            ArtifactFigure.status == figures_store.STATUT_RUNNING,
        )
        .update(
            {
                "claimed_at": instant,
                "heartbeat_at": instant,
                "updated_at": instant,
            },
            synchronize_session=False,
        )
    )
    session.commit()
    return bool(lignes)


def enregistrer_crop(
    session,
    figure_id: str,
    claim_token: str,
    *,
    crop_relpath: str,
    crop_sha256: str,
    largeur: int,
    hauteur: int,
) -> bool:
    """Persiste les métadonnées du crop, sous le claim courant. Committe."""
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure_id,
            ArtifactFigure.claim_token == claim_token,
            ArtifactFigure.status == figures_store.STATUT_RUNNING,
        )
        .update(
            {
                "crop_relpath": crop_relpath,
                "crop_sha256": crop_sha256,
                "crop_width": int(largeur),
                "crop_height": int(hauteur),
                "updated_at": _maintenant(),
            },
            synchronize_session=False,
        )
    )
    session.commit()
    return bool(lignes)


def marquer_succes(
    session,
    figure: FigureClaim,
    *,
    description: str,
    description_sha256: str,
    observation_id: str | None,
    applied_revision: int,
    commit: bool = False,
) -> bool:
    """Clôt la figure en `done`, si et seulement si le claim est encore le sien."""
    instant = _maintenant()
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure.figure_id,
            ArtifactFigure.claim_token == figure.claim_token,
            ArtifactFigure.status == figures_store.STATUT_RUNNING,
        )
        .update(
            {
                "status": figures_store.STATUT_DONE,
                "description": description,
                "description_sha256": description_sha256,
                "description_observation_id": observation_id,
                "applied_revision": int(applied_revision),
                "last_error": None,
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "next_attempt_at": None,
                "done_at": instant,
                "updated_at": instant,
            },
            synchronize_session=False,
        )
    )
    if not lignes:
        session.rollback()
        return False
    figures_store.recalculer_etat_figures(session, figure.artifact_id, commit=False)
    if commit:
        session.commit()
    return True


def marquer_limite(
    session,
    figure: FigureClaim,
    cause: str,
    *,
    description: str | None = None,
    description_sha256: str | None = None,
    observation_id: str | None = None,
    commit: bool = False,
) -> bool:
    """Clôt la figure en `limite` terminale, motif nommé. Ne devine rien."""
    instant = _maintenant()
    changement: dict[str, Any] = {
        "status": figures_store.STATUT_LIMITE,
        "last_error": " ".join(str(cause or "").split())[:1000] or "limite sans motif",
        "claim_token": None,
        "claimed_at": None,
        "heartbeat_at": None,
        "next_attempt_at": None,
        "updated_at": instant,
    }
    if description is not None:
        changement["description"] = description
        changement["description_sha256"] = description_sha256
        changement["description_observation_id"] = observation_id
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure.figure_id,
            ArtifactFigure.claim_token == figure.claim_token,
            ArtifactFigure.status == figures_store.STATUT_RUNNING,
        )
        .update(changement, synchronize_session=False)
    )
    if not lignes:
        session.rollback()
        return False
    figures_store.recalculer_etat_figures(session, figure.artifact_id, commit=False)
    if commit:
        session.commit()
    return True


def marquer_echec_reessayable(
    session,
    figure: FigureClaim,
    cause: str,
    *,
    commit: bool = False,
) -> str:
    """Planifie un nouvel essai, ou clôt en limite au plafond. Rend l'état visé.

    `figure.attempts` est déjà incrémenté par le claim. Un échec de la
    1ʳᵉ exécution attend donc la première valeur du backoff (1 min), pas la
    seconde. Au plafond, la ligne devient `limite` : plus rien ne la réclame, et
    le motif reste lisible.
    """
    instant = _maintenant()
    epuise = int(figure.attempts) >= MAX_ESSAIS
    if epuise:
        termine = marquer_limite(session, figure, cause, commit=commit)
        return "limite" if termine else "perdu"
    index = max(0, min(int(figure.attempts) - 1, len(BACKOFF_MINUTES) - 1))
    prochaine = instant + timedelta(minutes=int(BACKOFF_MINUTES[index]))
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure.figure_id,
            ArtifactFigure.claim_token == figure.claim_token,
            ArtifactFigure.status == figures_store.STATUT_RUNNING,
        )
        .update(
            {
                "status": figures_store.STATUT_PENDING,
                "last_error": " ".join(str(cause or "").split())[:1000],
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "next_attempt_at": prochaine,
                "updated_at": instant,
            },
            synchronize_session=False,
        )
    )
    if not lignes:
        session.rollback()
        return "perdu"
    figures_store.recalculer_etat_figures(session, figure.artifact_id, commit=False)
    if commit:
        session.commit()
    return "pending"


def marquer_differe(
    session,
    figure: FigureClaim,
    cause: str,
    *,
    commit: bool = False,
) -> bool:
    """Rend la figure pending sans consommer d'essai (chat/GPU indisponible).

    Le claim avait incrémenté `attempts` : le différé l'anti-compte. Sans cela,
    trois passages à GPU occupé condamneraient une figure que personne n'a
    jamais tenté de décrire.

    `last_error` reste NULL : un différé n'a pas échoué. L'écrire ferait passer
    l'état global en `echec` (voir `recalculer_etat_figures`), alors qu'aucun
    essai n'a produit de faute. La cause vit dans le journal du worker.
    """
    instant = _maintenant()
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure.figure_id,
            ArtifactFigure.claim_token == figure.claim_token,
            ArtifactFigure.status == figures_store.STATUT_RUNNING,
        )
        .update(
            {
                "status": figures_store.STATUT_PENDING,
                "attempts": max(0, int(figure.attempts) - 1),
                "last_error": None,
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "next_attempt_at": instant + timedelta(seconds=SECONDES_DIFFERE),
                "updated_at": instant,
            },
            synchronize_session=False,
        )
    )
    if not lignes:
        session.rollback()
        return False
    figures_store.recalculer_etat_figures(session, figure.artifact_id, commit=False)
    if commit:
        session.commit()
    return True


def marquer_superseded(session, figure: FigureClaim, *, commit: bool = False) -> bool:
    """Retire une figure d'une représentation périmée, sans patch ni description."""
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.id == figure.figure_id,
            ArtifactFigure.claim_token == figure.claim_token,
        )
        .update(
            {
                "status": figures_store.STATUT_SUPERSEDED,
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "next_attempt_at": None,
                "updated_at": _maintenant(),
            },
            synchronize_session=False,
        )
    )
    if not lignes:
        session.rollback()
        return False
    figures_store.recalculer_etat_figures(session, figure.artifact_id, commit=False)
    if commit:
        session.commit()
    return True


def representation_courante(session, representation_id: str):
    """La représentation, si elle est encore courante ; sinon `None`."""
    return (
        session.query(ArtifactRepresentation)
        .filter(
            ArtifactRepresentation.id == representation_id,
            ArtifactRepresentation.is_current == 1,
        )
        .first()
    )


def chemin_blob(session, artifact_id: str) -> str | None:
    """Chemin réel du blob d'un artefact, confiné aux racines de données."""
    from backend.ipc import paths_bridge as _pb

    artefact = session.get(Artifact, artifact_id)
    blob = getattr(artefact, "blob", None) if artefact is not None else None
    relatif = getattr(blob, "relative_path", None)
    if not relatif:
        return None
    for base in _pb.data_dirs():
        candidat = base / relatif
        if candidat.exists():
            return str(candidat)
    return None


def ecrire_crop_png(crop: Any) -> tuple[str, str, int, int]:
    """Persiste un crop en PNG content-addressed. Rend (relpath, sha, l, h).

    Le chemin est `derived/figures/<2 premiers hex>/<sha>.png`, relatif à la
    racine de données : un déménagement de dossier ne casse pas le lien. Le
    même crop réécrit le même fichier (idempotent), et l'écriture passe par un
    temporaire puis `os.replace` pour ne jamais exposer un PNG à moitié écrit.
    """
    from backend.ipc import paths_bridge as _pb

    if crop is None:
        raise ValueError("crop vide : aucune figure à persister")
    racine = _pb.data_dirs()[0]
    dossier = racine / "derived" / "figures"
    tampon = io.BytesIO()
    crop.save(tampon, format="PNG")
    octets = tampon.getvalue()
    sha = hashlib.sha256(octets).hexdigest()
    destination = dossier / sha[:2] / f"{sha}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporaire = destination.parent / f".{sha}.tmp"
    temporaire.write_bytes(octets)
    os.replace(temporaire, destination)
    relpath = destination.relative_to(racine).as_posix()
    return relpath, sha, int(crop.width), int(crop.height)


__all__ = [
    "BACKOFF_MINUTES",
    "FigureClaim",
    "HEARTBEAT_S",
    "MAX_ESSAIS",
    "SECONDES_DIFFERE",
    "TTL_CLAIM_S",
    "battre_coeur_figure",
    "chemin_blob",
    "ecrire_crop_png",
    "enregistrer_crop",
    "marquer_differe",
    "marquer_echec_reessayable",
    "marquer_limite",
    "marquer_succes",
    "marquer_superseded",
    "reclamer_figure",
    "representation_courante",
]

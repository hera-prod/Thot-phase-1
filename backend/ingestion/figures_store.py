"""File persistante des figures d'une représentation (étape A).

L'étape A ne décrit AUCUNE figure : elle pose une ligne `pending` et un
placeholder positionnel dans le Markdown. Ce module est le seul point d'écriture
de `artifact_figures` en étape A, et il tient un choix documenté :

`crop_relpath` reste NULL. Persister un crop dès l'ingestion obligerait à
écrire un fichier au moment où le texte est encore en mémoire, et rendrait la
borne 8/page dépendante du disque. L'étape B recalculera le crop depuis le PDF
(la page et la région suffisent : le layout est déterministe pour une révision
donnée), et c'est seulement là que `crop_relpath`, `crop_sha256`, `crop_width`
et `crop_height` seront renseignés. Le prix assumé : un recalcul de layout en
étape B, jamais un crop fantôme.

Le DÉTERMINISME de ce recalcul dépend du DPI de rendu de la page :
`DPI_RENDU_PDF` (200 ppp par défaut, `attachment_ingest`) a produit les bbox
stockées dans `bbox_json`. L'étape B doit rendre la page au MÊME DPI avant de
recadrer, sinon la région ne désigne plus le même pixels. Le DPI n'est pas
encore persisté dans `blocks_json` : c'est une métadonnée à ajouter sans
migration le jour où Stage B en aura besoin.

UNE REPRÉSENTATION SUPERSEDED NE GARDE PAS DE DETTE. Quand `creer_representation`
ou `basculer_courante` périme une représentation, ses figures passent en
`STATUT_SUPERSEDED` dans la même transaction : elles ne sont plus `pending`, et
la file de l'étape B ne peut plus les réclamer. `figures_state` et
`figures_total` ne comptent que les figures de la représentation COURANTE —
`figures_de_la_representation_courante` est le prédicat réutilisable pour que
l'étape B filtre elle aussi sur `is_current=1`.

`figures_total` compte les lignes réellement créées de la représentation
courante, pas les régions repérées : une borne de 8/page peut écarter des
régions, et `figures_limited` garde ce nombre séparément pour que la perte soit
visible sans gonfler le travail dû.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func

from backend.db.models import Artifact, ArtifactFigure, ArtifactRepresentation
from backend.ingestion import figures_pdf as FIG

# État de l'artefact. `sans` est le défaut honnête pour un artefact legacy.
# `en_attente` n'est écrit que lorsqu'au moins une ligne `artifact_figures`
# pending existe. L'étape B ajoute les états de progression : `en_cours` tant
# qu'une figure est réclamée, puis un terminal (`complet`, `limite`, `echec`).
ETAT_FIGURES_SANS = "sans"
ETAT_FIGURES_EN_ATTENTE = "en_attente"
ETAT_FIGURES_EN_COURS = "en_cours"
ETAT_FIGURES_COMPLET = "complet"
ETAT_FIGURES_LIMITE = "limite"
ETAT_FIGURES_ECHEC = "echec"

STATUT_PENDING = "pending"
STATUT_RUNNING = "running"
STATUT_DONE = "done"
STATUT_LIMITE = "limite"
STATUT_ECHEC = "echec"
STATUT_SUPERSEDED = "superseded"

# Une borne de l'étape A a écarté des régions SANS créer de ligne. Le compte
# est écrit dans `figures_reason` sous forme STRUCTURÉE —
# `{"stage_a_limited": N, "reasons": [...]}` — pour que l'étape B le relise
# sans dépendre d'une phrase française exacte. La regex reste en secours pour
# les lignes écrites avant ce format (compatibilité sans migration).
_REGIONS_ECARTEES_RE = re.compile(r"(\d+)\s+région\(s\)\s+figure\(s\)\s+écartée")


def _encoder_raison(*, stage_a_limited: int, motifs: list[str]) -> str | None:
    """Sérialise la raison en JSON stable, ou None s'il n'y a rien à dire.

    La phrase française reste dans `reasons` : les lecteurs humains et le test
    de l'étape A qui cherche « 2 région » continuent de la trouver, mais le
    COMPTE ne dépend plus de sa formulation.
    """
    propres = [str(motif).strip() for motif in motifs if str(motif or "").strip()]
    if stage_a_limited <= 0 and not propres:
        return None
    return json.dumps(
        {"stage_a_limited": int(stage_a_limited), "reasons": propres},
        ensure_ascii=False,
    )


def _maintenant() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bbox_json(figure: Any) -> str | None:
    """Sérialise une bbox de figure, ou None si elle n'est pas connue. Pure."""
    bbox = getattr(figure, "bbox", None)
    if bbox is None or len(bbox) < 4:
        return None
    try:
        return json.dumps([float(v) for v in bbox[:4]])
    except (TypeError, ValueError):
        return None


def enregistrer_figures_en_attente(
    session,
    artifact_id: str,
    representation_id: str,
    figures: list[Any],
    *,
    extractor_version: str,
) -> list[ArtifactFigure]:
    """Insère les figures `pending` d'une représentation. Idempotent par emplacement.

    `figures` porte des `figures_pdf.FigurePlaceholder`. L'unicité
    `(artifact_id, representation_id, page, region)` est la garantie de base :
    une seconde ingestion de la MÊME représentation ne double pas une figure.
    Les doublons de la liste d'entrée sont écartés ici, avant l'INSERT, pour que
    le message d'erreur ne soit pas une violation de contrainte SQLite.
    """
    if not artifact_id or not representation_id:
        raise ValueError("figure sans artefact ou sans représentation")
    instant = _maintenant()
    vus: set[tuple[int, int]] = set()
    creees: list[ArtifactFigure] = []
    for figure in figures:
        emplacement = (int(figure.page), int(figure.region))
        if emplacement in vus:
            continue
        vus.add(emplacement)
        deja = (
            session.query(ArtifactFigure.id)
            .filter(
                ArtifactFigure.artifact_id == artifact_id,
                ArtifactFigure.representation_id == representation_id,
                ArtifactFigure.page == emplacement[0],
                ArtifactFigure.region == emplacement[1],
            )
            .first()
        )
        if deja is not None:
            continue
        ligne = ArtifactFigure(
            id=f"fig_{uuid.uuid4().hex}",
            artifact_id=artifact_id,
            representation_id=representation_id,
            page=emplacement[0],
            region=emplacement[1],
            label=getattr(figure, "label", None),
            score=getattr(figure, "score", None),
            bbox_json=_bbox_json(figure),
            crop_relpath=None,
            crop_sha256=None,
            crop_width=None,
            crop_height=None,
            status=STATUT_PENDING,
            extractor_version=extractor_version,
            attempts=0,
            created_at=instant,
            updated_at=instant,
        )
        session.add(ligne)
        creees.append(ligne)
    session.flush()
    return creees


def figures_de_la_representation_courante(session, artifact_id: str):
    """Requête des figures de la représentation COURANTE d'un artefact.

    Prédicat réutilisable par l'étape B : une figure appartient à une version
    précise, et seule la version courante fait autorité. Filtrer sur
    `ArtifactFigure.status` sans cette jointure exposerait les figures
    superseded d'une ancienne ingestion.
    """
    return (
        session.query(ArtifactFigure)
        .join(
            ArtifactRepresentation,
            ArtifactFigure.representation_id == ArtifactRepresentation.id,
        )
        .filter(
            ArtifactFigure.artifact_id == artifact_id,
            ArtifactRepresentation.is_current == 1,
        )
        .order_by(ArtifactFigure.page, ArtifactFigure.region)
    )


def marquer_figures_superseded(session, representation_ids: list[str]) -> int:
    """Retire de la file les figures de représentations périmées. Rend le compte.

    Appelée DANS la transaction qui périme les représentations : le statut
    `superseded` et la perte de `is_current` tombent ensemble. Les colonnes de
    claim sont vidées pour qu'aucun worker ne puisse réclamer une dette morte ;
    l'historique de description, lui, reste lisible.
    """
    ids = [
        str(representation_id)
        for representation_id in representation_ids
        if representation_id
    ]
    if not ids:
        return 0
    lignes = (
        session.query(ArtifactFigure)
        .filter(
            ArtifactFigure.representation_id.in_(ids),
            ArtifactFigure.status != STATUT_SUPERSEDED,
        )
        .update(
            {
                "status": STATUT_SUPERSEDED,
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "next_attempt_at": None,
                "updated_at": _maintenant(),
            },
            synchronize_session="fetch",
        )
    )
    session.flush()
    return int(lignes)


def mettre_a_jour_etat_figures(
    session,
    artifact_id: str,
    *,
    figures_limited: int = 0,
    figures_done: int = 0,
) -> str:
    """Écrit l'état et les compteurs de figures d'un artefact. Rend l'état écrit.

    Le total est COMPTÉ depuis la représentation courante, jamais reçu de
    l'appelant : après une ré-ingestion, les figures de l'ancienne version sont
    `superseded` et ne gonflent plus le travail dû. `en_attente` est réservé aux
    artefacts qui ont AU MOINS une ligne courante ; sinon `sans`. Une borne qui a
    écarté des régions sans en laisser aucune ne fabrique donc pas une attente
    vide : elle laisse `figures_limited` visible.
    """
    representation_id = (
        session.query(ArtifactRepresentation.id)
        .filter(
            ArtifactRepresentation.artifact_id == artifact_id,
            ArtifactRepresentation.is_current == 1,
        )
        .scalar()
    )
    if representation_id is None:
        raise ValueError(
            f"représentation courante absente : état de figures non écrit "
            f"({artifact_id!r})"
        )
    figures_total = int(
        session.query(func.count(ArtifactFigure.id))
        .filter(
            ArtifactFigure.artifact_id == artifact_id,
            ArtifactFigure.representation_id == representation_id,
            ArtifactFigure.status != STATUT_SUPERSEDED,
        )
        .scalar()
        or 0
    )
    etat = ETAT_FIGURES_EN_ATTENTE if figures_total > 0 else ETAT_FIGURES_SANS
    motif_borne = (
        f"{figures_limited} région(s) figure(s) écartée(s) par la borne "
        f"{FIG.CROPS_FIGURE_MAX_PAR_PAGE} par page"
        if figures_limited > 0
        else ""
    )
    raison = _encoder_raison(stage_a_limited=int(figures_limited), motifs=[motif_borne])
    lignes = (
        session.query(Artifact)
        .filter(Artifact.id == artifact_id)
        .update(
            {
                "figures_state": etat,
                "figures_done": int(figures_done),
                "figures_total": figures_total,
                "figures_limited": int(figures_limited),
                "figures_reason": raison,
                "figures_updated_at": _maintenant(),
            },
            synchronize_session="fetch",
        )
    )
    if not lignes:
        raise ValueError(
            f"artefact absent : état de figures non écrit ({artifact_id!r})"
        )
    session.flush()
    return etat


def regions_ecartees_depuis_raison(raison: str | None) -> int:
    """Compte des régions écartées par la borne de l'étape A.

    Le format courant est le JSON structuré écrit par ce module : le compte
    survit à toute reformulation de la phrase. Une raison LEGACY (phrase seule)
    reste lue par l'ancienne regex, pour ne pas perdre la perte déjà
    documentée au premier recalcul après mise à jour.
    """
    if not raison:
        return 0
    try:
        charge = json.loads(raison)
    except (TypeError, ValueError):
        charge = None
    if isinstance(charge, dict):
        try:
            return max(0, int(charge.get("stage_a_limited", 0)))
        except (TypeError, ValueError):
            return 0
    motif = _REGIONS_ECARTEES_RE.search(str(raison))
    return int(motif.group(1)) if motif else 0


def recalculer_etat_figures(
    session,
    artifact_id: str,
    *,
    commit: bool = False,
) -> str:
    """Recalcule `figures_state` et les compteurs depuis la représentation COURANTE.

    Les lignes `superseded` de versions périmées ne comptent pas : elles ne sont
    ni du travail dû, ni des échecs. L'état suit une priorité lisible :

      - `en_cours` dès qu'une figure est réclamée (claim vivant) ;
      - `echec` si une figure attend un nouvel essai après un échec ;
      - `en_attente` si une figure reste à décrire sans erreur connue ;
      - `limite` si toutes les figures restantes sont terminales en limite ;
      - `complet` si toutes les figures courantes sont décrites.

    `figures_limited` additionne les régions écartées par la borne de l'étape A
    (relues du champ structuré `stage_a_limited` de `figures_reason`) et les
    figures terminales en limite. Le motif est réécrit en conservant la phrase
    de l'étape A : une perte documentée ne disparaît pas au premier recalcul,
    et la reformuler ne change pas son compte.
    """
    representation_id = (
        session.query(ArtifactRepresentation.id)
        .filter(
            ArtifactRepresentation.artifact_id == artifact_id,
            ArtifactRepresentation.is_current == 1,
        )
        .scalar()
    )
    if representation_id is None:
        raise ValueError(
            f"représentation courante absente : état de figures non recalculé "
            f"({artifact_id!r})"
        )

    lignes = (
        session.query(ArtifactFigure.status, ArtifactFigure.last_error)
        .filter(
            ArtifactFigure.artifact_id == artifact_id,
            ArtifactFigure.representation_id == representation_id,
            ArtifactFigure.status != STATUT_SUPERSEDED,
        )
        .all()
    )
    total = len(lignes)
    done = sum(1 for statut, _ in lignes if statut == STATUT_DONE)
    limites = sum(1 for statut, _ in lignes if statut == STATUT_LIMITE)
    running = sum(1 for statut, _ in lignes if statut == STATUT_RUNNING)
    pending = sum(1 for statut, _ in lignes if statut == STATUT_PENDING)
    pending_erreur = sum(
        1
        for statut, erreur in lignes
        if statut == STATUT_PENDING and (erreur or "").strip()
    )
    echecs = sum(1 for statut, _ in lignes if statut == STATUT_ECHEC)

    if total == 0:
        etat = ETAT_FIGURES_SANS
    elif running > 0:
        etat = ETAT_FIGURES_EN_COURS
    elif pending_erreur > 0 or echecs > 0:
        etat = ETAT_FIGURES_ECHEC
    elif pending > 0:
        etat = ETAT_FIGURES_EN_ATTENTE
    elif limites > 0:
        etat = ETAT_FIGURES_LIMITE
    elif done == total:
        etat = ETAT_FIGURES_COMPLET
    else:
        etat = ETAT_FIGURES_ECHEC

    artefact = session.query(Artifact).filter(Artifact.id == artifact_id).first()
    if artefact is None:
        raise ValueError(
            f"artefact absent : état de figures non recalculé ({artifact_id!r})"
        )
    ecartees = regions_ecartees_depuis_raison(getattr(artefact, "figures_reason", None))
    morceaux: list[str] = []
    if ecartees:
        morceaux.append(
            f"{ecartees} région(s) figure(s) écartée(s) par la borne "
            f"{FIG.CROPS_FIGURE_MAX_PAR_PAGE} par page"
        )
    if limites:
        morceaux.append(f"{limites} figure(s) non décrite(s) (limite)")
    if not morceaux and etat == ETAT_FIGURES_ECHEC:
        derniere = next(
            (
                (erreur or "").strip()
                for statut, erreur in lignes
                if statut in (STATUT_PENDING, STATUT_ECHEC) and (erreur or "").strip()
            ),
            "",
        )
        if derniere:
            morceaux.append(derniere[:400])

    session.query(Artifact).filter(Artifact.id == artifact_id).update(
        {
            "figures_state": etat,
            "figures_done": done,
            "figures_total": total,
            "figures_limited": ecartees + limites,
            "figures_reason": _encoder_raison(
                stage_a_limited=ecartees, motifs=morceaux
            ),
            "figures_updated_at": _maintenant(),
        },
        synchronize_session="fetch",
    )
    session.flush()
    if commit:
        session.commit()
    return etat

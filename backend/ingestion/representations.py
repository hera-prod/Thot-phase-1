"""Représentations documentaires complètes et versionnées (étape A).

D'OÙ VIENT CE MODULE : l'observation d'extraction borne le texte à 16 k
(`CARACTERES_MAX_INDEXES`) — cette borne est une contrainte de PROMPT, pas une
propriété du document. En faire la seule vérité documentaire faisait perdre tout
le texte au-delà de 16 k, et rendait impossible le patch des figures sur une
représentation structurée.

UNE REPRÉSENTATION EST UNE VERSION IMMUABLE. `creer_representation` n'écrase
jamais : elle périme la version courante et insère N+1. `is_current=1` désigne
la version courante, avec un index unique partiel en base (`uq_representations_
current`) : deux ingestions concurrentes ne peuvent pas produire deux courantes.

Le Markdown est stocké INTÉGRALEMENT. `chars_total` et `markdown_sha256` sont
calculés ici, une seule fois, pour que les lecteurs n'aient pas à refaire le
calcul ni à se demander quelle version ils lisent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.db.models import ArtifactRepresentation
from backend.ingestion import figures_store

VERSION_REPRESENTATION = "attachment_ingest/representation-v1"


def _maintenant() -> datetime:
    """UTC naïf, le format que `func.now()` produit. Une seule horloge."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256_texte(markdown: str) -> str:
    """Empreinte stable du Markdown complet. Pure."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _prochaine_revision(session, artifact_id: str) -> int:
    """Numéro de révision suivant pour un artefact. Lecture seule, pure vis-à-vis
    de la session : elle ne modifie rien et ne devine pas de total."""
    derniere = (
        session.query(ArtifactRepresentation.revision)
        .filter(ArtifactRepresentation.artifact_id == artifact_id)
        .order_by(ArtifactRepresentation.revision.desc())
        .first()
    )
    return int(derniere[0]) + 1 if derniere is not None else 1


def creer_representation(
    session,
    artifact_id: str,
    markdown: str,
    blocks: list[dict[str, Any]] | None,
    *,
    source_observation_id: str | None = None,
    extractor_version: str = VERSION_REPRESENTATION,
    commit: bool = False,
) -> ArtifactRepresentation:
    """Crée une nouvelle représentation courante et périme l'ancienne.

    `blocks` est la liste ordonnée de dictionnaires `page`, `region`, `type`,
    `bbox`, `ordre`, `contenu`. `None` signifie « pas de structure de blocs » et
    reste NULL en base — un tableau vide serait un autre fait.

    `source_observation_id` reste NULL en étape A : la représentation est écrite
    AVANT l'observation bornée, qui n'est qu'un audit du texte. La lier
    demanderait une seconde écriture après coup ; l'étape A préfère une
    provenance honnêtement absente à un identifiant deviné.

    La transaction est laissée à l'appelant (`commit=False` par défaut) pour que
    la représentation, ses figures et les compteurs de l'artefact puissent être
    écrits atomiquement. Le `flush` force l'ordre UPDATE → INSERT : l'index
    unique partiel refuse deux courantes, même transitoirement. Les figures de
    l'ancienne courante sont marquées `superseded` dans la MÊME transaction que
    son passage à `is_current=0`.
    """
    if not artifact_id:
        raise ValueError("création de représentation sans artifact_id")
    if not isinstance(markdown, str):
        raise TypeError("le Markdown d'une représentation doit être une chaîne")

    instant = _maintenant()
    perimees = [
        str(row[0])
        for row in session.query(ArtifactRepresentation.id)
        .filter(
            ArtifactRepresentation.artifact_id == artifact_id,
            ArtifactRepresentation.is_current == 1,
        )
        .all()
    ]
    session.query(ArtifactRepresentation).filter(
        ArtifactRepresentation.artifact_id == artifact_id,
        ArtifactRepresentation.is_current == 1,
    ).update(
        {"is_current": 0, "superseded_at": instant, "updated_at": instant},
        synchronize_session="fetch",
    )
    figures_store.marquer_figures_superseded(session, perimees)

    representation = ArtifactRepresentation(
        id=f"rep_{uuid.uuid4().hex}",
        artifact_id=artifact_id,
        revision=_prochaine_revision(session, artifact_id),
        is_current=1,
        markdown=markdown,
        blocks_json=(
            json.dumps(blocks, ensure_ascii=False) if blocks is not None else None
        ),
        chars_total=len(markdown),
        markdown_sha256=sha256_texte(markdown),
        source_text_observation_id=source_observation_id,
        source_extractor_version=extractor_version,
        figures_revision=0,
        created_at=instant,
        updated_at=instant,
    )
    session.add(representation)
    session.flush()
    if commit:
        session.commit()
    return representation


def representation_pour(session, artifact_id: str) -> ArtifactRepresentation | None:
    """Représentation courante d'un artefact, ou None. Lecture seule."""
    return (
        session.query(ArtifactRepresentation)
        .filter(
            ArtifactRepresentation.artifact_id == artifact_id,
            ArtifactRepresentation.is_current == 1,
        )
        .order_by(ArtifactRepresentation.revision.desc())
        .first()
    )


def basculer_courante(
    session,
    artifact_id: str,
    representation_id: str,
    *,
    commit: bool = False,
) -> ArtifactRepresentation:
    """Rend `representation_id` courante pour `artifact_id`, périme les autres.

    Lève si la représentation n'appartient pas à l'artefact : basculer une ligne
    d'un autre document serait un mensonge de provenance, pas une opération
    partielle. `commit=False` laisse la transaction à l'appelant.

    Les figures des représentations qui perdent `is_current=1` sont marquées
    `superseded` dans la même transaction : une bascule ne laisse jamais une
    ancienne version avec des lignes `pending` que l'étape B pourrait réclamer.
    """
    representation = (
        session.query(ArtifactRepresentation)
        .filter(ArtifactRepresentation.id == representation_id)
        .first()
    )
    if representation is None:
        raise ValueError(f"représentation absente : {representation_id!r}")
    if representation.artifact_id != artifact_id:
        raise ValueError(
            f"représentation {representation_id!r} étrangère à l'artefact "
            f"{artifact_id!r}"
        )

    instant = _maintenant()
    perimees = [
        str(row[0])
        for row in session.query(ArtifactRepresentation.id)
        .filter(
            ArtifactRepresentation.artifact_id == artifact_id,
            ArtifactRepresentation.id != representation_id,
            ArtifactRepresentation.is_current == 1,
        )
        .all()
    ]
    session.query(ArtifactRepresentation).filter(
        ArtifactRepresentation.artifact_id == artifact_id,
        ArtifactRepresentation.id != representation_id,
        ArtifactRepresentation.is_current == 1,
    ).update(
        {"is_current": 0, "superseded_at": instant, "updated_at": instant},
        synchronize_session="fetch",
    )
    figures_store.marquer_figures_superseded(session, perimees)
    representation.is_current = 1
    representation.superseded_at = None
    representation.updated_at = instant
    session.flush()
    if commit:
        session.commit()
    return representation


def blocs_depuis_pages(pages: list[Any]) -> list[dict[str, Any]]:
    """Blocs ordonnés de toutes les pages, prêts pour `blocks_json`. Pure.

    L'ordre est celui de lecture déjà appliqué par `markdown_pdf` : `(y0, x0)`
    quand la bbox existe, repli en fin de page sinon. On ne réordonne pas les
    pages : leur ordre vient du parcours physique du document.
    """
    from backend.ingestion import markdown_pdf as MD

    return MD.blocs_du_document(pages)


def blocs_figure(representation: ArtifactRepresentation) -> list[dict[str, Any]]:
    """Blocs d'une représentation relus depuis `blocks_json`. Pure (hors JSON).

    Une représentation sans `blocks_json` rend une liste vide : l'absence de
    structure est réelle, pas une erreur de lecture. Un JSON illisible lève,
    parce qu'une représentation corrompue ne doit pas se faire passer pour une
    représentation sans blocs.
    """
    if not representation.blocks_json:
        return []
    return list(json.loads(representation.blocks_json))


class RepresentationPerimee(RuntimeError):
    """La représentation n'est plus courante : le patch doit être abandonné."""


class PatchFigureImpossible(RuntimeError):
    """Le placeholder d'une figure ne peut pas être remplacé sans deviner."""


def patcher_figure(
    session,
    representation: ArtifactRepresentation,
    numero_page: int,
    index_region: int,
    nouveau_contenu: str,
    *,
    commit: bool = False,
) -> int:
    """Enrichit la représentation COURANTE d'une figure, sans créer de version.

    Le patch porte sur les DEUX vues : le Markdown et `blocks_json`. Les
    compteurs (`figures_revision`, `markdown_sha256`, `chars_total`) sont
    recalculés ici, dans la même écriture, pour qu'aucun lecteur ne voie un
    Markdown et une empreinte qui se contredisent.

    Une représentation périmée lève `RepresentationPerimee` AVANT toute
    écriture : un worker qui a réclamé une figure juste avant une ré-ingestion
    ne doit pas patcher une version morte. Un placeholder introuvable ou ambigu
    lève `PatchFigureImpossible` : l'appelant documente une limite, il ne
    devine pas la position.
    """
    from backend.ingestion import markdown_pdf as _md

    if int(getattr(representation, "is_current", 0) or 0) != 1:
        raise RepresentationPerimee(
            f"représentation {representation.id!r} n'est plus courante"
        )

    markdown_actuel = representation.markdown or ""
    blocs = blocs_figure(representation) if representation.blocks_json else None
    try:
        markdown_nouveau, blocs_nouveaux = _md.remplacer_bloc_figure(
            markdown_actuel,
            blocs,
            numero_page,
            index_region,
            nouveau_contenu,
        )
    except _md.BlocFigureIntrouvable as exc:
        raise PatchFigureImpossible(str(exc)) from exc

    representation.markdown = markdown_nouveau
    representation.blocks_json = json.dumps(blocs_nouveaux, ensure_ascii=False)
    representation.figures_revision = int(representation.figures_revision or 0) + 1
    representation.markdown_sha256 = sha256_texte(markdown_nouveau)
    representation.chars_total = len(markdown_nouveau)
    representation.updated_at = _maintenant()
    session.flush()
    if commit:
        session.commit()
    return int(representation.figures_revision)

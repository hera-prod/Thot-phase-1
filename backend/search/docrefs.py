# backend/search/docrefs.py — Compteur doc_N par discussion
# Base partagée + filtre conversation : artefacts globaux (art_xxx), labels locaux (doc_N).
# Le label public est toujours résolu dans le scope de la conversation ; un artifact_id
# technique ne doit jamais devenir une référence visible.
from __future__ import annotations

import re
import uuid
from collections.abc import Iterable

_PUBLIC_ID_RE = re.compile(r"doc_[1-9]\d*\Z")
_TECHNICAL_PREFIXES = ("art_", "sti_")

# Nombre de reprises de `_allouer` sur refus de l'index unique. Borné à dessein :
# deux allocations concurrentes se reprennent UNE fois chacune ; un quatrième
# échec signifie que le registre est verrouillé ou saturé, pas qu'il y a une
# course. On le dit alors, au lieu de boucler sur quelque chose qu'on ne
# corrigera pas en réessaayant.
_TENTATIVES_ALLOCATION = 4


def is_public_id(value: object) -> bool:
    """Retourne vrai uniquement pour un label public ``doc_N`` alloué par le registre."""
    return isinstance(value, str) and _PUBLIC_ID_RE.fullmatch(value.strip()) is not None


def is_technical_id(value: object) -> bool:
    """Les identifiants techniques ne sont jamais des IDs publics."""
    return isinstance(value, str) and value.strip().lower().startswith(_TECHNICAL_PREFIXES)


def _conversation_link(db, conv_id: str, artifact_id: str):
    """Ligne de lien de l'artefact dans la conversation, quel que soit son état."""
    from backend.db.models import ArtifactLink

    return db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "conversation",
        ArtifactLink.scope_id == conv_id,
        ArtifactLink.artifact_id == artifact_id,
    ).first()


def _active_link(db, conv_id: str, artifact_id: str):
    """Lien de scope ACTIF : un lien retiré (corbeille) ne porte plus rien."""
    link = _conversation_link(db, conv_id, artifact_id)
    return link if link is not None and link.removed_at is None else None


def _live_artifact(db, artifact_id: str):
    from backend.db.models import Artifact

    return db.query(Artifact).filter(
        Artifact.id == artifact_id,
        Artifact.deleted_at.is_(None),
    ).first()


def _workspace_de_la_conversation(db, conv_id: str) -> str | None:
    """L'espace dont fait partie cette conversation, ou ``None``.

    Lu en base, jamais reçu d'un appelant : c'est le même refus que
    `scope.resolve_allowed_artifacts`, et pour la même raison — un identifiant
    d'espace reçu du client ne prouverait rien.
    """
    from backend.db.models import Conversation

    if not conv_id:
        return None
    row = db.query(Conversation.workspace_id).filter(Conversation.id == conv_id).first()
    if row is None:
        return None
    value = row[0]
    return str(value).strip() if value else None


def _depot_link(db, workspace_id: str | None, artifact_id: str):
    """Lien actif du dépôt de cet espace pour cet artefact, ou ``None``.

    ``removed_at`` fait foi : un fichier retiré du dépôt par l'utilisateur cesse
    d'être nommable dans le tour même, sans qu'aucune ligne ne soit à effacer.
    """
    from backend.db.models import ArtifactLink

    if not workspace_id or not artifact_id:
        return None
    link = db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "workspace",
        ArtifactLink.scope_id == workspace_id,
        ArtifactLink.artifact_id == artifact_id,
    ).first()
    return link if link is not None and link.removed_at is None else None


def _labelisable(db, conv_id: str, artifact_id: str) -> bool:
    """Ce fichier porte-t-il un nom dans CETTE conversation ?

    Deux origines, parce que le contrat en compte deux : un fichier joint à la
    conversation, ou un fichier que l'utilisateur a versé au dépôt de l'espace
    dont elle fait partie (`01_architecture_registre_index.md`, matrice de
    permissions). Un dépôt sans espace de rattachement ne nomme rien.
    """
    if _active_link(db, conv_id, artifact_id) is not None:
        return True
    return _depot_link(db, _workspace_de_la_conversation(db, conv_id), artifact_id) is not None


def _ensure_conversation_link(db, conv_id: str, artifact_id: str) -> None:
    """Répare un lien de scope MANQUANT sans changer le label public déjà attribué.

    Un lien retiré n'est jamais ressuscité : le retrait est une décision de
    l'utilisateur, l'allocation ne l'annule pas.
    """
    from backend.db.models import ArtifactLink

    if _conversation_link(db, conv_id, artifact_id) is not None:
        return
    db.add(ArtifactLink(
        id=f"lnk_{uuid.uuid4().hex}",
        artifact_id=artifact_id,
        scope_kind="conversation",
        scope_id=conv_id,
        role="attachment",
    ))


def allocate_doc_label(db, conv_id: str, artifact_id: str) -> str:
    """Retourne le ``doc_N`` local, ou l'allocation suivante pour cette conversation.

    L'artefact sous-jacent peut être partagé : son label public ne l'est pas. La ligne
    DocRef porte donc la paire (conversation, artefact), et son identifiant de base est
    interne. Une suppression conserve le numéro ; une réactivation ne le recycle pas.

    Allouer, c'est déclarer l'artefact dans le périmètre de la conversation : un
    artefact supprimé ou retiré (lien ``removed_at``) lève au lieu de récupérer un
    ``doc_N``. Le retrait se défait via la corbeille, jamais par une lecture de registre.
    """
    from backend.db.models import DocRef

    conv_id = str(conv_id or "").strip()
    artifact_id = str(artifact_id or "").strip()
    if not conv_id or not artifact_id:
        raise ValueError("conv_id et artifact_id requis")
    if _live_artifact(db, artifact_id) is None:
        raise ValueError(f"artefact {artifact_id} absent ou supprimé : aucun label public")
    link = _conversation_link(db, conv_id, artifact_id)
    if link is not None and link.removed_at is not None:
        raise ValueError(
            f"artefact {artifact_id} retiré de la conversation {conv_id} : aucun label public")
    _ensure_conversation_link(db, conv_id, artifact_id)
    return _allouer(db, conv_id, artifact_id)


def _prochain_index(db, conv_id: str) -> int:
    """Le plus grand numéro déjà porté par CETTE conversation, plus un.

    Une SUPPOSITION, jamais une garantie : deux allocations concurrentes lisent
    le même `max` et proposent le même numéro. Ce que la proposition devient
    dépend entièrement de `uq_doc_refs_conv_label`, qui tranche.
    """
    from backend.db.models import DocRef

    rows = db.query(DocRef).filter(DocRef.conv_id == conv_id).all()
    indexes: list[int] = []
    for row in rows:
        try:
            indexes.append(int(row.doc_index or 0))
        except (TypeError, ValueError):
            continue
    return max(indexes, default=0) + 1


def _collision_de_label(db, conv_id: str, label: str) -> bool:
    """Ce `IntegrityError` vient-il de l'unicité du label, ou d'autre chose ?

    Une lecture, pas une analyse du message SQLite : on demande à la base si le
    numéro existe déjà POUR CETTE CONVERSATION. La réponse sépare les deux
    mondes. Un label déjà pris est la course qu'on sait reprendre ; une clé
    dupliquée sur une autre contrainte (clé primaire, liaison de clé étrangère)
    est un bug, et la relire en boucle le maquillerait en lenteur au lieu de
    le faire remonter.
    """
    from backend.db.models import DocRef

    row = db.query(DocRef.id).filter(
        DocRef.conv_id == conv_id,
        DocRef.doc_label == label,
    ).first()
    return row is not None


def _allouer(db, conv_id: str, artifact_id: str) -> str:
    """Le numéro suivant pour CETTE conversation, ou celui déjà attribué.

    Séparé de `allocate_doc_label` parce que le rattachement et le nommage sont
    deux décisions distinctes : un fichier du dépôt reçoit un numéro sans
    recevoir de lien de conversation. Le numéro n'est pas recyclé : une
    suppression conserve le sien, une réactivation le récupère.

    L'allocation est ATOMIQUE grâce à `uq_doc_refs_conv_label`, pas grâce au
    `max + 1`. `max + 1` est une supposition que deux écritures concurrentes
    peuvent partager ; l'index unique, lui, ne laisse qu'un gagnant. Donc : on
    propose, on laisse la base trancher, et sur refus on relit `max` et on
    repropose. Le nombre de reprises est BORNÉ — au-delà, ce n'est plus une
    course, c'est quelque chose d'autre, et on le dit au lieu de boucler.

    `BEGIN IMMEDIATE` aurait été l'autre voie, impossible ici : la session
    appartient à l'appelant (`attachments`, `brief`), qui tient déjà une
    transaction ouverte, et on ne rétrograde pas un `BEGIN` paresseux en
    `BEGIN IMMEDIATE` depuis une fonction. Le dépôt de l'appelant ferait
    committer son `allocate_doc_label` au passage. Même arbitrage que
    `memoire.py` et `effect_ledger.py` : la contrainte décide, le code relit et
    rejoue.
    """
    from sqlalchemy.exc import IntegrityError

    from backend.db.models import DocRef

    existing = db.query(DocRef).filter(
        DocRef.conv_id == conv_id,
        DocRef.artifact_id == artifact_id,
    ).first()
    if existing is not None:
        if existing.removed_at is not None:
            existing.removed_at = None
        db.flush()
        return str(existing.doc_label)

    for _ in range(_TENTATIVES_ALLOCATION):
        next_index = _prochain_index(db, conv_id)
        label = f"doc_{next_index}"
        try:
            # SAVEPOINT : l'INSERT refusé ne doit pas abîmer la transaction de
            # l'appelant. Un rollback nu jetterait avec lui le lien créé par
            # `_ensure_conversation_link`, et l'appelant perdrait son rattachement
            # pour une simple course sur un numéro.
            with db.begin_nested():
                db.add(DocRef(
                    id=f"dref_{uuid.uuid4().hex}",
                    conv_id=conv_id,
                    doc_label=label,
                    doc_index=next_index,
                    artifact_id=artifact_id,
                ))
                db.flush()
        except IntegrityError:
            if not _collision_de_label(db, conv_id, label):
                # Pas le label : une autre contrainte, donc un défaut réel.
                # Remonter tout de suite, sans retry.
                raise
            continue
        return label
    raise RuntimeError(
        f"aucun doc_N libre pour la conversation {conv_id} après "
        f"{_TENTATIVES_ALLOCATION} tentatives : le registre est saturé ou verrouillé"
    )


def allocate_depot_label(db, conv_id: str, artifact_id: str) -> str:
    """Alloue un ``doc_N`` à un fichier du dépôt, vu depuis une discussion de l'espace.

    Ce label est celui d'un EMPLOI, pas d'un rattachement : il dit « cette
    discussion peut nommer ce fichier », pas « ce fichier appartient à cette
    discussion ». La différence se voit au cycle de vie. Un fichier joint porte un
    lien `conversation` et disparaît avec sa discussion ; celui-ci n'en reçoit
    aucun, donc rien ne le suit à la suppression d'un chat d'espace. Lui en
    donner un ferait de lui un fichier de conversation — et le contrat veut
    qu'un fichier de dépôt survive à ses discussions.

    L'allocation est paresseuse : elle n'a lieu que pour un fichier que
    l'orchestrateur réclame effectivement. Un dépôt de cinquante fichiers ne
    coûte donc rien tant qu'on ne le consulte pas.
    """
    from backend.db.models import DocRef

    conv_id = str(conv_id or "").strip()
    artifact_id = str(artifact_id or "").strip()
    if not conv_id or not artifact_id:
        raise ValueError("conv_id et artifact_id requis")
    if _live_artifact(db, artifact_id) is None:
        raise ValueError(f"artefact {artifact_id} absent ou supprimé : aucun label public")

    workspace_id = _workspace_de_la_conversation(db, conv_id)
    if not workspace_id:
        raise ValueError(f"la conversation {conv_id} n'appartient à aucun espace")
    if _depot_link(db, workspace_id, artifact_id) is None:
        raise ValueError(
            f"artefact {artifact_id} absent du dépôt de l'espace : aucun label public")

    existing = db.query(DocRef).filter(
        DocRef.conv_id == conv_id,
        DocRef.artifact_id == artifact_id,
    ).first()
    if existing is not None:
        if existing.removed_at is not None:
            existing.removed_at = None
        db.flush()
        return str(existing.doc_label)

    return _allouer(db, conv_id, artifact_id)


def public_id_for_artifact(db, conv_id: str, artifact_id: str) -> str | None:
    """Résout un ``doc_N`` local, jamais un ``art_...``/``sti_...``.

    Le lien actif et l'artefact vivant sont vérifiés avant toute allocation. Une absence
    de scope est donc un refus (``None``), pas un label deviné depuis l'identifiant interne.
    """
    conv_id = str(conv_id or "").strip()
    artifact_id = str(artifact_id or "").strip()
    if not conv_id or not artifact_id or is_public_id(artifact_id):
        return None
    if not _labelisable(db, conv_id, artifact_id) or _live_artifact(db, artifact_id) is None:
        return None
    if _active_link(db, conv_id, artifact_id) is not None:
        return allocate_doc_label(db, conv_id, artifact_id)
    return allocate_depot_label(db, conv_id, artifact_id)


def public_ids_for_artifacts(db, conv_id: str, artifact_ids: Iterable[object]) -> dict[str, str]:
    """Construit la mapping interne ``artifact_id → public_id`` pour le scope fourni."""
    mapping: dict[str, str] = {}
    for raw_artifact_id in artifact_ids or ():
        artifact_id = str(raw_artifact_id or "").strip()
        if not artifact_id or artifact_id in mapping:
            continue
        public_id = public_id_for_artifact(db, conv_id, artifact_id)
        if public_id is not None:
            mapping[artifact_id] = public_id
    return mapping


def resolve_doc_label(db, conv_id: str, doc_label: str) -> str | None:
    """Résout ``doc_N`` vers l'artefact interne seulement s'il est dans le scope."""
    from backend.db.models import DocRef

    conv_id = str(conv_id or "").strip()
    doc_label = str(doc_label or "").strip()
    if not is_public_id(doc_label):
        return None
    row = db.query(DocRef).filter(
        DocRef.conv_id == conv_id,
        DocRef.doc_label == doc_label,
        DocRef.removed_at.is_(None),
    ).first()
    if row is None:
        return None
    artifact_id = str(row.artifact_id or "").strip()
    if not artifact_id or not _labelisable(db, conv_id, artifact_id):
        return None
    if _live_artifact(db, artifact_id) is None:
        return None
    return artifact_id


def list_for_conv(db, conv_id: str) -> list[dict]:
    """Liste les labels publics actifs, dans l'ordre local de la conversation.

    Les liens sans DocRef sont récupérés de façon déterministe : une recherche ne doit
    jamais retomber sur ``artifact_id`` technique simplement parce que le backfill manque.
    """
    from backend.db.models import ArtifactLink, DocRef

    conv_id = str(conv_id or "").strip()
    if not conv_id:
        return []
    links = db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "conversation",
        ArtifactLink.scope_id == conv_id,
        ArtifactLink.removed_at.is_(None),
    ).order_by(ArtifactLink.created_at.asc(), ArtifactLink.id.asc()).all()
    scoped_ids_in_order = [str(link.artifact_id or "").strip() for link in links]
    scoped_ids_in_order = [artifact_id for artifact_id in scoped_ids_in_order if artifact_id]
    scoped_ids = set(scoped_ids_in_order)
    known_rows = db.query(DocRef).filter(
        DocRef.conv_id == conv_id,
        DocRef.removed_at.is_(None),
    ).order_by(DocRef.doc_index.asc()).all()
    known_by_artifact = {
        str(row.artifact_id or "").strip(): row
        for row in known_rows
        if is_public_id(row.doc_label)
    }
    for artifact_id in scoped_ids_in_order:
        if artifact_id not in known_by_artifact and _live_artifact(db, artifact_id) is not None:
            try:
                allocate_doc_label(db, conv_id, artifact_id)
            except Exception:
                continue
    rows = db.query(DocRef).filter(
        DocRef.conv_id == conv_id,
        DocRef.removed_at.is_(None),
    ).order_by(DocRef.doc_index.asc()).all()
    out: list[dict] = []
    for row in rows:
        artifact_id = str(row.artifact_id or "").strip()
        label = str(row.doc_label or "").strip()
        if not is_public_id(label):
            continue
        # Un fichier du dépôt entre dans cette liste par son seul DocRef : on ne
        # le NUMÉROTE pas au passage. Le numéro se mérite — il se donne quand une
        # discussion choisit ce fichier, pas parce qu'on a ouvert un espace qui en
        # contient cinquante. C'est ce qui garde le contexte de l'orchestrateur
        # constant quelle que soit la taille du dépôt.
        if artifact_id not in scoped_ids and not _labelisable(db, conv_id, artifact_id):
            continue
        if _live_artifact(db, artifact_id) is None:
            continue
        out.append({
            "doc_label": label,
            "public_id": label,
            "artifact_id": artifact_id,
        })
    db.flush()
    return out


def backfill_from_links(db, conv_id: str) -> list[dict]:
    """Rattrape les DocRef manquants à partir des liens de conversation actifs."""
    from backend.db.models import ArtifactLink

    conv_id = str(conv_id or "").strip()
    if not conv_id:
        return []
    links = db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "conversation",
        ArtifactLink.scope_id == conv_id,
        ArtifactLink.removed_at.is_(None),
    ).order_by(ArtifactLink.created_at.asc(), ArtifactLink.id.asc()).all()
    for link in links:
        artifact_id = str(link.artifact_id or "").strip()
        if not artifact_id or _live_artifact(db, artifact_id) is None:
            continue
        try:
            allocate_doc_label(db, conv_id, artifact_id)
        except Exception:
            continue
    try:
        db.commit()
    except Exception:
        db.rollback()
        return []
    return list_for_conv(db, conv_id)

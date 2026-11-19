# backend/search/cards.py — Fiches ArtifactCard versionnées (annexe 11, Lot A)
#
# D'OÙ VIENT CE FICHIER : la fiche facilite la DÉCOUVERTE (« probablement une facture »),
# elle n'est jamais une preuve primaire. Créée minimale à l'import (sans LLM), révisée
# après chaque observation. Immuable par révision : N+1 + bascule is_current.
# OÙ VA QUOI : ensure_card_on_import() appelée en paresseux (self-healing — les Artifact
# sont créés par Electron, Python assure la fiche au 1er contact) ; revise_card_after_observation()
# appelée par observations.record_tool_observation(). reindexer_carte() réindexe la fiche
# active ET statue sur elle, à partir du verdict que fts arbitre.
# POURQUOI PARESSEUX ET PAS À L'IMPORT Electron : le pont Electron écrit Artifact/Link en rusqlite
# direct ; plutôt que de dupliquer la logique fiche en Electron, Python la garantit à l'usage.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from backend.db.models import ArtifactCard

# Capacités devinées par kind (sans modèle — affinées après observation)
KIND_CAPABILITIES: dict[str, list[str]] = {
    "image": ["vision", "ocr"],
    "pdf": ["ocr", "text_search"],
    "text": ["text_search"],
}


def _keywords_for(filename: str) -> list[str]:
    """Mots-clés initiaux depuis le nom de fichier (sans LLM)."""
    stem = (filename or "").rsplit(".", 1)[0]
    parts = [
        p.strip().lower() for p in stem.replace("-", "_").replace(" ", "_").split("_")
    ]
    seen: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return seen[:12]


TEXT_PREVIEW_CHARS = 2000  # aperçu indexé pour kind=text (Lot B : texte, sections)
TEXT_PREVIEW_MAX_BYTES = 200_000  # garde-fou : pas d'indexation de gros blobs


def _text_preview(db, artifact) -> str:
    """Aperçu indexable pour les artefacts texte (lu privé, jamais de chemin exposé)."""
    try:
        if (artifact.kind or "").lower() != "text":
            return ""
        from backend.db.models import Blob as _BlobT
        from backend.config.paths import get_data_dir as _gddT

        blob = db.query(_BlobT).filter(_BlobT.id == artifact.blob_id).first()
        if blob is None or (blob.size_bytes or 0) > TEXT_PREVIEW_MAX_BYTES:
            return ""
        cand = _gddT() / blob.relative_path
        if not cand.exists():
            return ""
        return cand.read_text(encoding="utf-8", errors="replace")[:TEXT_PREVIEW_CHARS]
    except Exception:
        return ""


def appliquer_verdict_indexation(card, verdict: dict) -> None:
    """Le VERDICT d'indexation décide de l'ÉTAT d'une fiche. Ici, et nulle part ailleurs.

    Un module qui juge des OCTETS n'a rien à dire de l'état d'une fiche : il ne voit
    ni ce qui a été demandé à ce fichier, ni si une autre extraction a réussi depuis.
    `fts.py` juge donc et rend un verdict ; ce module, qui détient la fiche, en tire
    le statut et le résumé. Une seule frontière, une seule décision.

    TROIS VERDICTS, UNE SEULE CONSÉQUENCE. `binaire_refuse` condamne — statut ET
    motif. `tronque` ne condamne pas : la borne de 2000 caractères est un choix de
    conception, pas une faute, et condamner chaque gros PDF en `failed` viderait
    l'état de toute information. `indexe` ne touche à rien.

    Le motif va dans le RÉSUMÉ, pas seulement dans le statut : « failed » seul dit
    qu'il y a un problème, et l'utilisateur ne saurait ni lequel ni ce qu'il peut y
    faire. Ici, il sait que le fichier qu'il a déposé n'a pas été lu, et pourquoi. Le
    résumé précédent est écrasé volontairement : il décrivait une ingestion réussie
    qui n'a jamais eu lieu, et le garder serait conserver le mensonge qu'on vient de
    supprimer.
    """
    if card is None:
        return
    from backend.search import fts as _fts

    if verdict.get("extract") != _fts.EXTRACT_BINAIRE:
        return
    card.analysis_status = "failed"
    card.status = "failed"
    card.summary = verdict.get("reason") or ""


def reindexer_carte(
    db, artifact, card, scope_kind: str, scope_id: str, extracted_preview: str = ""
) -> dict:
    """Verdict → fiche → ligne d'index, dans cet ordre imposé. Rend le verdict.

    L'ordre n'est pas un détail d'écriture : le motif d'un refus doit être dans le
    RÉSUMÉ de la fiche AVANT l'insertion, sinon la ligne d'index porterait l'ancien
    résumé — celui d'une ingestion réussie qui n'a pas eu lieu — et le refus
    deviendrait introuvable par la recherche.

    C'est le SEUL chemin de production qui statue sur une fiche. `fts.index_card_row`
    existe pour qui veut indexer sans décider (tests, outils de mesure) ; l'utiliser
    en production produirait des fiches qui disent « analysé » sur un contenu jamais
    lu, exactement le défaut que la Bible interdit.
    """
    from backend.search import fts as _fts

    verdict = _fts.arbitrer_extrait(extracted_preview)
    appliquer_verdict_indexation(card, verdict)
    _fts.ecrire_ligne_index(
        db, artifact, card, scope_kind, scope_id, verdict["preview"]
    )
    verdict["card_status"] = (
        (card.analysis_status or card.status or "pending") if card is not None else None
    )
    return verdict


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


def ensure_card_on_import(db, artifact) -> ArtifactCard:
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
        from backend.db.models import ArtifactLink as _Link

        links = (
            db.query(_Link)
            .filter(_Link.artifact_id == artifact.id, _Link.removed_at.is_(None))
            .all()
        )
        for link in links:
            reindexer_carte(
                db,
                artifact,
                card,
                link.scope_kind,
                link.scope_id,
                extracted_preview=_text_preview(db, artifact),
            )
    except Exception:
        pass
    return card


def revise_card_after_observation(
    db, artifact, observation, excerpt: str = ""
) -> object:
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
        new_status, summary = (
            "failed",
            f"Analyse {tool} impossible sur ce fichier pour l'instant.",
        )
    elif status == "partial":
        new_status, summary = (
            "partial",
            f"Analyse {tool} partielle : résultat incomplet, à compléter si besoin.",
        )
    elif tool in ("doc_ocr",):
        new_status, summary = (
            "analysed",
            "Texte extrait par OCR ; extrait indexé pour la recherche.",
        )
    elif tool in ("vision",):
        new_status, summary = (
            "analysed",
            "Image décrite par analyse visuelle ; description indexée.",
        )
    elif tool in ("read_text",):
        new_status, summary = (
            "analysed",
            "Contenu texte lu ; extrait indexé pour la recherche.",
        )
    else:
        new_status, summary = (
            "indexed",
            f"Fichier vu via {tool or 'outil'} ; fiche indexée.",
        )

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
        entities_json=(current.entities_json if current else None)
        or json.dumps([], ensure_ascii=False),
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
        from backend.db.models import ArtifactLink as _Link

        links = (
            db.query(_Link)
            .filter(_Link.artifact_id == artifact.id, _Link.removed_at.is_(None))
            .all()
        )
        for link in links:
            reindexer_carte(
                db,
                artifact,
                card,
                link.scope_kind,
                link.scope_id,
                extracted_preview=(excerpt or "")[:TEXT_PREVIEW_CHARS],
            )
    except Exception:
        pass
    return card


def revise_card_on_rename(db, artifact, nouveau_nom: str) -> object:
    """Crée la fiche N+1 après un renommage (jamais d'écrasement).

    `depot.md` §5.2 : « L'utilisateur peut renommer un fichier du dépôt. Cette
    action modifie le nom affiché par Thot, sans altérer son contenu ni les
    informations extraites qui en dépendent. » — et `espaces.md:102` : « Un
    fichier renommé conserve son contenu, ses représentations et son identité
    fonctionnelle, sauf décision explicite de réingestion. »

    CONCRÈTEMENT, CETTE FONCTION NE FAIT PAS PLUS QUE DÉPLACER L'ÉTIQUETTE :
    le titre porte le nouveau nom, les mots-clés GARDENT les précédents et
    gagnent les stems du nouveau — l'ancien nom continue donc de raccrocher la
    fiche — et TOUT le reste (résumé, statut, entités, langue, type, fichier de
    source) est COPIÉ tel quel. Aucune nouvelle extraction : les informations
    extraites ne dépendent PAS du nom.

    L'APERÇU INDEXÉ EST PRÉSERVÉ, et c'est le point délicat. `reindexer_carte`
    statue sur la fiche à partir du verdict sur un extrait ; avec un aperçu
    reconstruit par `_text_preview` (qui ne lit que kind=text), une image
    déjà indexée pourrait passer une condamnation fausse. On RELIT donc
    l'aperçu DÉJÀ INDEXÉ, ligne par ligne de portée, et `_text_preview` ne sert
    que de repli quand une portée active n'a aucune ligne — le cas d'un index
    incomplet, traité comme l'import l'aurait traité.
    """
    from backend.db.models import ArtifactCard as _Card, ArtifactLink as _Link
    from sqlalchemy import text as _sqla_text

    current = get_current_card(db, artifact.id)
    if current is None:
        # Aucune fiche à réviser : le premier contact crée la rev 1 (self-
        # healing, même chemin que l'observation) — puis elle est révisée
        # comme les autres, pour porter le NOUVEAU nom et pas celui de
        # l'import.
        current = ensure_card_on_import(db, artifact)

    name = (nouveau_nom or "").strip() or (
        current.title if current.title else artifact.id
    )
    kind = (artifact.kind or "text").lower()

    try:
        prev_kw = json.loads(current.keywords_json or "[]")
    except Exception:
        prev_kw = []
    keywords: list[str] = []
    for k in list(prev_kw) + _keywords_for(name):
        kl = str(k).lower()
        if kl and kl not in keywords:
            keywords.append(kl)

    # L'aperçu DÉJÀ INDEXÉ, par portée : chaque ligne artifact_search porte
    # l'extrait qui a été jugé, et le rejouer évite de redécider ce qui a déjà
    # été tranché.
    extraits: dict[tuple[str, str], str] = {}
    try:
        rows = (
            db.execute(
                _sqla_text(
                    "SELECT scope_kind, scope_id, extracted_preview FROM artifact_search "
                    "WHERE artifact_id = :a"
                ),
                {"a": artifact.id},
            )
            .mappings()
            .all()
        )
        for row in rows:
            extraits[(row["scope_kind"], row["scope_id"])] = str(
                row["extracted_preview"] or ""
            )
    except Exception:
        extraits = {}

    now = datetime.now(timezone.utc)
    prev_rev = int(current.revision or 0)
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
        title=name[:200],
        # Copiés TEL QUEL : un renommage n'est ni une observation ni une
        # réingestion — décider d'un résumé ici serait écrire une information
        # que personne n'a mesurée.
        summary=current.summary or "",
        status=current.status or "indexed",
        analysis_status=current.analysis_status or current.status or "pending",
        keywords_json=json.dumps(keywords[:20], ensure_ascii=False),
        entities_json=current.entities_json or json.dumps([], ensure_ascii=False),
        detected_language=current.detected_language,
        document_type=current.document_type or kind,
        capabilities=current.capabilities,
        capabilities_json=current.capabilities_json,
        quality=current.quality,
        source_observation_id=current.source_observation_id,
    )
    db.add(card)
    db.flush()
    try:
        links = (
            db.query(_Link)
            .filter(_Link.artifact_id == artifact.id, _Link.removed_at.is_(None))
            .all()
        )
        for link in links:
            extrait = extraits.get((link.scope_kind, link.scope_id))
            if extrait is None:
                extrait = _text_preview(db, artifact)
            reindexer_carte(
                db,
                artifact,
                card,
                link.scope_kind,
                link.scope_id,
                extracted_preview=extrait,
            )
    except Exception:
        pass
    return card

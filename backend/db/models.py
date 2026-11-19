# Modèles SQLAlchemy — 25 tables métier Phase 1 (fresh start, pas d'héritage v0.8)
# + schema_migrations + 2 tables FTS (artifact_search, chunk_search) créées en DDL.
# CASCADE + INDEX pour perfs, JSON pour args toolcalls
# Typage Mapped[] (SQLAlchemy 2) : les attributs d'instance sont des valeurs (str, int…),
# pas des Column — le LSP résout correctement les accès après requête.
from datetime import datetime
from typing import Any

from sqlalchemy import (
    String,
    Text,
    Integer,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship, declarative_base
from backend.image_engine.runner.memprof import RunRecord as _MEMPROF_RUN_RECORD

Base = declarative_base()  # base pour tous les modèles


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[str | None] = mapped_column(String, nullable=True)  # chat | agent
    # C17:248 — optionnel, « pour ne pas forcer la migration de tout l'historique ».
    # Zéro ou un espace. `ondelete=SET NULL` et non CASCADE : supprimer un espace
    # rétrograde ses conversations en chats simples au lieu de les détruire
    # (décision E-SUPPR, alignée sur 06_classe_cycle_de_vie.md:60-64).
    workspace_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, onupdate=func.now(), nullable=True
    )
    # Corbeille de la conversation (cycle_de_vie_des_informations.md §4.1) :
    # `delete_conversation` ne détruit plus, il MARQUE. `deleted_at` non nul
    # retire la discussion des vues et usages actifs (listes, lecture,
    # périmètre d'espace) ; la ligne reste 7 jours (annexe10:146, décision
    # E-SUPPR appliquée aux espaces), puis `balayer_corbeille`
    # (backend/services/corbeille.py) purge les données propres restantes.
    # `NULL` ≡ active : pas de colonne `state` séparée — une seule source de
    # vérité pour « en corbeille ou pas ».
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    conv_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(
        String, nullable=True
    )  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=True)
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=True)
    # Trace orchestrateur (bulle UI, repliée par défaut) — remplie par main.py depuis
    # le retour de run_agent_loop, AVANT le terminal (C08). NULL = pas de trace (legacy).
    orchestrator_thinking: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # contenus itérations (pas labels)
    brief: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # brief rendu pour l'UI
    brief_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # payload exact injecté dans le générateur
    plan_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # dernier execution_plan (intent/outil/raison)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="messages"
    )
    toolcalls: Mapped[list["ToolCall"]] = relationship(
        "ToolCall", back_populates="message", cascade="all, delete-orphan"
    )
    sources: Mapped[list["Source"]] = relationship(
        "Source", back_populates="message", cascade="all, delete-orphan"
    )
    __table_args__ = (Index("idx_messages_conv_created", "conv_id", "created_at"),)


class ToolCall(Base):
    __tablename__ = "toolcalls"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    msg_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # vision | doc_ocr | search_documents | get_document_card | read_document | …
    args: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )  # {artifact_id, query…} — jamais base64
    result: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # complet pour audit (borné 16k à l'écriture)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    logical_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String, default="received", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    message: Mapped["Message"] = relationship("Message", back_populates="toolcalls")
    __table_args__ = (
        Index("idx_toolcalls_msg_id", "msg_id"),
        Index("idx_toolcalls_run", "run_id"),
        UniqueConstraint("run_id", "tool_call_id", name="uq_toolcalls_run_call"),
    )


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    msg_id: Mapped[str] = mapped_column(
        String, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String, nullable=True)  # attachment
    path: Mapped[str] = mapped_column(String, nullable=True)  # legacy absolute path
    hash: Mapped[str] = mapped_column(String, nullable=True)  # legacy sha256
    # Enrichi Réponses.md n°7-8 : cache persistant + manifest
    media_kind: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # image | pdf | text | markdown
    original_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    relative_path: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # media/{sha256}.ext
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    parent_source_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    analysis_version: Mapped[int | None] = mapped_column(
        Integer, server_default="0", nullable=True
    )
    message: Mapped["Message"] = relationship("Message", back_populates="sources")
    __table_args__ = (
        Index("idx_sources_msg_id", "msg_id"),
        Index("idx_sources_sha256", "sha256"),
        Index("idx_sources_conversation_created", "conversation_id", "created_at"),
    )


class Run(Base):
    """Run C09 : tentative d'exécution (chat ou production), idempotence C11."""

    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(
        String, primary_key=True
    )  # run_id == request_id Phase 1
    conv_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    request_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(
        String, default="chat", nullable=True
    )  # chat | production
    status: Mapped[str] = mapped_column(
        String, default="queued", nullable=True
    )  # queued|running|cancelling|completed|cancelled|failed|interrupted
    fingerprint: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # empreinte canonique C11
    output_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(String, nullable=True)
    runtime_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int] = mapped_column(
        Integer, default=0, nullable=True
    )  # dernier seq émis persisté C08
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )
    __table_args__ = (
        Index("idx_runs_conv_status", "conv_id", "status"),
        Index("idx_runs_request", "request_id"),
    )


class ImageEffect(Base):
    __tablename__ = "image_effects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    logical_action_id: Mapped[str] = mapped_column(String, nullable=False)
    output_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)
    effective_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="claimed", nullable=False)
    output_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    staged_path: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )
    __table_args__ = (
        UniqueConstraint(
            "run_id", "logical_action_id", "output_index", name="uq_image_effect_action"
        ),
        Index("idx_image_effect_status", "status"),
        Index("idx_image_effect_run", "run_id"),
        Index(
            "uq_image_effect_active_fingerprint",
            "fingerprint",
            unique=True,
            sqlite_where=text(
                "status IN ('claimed', 'running', 'uncertain', 'published')"
            ),
        ),
    )


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_key: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_event_outbox_key"),
        Index("idx_event_outbox_pending", "status", "created_at"),
    )


class ContextSnapshot(Base):
    """Snapshot C06 : projection compacte non destructive, versionnée."""

    __tablename__ = "context_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_through_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # borne C06
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    __table_args__ = (Index("idx_snapshots_conv_rev", "conversation_id", "revision"),)


# Annexe 10 : pipeline pièces jointes — stockage durable Blob/Artifact/ArtifactLink
class Blob(Base):
    """Octets dédupliqués adressés par hash (annexe 10)."""

    __tablename__ = "blobs"
    id: Mapped[str] = mapped_column(
        String, primary_key=True
    )  # blob_id = sha256 ou uuid
    sha256: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    relative_path: Mapped[str] = mapped_column(
        String, nullable=False
    )  # ex. blobs/ab/…bin (sharding 2 hex) ; variantes selon le producteur — la lecture suit la valeur stockée
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_detected: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    state: Mapped[str] = mapped_column(String, default="available", nullable=True)
    __table_args__ = (Index("idx_blobs_sha256", "sha256"),)


class Artifact(Base):
    """Ressource logique adressable (annexe 10)."""

    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # artifact_id
    blob_id: Mapped[str] = mapped_column(
        String, ForeignKey("blobs.id", ondelete="CASCADE"), nullable=False
    )
    # `image | pdf | sheet | text` : ce sont les quatre `kind` que rend
    # `sniff_mime` (backend/ipc/attachments.py), seul chemin qui écrit cette
    # colonne — `sheet` est donc PERSISTÉ, pas un simple mot de reader.py.
    # `text` est produit depuis l'ouverture du dépôt aux `.txt`/`.md`
    # (fiche texte.md §1.2) et par l'ingestion des sources web
    # (backend/ingestion/sources_web.py). La liste tenue à jour, et ce
    # qu'elle a coûté, est en
    # backend/ingestion/attachment_ingest.py
    # (`KIND_IMAGE`/`KIND_PDF`/`KIND_TABLEUR`/`KIND_TEXTE`).
    kind: Mapped[str] = mapped_column(String, nullable=False)
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    # 1.6 — L'ÉTAT D'INGESTION EST DANS L'ARTEFACT, et il est NOT NULL.
    #
    # Le défaut mesuré : l'état était DÉDUIT de l'absence d'observation « completed ».
    # Un artefact jamais traité était alors rigoureusement indiscernable d'un artefact
    # dont l'extraction avait échoué — et « un fichier mal traité ne doit jamais être
    # considéré silencieusement comme correctement traité » (Bible 2.6) devenait
    # indécidable : la seule question à poser était « y a-t-il une observation ? ».
    #
    # Une colonne NOT NULL rend cette question sans objet : chaque artefact a un état,
    # y compris celui qu'on n'a jamais touché. Son seul défaut est de pouvoir se
    # DÉGRADER — un oubli d'écriture la laisserait dans son état précédent — et il est
    # borné par trois choses : un point d'écriture unique (`backend/ingestion/etat.py`),
    # un balayage de reprise qui rejoue ce qui traîne, et des détecteurs qui lisent cet
    # état au lieu de le ré-inférer. Une table dédiée aurait le défaut inverse, plus
    # grave : l'absence de ligne y EST l'absence d'information, donc indécidable.
    #
    # `server_default` ET `nullable=False` ensemble : SQLite refuse `ADD COLUMN NOT NULL`
    # sans défaut, et un défaut SANS `nullable=False` laisserait passer les écritures
    # brutes qui oubliassent la colonne — dont celles d'un pont écrit hors de l'ORM.
    # Le littéral est répété ici plutôt qu'importé de `backend.ingestion.etat` : les
    # modèles sont la RACINE du graphe d'imports, et un modèle qui importe un module
    # d'ingestion ne pourrait plus être importé par lui.
    #
    # LE DÉFAUT EST `inconnu`, ET C'EST DÉLIBÉRÉ. Le dépôt écrit `depose` lui-même
    # (`attachments_prepare`), parce que là il sait ce qui s'est passé. Une écriture
    # qui omet la colonne, elle, ne le sait pas — et le dire `depose` serait déclarer
    # une dette qui n'est peut-être pas là. Le défaut couvre donc aussi le cas de la
    # migration : `ALTER TABLE ADD COLUMN ... DEFAULT 'inconnu'` remplit les lignes
    # DÉJÀ présentes, ce qui dispense d'un backfill séparé et rend l'opération atomique.
    ingest_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'inconnu'"), default="inconnu"
    )
    # deposited|ingesting|extracted|truncated|partial|binary_rejected|empty|failed|obsolete|unknown —
    # le vocabulaire et ses invariants sont tenus par `backend/ingestion/etat.py`, qui
    # est le seul module autorisé à écrire ces colonnes.
    ingest_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # motif lisible, jamais un code
    ingest_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # naïf UTC
    # L'état d'AVANT le `en_cours` courant. `marquer` ne l'écrase que si l'état
    # courant n'est pas déjà `en_cours` ; à l'échec, il restaure `extrait`,
    # `tronque` ou `partiel` au lieu de détruire un texte déjà lisible.
    ingest_previous_state: Mapped[str | None] = mapped_column(String, nullable=True)
    # Backoff de reprise (Phase 2, tâche 2.2). `ingest_attempts` compte les
    # tentatives de CETTE dette ; il est remis à zéro au succès complet et
    # plafonne à `PLAFOND_ESSAIS` (backend/ingestion/etat.py). Les deux dates
    # sont UTC naïf, comme `ingest_updated_at`.
    ingest_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    ingest_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    ingest_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # Battement de cœur de l'extraction en cours (Phase 2, tâche 2.5). Distinct
    # de `ingest_updated_at`, qui date le dernier CHANGEMENT d'état : un OCR de
    # vingt pages ne change pas d'état, mais il prouve qu'il vit.
    ingest_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # Claim d'ingestion (Phase 3, tâche 3.2) : jeton propriétaire, date d'acquisition,
    # propriétaire lisible. TTL 5 min, heartbeat 60 s. Un claim expiré est ré-acquérable.
    # NULL = pas de claim actif. La colonne n'est PAS unique globalement (un claim expiré
    # doit rester lisible pour le fencing), mais l'unicité est portée par la logique
    # d'acquisition conditionnelle.
    ingest_claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    ingest_claim_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingest_claim_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    # Figures d'un artefact — état et compteurs (étape A de l'ingestion en deux
    # étages). `sans` est le défaut honnête pour tout artefact legacy : aucune
    # figure n'a été repérée, et aucune dette n'est inventée. `en_attente` n'est
    # écrit que lorsqu'au moins une ligne `artifact_figures` pending existe.
    #
    # Ces colonnes vivent dans `artifacts` et non dans une table dédiée pour la
    # même raison que `ingest_state` : chaque artefact a un état, y compris celui
    # qu'on n'a jamais touché. Un défaut NOT NULL rend l'absence indiscernable
    # d'un « sans figure » — et c'est exactement ce qu'on veut : une figure
    # jamais demandée n'est pas une figure en échec.
    figures_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'sans'"), default="sans"
    )
    figures_done: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    figures_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    figures_limited: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    figures_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    figures_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    blob: Mapped["Blob"] = relationship("Blob")
    __table_args__ = (
        Index("idx_artifacts_blob", "blob_id"),
        # Le balayage de reprise demande « les états non terminés » : sans
        # cet index, il relit toute la table à chaque démarrage.
        Index("idx_artifacts_ingest_state", "ingest_state"),
    )


class ArtifactLink(Base):
    """Rattachement explicite à conversation/espace/bibliothèque (annexe 10)."""

    __tablename__ = "artifact_links"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    scope_kind: Mapped[str] = mapped_column(
        String, nullable=False
    )  # conversation | workspace | library
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, default="attachment", nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    artifact: Mapped["Artifact"] = relationship("Artifact")
    __table_args__ = (
        Index("idx_links_scope", "scope_kind", "scope_id"),
        Index("idx_links_artifact", "artifact_id"),
        # GC (Phase 4) : « aucun lien actif ou récent » se lit par artefact et
        # par date de retrait. Sans cet index, chaque balayage rejoint toute la
        # table `artifact_links` pour chaque artefact candidat. Additif, même
        # nom que la migration v19, pour que `create_all` et la migration ne
        # créent pas deux index pour le même couple.
        Index("idx_links_artifact_removed", "artifact_id", "removed_at"),
    )


class MessageAttachment(Base):
    """Pièce jointe exacte d'un message (annexe 10)."""

    __tablename__ = "message_attachments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    artifact: Mapped["Artifact"] = relationship("Artifact")
    __table_args__ = (
        Index("idx_msg_att_msg", "message_id"),
        Index("idx_msg_att_artifact", "artifact_id"),
    )


class ArtifactCard(Base):
    """Fiche révisable d'un artefact (annexe 10, étendue annexe 11 Lot A).

    Une seule fiche active par artefact (is_current=1). Immuable par révision :
    toute mise à jour crée N+1 et bascule le flag, jamais d'écrasement.
    """

    __tablename__ = "artifact_cards"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # card_...
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=True)
    is_current: Mapped[int] = mapped_column(
        Integer, default=1, nullable=True
    )  # 1 = fiche active (unique par artefact)
    # Contenu prudent (aide à la découverte, jamais preuve primaire)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # liste normalisée JSON
    entities_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # noms, lieux, projets, dates JSON
    detected_language: Mapped[str | None] = mapped_column(String, nullable=True)
    document_type: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # invoice, note, photo…
    capabilities: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # compat : miroir CSV de capabilities_json
    capabilities_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # ["ocr", "text_search", "vision"]
    quality: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # compat annexe 10
    status: Mapped[str | None] = mapped_column(
        String, default="pending", nullable=True
    )  # compat : miroir de analysis_status
    analysis_status: Mapped[str | None] = mapped_column(
        String, default="pending", nullable=True
    )  # pending|indexed|partial|analysed|failed|stale
    source_observation_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # observation ayant produit cette révision
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_cards_artifact", "artifact_id"),
        Index(
            "idx_cards_current_status", "is_current", "analysis_status", "document_type"
        ),
    )


class Observation(Base):
    """Résultat versionné d'un outil (annexe 10, étendue annexe 11 Lot A).

    Immuable après écriture : toute nouvelle analyse = nouvelle ligne.
    Réutilisable SSI même artefact + même sha + capacité compatible + question couverte.
    """

    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # obs_...
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str] = mapped_column(
        String, nullable=False
    )  # vision|doc_ocr|read_text|get_source|search_documents
    tool_version: Mapped[str | None] = mapped_column(String, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # sha(blob+outil+modèle+params+query)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String, default="completed", nullable=True
    )  # completed|partial|failed|cancelled
    result_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # extrait structuré borné (pour le prompt)
    payload_ref: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # contenu de référence borné (hors prompt par défaut)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    __table_args__ = (
        Index("idx_obs_artifact", "artifact_id"),
        Index("idx_obs_artifact_created", "artifact_id", "created_at"),
    )


class ArtifactChunk(Base):
    """Fragment borné d'un document pour la recherche hybride (annexe 11, Lot C).

    Découpage déterministe (800 car., chevauchement 100) ; contexte optionnel
    (contextualisation LLM à l'ingestion, Phase 5) ; vecteur local en table dédiée.
    """

    __tablename__ = "artifact_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # chk_...
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    # Provenance de la représentation qui a produit ce fragment (étape A). NULL
    # pour les fragments legacy : on ne fabrique pas une origine qu'on n'a pas
    # observée. `figures_revision` et `text_sha256` permettront au patch futur
    # des figures de ne réindexer que ce qui a réellement changé.
    representation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    figures_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    text_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    card_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # 50-100 tokens situant le fragment
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    __table_args__ = (Index("idx_chunks_artifact", "artifact_id", "ordinal"),)


class ArtifactRepresentation(Base):
    """Représentation complète et versionnée d'un artefact (ingestion en deux étages).

    Le défaut corrigé : l'observation stockait le texte borné à 16 k, et cette
    borne devenait la seule vérité documentaire. Une représentation est une
    version IMMUABLE du Markdown complet ; `is_current=1` désigne la version
    courante (même idiome que `ArtifactCard` et `MemoryEntry`). Toute nouvelle
    ingestion crée N+1 et périme l'ancienne, jamais d'écrasement : le patch futur
    des figures devra savoir sur quelle version il écrit.

    `blocks_json` porte les blocs ordonnés (`page`, `region`, `type`, `bbox`,
    `ordre`, `contenu`) pour permettre ce patch positionnel sans reparser le
    Markdown. Il est NULL pour les représentations qui n'ont pas de structure de
    blocs ; l'absence est réelle, jamais un tableau vide déguisé.
    """

    __tablename__ = "artifact_representations"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # rep_...
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    blocks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    chars_total: Mapped[int] = mapped_column(Integer, nullable=False)
    markdown_sha256: Mapped[str] = mapped_column(String, nullable=False)
    source_text_observation_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("observations.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    figures_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        # Une SEULE représentation courante par artefact. Index partiel : les
        # lignes `is_current=0` (versions périmées) ne se heurtent pas.
        Index(
            "uq_representations_current",
            "artifact_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
        Index("idx_representations_artifact_revision", "artifact_id", "revision"),
    )


class ArtifactFigure(Base):
    """Figure repérée dans une représentation — file de travail de l'étape B.

    L'étape A ne décrit AUCUNE figure : elle pose une ligne `pending` et un
    placeholder positionnel dans le Markdown. La description LFM-VL est
    différée, et cette table est le seul état qui dit ce qui reste à faire.
    `status`, `claim_token`, `heartbeat_at` et `next_attempt_at` forment le même
    vocabulaire de claim que les autres files du dépôt ; `crop_relpath` reste
    NULL en étape A (Stage B recalcule le crop depuis le PDF, documenté dans
    `figures_store`).
    """

    __tablename__ = "artifact_figures"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # fig_...
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    representation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("artifact_representations.id", ondelete="CASCADE"),
        nullable=False,
    )
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    crop_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    crop_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    crop_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crop_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'"), default="pending"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    description_observation_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        # Une figure est identifiée par sa représentation, sa page et sa région :
        # deux ingestions d'une même représentation ne doivent pas empiler deux
        # lignes pour le même emplacement.
        UniqueConstraint(
            "artifact_id",
            "representation_id",
            "page",
            "region",
            name="uq_figures_artifact_representation_page_region",
        ),
        Index("idx_figures_status_next", "status", "next_attempt_at"),
        Index("idx_figures_artifact_status", "artifact_id", "status"),
        Index("idx_figures_crop_sha256", "crop_sha256"),
    )


class DocRef(Base):
    """Compteur doc_N par discussion (brouillon : Notation d'id).

    Base partagée + filtre conversation : le blob/artefact reste global dédupliqué
    par sha, mais chaque discussion voit ses fichiers en doc_1, doc_2, … dans
    l'ordre d'arrivée. Jamais réattribué après suppression (removed_at).
    """

    __tablename__ = "doc_refs"
    id: Mapped[str] = mapped_column(
        String, primary_key=True
    )  # doc_{conv8}_{n} technique, label affiché = doc_N
    conv_id: Mapped[str] = mapped_column(String, nullable=False)
    doc_label: Mapped[str] = mapped_column(String, nullable=False)  # doc_1, doc_2, …
    doc_index: Mapped[int] = mapped_column(Integer, nullable=False)  # N séquentiel
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    # `uq_doc_refs_conv_label` est l'un SEUL endroit où « deux fichiers ne
    # peuvent pas porter le même doc_N dans une discussion » devient une vérité
    # de la base. Partout ailleurs, l'unicité n'est qu'une promesse de code
    # (`docrefs._allouer`), donc rien n'empêche deux écritures concurrentes de
    # lire le même `max(doc_index)` et d'allouer le même numéro — c'est déjà
    # arrivé, et ça corrompt les citations de l'orchestrateur (`doc_2` qui
    # désigne deux fichiers différents).
    #
    # SANS filtre sur `removed_at`, délibérément : un `WHERE removed_at IS NULL`
    # libérerait le numéro d'un document supprimé, alors que le contrat veut
    # l'inverse — « jamais recyclé, une suppression garde son numéro ». Un index
    # partiel casserait ce contrat en silence, et seulement pour les suppressions.
    __table_args__ = (
        Index("idx_docrefs_conv", "conv_id", "doc_index"),
        Index("idx_docrefs_conv_artifact", "conv_id", "artifact_id"),
        Index("uq_doc_refs_conv_label", "conv_id", "doc_label", unique=True),
    )


class WebSnapshot(Base):
    """Source web figée, c'est-à-dire un état de CONTENU daté (C16).

    Une ligne n'est pas une adresse : c'est le contenu que l'URL a rendu à une date
    donnée. `public_id` dérive de l'URL *et* du hash du contenu — C16 : « un hash
    d'URL ne remplace pas le hash du contenu » — donc la même adresse relue et
    inchangée retombe sur la MÊME ligne pendant qu'un contenu différent en crée une
    nouvelle, sans réécrire l'ancienne. Deux versions d'une page qui change sont
    deux lignes, jamais une écrasée.

    `conv_id` + `scope_kind`/`scope_id` disent pour QUI ce snapshot est
    ré-exploitable : la portée décide de la réutilisation, jamais le hash.
    """

    __tablename__ = "web_snapshots"
    # id technique interne `web_snap_{uuid hex}` : jamais rendu. Seul `public_id`
    # (`web_<12 hex>`) traverse l'IPC, parce qu'il doit signifier la même chose
    # dans toutes les conversations.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String, unique=True, index=True, nullable=False
    )  # web_<12 hex>, unique
    # Pas de clé étrangère vers `conversations` : le cache doit pouvoir être purgé
    # après la disparition de la conversation, pas mourir avec elle.
    conv_id: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )  # portée = clé du cache ré-exploitable
    # URL demandée/finale : égales aujourd'hui (zéro redirection suivie, C18), distinctes demain.
    requested_url: Mapped[str] = mapped_column(String, nullable=False)
    final_url: Mapped[str] = mapped_column(String, nullable=False)
    # Horodatages NAÏFS et locaux, comme le `datetime.now()` du fetcher : c'est la
    # seule horloge commune à la lecture et à l'écriture de la fraîcheur.
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # « si connue » : NULL ≠ date
    status: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # statut HTTP de la lecture
    # Validateurs disponibles + Cache-Control : ce qui permet une revalidation
    # conditionnelle au lieu d'un nouveau téléchargement complet.
    etag: Mapped[str | None] = mapped_column(String, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String, nullable=True)
    cache_control: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # source de la fraîcheur (RFC 9111 §4.2.2)
    # Type MIME et titre : sans eux, le texte extrait ne dit pas ce qu'il a été
    # extrait DE, et une page re-servie ne pourrait pas être rendue comme telle.
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content_sha256: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )  # hash du CONTENU (C16)
    text: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # jamais renvoyé en entier : c'est au rendu de le borner
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    scope_kind: Mapped[str] = mapped_column(
        String, nullable=False
    )  # conversation | global
    scope_id: Mapped[str] = mapped_column(
        String, nullable=False
    )  # la conversation, ou la chaîne globale
    query_key: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )  # WebQuery.cache_key() qui a produit ce snapshot
    source_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # general | news | academic | official
    proposed_by: Mapped[str] = mapped_column(
        String, nullable=False
    )  # moteur qui a proposé l'URL, jamais qui l'a certifiée
    reused_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # LA fonctionnalité : combien de fois ré-exploité
    __table_args__ = (
        Index("idx_web_snapshots_conv_query", "conv_id", "query_key"),
        Index("idx_web_snapshots_conv_url", "conv_id", "requested_url"),
    )


# ---------------------------------------------------------------------------
# Espaces et mémoire — C17, docs/espaces/01_classe_espace.md, 05_classe_memoire.md
# ---------------------------------------------------------------------------


class Workspace(Base):
    """Espace : conteneur de rattachement. Il ne possède aucun octet.

    Sa seule fonction est de regrouper des conversations et des ressources sous
    une portée de lecture et de partage. Les fichiers qu'il contient ne lui
    appartiennent pas : ils appartiennent à leur conversation ou à son dossier
    utilisateur, et « la propriété d'un fichier ne change jamais »
    (`01_architecture_registre_index.md:80-87`).

    Aucun chemin absolu et aucun paramètre secret ici (C18:270,
    `backend_espaces.md:20-24`) : les blobs vivent dans la racine gérée et la
    portée se déduit des `ArtifactLink`, jamais d'une colonne de chemin.
    """

    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # ws_<12 hex>
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Image de carte : référence à un artefact déjà dans la racine gérée comme
    # les images de la galerie (C13 — pas de table dupliquée).
    image_artifact_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    # `active` | `trashed`. La corbeille dure sept jours comme une conversation
    # (annexe10:146) ; à l'expiration la purge rétrograde les conversations en
    # chats simples au lieu de les détruire (décision E-SUPPR).
    state: Mapped[str] = mapped_column(String, default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, onupdate=func.now(), nullable=True
    )
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Compteur de tours pour le rafraichissement automatique de la mémoire.
    auto_memory_turn_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default=text("0")
    )
    # Dernier tour réussi : c'est le déclencheur d'inactivité (le seuil se mesure
    # depuis cet instant, pas depuis un registre volatil).
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Borne des messages déjà traités par l'extraction : tout ce qui est plus
    # récent reste à traiter, tout ce qui est plus ancien a déjà été lu.
    memory_last_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    __table_args__ = (Index("idx_workspaces_state_created", "state", "created_at"),)


class MemoryEntry(Base):
    """Fiche de mémoire d'espace — versionnée, jamais réécrite en place.

    Une ligne EST une version. `entry_key` est l'identité logique stable de la
    fiche et ne change pas ; `is_current=1` désigne la version Courante
    (même idiome que `ArtifactCard`). Une correction, une fusion ou une
    invalidation crée N+1 et bascule le drapeau au lieu d'écraser
    (`phase_5:66`, `backend_espaces.md:144-149`).

    `status` transitions : `candidate` → `accepted` (décision humaine) → `stale`.
    `stale` n'est pas une création, c'est une transition subie : une source
    supprimée, une portée révoquée ou une contradiction nouvelle y pousse la
    fiche toute seule (C17:256).

    Les colonnes JSON ne sont pas suivies en place (aucun `MutableList` dans le
    projet) : toute modification assigne une liste neuve, jamais `append`.
    """

    __tablename__ = "memory_entries"
    id: Mapped[str] = mapped_column(
        String, primary_key=True
    )  # mem_<12 hex> — une version
    entry_key: Mapped[str] = mapped_column(
        String, nullable=False
    )  # fiche logique, stable sur les versions
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_current: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )  # 1 = version courante
    # Champs minimum (phase_5:64-65) :
    content: Mapped[str] = mapped_column(Text, nullable=False)  # contenu synthétique
    status: Mapped[str] = mapped_column(
        String, default="candidate", nullable=False
    )  # candidate|accepted|stale|rejected
    # Vocabulaire Phase 6 (T6.2) — catégorie du fait et statut d'affirmation de
    # l'affirmation. NULLables : les fiches legacy n'ont ni l'un ni l'autre, et
    # la taxonomie est une annotation, pas une contrainte de stockage.
    category: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # six catégories de `memoire.md` §2
    affirmation_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # decision|hypothese|observation|information_a_verifier
    source_artifact_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )  # sources accessibles
    origin_context: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # contexte d'origine
    origin_conversation_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # provenance, jamais FK : la purge ne doit rien emporter
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    # Champs recommandés (phase_5:65) :
    promotion_reasons: Mapped[list] = mapped_column(
        JSON, nullable=True, default=list
    )  # raisons structurées
    decision: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )  # {actor, at, action, motif}
    evidence_ids: Mapped[list] = mapped_column(
        JSON, nullable=True, default=list
    )  # références de preuves
    # Motif STRUCTURÉ de la transition vers `stale` (Phase 5). `decision.reason`
    # reste le récit de l'acteur ; cette colonne est la valeur que la
    # réhabilitation compare (`source_inaccessible`, `discussion supprimée`,
    # contradiction…). NULL = fiche jamais invalidée, ou fiche legacy.
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Statut d'AVANT la transition vers `stale`. Il n'est écrit qu'à cette
    # transition : une réhabilitation restaure exactement ce statut, sans jamais
    # promouvoir une `candidate`. NULL legacy → `accepted` au plus.
    previous_status: Mapped[str | None] = mapped_column(String, nullable=True)
    # Préparé Phase 8 : provenance documentaire d'un fait extrait d'une
    # observation et version de l'extracteur. Aucun backfill : une fiche qui
    # n'en a pas n'a simplement pas été produite par ce chemin.
    source_observation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    extractor_version: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_from: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # version précédente
    # Signaux d'usage — secondaires, jamais une validation de vérité (C17:260).
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # C09 : la clé d'idempotence de la proposition, rejouable sans doublon.
    # Index unique PARTIEL comme `usage_key` : plusieurs fiches sans clé
    # coexistent (NULL n'est pas dupliqué en SQLite), mais deux propositions de
    # même clé ne peuvent pas. Un SELECT puis INSERT ne suffit pas — deux
    # rejeux entrelacés passeraient tous les deux.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        Index("idx_memory_ws_current", "workspace_id", "is_current", "status"),
        Index("idx_memory_entry_version", "entry_key", "version"),
        # Une SEULE version courante par fiche (les lignes legacy is_current NULL
        # sont exclues), même idiome que `uq_current_card_per_artifact`.
        Index(
            "uq_memory_current_entry",
            "entry_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
        # Idempotence de la proposition, cloisonnée par espace : même clé dans
        # deux espaces = deux propositions indépendantes, pas un doublon.
        Index(
            "uq_memory_idem",
            "workspace_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class MemoryDocumentExtraction(Base):
    """Ledger d'extraction documentaire (Phase 8, tâche 8.1).

    Une ligne = UNE tentative d'extraire les faits d'un document pour un espace,
    une observation et une version d'extracteur. Le ledger est le seul point
    d'entrée de l'exécution : il porte le claim (TTL 5 min), le heartbeat, les
    compteurs et l'erreur terminale, pour qu'un redémarrage ne perde jamais un
    document et qu'un worker stallé ne puisse pas écrire après reprise.

    UNICITÉ AVEC `observation_id` NULL : SQLite considère deux NULL comme
    distincts, donc une clé unique naïvement posée sur les quatre colonnes
    autoriserait des doublons pour les lignes sans observation (backfill d'un
    document lu avant l'existence du ledger). On pose donc deux index partiels :
    - `uq_mdoc_extraction_obs` pour les lignes réelles (observation connue) ;
    - `uq_mdoc_extraction_sans_obs` pour les lignes sans observation.
    C'est la même clé logique `(workspace, artifact, observation, version)`,
    avec la sémantique NULL rendue explicite au lieu d'être laissée au hasard
    de SQLite.
    """

    __tablename__ = "memory_document_extractions"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # mdx_<12 hex>
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL : la trace du ledger survit à la rétention des observations.
    observation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("observations.id", ondelete="SET NULL"), nullable=True
    )
    # SET NULL : le fragment peut être remplacé par une réingestion ; la preuve
    # qui le citait devient alors invalide, jamais silencieusement redirigée.
    chunk_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("artifact_chunks.id", ondelete="SET NULL"), nullable=True
    )
    card_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    entry_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # pending|running|done|failed|skipped|deferred
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'"), default="pending"
    )
    claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    deferrals: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )
    __table_args__ = (
        Index("idx_mdoc_status_next", "status", "next_attempt_at"),
        Index("idx_mdoc_ws_artifact", "workspace_id", "artifact_id"),
        Index(
            "uq_mdoc_extraction_obs",
            "workspace_id",
            "artifact_id",
            "observation_id",
            "extractor_version",
            unique=True,
            sqlite_where=text("observation_id IS NOT NULL"),
        ),
        Index(
            "uq_mdoc_extraction_sans_obs",
            "workspace_id",
            "artifact_id",
            "extractor_version",
            unique=True,
            sqlite_where=text("observation_id IS NULL"),
        ),
    )


class MemorySynthesis(Base):
    """Synthèse distincte de l'espace — dimension synthétique produite par Thot.

    Table séparée (`memory_syntheses`) pour que la synthèse d'ensemble ne se
    confonde pas avec les fiches de faits (`memory_entries`) — elle porte sa
    propre version et sa propre provenance (les fiches qu'elle résume), et
    n'est jamais comptée comme une fiche.
    """

    __tablename__ = "memory_syntheses"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # syn_<12 hex>
    workspace_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_current: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_entry_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )  # fiches résumées
    idempotency_key: Mapped[str | None] = mapped_column(
        String, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_memory_syntheses_ws_current", "workspace_id", "is_current"),
        Index(
            "uq_memory_syntheses_current",
            "workspace_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )


class MemoryUsage(Base):
    """Usage réel d'une fiche — une ligne par couple (réponse, version).

    Sépare deux choses que le contrat refuse de confondre : consulter une fiche
    et l'avoir réellement retenue pour une réponse. Seul ce dernier cas
    incrémente (`phase_5:96`, `backend_espaces.md:136-142`), et `usage_key`
    rend la relance d'une réponse idempotente au lieu de gonfler le compteur.
    """

    __tablename__ = "memory_usage"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # mus_<12 hex>
    entry_id: Mapped[str] = mapped_column(
        String, ForeignKey("memory_entries.id", ondelete="CASCADE"), nullable=False
    )
    usage_key: Mapped[str] = mapped_column(
        String, nullable=False
    )  # sha256(reponse_id|entry_id|version)
    response_id: Mapped[str] = mapped_column(
        String, nullable=False
    )  # message/run ayant retenu la preuve
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    __table_args__ = (
        UniqueConstraint("usage_key", name="uq_memory_usage_key"),
        Index("idx_memory_usage_entry", "entry_id"),
    )


class MemoryExclusion(Base):
    """Contenu durablement exclu de la mémoire d'un espace.

    Une exclusion est une décision de l'utilisateur : elle survit à la purge de
    la fiche qui l'a motivée, et s'applique par empreinte de contenu normalisé.
    `entry_key` garde la trace de la fiche d'origine quand elle existe encore ;
    `reason` dit pourquoi, sans jamais conditionner l'exclusion elle-même.
    """

    __tablename__ = "memory_exclusions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_key: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "content_hash", name="uq_memory_exclusion_content"
        ),
        Index("idx_memory_exclusions_ws", "workspace_id"),
    )

# Modèles SQLAlchemy — 4 tables Phase 1 (fresh start, pas d'héritage v0.8)
# CASCADE + INDEX pour perfs, JSON pour args toolcalls
from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()  # base pour tous les modèles

class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(String, primary_key=True)
    title = Column(String)
    mode = Column(String)  # chat | agent
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"
    id = Column(String, primary_key=True)
    conv_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"))
    role = Column(String)  # user | assistant | system
    content = Column(Text)
    thinking = Column(Text, nullable=True)
    run_id = Column(String)
    status = Column(String)
    tokens_in = Column(Integer)
    tokens_out = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())
    conversation = relationship("Conversation", back_populates="messages")
    toolcalls = relationship("ToolCall", back_populates="message", cascade="all, delete-orphan")
    sources = relationship("Source", back_populates="message", cascade="all, delete-orphan")
    __table_args__ = (Index("idx_messages_conv_created", "conv_id", "created_at"),)

class ToolCall(Base):
    __tablename__ = "toolcalls"
    id = Column(String, primary_key=True)
    msg_id = Column(String, ForeignKey("messages.id", ondelete="CASCADE"))
    tool_call_id = Column(String)
    tool = Column(String)  # vision | doc_ocr
    args = Column(JSON)  # {image_hash, image_path/pdf_path, query} — jamais base64
    result = Column(Text)  # complet pour audit
    ok = Column(Boolean)
    latency_ms = Column(Integer)
    message = relationship("Message", back_populates="toolcalls")
    __table_args__ = (Index("idx_toolcalls_msg_id", "msg_id"),)

class Source(Base):
    __tablename__ = "sources"
    id = Column(String, primary_key=True)
    msg_id = Column(String, ForeignKey("messages.id", ondelete="CASCADE"))
    source_type = Column(String)  # attachment
    path = Column(String)  # legacy absolute path
    hash = Column(String)  # legacy sha256
    # Enrichi Réponses.md n°7-8 : cache persistant + manifest
    media_kind = Column(String, nullable=True)  # image | pdf | text | markdown
    original_name = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    sha256 = Column(String, nullable=True)
    relative_path = Column(String, nullable=True)  # media/{sha256}.ext
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    last_accessed_at = Column(DateTime, nullable=True)
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    parent_source_id = Column(String, ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    analysis_version = Column(Integer, server_default="0", nullable=True)
    message = relationship("Message", back_populates="sources")
    __table_args__ = (Index("idx_sources_msg_id", "msg_id"), Index("idx_sources_sha256", "sha256"), Index("idx_sources_conversation_created", "conversation_id", "created_at"),)


class Run(Base):
    """Run C09 : tentative d'exécution (chat ou production), idempotence C11."""
    __tablename__ = "runs"
    id = Column(String, primary_key=True)  # run_id == request_id Phase 1
    conv_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    request_id = Column(String, nullable=False, index=True)
    kind = Column(String, default="chat")  # chat | production
    status = Column(String, default="queued")  # queued|running|cancelling|completed|cancelled|failed|interrupted
    fingerprint = Column(String, nullable=True)  # empreinte canonique C11
    seq = Column(Integer, default=0)  # dernier seq émis persisté C08
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    __table_args__ = (Index("idx_runs_conv_status", "conv_id", "status"), Index("idx_runs_request", "request_id"),)


class ContextSnapshot(Base):
    """Snapshot C06 : projection compacte non destructive, versionnée."""
    __tablename__ = "context_snapshots"
    id = Column(String, primary_key=True)
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    revision = Column(Integer, nullable=False)
    covered_through_message_id = Column(String, nullable=True)  # borne C06
    summary = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (Index("idx_snapshots_conv_rev", "conversation_id", "revision"),)


# Annexe 10 : pipeline pièces jointes — stockage durable Blob/Artifact/ArtifactLink
class Blob(Base):
    """Octets dédupliqués adressés par hash (annexe 10)."""
    __tablename__ = "blobs"
    id = Column(String, primary_key=True)  # blob_id = sha256 ou uuid
    sha256 = Column(String, unique=True, nullable=False)
    relative_path = Column(String, nullable=False)  # blobs/sha256/ab/...bin
    size_bytes = Column(Integer, nullable=False)
    mime_detected = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    state = Column(String, default="available")
    __table_args__ = (Index("idx_blobs_sha256", "sha256"),)


class Artifact(Base):
    """Ressource logique adressable (annexe 10)."""
    __tablename__ = "artifacts"
    id = Column(String, primary_key=True)  # artifact_id
    blob_id = Column(String, ForeignKey("blobs.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String, nullable=False)  # image | pdf | text
    original_filename = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True)
    blob = relationship("Blob")
    __table_args__ = (Index("idx_artifacts_blob", "blob_id"),)


class ArtifactLink(Base):
    """Rattachement explicite à conversation/espace/bibliothèque (annexe 10)."""
    __tablename__ = "artifact_links"
    id = Column(String, primary_key=True)
    artifact_id = Column(String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False)
    scope_kind = Column(String, nullable=False)  # conversation | workspace | library
    scope_id = Column(String, nullable=False)
    role = Column(String, default="attachment")
    created_at = Column(DateTime, server_default=func.now())
    removed_at = Column(DateTime, nullable=True)
    artifact = relationship("Artifact")
    __table_args__ = (Index("idx_links_scope", "scope_kind", "scope_id"), Index("idx_links_artifact", "artifact_id"),)


class MessageAttachment(Base):
    """Pièce jointe exacte d'un message (annexe 10)."""
    __tablename__ = "message_attachments"
    id = Column(String, primary_key=True)
    message_id = Column(String, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    artifact_id = Column(String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False)
    ordinal = Column(Integer, nullable=False)
    caption = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    artifact = relationship("Artifact")
    __table_args__ = (Index("idx_msg_att_msg", "message_id"), Index("idx_msg_att_artifact", "artifact_id"),)


class ArtifactCard(Base):
    """Description courte réutilisable (annexe 10)."""
    __tablename__ = "artifact_cards"
    id = Column(String, primary_key=True)
    artifact_id = Column(String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False)
    revision = Column(Integer, default=0)
    summary = Column(Text, nullable=True)
    capabilities = Column(String, nullable=True)
    quality = Column(String, nullable=True)
    status = Column(String, default="available")
    __table_args__ = (Index("idx_cards_artifact", "artifact_id"),)


class Observation(Base):
    """Résultat structuré d'un outil (annexe 10)."""
    __tablename__ = "observations"
    id = Column(String, primary_key=True)
    artifact_id = Column(String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False)
    tool_name = Column(String, nullable=False)
    tool_version = Column(String, nullable=True)
    input_fingerprint = Column(String, nullable=True)
    query = Column(Text, nullable=True)
    status = Column(String, default="completed")
    payload_ref = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (Index("idx_obs_artifact", "artifact_id"),)

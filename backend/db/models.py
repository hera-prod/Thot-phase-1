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
    # Enrichi Réponses.md n°7 : cache persistant
    media_kind = Column(String, nullable=True)  # image | pdf
    original_name = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    sha256 = Column(String, nullable=True)
    relative_path = Column(String, nullable=True)  # media/{sha256}.ext
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    message = relationship("Message", back_populates="sources")
    __table_args__ = (Index("idx_sources_msg_id", "msg_id"), Index("idx_sources_sha256", "sha256"),)

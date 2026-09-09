# Base SQLite Phase 1 — data/thot-phase-1.db (indépendante de data/thot.db v0.8)
# Fusion v0.8 : PRAGMA foreign_keys+WAL + ensure_data_dir, mais 4 tables fresh start Phase 1
from __future__ import annotations

import logging
import os
from pathlib import Path
from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base

log = logging.getLogger(__name__)

# Résolution DB_PATH :
# 1. Env THOT_DB_PATH (injecté par Rust Tauri via app_data_dir) — priorité absolue
# 2. Fallback : data/{reqId}/thot-phase-1.db (projet local ou app_data)
_THOT_DB_PATH = os.environ.get("THOT_DB_PATH")

if _THOT_DB_PATH:
    DB_PATH = _THOT_DB_PATH
    log.info(f"DB_PATH from env THOT_DB_PATH: {DB_PATH}")
else:
    try:
        from backend.config.paths import resolve_data_dir  # type: ignore
        _data_dir = resolve_data_dir(None)
        # Nouveau contrat portable : data/database/thot-phase-1.db ; legacy : data/thot-phase-1.db
        new_path = _data_dir / "database" / "thot-phase-1.db"
        legacy_path = _data_dir / "thot-phase-1.db"
        if new_path.exists():
            DB_PATH = str(new_path)
            log.info(f"DB_PATH from resolve_data_dir (new): {DB_PATH}")
        elif legacy_path.exists():
            DB_PATH = str(legacy_path)
            log.info(f"DB_PATH from resolve_data_dir (legacy): {DB_PATH}")
        else:
            # Par défaut : nouveau chemin (database/)
            DB_PATH = str(new_path)
            log.info(f"DB_PATH from resolve_data_dir (default new): {DB_PATH}")
    except Exception:
        DB_PATH = os.path.join(os.path.dirname(__file__), "../../data/thot-phase-1.db")
        log.warning(f"DB_PATH fallback (no resolve_data_dir): {DB_PATH}")

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)  # fabrique de sessions


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection: SQLite3Connection, _: object) -> None:
    """PRAGMA critiques copiés v0.8 — WAL + FK ON + busy_timeout."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA temp_store = MEMORY;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    cursor.close()


def init_db():
    """Crée les 4 tables si elles n'existent pas (appelé au démarrage du sidecar)."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Générateur de session (usage direct ou futur Tauri)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_data_dir() -> Path:
    """Assure existence data_dir (compat v0.8 ensure_data_dir)."""
    p = Path(DB_PATH).parent
    p.mkdir(parents=True, exist_ok=True)
    return p

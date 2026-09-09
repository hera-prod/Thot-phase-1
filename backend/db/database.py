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
    """Crée les tables si elles n'existent pas (appelé au démarrage du sidecar).

    Annexe 11 Lot A : create_all ne rajoute PAS les colonnes aux tables existantes,
    donc migration légère (ALTER TABLE ADD COLUMN si absente) + DDL FTS5 + index
    complémentaires. Chaque étape est bornée en try/except : un index en conflit
    ne doit jamais empêcher le démarrage.
    """
    Base.metadata.create_all(bind=engine)
    _migrate_search_columns()
    _ensure_fts()


# Colonnes annexe 11 Lot A : {table: [(colonne, type DDL)]}
_SEARCH_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "artifact_cards": [
        ("is_current", "INTEGER"),
        ("title", "VARCHAR"),
        ("keywords_json", "TEXT"),
        ("entities_json", "TEXT"),
        ("detected_language", "VARCHAR"),
        ("document_type", "VARCHAR"),
        ("capabilities_json", "TEXT"),
        ("analysis_status", "VARCHAR"),
        ("source_observation_id", "VARCHAR"),
        ("created_at", "DATETIME"),
        ("superseded_at", "DATETIME"),
    ],
    "observations": [
        ("run_id", "VARCHAR"),
        ("tool_call_id", "VARCHAR"),
        ("model_id", "VARCHAR"),
        ("result_json", "TEXT"),
    ],
}

_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS artifact_search USING fts5(
    artifact_id UNINDEXED,
    scope_kind UNINDEXED,
    scope_id UNINDEXED,
    kind UNINDEXED,
    card_revision UNINDEXED,
    filename,
    title,
    summary,
    keywords,
    entities,
    extracted_preview,
    tokenize = 'unicode61 remove_diacritics 2'
)"""

_SEARCH_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_artifact_links_scope ON artifact_links(scope_kind, scope_id, removed_at, artifact_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind, deleted_at)",
    "CREATE INDEX IF NOT EXISTS idx_cards_current_status ON artifact_cards(is_current, analysis_status, document_type)",
    "CREATE INDEX IF NOT EXISTS idx_observations_artifact_created ON observations(artifact_id, created_at)",
    # Une seule fiche active par artefact (les lignes legacy ont is_current NULL → exclues)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_current_card_per_artifact ON artifact_cards(artifact_id) WHERE is_current = 1",
]


def _migrate_search_columns(target_engine=None) -> None:
    """Ajoute les colonnes manquantes (idempotent, burntolerant aux conflits)."""
    eng = target_engine if target_engine is not None else engine
    try:
        from sqlalchemy import text as _text
        with eng.connect() as conn:
            for table, cols in _SEARCH_MIGRATIONS.items():
                try:
                    existing = {row[1] for row in conn.execute(_text(f"PRAGMA table_info({table})")).fetchall()}
                except Exception:
                    continue
                for name, ddl in cols:
                    if name not in existing:
                        try:
                            conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                        except Exception as e:
                            log.warning(f"migration {table}.{name} ignorée: {e}")
            conn.commit()
    except Exception as e:
        log.warning(f"migration search ignorée: {e}")


def _ensure_fts(target_engine=None) -> None:
    """Crée la table FTS5 + index complémentaires (idempotent)."""
    eng = target_engine if target_engine is not None else engine
    try:
        from sqlalchemy import text as _text
        with eng.connect() as conn:
            try:
                conn.execute(_text(_FTS_DDL))
            except Exception as e:
                log.warning(f"DDL FTS5 ignoré: {e}")
            for stmt in _SEARCH_INDEXES:
                try:
                    conn.execute(_text(stmt))
                except Exception as e:
                    log.warning(f"index search ignoré ({stmt[:60]}…): {e}")
            conn.commit()
    except Exception as e:
        log.warning(f"init FTS ignoré: {e}")


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

from __future__ import annotations

import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from backend.db.migrations import apply_migrations

# Les colonnes que C16 impose à un snapshot web, et que la migration doit créer.
COLONNES_WEB_SNAPSHOT = {
    "id",
    "public_id",
    "conv_id",
    "requested_url",
    "final_url",
    "fetched_at",
    "published_at",
    "status",
    "etag",
    "last_modified",
    "cache_control",
    "content_type",
    "title",
    "content_sha256",
    "text",
    "extractor_version",
    "scope_kind",
    "scope_id",
    "query_key",
    "source_type",
    "proposed_by",
    "reused_count",
}
COLONNES_WEB_QUERIES = {"id", "conv_id", "query_key", "created_at"}


def test_toolcall_migration_backfills_ids_and_is_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE runs (id VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(text("CREATE TABLE messages (id VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE image_effects (id VARCHAR PRIMARY KEY, fingerprint VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE toolcalls (id VARCHAR PRIMARY KEY, run_id VARCHAR, tool_call_id VARCHAR)"
            )
        )
        connection.execute(text("INSERT INTO runs VALUES ('run_1', 'req_1')"))
        connection.execute(text("INSERT INTO toolcalls VALUES ('tc_1', 'run_1', NULL)"))
        connection.execute(
            text("INSERT INTO toolcalls VALUES ('tc_2', 'run_1', 'call_1')")
        )
        connection.execute(
            text("INSERT INTO toolcalls VALUES ('tc_3', 'run_1', 'call_1')")
        )

    apply_migrations(engine)
    apply_migrations(engine)

    with engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(toolcalls)"))
        }
        rows = connection.execute(
            text("SELECT id, tool_call_id, status FROM toolcalls ORDER BY id")
        ).fetchall()
        indexes = {
            row[1] for row in connection.execute(text("PRAGMA index_list(toolcalls)"))
        }
        effect_indexes = {
            row[1]
            for row in connection.execute(text("PRAGMA index_list(image_effects)"))
        }
        versions = connection.execute(
            text("SELECT version FROM schema_migrations")
        ).fetchall()

    assert {
        "logical_action_id",
        "ordinal",
        "status",
        "attempt",
        "provenance",
    } <= columns
    assert len(rows) == 3
    assert len({(row[0], row[1]) for row in rows}) == 3
    assert all(row[2] == "received" for row in rows)
    assert "uq_toolcalls_run_call" in indexes
    assert "uq_image_effect_active_fingerprint" in effect_indexes
    # Les versions s'appliquent dans l'ordre : la 1 d'abord, puis la 2 (cache web).
    assert [row[0] for row in versions] == sorted(row[0] for row in versions)
    assert [row[0] for row in versions][:1] == [1]


def test_migration_failure_is_not_recorded() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE runs (id VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL)"
            )
        )

    from backend.db import migrations

    original = migrations._MIGRATIONS
    migrations._MIGRATIONS = (
        (99, "broken", lambda _connection: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    try:
        try:
            apply_migrations(engine)
        except migrations.MigrationError:
            pass
        else:
            raise AssertionError("migration failure must be surfaced")
        with engine.connect() as connection:
            versions = connection.execute(
                text("SELECT version FROM schema_migrations")
            ).fetchall()
        assert versions == []
    finally:
        migrations._MIGRATIONS = original


# --------------------------------------------------------------------------
# Migration 2 — le cache web indexé sur la conversation
# --------------------------------------------------------------------------
def _base_avec_les_tables_de_la_migration_1(url: str) -> Engine:
    """Un moteur neuf, avec les quatre tables dont la migration 1 a besoin."""
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE runs (id VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(text("CREATE TABLE messages (id VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE image_effects (id VARCHAR PRIMARY KEY, fingerprint VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE toolcalls (id VARCHAR PRIMARY KEY, run_id VARCHAR, tool_call_id VARCHAR)"
            )
        )
    return engine


def test_migration_web_cree_le_cache_et_est_idempotente() -> None:
    engine = _base_avec_les_tables_de_la_migration_1("sqlite:///:memory:")

    apply_migrations(engine)
    apply_migrations(engine)  # rejouer ne doit rien casser ni rien dupliquer

    with engine.connect() as connection:
        colonnes = {
            row[1]: row
            for row in connection.execute(text("PRAGMA table_info(web_snapshots)"))
        }
        requetes = {
            row[1] for row in connection.execute(text("PRAGMA table_info(web_queries)"))
        }
        index_snapshots = {
            row[1]: row[2]
            for row in connection.execute(text("PRAGMA index_list(web_snapshots)"))
        }
        versions = [
            row[0]
            for row in connection.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            )
        ]

    # C16 : URL demandée/finale, dates, statut, validateurs, hash du CONTENU,
    # texte, version d'extracteur, portée — plus l'identifiant public et les
    # compteurs qui en font une preuve réutilisable.
    assert COLONNES_WEB_SNAPSHOT <= set(colonnes)
    assert COLONNES_WEB_QUERIES <= requetes
    # « date de publication si connue » : la colonne existe et accepte l'absence.
    assert colonnes["published_at"][3] == 0
    # Un contenu inchangé doit retomber sur la même ligne : l'identifiant public est unique.
    assert index_snapshots["ix_web_snapshots_public_id"] == 1
    assert "idx_web_snapshots_conv_query" in index_snapshots
    # Même lecture que `test_etat_ingestion` : le registre continue de croître
    # après ce test (la 7, corbeille des conversations, est additive et sans
    # rapport avec le cache web) — l'écriture utile est le préfixe sans doublon.
    assert versions[:6] == [1, 2, 3, 4, 5, 6], (
        "chaque version n'est enregistrée qu'une fois"
    )
    assert versions == sorted(set(versions)), "aucune version dupliquée"
    # La liste est volontairement ÉCRITE EN DUR : une version de migration qui ne se
    # verrait pas ici n'aurait pas de test qui sache qu'elle existe. C'est le coût d'un
    # garde-fou, et il vaut le coup.


def test_le_reused_count_demarre_a_zero_et_le_texte_est_texte(tmp_path) -> None:
    engine = _base_avec_les_tables_de_la_migration_1(f"sqlite:///{tmp_path / 'web.db'}")
    apply_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO web_snapshots (id, public_id, conv_id, requested_url, final_url,"
                " fetched_at, status, content_type, title, content_sha256, text,"
                " extractor_version, scope_kind, scope_id, query_key, source_type, proposed_by)"
                " VALUES ('web_snap_x', 'web_000000000000', 'conv_a', 'https://exemple.test',"
                " 'https://exemple.test', '2026-01-01 00:00:00', 200, 'text/html', 'T',"
                " 'sha', 'le texte', 'extract-1', 'conversation', 'conv_a', 'general|cached|q',"
                " 'general', 'moteur')"
            )
        )
        lignes = connection.execute(
            text(
                "SELECT reused_count, typeof(text), published_at, fetched_at FROM web_snapshots"
            )
        ).fetchall()
    assert lignes[0][0] == 0, "un snapshot neuf n'a encore été ré-exploité par personne"
    assert lignes[0][1] == "text", (
        "le texte extrait ne doit pas être tronqué par un VARCHAR"
    )
    assert lignes[0][2] is None, "publication inconnue ≠ date de publication"


def test_une_base_neuve_obtient_le_cache_web_par_init_db(tmp_path, monkeypatch) -> None:
    from backend.db import database

    engine = create_engine(f"sqlite:///{tmp_path / 'neuve.db'}")
    monkeypatch.setattr(database, "engine", engine)

    # `init_db()` fait `create_all` PUIS `apply_migrations` : les deux chemins
    # doivent aboutir à la même table, sans qu'aucun des deux ne l'écrase.
    database.init_db()
    database.init_db()

    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
        colonnes = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(web_snapshots)"))
        }
        requetes = {
            row[1] for row in connection.execute(text("PRAGMA table_info(web_queries)"))
        }
        versions = [
            row[0]
            for row in connection.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            )
        ]

    assert {"web_snapshots", "web_queries"} <= tables
    assert COLONNES_WEB_SNAPSHOT <= colonnes
    assert COLONNES_WEB_QUERIES <= requetes
    assert 2 in versions


def test_migration_v13_une_seule_synthese_courante_par_espace() -> None:
    """v13 — l'index partiel rend deux courantes impossibles, après avoir gardé
    la version la plus haute."""
    engine = _base_avec_les_tables_de_la_migration_1("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE memory_syntheses ("
                "id VARCHAR PRIMARY KEY, "
                "workspace_id VARCHAR NOT NULL, "
                "version INTEGER NOT NULL DEFAULT 1, "
                "is_current INTEGER NOT NULL DEFAULT 1, "
                "content TEXT NOT NULL, "
                "source_entry_ids JSON NOT NULL DEFAULT '[]', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "superseded_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO memory_syntheses "
                "(id, workspace_id, version, is_current, content, created_at) VALUES "
                "('syn_a1', 'ws_a', 1, 1, 'ancienne', '2026-01-01 00:00:00'),"
                "('syn_a2', 'ws_a', 2, 1, 'récente', '2026-01-02 00:00:00'),"
                "('syn_b1', 'ws_b', 1, 1, 'seule', '2026-01-03 00:00:00')"
            )
        )

    apply_migrations(engine)
    apply_migrations(engine)

    with engine.connect() as connection:
        courantes = connection.execute(
            text(
                "SELECT workspace_id, id FROM memory_syntheses "
                "WHERE is_current = 1 ORDER BY workspace_id"
            )
        ).fetchall()
        index = {
            row[1]: row[2]
            for row in connection.execute(text("PRAGMA index_list(memory_syntheses)"))
        }
        versions = {
            row[0]
            for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }

    assert [tuple(row) for row in courantes] == [("ws_a", "syn_a2"), ("ws_b", "syn_b1")]
    assert index["uq_memory_syntheses_current"] == 1
    assert 13 in versions


def test_migration_v19_index_artifact_links_sur_base_existante() -> None:
    """v19 crée son index sur une base qui a DÉJÀ `artifact_links`."""
    engine = _base_avec_les_tables_de_la_migration_1("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE artifact_links ("
                "id VARCHAR PRIMARY KEY, "
                "artifact_id VARCHAR NOT NULL, "
                "removed_at DATETIME)"
            )
        )

    apply_migrations(engine)
    apply_migrations(engine)  # idempotente

    with engine.connect() as connection:
        index = {
            row[1]: row[2]
            for row in connection.execute(text("PRAGMA index_list(artifact_links)"))
        }
        colonnes = [
            row[2]
            for row in connection.execute(
                text("PRAGMA index_info('idx_links_artifact_removed')")
            )
        ]
        versions = {
            row[0]
            for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }

    assert "idx_links_artifact_removed" in index
    assert index["idx_links_artifact_removed"] == 0, "l'index du GC n'est pas UNIQUE"
    assert colonnes == ["artifact_id", "removed_at"]
    assert 19 in versions


def test_migration_v20_provenance_additive_et_idempotente() -> None:
    """v20 ajoute les quatre colonnes de provenance à une base EXISTANTE.

    Aucun backfill : les fiches legacy gardent NULL, et l'absence est l'état
    réel (`previous_status` NULL → `accepted` au plus lors d'une réhabilitation).
    """
    engine = _base_avec_les_tables_de_la_migration_1("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE memory_entries ("
                "id VARCHAR PRIMARY KEY, "
                "workspace_id VARCHAR NOT NULL, "
                "entry_key VARCHAR NOT NULL, "
                "version INTEGER NOT NULL DEFAULT 1, "
                "is_current INTEGER NOT NULL DEFAULT 1, "
                "content TEXT NOT NULL, "
                "status VARCHAR NOT NULL DEFAULT 'candidate', "
                "idempotency_key VARCHAR)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO memory_entries (id, workspace_id, entry_key, content)"
                " VALUES ('mem_legacy', 'ws_a', 'k', 'un fait ancien')"
            )
        )

    apply_migrations(engine)
    apply_migrations(engine)  # idempotente

    with engine.connect() as connection:
        colonnes = {
            row[1]: row
            for row in connection.execute(text("PRAGMA table_info(memory_entries)"))
        }
        legacy = connection.execute(
            text(
                "SELECT stale_reason, previous_status, source_observation_id,"
                " extractor_version FROM memory_entries WHERE id = 'mem_legacy'"
            )
        ).first()
        versions = {
            row[0]
            for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }

    assert {
        "stale_reason",
        "previous_status",
        "source_observation_id",
        "extractor_version",
    } <= set(colonnes)
    # NULLable, sans défaut : aucune valeur inventée pour les fiches legacy.
    for nom in (
        "stale_reason",
        "previous_status",
        "source_observation_id",
        "extractor_version",
    ):
        assert colonnes[nom][3] == 0
        assert colonnes[nom][4] is None
    assert legacy is not None
    assert tuple(legacy) == (None, None, None, None)
    assert 20 in versions


def test_migration_v21_ledger_documentaire_additive_et_idempotente() -> None:
    """v21 crée le ledger documentaire sur une base qui a déjà tout le reste.

    On retire la table d'une base créée par `create_all` pour reproduire une
    base EXISTANTE d'avant la Phase 8 : c'est la migration, et non l'ORM, qui
    doit la recréer avec les mêmes colonnes, défauts et index partiels.
    """
    from backend.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE memory_document_extractions"))
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, state)"
                " VALUES ('ws_mig', 'Espace', 'active')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO blobs (id, sha256, relative_path, size_bytes, mime_detected)"
                " VALUES ('blb_mig', 'sha_mig', 'blobs/mig.bin', 1, 'text/plain')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO artifacts (id, blob_id, kind, original_filename, display_name)"
                " VALUES ('art_mig', 'blb_mig', 'text', 'mig.txt', 'mig.txt')"
            )
        )

    apply_migrations(engine)
    apply_migrations(engine)  # idempotente

    with engine.begin() as connection:
        colonnes = {
            row[1]: row
            for row in connection.execute(
                text("PRAGMA table_info(memory_document_extractions)")
            )
        }
        indexes = {
            row[1]: row[2]
            for row in connection.execute(
                text("PRAGMA index_list(memory_document_extractions)")
            )
        }
        connection.execute(
            text(
                "INSERT INTO memory_document_extractions"
                " (id, workspace_id, artifact_id, extractor_version)"
                " VALUES ('mdx_mig', 'ws_mig', 'art_mig', 'v/1')"
            )
        )
        ligne = connection.execute(
            text(
                "SELECT status, card_revision, attempts, deferrals,"
                " claim_token, observation_id, chunk_id, created_at"
                " FROM memory_document_extractions WHERE id = 'mdx_mig'"
            )
        ).first()
        versions = {
            row[0]
            for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }
        # Types et actions FK : « additive » ne veut pas dire « non contrainte ».
        types = {name: str(row[2] or "").upper() for name, row in colonnes.items()}
        actions_fk = {
            row[3]: str(row[6] or "").upper()
            for row in connection.execute(
                text("PRAGMA foreign_key_list(memory_document_extractions)")
            )
        }

    assert {
        "id",
        "workspace_id",
        "artifact_id",
        "observation_id",
        "chunk_id",
        "card_revision",
        "extractor_version",
        "entry_key",
        "status",
        "claim_token",
        "claimed_at",
        "heartbeat_at",
        "attempts",
        "deferrals",
        "next_attempt_at",
        "last_error",
        "created_at",
        "updated_at",
    } <= set(colonnes)
    # Types attendus (SQLite rend VARCHAR/INTEGER/DATETIME).
    assert types["id"] in {"VARCHAR", "TEXT"}
    assert types["workspace_id"] in {"VARCHAR", "TEXT"}
    assert types["attempts"] == "INTEGER"
    assert types["card_revision"] == "INTEGER"
    assert types["created_at"] in {"DATETIME", "TIMESTAMP"}
    # Actions FK : CASCADE quand la ligne disparaît avec son propriétaire,
    # SET NULL quand la preuve survit à la rétention de la source.
    assert actions_fk["workspace_id"] == "CASCADE"
    assert actions_fk["artifact_id"] == "CASCADE"
    assert actions_fk["observation_id"] == "SET NULL"
    assert actions_fk["chunk_id"] == "SET NULL"
    assert ligne is not None
    assert tuple(ligne)[:7] == ("pending", 0, 0, 0, None, None, None)
    assert ligne[7] is not None, "created_at a un défaut serveur"
    assert indexes["uq_mdoc_extraction_obs"] == 1, "index unique partiel"
    assert indexes["uq_mdoc_extraction_sans_obs"] == 1, "index unique partiel"
    assert "idx_mdoc_status_next" in indexes
    assert "idx_mdoc_ws_artifact" in indexes
    assert {21, 22} <= versions


def test_migrations_sur_base_neuve_cree_le_schema_attendu() -> None:
    """`create_all` + `apply_migrations` sur une base neuve : schéma complet.

    Le chemin de production (`init_db`) fait `create_all` PUIS les migrations.
    Une base neuve doit donc ressortir avec le ledger v22, ses index partiels
    et ses contraintes FK, sans qu'aucune migration ne casse le schéma de base.
    """
    from backend.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    apply_migrations(engine)  # idempotente sur base neuve

    with engine.connect() as connection:
        versions = {
            int(row[0])
            for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }
        colonnes = {
            row[1]: row
            for row in connection.execute(
                text("PRAGMA table_info(memory_document_extractions)")
            )
        }
        indexes = {
            row[1]
            for row in connection.execute(
                text("PRAGMA index_list(memory_document_extractions)")
            )
        }
        actions_fk = {
            row[3]: str(row[6] or "").upper()
            for row in connection.execute(
                text("PRAGMA foreign_key_list(memory_document_extractions)")
            )
        }

    assert versions == set(range(1, 23)), (
        "toutes les migrations doivent être enregistrées"
    )
    assert 22 in versions, "la migration des représentations et figures est appliquée"
    assert "observation_id" in colonnes
    assert "chunk_id" in colonnes
    assert "uq_mdoc_extraction_obs" in indexes
    assert "uq_mdoc_extraction_sans_obs" in indexes
    assert actions_fk["observation_id"] == "SET NULL"
    assert actions_fk["chunk_id"] == "SET NULL"


def test_l_identifiant_public_web_respecte_son_format() -> None:
    from backend.services.web.cache import public_id_for

    # La forme affichée au modèle et au brief : `web_` + 12 hex, rien d'autre.
    assert re.fullmatch(
        r"web_[0-9a-f]{12}", public_id_for("https://exemple.test", "abc123")
    )

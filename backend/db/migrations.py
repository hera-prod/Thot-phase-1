from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


class MigrationError(RuntimeError):
    pass


Migration = tuple[int, str, Callable[[Any], None]]

_MIGRATIONS: tuple[Migration, ...] = ()


def _table_columns(connection: Any, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
    }


def _table_exists(connection: Any, table: str) -> bool:
    row = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).first()
    return row is not None


def _add_column(connection: Any, table: str, name: str, ddl: str) -> None:
    if not _table_exists(connection, table):
        # La table n'existe pas encore : `create_all` la créera avec la colonne
        # déjà déclarée dans le modèle. ALTER TABLE échouerait, et ce n'est pas
        # une migration incomplète — c'est une base qui n'a pas encore le socle.
        return
    if name in _table_columns(connection, table):
        return
    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _migrate_toolcall_durability(connection: Any) -> None:
    _add_column(connection, "runs", "runtime_id", "VARCHAR")
    _add_column(connection, "runs", "runtime_fingerprint", "VARCHAR")
    _add_column(connection, "messages", "brief_json", "TEXT")
    _add_column(connection, "toolcalls", "logical_action_id", "VARCHAR")
    _add_column(connection, "toolcalls", "ordinal", "INTEGER NOT NULL DEFAULT 0")
    _add_column(
        connection, "toolcalls", "status", "VARCHAR NOT NULL DEFAULT 'received'"
    )
    _add_column(connection, "toolcalls", "attempt", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "toolcalls", "provenance", "JSON")

    rows = connection.execute(
        text("SELECT id, run_id, tool_call_id FROM toolcalls ORDER BY rowid")
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        row_id = str(row[0])
        run_id = str(row[1] or "")
        call_id = str(row[2] or "").strip()
        if not call_id:
            call_id = f"legacy_{row_id}"
        key = (run_id, call_id)
        if key in seen:
            call_id = f"{call_id}_legacy_{row_id}"
            key = (run_id, call_id)
        seen.add(key)
        connection.execute(
            text(
                "UPDATE toolcalls SET tool_call_id=:call_id, ordinal=rowid, status=COALESCE(status, 'received') WHERE id=:id"
            ),
            {"call_id": call_id, "id": row_id},
        )

    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_toolcalls_run_call ON toolcalls(run_id, tool_call_id)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_image_effect_active_fingerprint "
            "ON image_effects(fingerprint) WHERE status IN ('claimed', 'running', 'uncertain', 'published')"
        )
    )


def _migrate_web_cache(connection: Any) -> None:
    """Crée le cache web : `web_snapshots` (preuves datées) + `web_queries` (journal).

    Entièrement `IF NOT EXISTS`, donc rejouable : `init_db()` fait déjà
    `create_all` puis `apply_migrations`, et les deux chemins peuvent se croiser
    dans un ordre quelconque. `_add_column` ne sert qu'aux tables EXISTANTES, ce
    qui n'est pas le cas ici — une table neuve se crée, ou ne se crée pas.

    Les noms d'index sont ceux que produit l'ORM pour les mêmes colonnes, afin qu'un
    index créé par `create_all` ne soit pas dupliqué sous un second nom.
    """
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS web_snapshots ("
            "id VARCHAR PRIMARY KEY, "
            "public_id VARCHAR NOT NULL, "
            "conv_id VARCHAR NOT NULL, "
            "requested_url VARCHAR NOT NULL, "
            "final_url VARCHAR NOT NULL, "
            "fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "published_at DATETIME, "
            "status INTEGER NOT NULL, "
            "etag VARCHAR, "
            "last_modified VARCHAR, "
            "cache_control VARCHAR, "
            "content_type VARCHAR NOT NULL, "
            "title VARCHAR NOT NULL, "
            "content_sha256 VARCHAR NOT NULL, "
            "text TEXT NOT NULL, "
            "extractor_version VARCHAR NOT NULL, "
            "scope_kind VARCHAR NOT NULL, "
            "scope_id VARCHAR NOT NULL, "
            "query_key VARCHAR NOT NULL, "
            "source_type VARCHAR NOT NULL, "
            "proposed_by VARCHAR NOT NULL, "
            "reused_count INTEGER NOT NULL DEFAULT 0)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_web_snapshots_public_id ON web_snapshots(public_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_web_snapshots_conv_id ON web_snapshots(conv_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_web_snapshots_content_sha256 ON web_snapshots(content_sha256)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_web_snapshots_query_key ON web_snapshots(query_key)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_web_snapshots_conv_query ON web_snapshots(conv_id, query_key)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_web_snapshots_conv_url ON web_snapshots(conv_id, requested_url)"
        )
    )
    # Journal sobre des requêtes moteur de la conversation (C15). Il n'a pas de
    # classe ORM : trois colonnes, une contrainte d'unicité, et le doublon ne
    # double pas le compte — « un enregistrement par (conv_id, query_key) ».
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS web_queries ("
            "id VARCHAR PRIMARY KEY, "
            "conv_id VARCHAR NOT NULL, "
            "query_key VARCHAR NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_queries_conv_key ON web_queries(conv_id, query_key)"
        )
    )


def _migrate_espaces_memoire(connection: Any) -> None:
    """Crée les espaces et la mémoire : `workspaces`, `memory_entries`, `memory_usage`.

    Entièrement `IF NOT EXISTS` / `_add_column`, donc rejouable, comme
    `_migrate_web_cache`. `conversations.workspace_id` passe par `_add_column`
    parce que la table EXISTE déjà : `create_all` ne rajoute pas de colonne.

    Le `ON DELETE SET NULL` de `conversations.workspace_id` est la décision
    E-SUPPR encodée dans le schéma : purger un espace rétrograde ses
    conversations en chats simples au lieu de les détruire. Une contrainte de
    base de données est le dernier endroit où une décision de cycle de vie
    mérite d'être défendue.
    """
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS workspaces ("
            "id VARCHAR PRIMARY KEY, "
            "name VARCHAR NOT NULL, "
            "description TEXT, "
            "image_artifact_id VARCHAR, "
            "state VARCHAR NOT NULL DEFAULT 'active', "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "trashed_at DATETIME, "
            "purge_after DATETIME)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_workspaces_state_created ON workspaces(state, created_at)"
        )
    )

    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS memory_entries ("
            "id VARCHAR PRIMARY KEY, "
            "entry_key VARCHAR NOT NULL, "
            "workspace_id VARCHAR NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "is_current INTEGER NOT NULL DEFAULT 1, "
            "content TEXT NOT NULL, "
            "status VARCHAR NOT NULL DEFAULT 'candidate', "
            "category VARCHAR, "
            "affirmation_status VARCHAR, "
            "source_artifact_ids JSON NOT NULL DEFAULT '[]', "
            "origin_context TEXT, "
            "origin_conversation_id VARCHAR, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "promotion_reasons JSON, "
            "decision JSON, "
            "evidence_ids JSON, "
            "superseded_from VARCHAR, "
            "usage_count INTEGER NOT NULL DEFAULT 0, "
            "last_used_at DATETIME, "
            "superseded_at DATETIME, "
            "idempotency_key VARCHAR)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_memory_ws_current ON memory_entries(workspace_id, is_current, status)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_memory_entry_version ON memory_entries(entry_key, version)"
        )
    )
    # Les index partiels de `memory_entries` ne sont PAS créés ici : ils sont
    # arrival avec la correction de `propose`, et une base ayant déjà appliqué
    # cette version ne les recevrait jamais. Ils sont en version 4, leur propre
    # version, ce qui est la seule façon de les atteindre sur une base
    # existante. Voir `_migrate_memory_indexes`.

    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS memory_usage ("
            "id VARCHAR PRIMARY KEY, "
            "entry_id VARCHAR NOT NULL REFERENCES memory_entries(id) ON DELETE CASCADE, "
            "usage_key VARCHAR NOT NULL, "
            "response_id VARCHAR NOT NULL, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_usage_key ON memory_usage(usage_key)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_memory_usage_entry ON memory_usage(entry_id)"
        )
    )

    # `conversations` est la SEULE table de cette migration qui puisse ne pas
    # exister : elle est antérieure au lot espaces. Les deux opérations sont
    # groupées sous la même existence, sinon ALTER passe et CREATE INDEX échoue.
    if _table_exists(connection, "conversations"):
        _add_column(
            connection,
            "conversations",
            "workspace_id",
            "VARCHAR REFERENCES workspaces(id) ON DELETE SET NULL",
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_conversations_workspace_id ON conversations(workspace_id)"
            )
        )


def _migrate_memory_indexes(connection: Any) -> None:
    """Index partiels de `memory_entries` — version 4, donc jamais 3.

    Une version de migration s'applique UNE fois. Ajouter un index à une
    version déjà livrée ne l'atteint sur aucune base existante : `create_all`
    ne rajoute pas d'index à une table qui existe, et `apply_migrations` saute
    une version présente dans `schema_migrations`. Un index posé dans une
    version ancienne n'existe donc que sur les bases créées depuis — c'est-à-dire
    personne. D'où une version propre.

    Les noms sont VOLONTAIREMENT ceux que produit `create_all` : un index déjà
    présent n'est donc pas recréé sous un second nom, et `IF NOT EXISTS` rend le
    croisement des deux chemins sans effet de bord.
    """
    # Une seule version courante par fiche. Sans cet index, deux lignes
    # `is_current=1` sur la même clé seraient acceptées, et la fiche aurait
    # deux versions « courantes » que le classement ne saurait pas départager.
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_current_entry "
            "ON memory_entries(entry_key) WHERE is_current = 1"
        )
    )
    # Idempotence de la proposition, par espace (C09). Partiel car plusieurs
    # fiches sans clé doivent coexister.
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_idem "
            "ON memory_entries(workspace_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    )


def _migrate_etat_ingestion(connection: Any) -> None:
    """Version 5 : l'état d'ingestion porté par l'artefact (tâche 1.6).

    TROIS COLONNES, et pas une table de plus. Une table dédiée aurait fait de
    l'absence de ligne la réponse par défaut — c'est-à-dire exactement le défaut
    qu'on supprime : « pas de ligne » y signifierait « on ne sait pas » ET « jamais
    traité », les deux étant indiscernables. Une colonne NOT NULL donne à CHAQUE
    artefact un état, y compris celui qu'on n'a jamais touché.

    LE DDL PORTE LE DEFAULT, et c'est obligatoire : SQLite refuse
    `ALTER TABLE ... ADD COLUMN ... NOT NULL` sans valeur par défaut. La même
    raison fait que `create_all` et cette migration produisent la MÊME contrainte —
    un pont écrit en SQL brut (le dépôt des pièces jointes) omettrait sinon la
    colonne et se ferait rejeter par une base neuve alors que la base ancienne
    l'accepterait. Deux bases, deux comportements : exactement le piège.

    LE DEFAULT EST `inconnu`, ET C'EST LE DDL QUI FAIT LE BACKFILL :
    `ALTER TABLE ADD COLUMN ... DEFAULT 'inconnu'` remplit les lignes DÉJÀ présentes
    de cette valeur, donc la migration est atomique et il n'y a pas de fenêtre pendant
    laquelle un artefact porterait « jamais traité » — une affirmation qui serait
    fausse pour la plupart du patrimoine. Le dépôt, lui, écrit `depose` explicitement
    parce que là il sait qu'une extraction est due.

    Le choix `inconnu` plutôt que `depose` est donc PROOF : un artefact qui existait
    avant ce registre a peut-être déjà été extrait, et l'affirmer serait fabriquer une
    dette ; dire « jamais traité » serait fabriquer une faute. `inconnu` est l'absence
    d'information nommée comme telle, et le balayage de reprise sait la traiter : il ne
    rejoue que ceux qui n'ont aucune observation d'ingestion réussie. Laisser le défaut
    du DDL (`depose`) ferait crier « extraction jamais demandée » sur tout un
    patrimoine dès le premier démarrage.

    AUCUN INDEX UNIQUE ICI, donc aucun doublon à dédoublonner : c'est délibéré. Un
    index unique sur `artifacts` n'aurait de sens que pour une contrainte d'unicité
    métier, et il faudrait alors réécrire des lignes existantes — donc détruire de
    la donnée — pour qu'un démarrage réussisse. Un démarrage qui échoue faute
    d'avoir su dédoublonner est un pire défaut qu'une contrainte absente.
    """
    if not _table_exists(connection, "artifacts"):
        # Base sans socle : `create_all` créera `artifacts` avec les colonnes déjà
        # déclarées dans le modèle. Même règle que `_add_column`, appliquée à la
        # migration entière plutôt qu'à chaque colonne.
        return
    _add_column(
        connection, "artifacts", "ingest_state", "VARCHAR NOT NULL DEFAULT 'inconnu'"
    )
    _add_column(connection, "artifacts", "ingest_reason", "TEXT")
    _add_column(connection, "artifacts", "ingest_updated_at", "DATETIME")
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_ingest_state ON artifacts(ingest_state)"
        )
    )
    # Le `NOT NULL` du DDL ne rattrape pas une base qui AVAIT déjà la colonne : un
    # `ALTER` antérieur, ou une écriture brute, peut y avoir laissé NULL. On couvre le
    # cas parce que `lire()` en fait `inconnu` — mais la base doit dire la même chose
    # que le modèle, sinon deux bases donneraient deux réponses à une question unique.
    connection.execute(
        text(
            "UPDATE artifacts SET ingest_state='inconnu'"
            " WHERE ingest_state IS NULL OR TRIM(ingest_state) = ''"
        )
    )


def _migrate_docrefs_unicite_label(connection: Any) -> None:
    """Version 6 : UNIQUE(conv_id, doc_label) sur `doc_refs`.

    `doc_refs` naissait jusqu'ici de `create_all` seul — une table applicative
    sans version : rien ne la faisait évoluer sur une base déjà créée, et c'est
    ce qui a permis à `allocate_doc_label` d'être le seul juge de l'unicité du
    `doc_N`. Un `SELECT max(doc_index) + 1` n'est pas atomique ; la garantie
    doit être dans le schéma.

    UN `CREATE UNIQUE INDEX` et non une reconstruction de table : SQLite exige
    de réécrire `doc_refs` pour ajouter une contrainte `UNIQUE(...)` en table,
    et réécrire une table applicative pour poser une contrainte est exactement le
    geste qui perd de la donnée s'il se passe mal. L'index pose exactement la
    même garantie, en place, et se supprime comme un index.

    AUCUN `WHERE removed_at IS NULL` : un index partiel libérerait le numéro d'un
    document supprimé, or le contrat est « jamais recyclé, une suppression garde
    son numéro ». Le partiel casserait ce contrat pour les seules suppressions,
    ce qui est le pire moment pour le découvrir.

    Entièrement `IF NOT EXISTS` comme les migrations voisines, donc rejouable.
    Si des données existing violaient déjà la contrainte, SQLite refuse de créer
    l'index : la migration échoue, elle ne réécrit rien. Un démarrage qui échoue
    vaut mieux qu'un `doc_N` partagé par deux fichiers.
    """
    if not _table_exists(connection, "doc_refs"):
        # Base sans socle : `create_all` créera `doc_refs` avec l'index déjà
        # déclaré dans le modèle. Même règle que `_migrate_etat_ingestion`.
        return
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_doc_refs_conv_label "
            "ON doc_refs(conv_id, doc_label)"
        )
    )


def _migrate_corbeille_conversations(connection: Any) -> None:
    """Version 7 : la corbeille des conversations (`deleted_at`, `purge_after`).

    `delete_conversation` ne détruit plus en cascade : il marque. La fiche
    `cycle_de_vie_des_informations.md` ne précise NI le mécanisme de marquage
    NI le délai (« corbeille » est listée à ses décisions encore ouvertes,
    §5.3) — le délai de sept jours vient du contrat d'annexe 10 (annexe10:146),
    déjà appliqué aux espaces par `TRASH_RETENTION_DAYS` et au registre des
    artefacts par `backend/search/trash.py` (`TRASH_DAYS = 7`).

    Le schéma retenu est le plus simple et le MÊME que `workspaces`
    (`trashed_at` / `purge_after`, version 3) : deux colonnes DATETIME
    nullables sur `conversations`. `NULL` ≡ active — une conversation sans
    marque est rigoureusement identique à une conversation d'avant la
    migration, donc la migration est ADDITIVE : aucune base existante ne
    change de comportement au démarrage, aucune donnée n'est réécrite.

    La réversibilité est l'abandon, pas la destruction : retirer le lot
    signifie retraiter ces colonnes comme nulles (`UPDATE ... SET
    deleted_at = NULL`), jamais un `DROP COLUMN` — les bases migrées gardent
    des colonnes inutilisées mais neutres.
    """
    if not _table_exists(connection, "conversations"):
        # Base sans socle : `create_all` créera `conversations` avec les colonnes
        # déjà déclarées dans le modèle. Même règle que `_migrate_etat_ingestion`.
        return
    _add_column(connection, "conversations", "deleted_at", "DATETIME")
    _add_column(connection, "conversations", "purge_after", "DATETIME")
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversations_purge_after "
            "ON conversations(purge_after) WHERE deleted_at IS NOT NULL"
        )
    )
    connection.execute(
        text(
            "UPDATE conversations SET purge_after = NULL"
            " WHERE deleted_at IS NULL AND purge_after IS NOT NULL"
        )
    )


def _migrate_memoire_taxonomy(connection: Any) -> None:
    """Version 8 : vocabulaire de la fiche de mémoire (catégorie + statut épistémique).

    `memory_entries` reçoit deux colonnes NULLABLES, `category` et
    `affirmation_status`, pour porter le vocabulaire de `memoire.md` §2 sans
    contraindre les fiches legacy ni les bases existantes. L'absence reste NULL,
    comme pour `origin_context` : la taxonomie est une annotation, pas une gate.
    """
    if not _table_exists(connection, "memory_entries"):
        # Base sans socle : `create_all` créera la table avec les colonnes déjà
        # déclarées dans le modèle. Même règle que les migrations précédentes.
        return
    _add_column(connection, "memory_entries", "category", "VARCHAR")
    _add_column(connection, "memory_entries", "affirmation_status", "VARCHAR")


def _migrate_memoire_syntheses(connection: Any) -> None:
    """Version 9 : dimension synthétique distincte (`memory_syntheses`)."""
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS memory_syntheses ("
            "id VARCHAR PRIMARY KEY, "
            "workspace_id VARCHAR NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE, "
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
            "CREATE INDEX IF NOT EXISTS idx_memory_syntheses_ws_current ON memory_syntheses(workspace_id, is_current)"
        )
    )


def _migrate_memoire_synthesis_idempotency(connection: Any) -> None:
    """Version 10 : idempotence de la synthèse d'espace (`idempotency_key`)."""
    if not _table_exists(connection, "memory_syntheses"):
        return
    _add_column(connection, "memory_syntheses", "idempotency_key", "VARCHAR")


def _migrate_memory_syntheses_unique_current(connection: Any) -> None:
    """Version 13 : une seule synthèse courante par espace, garantie par le schéma.

    Une synthèse courante est celle qui fait foi pour le contexte initial. Deux
    lignes `is_current=1` sur le même espace rendraient le choix dépendant du
    hasard de lecture. On conserve la version la plus haute (puis la plus
    récente à version égale) et on périme les autres AVANT de poser l'index :
    SQLite refuse un index unique sur des données qui le violent déjà.
    """
    if not _table_exists(connection, "memory_syntheses"):
        # Base sans socle : `create_all` créera la table avec l'index déclaré
        # dans le modèle. Même règle que les migrations précédentes.
        return
    rows = connection.execute(
        text(
            "SELECT workspace_id, id FROM memory_syntheses "
            "WHERE is_current = 1 "
            "ORDER BY workspace_id, version DESC, created_at DESC, id DESC"
        )
    ).fetchall()
    vus: set[str] = set()
    for workspace_id, synthesis_id in rows:
        espace = str(workspace_id)
        if espace not in vus:
            vus.add(espace)
            continue
        connection.execute(
            text(
                "UPDATE memory_syntheses "
                "SET is_current = 0, superseded_at = CURRENT_TIMESTAMP "
                "WHERE id = :id"
            ),
            {"id": str(synthesis_id)},
        )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_syntheses_current "
            "ON memory_syntheses(workspace_id) WHERE is_current = 1"
        )
    )


def _migrate_ingest_previous_state(connection: Any) -> None:
    """Version 14 : mémoriser l'état d'avant `en_cours` (`ingest_previous_state`).

    Sans cette colonne, un échec survenu après un succès écrivait `echec` et
    faisait perdre le fait qu'un texte restait lisible. La colonne est NULLABLE
    et additive : une base existante n'a rien à réécrire, et `marquer` la remplit
    seulement au prochain `en_cours`.
    """
    _add_column(connection, "artifacts", "ingest_previous_state", "VARCHAR")


def _migrate_ingest_backoff(connection: Any) -> None:
    """Version 15 : backoff persistant d'ingestion (tâche 2.2).

    `ingest_attempts` est NOT NULL avec défaut 0 : les lignes existantes sont
    des dettes qui n'ont pas encore été comptées, pas des succès. Les deux
    dates sont NULLABLES parce que « jamais tenté » et « pas encore dû » sont
    des absences réelles, pas des dates fabriquées.
    """
    _add_column(
        connection, "artifacts", "ingest_attempts", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column(connection, "artifacts", "ingest_last_attempt_at", "DATETIME")
    _add_column(connection, "artifacts", "ingest_next_attempt_at", "DATETIME")


def _migrate_ingest_heartbeat(connection: Any) -> None:
    """Version 16 : battement de cœur de l'extraction (tâche 2.5).

    Une extraction longue ne change pas d'état ; sans cette colonne, la seule
    date disponible serait celle du début, et un travail vivant passerait pour
    un zombie au bout de quinze minutes.
    """
    _add_column(connection, "artifacts", "ingest_heartbeat_at", "DATETIME")


def _migrate_ingest_claim(connection: Any) -> None:
    """Version 17 : claim d'ingestion tokenisé (Phase 3, tâche 3.2).

    Trois colonnes NULLABLES et additives : une base existante n'a rien à
    réécrire. `ingest_claim_token` porte le jeton propriétaire ; `ingest_claim_at`
    date l'acquisition (TTL 5 min) ; `ingest_claim_owner` nomme le propriétaire
    pour le diagnostic. L'unicité n'est pas portée par un index UNIQUE global :
    un claim expiré doit rester lisible pour le fencing, et deux claims expirés
    peuvent coexister transitoirement. L'acquisition atomique conditionnelle
    (WHERE claim_token IS NULL OR claim_at < now - 5min) est la seule garde.
    """
    _add_column(connection, "artifacts", "ingest_claim_token", "VARCHAR")
    _add_column(connection, "artifacts", "ingest_claim_at", "DATETIME")
    _add_column(connection, "artifacts", "ingest_claim_owner", "VARCHAR")


def _migrate_ingest_claim_indexes(connection: Any) -> None:
    """Version 18 : index du claim d'ingestion (Phase 3, tâche 3.2).

    Deux index, pour deux besoins distincts :

    1. `uq_artifacts_ingest_claim_token` — UNIQUE PARTIEL sur `ingest_claim_token`
       quand il n'est pas NULL. Un même jeton ne peut pas désigner deux artefacts :
       c'est la garantie qu'un `WHERE claim_token = :token` ne libère jamais la
       ligne d'un autre. Le filtre `WHERE ingest_claim_token IS NOT NULL` est
       indispensable : sans lui, l'index UNIQUE refuserait la seconde ligne sans
       claim (toutes les lignes NULL se heurteraient).
    2. `ix_artifacts_ingest_claim_at` — accélère le balayage des claims expirés
       (`claim_at < now - TTL`). Sans lui, chaque acquisition relit toute la table.

    L'unicité n'est PAS globale : un claim expiré reste lisible pour le fencing,
    et l'acquisition atomique conditionnelle reste la seule garde fonctionnelle.
    L'index est une ceinture de sécurité en plus de la condition.
    """
    if not _table_exists(connection, "artifacts"):
        # Base minimale (tests, socle absent) : `create_all` créera la table
        # avec ses index. Même règle que `_add_column` et la v19.
        return
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_ingest_claim_token "
            "ON artifacts(ingest_claim_token) WHERE ingest_claim_token IS NOT NULL"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_artifacts_ingest_claim_at "
            "ON artifacts(ingest_claim_at)"
        )
    )


def _migrate_artifact_links_removed_at_index(connection: Any) -> None:
    """Version 19 : index du GC sur `artifact_links(artifact_id, removed_at)`.

    La purge corrigée ne sélectionne plus « les artefacts sans lien ACTIF » (ce
    qui inclut la corbeille récente) mais « les artefacts dont aucun lien n'est
    actif ou récent ». La requête joint les liens sur `artifact_id` ET sur
    `removed_at >= cutoff` : sans index sur ce couple, chaque balayage relit
    toute la table `artifact_links` pour chaque artefact candidat.

    Additive et idempotente : `CREATE INDEX IF NOT EXISTS`, aucun backfill,
    aucune ligne réécrite. Le nom est celui que déclare le modèle, pour que
    `create_all` (base neuve) et la migration (base existante) ne créent pas
    deux index différents pour la même colonne.
    """
    if not _table_exists(connection, "artifact_links"):
        # Base sans socle : `create_all` créera la table avec l'index déjà
        # déclaré dans le modèle. Même règle que les migrations voisines.
        return
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_links_artifact_removed "
            "ON artifact_links(artifact_id, removed_at)"
        )
    )


def _migrate_memory_provenance(connection: Any) -> None:
    """Version 20 : provenance et invalidation à deux niveaux (Phase 5).

    Quatre colonnes NULLables, toutes additives et sans backfill :

    - `stale_reason` : le motif STRUCTURÉ de l'invalidation, distinct du récit
      `decision.reason`. La réhabilitation ne s'autorise que sur
      `source_inaccessible` ; une suppression de discussion ou une contradiction
      ne se réactive donc jamais par accident.
    - `previous_status` : statut d'avant la transition vers `stale`, écrit
      seulement à cette transition. Les fiches legacy n'en ont pas : l'absence
      est l'état réel, et la réhabilitation retombe alors sur `accepted` au plus.
    - `source_observation_id` / `extractor_version` : préparés pour l'extraction
      documentaire (Phase 8). Aucune écriture ici, aucune valeur inventée.

    `_add_column` est idempotent et ne touche pas une table absente : une base
    neuve reçoit ces colonnes par `create_all`, une base existante par cet ALTER.
    """
    _add_column(connection, "memory_entries", "stale_reason", "TEXT")
    _add_column(connection, "memory_entries", "previous_status", "VARCHAR")
    _add_column(connection, "memory_entries", "source_observation_id", "VARCHAR")
    _add_column(connection, "memory_entries", "extractor_version", "VARCHAR")


def _migrate_memory_document_extractions(connection: Any) -> None:
    """Version 21 : ledger d'extraction documentaire (Phase 8, tâche 8.1).

    Table NEUVE, entièrement `IF NOT EXISTS` : une base ancienne la reçoit ici,
    une base neuve la reçoit par `create_all`. Les deux chemins produisent le
    même DDL, y compris les deux index uniques PARTIELS qui rendent la clé
    `(workspace_id, artifact_id, observation_id, extractor_version)` correcte
    malgré la sémantique SQLite des NULL (deux NULL sont distincts).

    Aucune donnée n'est réécrite, aucun backfill n'est fait ici : le ledger se
    remplit par l'enqueue transactionnel et par la boucle de backfill. Une base
    migrée démarre donc avec zéro dette inventée.
    """
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS memory_document_extractions ("
            "id VARCHAR PRIMARY KEY, "
            "workspace_id VARCHAR NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE, "
            "artifact_id VARCHAR NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE, "
            "observation_id VARCHAR REFERENCES observations(id) ON DELETE SET NULL, "
            "chunk_id VARCHAR REFERENCES artifact_chunks(id) ON DELETE SET NULL, "
            "card_revision INTEGER NOT NULL DEFAULT 0, "
            "extractor_version VARCHAR NOT NULL, "
            "entry_key VARCHAR, "
            "status VARCHAR NOT NULL DEFAULT 'pending', "
            "claim_token VARCHAR, "
            "claimed_at DATETIME, "
            "heartbeat_at DATETIME, "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "deferrals INTEGER NOT NULL DEFAULT 0, "
            "next_attempt_at DATETIME, "
            "last_error TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_mdoc_status_next "
            "ON memory_document_extractions(status, next_attempt_at)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_mdoc_ws_artifact "
            "ON memory_document_extractions(workspace_id, artifact_id)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mdoc_extraction_obs "
            "ON memory_document_extractions("
            "workspace_id, artifact_id, observation_id, extractor_version"
            ") WHERE observation_id IS NOT NULL"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mdoc_extraction_sans_obs "
            "ON memory_document_extractions("
            "workspace_id, artifact_id, extractor_version"
            ") WHERE observation_id IS NULL"
        )
    )


def _migrate_representations_et_figures(connection: Any) -> None:
    """Version 22 : représentations complètes et file de figures (étape A).

    DEUX TABLES NEUVES, entièrement `IF NOT EXISTS` : une base ancienne les
    reçoit ici, une base neuve par `create_all`. Le DDL et les noms d'index sont
    ceux des modèles, pour que les deux chemins ne créent pas deux index pour la
    même colonne.

    TROIS GROUPES DE COLONNES ADDITIVES :

    - `artifacts.figures_*` : l'état et les compteurs des figures vivent dans
      l'artefact, comme `ingest_state`. Le défaut `sans` remplit les lignes déjà
      présentes : aucun backfill lourd, aucune dette inventée pour un patrimoine
      qui n'a jamais eu de figures.
    - `artifact_chunks.representation_id` / `figures_revision` / `text_sha256` :
      la provenance de la représentation. NULL / 0 pour les fragments legacy.

    L'INDEX UNIQUE `uq_representations_current` est PARTIEL (`WHERE is_current=1`) :
    plusieurs versions périmées coexistent, une seule est courante. C'est une
    décision de schéma, pas une promesse de code — deux ingestions concurrentes
    ne peuvent pas produire deux représentations courantes.
    """
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS artifact_representations ("
            "id VARCHAR PRIMARY KEY, "
            "artifact_id VARCHAR NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE, "
            "revision INTEGER NOT NULL, "
            "is_current INTEGER NOT NULL DEFAULT 0, "
            "markdown TEXT NOT NULL, "
            "blocks_json TEXT, "
            "chars_total INTEGER NOT NULL, "
            "markdown_sha256 VARCHAR NOT NULL, "
            "source_text_observation_id VARCHAR "
            "REFERENCES observations(id) ON DELETE SET NULL, "
            "source_extractor_version VARCHAR NOT NULL, "
            "figures_revision INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "superseded_at DATETIME)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_representations_current "
            "ON artifact_representations(artifact_id) WHERE is_current = 1"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_representations_artifact_revision "
            "ON artifact_representations(artifact_id, revision)"
        )
    )

    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS artifact_figures ("
            "id VARCHAR PRIMARY KEY, "
            "artifact_id VARCHAR NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE, "
            "representation_id VARCHAR NOT NULL "
            "REFERENCES artifact_representations(id) ON DELETE CASCADE, "
            "page INTEGER NOT NULL, "
            "region INTEGER NOT NULL, "
            "label VARCHAR, "
            "score REAL, "
            "bbox_json TEXT, "
            "crop_relpath VARCHAR, "
            "crop_sha256 VARCHAR, "
            "crop_width INTEGER, "
            "crop_height INTEGER, "
            "status VARCHAR NOT NULL DEFAULT 'pending', "
            "description TEXT, "
            "description_sha256 VARCHAR, "
            "description_observation_id VARCHAR, "
            "extractor_version VARCHAR NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "claim_token VARCHAR, "
            "claimed_at DATETIME, "
            "heartbeat_at DATETIME, "
            "next_attempt_at DATETIME, "
            "last_error TEXT, "
            "applied_revision INTEGER, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "done_at DATETIME, "
            "CONSTRAINT uq_figures_artifact_representation_page_region "
            "UNIQUE(artifact_id, representation_id, page, region))"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_figures_status_next "
            "ON artifact_figures(status, next_attempt_at)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_figures_artifact_status "
            "ON artifact_figures(artifact_id, status)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_figures_crop_sha256 "
            "ON artifact_figures(crop_sha256)"
        )
    )

    _add_column(
        connection, "artifacts", "figures_state", "VARCHAR NOT NULL DEFAULT 'sans'"
    )
    _add_column(connection, "artifacts", "figures_done", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "artifacts", "figures_total", "INTEGER NOT NULL DEFAULT 0")
    _add_column(
        connection, "artifacts", "figures_limited", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column(connection, "artifacts", "figures_reason", "TEXT")
    _add_column(connection, "artifacts", "figures_updated_at", "DATETIME")

    _add_column(connection, "artifact_chunks", "representation_id", "VARCHAR")
    _add_column(
        connection,
        "artifact_chunks",
        "figures_revision",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(connection, "artifact_chunks", "text_sha256", "VARCHAR")


def _register_migrations() -> tuple[Migration, ...]:
    global _MIGRATIONS
    if not _MIGRATIONS:
        _MIGRATIONS = (
            (1, "toolcall_durability", _migrate_toolcall_durability),
            (2, "web_cache", _migrate_web_cache),
            (3, "espaces_memoire", _migrate_espaces_memoire),
            (4, "memoire_indexes", _migrate_memory_indexes),
            (5, "etat_ingestion", _migrate_etat_ingestion),
            (6, "docrefs_unicite_label", _migrate_docrefs_unicite_label),
            (7, "corbeille_conversations", _migrate_corbeille_conversations),
            (8, "memoire_taxonomy", _migrate_memoire_taxonomy),
            (9, "memoire_syntheses", _migrate_memoire_syntheses),
            (
                10,
                "memoire_synthesis_idempotency",
                _migrate_memoire_synthesis_idempotency,
            ),
            (
                11,
                "workspace_auto_memory_turn_count",
                _migrate_workspace_auto_memory_turn_count,
            ),
            (
                12,
                "memory_exclusions_and_activity",
                _migrate_memory_exclusions,
            ),
            (
                13,
                "memory_syntheses_unique_current",
                _migrate_memory_syntheses_unique_current,
            ),
            (
                14,
                "ingest_previous_state",
                _migrate_ingest_previous_state,
            ),
            (
                15,
                "ingest_backoff",
                _migrate_ingest_backoff,
            ),
            (
                16,
                "ingest_heartbeat",
                _migrate_ingest_heartbeat,
            ),
            (
                17,
                "ingest_claim",
                _migrate_ingest_claim,
            ),
            (
                18,
                "ingest_claim_indexes",
                _migrate_ingest_claim_indexes,
            ),
            (
                19,
                "artifact_links_removed_at_index",
                _migrate_artifact_links_removed_at_index,
            ),
            (
                20,
                "memory_provenance",
                _migrate_memory_provenance,
            ),
            (
                21,
                "memory_document_extractions",
                _migrate_memory_document_extractions,
            ),
            (
                22,
                "representations_et_figures",
                _migrate_representations_et_figures,
            ),
        )
    return _MIGRATIONS


def _migrate_workspace_auto_memory_turn_count(connection: Any) -> None:
    """Version 11 : compteur de tours pour le rafraichissement automatique."""
    if not _table_exists(connection, "workspaces"):
        return
    _add_column(connection, "workspaces", "auto_memory_turn_count", "INTEGER")


def _migrate_memory_exclusions(connection: Any) -> None:
    """Version 12 : bornes d'activité de l'espace et exclusions durables.

    `last_activity_at` et `memory_last_processed_at` sont NULLABLES : une base
    migrée n'a pas d'historique à inventer, et l'absence est l'état réel (« on ne
    sait pas encore »), pas une date fabriquée. `_add_column` est idempotent et
    ne crée pas la table sur une base neuve — c'est `create_all` qui s'en charge.

    `memory_exclusions` est entièrement `IF NOT EXISTS` pour rester rejouable,
    et l'unicité `(workspace_id, content_hash)` est la garantie que deux
    exclusions du même contenu dans le même espace ne peuvent pas coexister.
    """
    _add_column(connection, "workspaces", "last_activity_at", "DATETIME")
    _add_column(connection, "workspaces", "memory_last_processed_at", "DATETIME")
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS memory_exclusions ("
            "id VARCHAR PRIMARY KEY, "
            "workspace_id VARCHAR NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE, "
            "content_hash VARCHAR(64) NOT NULL, "
            "entry_key VARCHAR, "
            "reason TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT uq_memory_exclusion_content UNIQUE(workspace_id, content_hash))"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_memory_exclusions_ws "
            "ON memory_exclusions(workspace_id)"
        )
    )


def apply_migrations(target_engine: Engine) -> None:
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name VARCHAR NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        applied = {
            int(row[0])
            for row in connection.execute(
                text("SELECT version FROM schema_migrations")
            ).fetchall()
        }

    for version, name, migration in _register_migrations():
        if version in applied:
            continue
        try:
            with target_engine.begin() as connection:
                migration(connection)
                connection.execute(
                    text(
                        "INSERT INTO schema_migrations(version, name) VALUES (:version, :name)"
                    ),
                    {"version": version, "name": name},
                )
        except Exception as exc:
            raise MigrationError(f"migration {version}:{name} failed") from exc

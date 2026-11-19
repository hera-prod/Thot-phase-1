"""Correctifs du service mémoire — schéma v12, exclusions et versions.

Ces tests prouvent les défauts corrigés, sans modèle ni GPU :

* une correction ne traverse pas les espaces et refuse un contenu hors bornes ;
* `mark_stale` est un état, pas un événement rejouable ;
* purger avec `exclure=True` dépose une exclusion durable qui survit à la fiche ;
* le rafraîchissement automatique versionne la MÊME fiche au lieu d'en semer une
  par tour, et l'idempotence suit le contenu ;
* `faits_courants` accepte un filtre de statuts sans changer son défaut ;
* `creer_synthese` ne laisse qu'une seule synthèse courante, même si la base en
  portait déjà plusieurs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import (
    Base,
    MemoryEntry,
    MemoryExclusion,
    MemorySynthesis,
    Workspace,
)
from backend.services import memoire as M


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Session sur base temporaire, injectée comme SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_fixes.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()


def _espace(db, workspace_id: str, name: str = "Espace") -> str:
    db.add(Workspace(id=workspace_id, name=name))
    db.commit()
    return workspace_id


# ---------- 1. `correct` : garde d'espace et bornes de contenu ----------


def test_correct_refuse_un_autre_espace(db):
    ws_a = _espace(db, "ws_a")
    _espace(db, "ws_b", "Autre")
    eid = M.propose(db, ws_a, "Fait initial.")["entry_id"]

    with pytest.raises(LookupError, match=r"\[MEMORY_NOT_IN_WORKSPACE\]"):
        M.correct(db, eid, "Correction.", workspace_id="ws_b")


def test_correct_refuse_un_contenu_trop_long(db):
    ws = _espace(db, "ws_long")
    eid = M.propose(db, ws, "Fait initial.")["entry_id"]

    with pytest.raises(ValueError, match=r"\[TOO_LONG\]"):
        M.correct(db, eid, "x" * (M.MAX_CONTENT_CHARS + 1), workspace_id=ws)

    # Rien n'a été écrit : la fiche est restée à sa version initiale.
    assert (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == M._current(db, eid).entry_key)
        .count()
        == 1
    )


# ---------- 2. `mark_stale` : idempotent ----------


def test_mark_stale_deux_fois_ne_cree_qu_une_version(db):
    ws = _espace(db, "ws_stale")
    eid = M.propose(db, ws, "Fait à invalider.")["entry_id"]
    key = M._current(db, eid).entry_key

    premier = M.mark_stale(db, eid, workspace_id=ws)
    second = M.mark_stale(db, eid, workspace_id=ws)

    assert premier["ok"] is True and premier["status"] == "stale"
    assert second["ok"] is True
    assert second["status"] == "stale"
    assert second["changed"] is False
    assert second["version"] == premier["version"]
    assert db.query(MemoryEntry).filter(MemoryEntry.entry_key == key).count() == 2


# ---------- 3. Purge avec exclusion durable ----------


def test_purge_avec_exclusion_survit_et_detecte_le_meme_contenu(db):
    ws = _espace(db, "ws_purge")
    eid = M.propose(db, ws, "Le projet utilise SQLite.")["entry_id"]

    out = M.purge_memory(db, eid, ws, exclure=True)

    assert out["ok"] is True
    assert out["excluded"] is True
    assert (
        db.query(MemoryExclusion).filter(MemoryExclusion.workspace_id == ws).count()
        == 1
    )
    # Le contenu est reconnu malgré la casse et les espaces superflus.
    assert M.est_exclu(db, ws, "  le   projet UTILISE sqlite.  ") is True
    # Une nouvelle fiche qui redirait la même chose est détectée.
    M.propose(db, ws, "Le projet utilise SQLite.")
    assert M.est_exclu(db, ws, "Le projet utilise SQLite.") is True
    # Une autre fiche, avec un autre contenu, n'est pas concernée.
    assert M.est_exclu(db, ws, "Le projet utilise PostgreSQL.") is False

    # Exclure deux fois ne duplique pas.
    encore = M.creer_exclusion(db, ws, "Le projet utilise SQLite.")
    assert encore["created"] is False
    assert len(M.lister_exclusions(db, ws)) == 1


# ---------- 4. Rafraîchissement : versions de la même fiche ----------


def test_rafraichir_versionne_la_meme_fiche_et_ne_duplique_pas(db):
    ws = _espace(db, "ws_auto")

    premier = M.rafraichir_memoire_espace(db, ws, contenu="Orientation alpha.")
    second = M.rafraichir_memoire_espace(db, ws, contenu="Orientation beta.")

    assert premier["ok"] is True and second["ok"] is True
    assert premier["entry_key"] == second["entry_key"] == f"space_auto:{ws}"
    assert (premier["version"], second["version"]) == (1, 2)

    # Même contenu rejoué : l'idempotence retourne l'existant, sans version de plus.
    troisieme = M.rafraichir_memoire_espace(db, ws, contenu="Orientation beta.")
    assert troisieme["ok"] is True
    assert troisieme["created"] is False
    assert (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == f"space_auto:{ws}")
        .count()
        == 2
    )


# ---------- 5. `faits_courants` : filtre optionnel de statuts ----------


def test_faits_courants_filtre_les_statuts_sans_changer_le_defaut(db):
    ws = _espace(db, "ws_faits")
    db.add_all(
        [
            MemoryEntry(
                id="mem_acc",
                entry_key="k_acc",
                workspace_id=ws,
                content="Fait accepté.",
                status="accepted",
                is_current=1,
            ),
            MemoryEntry(
                id="mem_rej",
                entry_key="k_rej",
                workspace_id=ws,
                content="Fait refusé.",
                status="rejected",
                is_current=1,
            ),
            MemoryEntry(
                id="mem_stale",
                entry_key="k_stale",
                workspace_id=ws,
                content="Fait périmé.",
                status="stale",
                is_current=1,
            ),
        ]
    )
    db.commit()

    acceptes = M.faits_courants(db, ws, statuts=("accepted",))
    assert [f["status"] for f in acceptes] == ["accepted"]
    assert {f["status"] for f in M.faits_courants(db, ws)} == {
        "accepted",
        "rejected",
        "stale",
    }


# ---------- 6. Synthèses : une seule courante ----------


def test_creer_synthese_ne_laisse_qu_une_synthese_courante(db):
    ws = _espace(db, "ws_syn")
    # Une base PEUT déjà porter deux courantes (ancienne course, écriture brute
    # antérieure à la migration v13). On retire l'index du moment pour simuler
    # cet état : sans lui, la contrainte refuserait la seconde insertion.
    db.execute(text("DROP INDEX IF EXISTS uq_memory_syntheses_current"))
    db.commit()
    db.add_all(
        [
            MemorySynthesis(
                id="syn_old_1",
                workspace_id=ws,
                content="Ancienne un.",
                version=1,
                is_current=1,
            ),
            MemorySynthesis(
                id="syn_old_2",
                workspace_id=ws,
                content="Ancienne deux.",
                version=2,
                is_current=1,
            ),
        ]
    )
    db.commit()

    out = M.creer_synthese(db, ws, "Synthèse neuve.", source_entry_ids=["mem_1"])

    courantes = (
        db.query(MemorySynthesis)
        .filter(MemorySynthesis.workspace_id == ws, MemorySynthesis.is_current == 1)
        .all()
    )
    assert len(courantes) == 1
    assert courantes[0].id == out["synthesis_id"]
    assert courantes[0].content == "Synthèse neuve."
    assert out["version"] == 3

    # `synthese_courante` rend la courante, pas la liste.
    courante = M.synthese_courante(db, ws)
    assert courante is not None
    assert courante["content"] == "Synthèse neuve."
    assert courante["version"] == 3
    assert courante["source_entry_ids"] == ["mem_1"]
    assert {"content", "version", "created_at", "source_entry_ids"} <= set(courante)


# ---------- 7. Migration v12 ----------


def test_migration_v12_cree_colonnes_et_table_sur_base_existante():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        # Le socle minimal dont les migrations antérieures ont besoin.
        connection.execute(
            text(
                "CREATE TABLE runs "
                "(id VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(text("CREATE TABLE messages (id VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE image_effects "
                "(id VARCHAR PRIMARY KEY, fingerprint VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE toolcalls "
                "(id VARCHAR PRIMARY KEY, run_id VARCHAR, tool_call_id VARCHAR)"
            )
        )
        # Une base d'avant la v12 : `workspaces` sans les colonnes d'activité.
        connection.execute(
            text(
                "CREATE TABLE workspaces ("
                "id VARCHAR PRIMARY KEY, "
                "name VARCHAR NOT NULL, "
                "state VARCHAR NOT NULL DEFAULT 'active', "
                "created_at DATETIME, "
                "updated_at DATETIME)"
            )
        )

    apply_migrations(engine)
    apply_migrations(engine)  # rejouable

    with engine.connect() as connection:
        colonnes = {
            row[1] for row in connection.execute(text("PRAGMA table_info(workspaces)"))
        }
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
        colonnes_exclusions = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(memory_exclusions)"))
        }
        index_exclusions = {
            row[1]
            for row in connection.execute(text("PRAGMA index_list(memory_exclusions)"))
        }
        versions = {
            row[0]
            for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }

    assert {"last_activity_at", "memory_last_processed_at"} <= colonnes
    assert "memory_exclusions" in tables
    assert {
        "id",
        "workspace_id",
        "content_hash",
        "entry_key",
        "reason",
        "created_at",
    } <= colonnes_exclusions
    assert "idx_memory_exclusions_ws" in index_exclusions
    assert 12 in versions


# ---------- 8. Une clé d'idempotence périmée ne ressuscite pas l'ancienne version ----------


def test_creer_synthese_recree_une_version_courante_apres_retour_au_meme_contenu(db):
    ws = _espace(db, "ws_syn_idem")

    v1 = M.creer_synthese(db, ws, "Synthèse A.", idempotency_key=f"synth:{ws}:a")
    v2 = M.creer_synthese(db, ws, "Synthèse B.", idempotency_key=f"synth:{ws}:b")
    assert (v1["version"], v2["version"]) == (1, 2)

    v3 = M.creer_synthese(db, ws, "Synthèse A.", idempotency_key=f"synth:{ws}:a")

    assert v3["version"] == 3
    courante = M.synthese_courante(db, ws)
    assert courante is not None
    assert courante["synthesis_id"] == v3["synthesis_id"]
    assert courante["content"] == "Synthèse A."
    assert courante["version"] == 3


# ---------- 9. Borne de provenance : les références les plus récentes ----------


def test_borner_references_garde_les_plus_recentes(db):
    recente = "discussion:conv_recente:message:msg_recente"
    ancienne = "discussion:conv_ancienne:message:msg_ancienne"
    maximum = len(recente) - 5

    borne = M._borner_references([ancienne, recente], maximum)

    assert borne == recente[:maximum]
    assert ancienne[:maximum] != borne


# ---------- 10. Borne des preuves : les 64 plus récentes ----------


def test_ajouter_preuve_garde_les_preuves_les_plus_recentes(db):
    ws = _espace(db, "ws_preuves")
    eid = M.propose(db, ws, "Fait à étayer par de nombreuses preuves.")["entry_id"]
    preuves = [f"msg_{i:02d}" for i in range(70)]

    out = M.ajouter_preuve(
        db,
        eid,
        origin_context="discussion:conv_preuves:message:msg_fait",
        evidence_ids=preuves,
    )

    assert len(out["evidence_ids"]) == 64
    assert out["evidence_ids"] == preuves[-64:]
    assert "msg_00" not in out["evidence_ids"]
    assert out["evidence_ids"][-1] == "msg_69"

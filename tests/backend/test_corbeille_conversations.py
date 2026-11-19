"""Corbeille des conversations (phase 3, tâche 3.1) — lot de conformité.

`cycle_de_vie_des_informations.md` §4.1/§4.2 : la suppression d'une discussion
retire la discussion et ses données propres des vues et usages actifs, ne
détruit ni le dépôt, ni les images préservées, ni les citations déjà portées
par d'autres discussions. L'ancien `DELETE` en cascade de `ipc/base.py`
contredisait le §4.2 ligne à ligne ; ce lot le remplace par deux temps :

  - mise à la corbeille marquée sept jours (annexe10:146) + nettoyage CIBLÉ
    immédiat (`artifact_links`, `doc_refs`, index de portée conversation,
    `web_queries`, caches web) — la corbeille ne garde que ce qui est nécessaire
    à la restauration de la conversation elle-même ;
  - purge physique à l'expiration (`balayer_corbeille`), avec zéro orphelin
    vérifié sous `PRAGMA foreign_keys = ON`.

Couvert ici : le marquage, la dépublication des vues, le nettoyage ciblé, la
migration additive, l'échéance et l'intégrité référentielle. Les images de
galerie et le verrou anti-purge rétroactive ont leurs propres fichiers de test.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    DocRef,
    Message,
    MessageAttachment,
    Run,
    Source,
    ToolCall,
)
from backend.services import corbeille as C


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Session sur base temporaire, injectée comme SessionLocal du projet.

    Même montage que `test_espaces.py`, avec le socle de production en plus :
    `apply_migrations` (tables web et espaces, v2/v3) et `ensure_fts`
    (`artifact_search`), donc le nettoyage ciblé s'exécute sur une base qui a
    déjà cherché — son périmètre réel n'est pas la base minimale.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'corbeille.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        from backend.search import chunks as _chunks, fts as _fts

        _fts.ensure_fts(s)
        _chunks.ensure_chunk_tables(s)
        yield s
    finally:
        s.close()


def _conversation(db, conv_id: str, *, messages: int = 0) -> str:
    db.add(Conversation(id=conv_id, title=f"Discussion {conv_id}", mode="chat"))
    for i in range(messages):
        db.add(Message(id=f"msg_{conv_id}_{i}", conv_id=conv_id, role="user",
                       content=f"message {i}"))
    db.commit()
    return conv_id


def _artefact(db, art_id: str, *, kind: str = "pdf") -> str:
    """Blob + artefact seuls : l'objet registre, sans aucun rattachement."""
    if db.query(Blob).filter(Blob.id == f"sha_{art_id}").first() is None:
        db.add(Blob(id=f"sha_{art_id}", sha256=f"sha_{art_id}",
                    relative_path=f"blobs/sha_{art_id}.bin", size_bytes=12,
                    mime_detected="application/pdf", state="available"))
        db.add(Artifact(id=art_id, blob_id=f"sha_{art_id}", kind=kind,
                        original_filename=f"{art_id}.pdf", display_name=f"{art_id}.pdf"))
        db.commit()
    return art_id


def _piece_jointe(db, art_id: str, conv_id: str, message_ids: list[str]) -> str:
    """Artefact + blob + lien conversation, tel qu'un import le crée."""
    _artefact(db, art_id)
    db.add(ArtifactLink(id=f"lnk_{art_id}_{conv_id}", artifact_id=art_id,
                        scope_kind="conversation", scope_id=conv_id, role="attachment"))
    for mid in message_ids:
        db.add(MessageAttachment(id=f"matt_{art_id}_{mid}", message_id=mid,
                                 artifact_id=art_id, ordinal=0))
    db.commit()
    return art_id


def _conversation_complete(db, conv_id: str, *, piece_art: str | None = None) -> str:
    """Une discussion qui a vraiment vécu : message, run, pièce, toolcall, source.

    `piece_art` nomme l'artefact de la pièce jointe de `msg_0` : sans lui, le
    message reste seul — un FK ouvert exigerait un artefact réel.
    """
    _conversation(db, conv_id, messages=1)
    db.add(Run(id=f"run_{conv_id}", conv_id=conv_id, request_id=f"req_{conv_id}"))
    if piece_art is not None:
        db.add(MessageAttachment(id=f"matt_{conv_id}", message_id=f"msg_{conv_id}_0",
                                 artifact_id=piece_art, ordinal=0))
    db.add(ToolCall(id=f"tc_{conv_id}", msg_id=f"msg_{conv_id}_0", tool="cheap"))
    db.add(Source(id=f"src_{conv_id}", msg_id=f"msg_{conv_id}_0", source_type="attachment"))
    db.commit()
    return conv_id


# ---------------------------------------------------------------------------
# 3.1 — mise à la corbeille : marquage et nettoyage ciblé
# ---------------------------------------------------------------------------

def test_la_mise_en_corbeille_marque_sans_detruire(db):
    """Un delete.conversation ne détruit plus : il dépublie et note l'échéance."""
    _conversation(db, "conv_a")
    retour = C.mettre_en_corbeille(db, "conv_a")
    assert retour["ok"] is True
    assert retour["retention_days"] == 7
    conv = db.query(Conversation).filter(Conversation.id == "conv_a").first()
    assert conv is not None, "la ligne de conversation reste en corbeille"
    assert conv.deleted_at is not None
    assert conv.purge_after is not None
    echue = datetime.fromisoformat(retour["purge_after"])
    base = conv.deleted_at.replace(tzinfo=None) if conv.deleted_at.tzinfo else conv.deleted_at
    assert timedelta(days=7) - timedelta(seconds=30) <= echue - base <= timedelta(days=8)


def test_la_mise_en_corbeille_est_idempotente(db):
    """Un re-delete ne raccourcit ni ne rallonge la fenêtre en cours."""
    _conversation(db, "conv_a")
    premiere = C.mettre_en_corbeille(db, "conv_a")
    echue_avant = db.query(Conversation).filter(
        Conversation.id == "conv_a").first().purge_after
    seconde = C.mettre_en_corbeille(db, "conv_a")
    assert seconde["already_trashed"] is True
    assert seconde["purge_after"] == premiere["purge_after"]
    assert db.query(Conversation).filter(
        Conversation.id == "conv_a").first().purge_after == echue_avant


def test_une_conversation_inconnue_ne_se_marque_pas(db):
    """Le refus d'existence n'est pas une panne : `[CONVERSATION_NOT_FOUND]`."""
    with pytest.raises(LookupError) as attrape:
        C.mettre_en_corbeille(db, "conv_nul")
    assert "CONVERSATION_NOT_FOUND" in str(attrape.value)


def test_les_vues_actives_ne_voient_plus_la_discussion(db):
    """§4.1 : retirée des vues et usages actifs, pas de l'existence.

    Listes IPC, fiche, historique, renommage : quatre façons de la lire,
    aucune ne la rend. L'historique rend le même vide que celui du pont
    d'origine pour un id inconnu — une 404 logique, jamais une exception.
    """
    from backend.ipc import base as I

    _conversation_complete(db, "conv_a")
    retour = I.delete_conversation(db, {"conversationId": "conv_a"})
    assert retour["ok"] is True and retour["retention_days"] == 7

    listing = I.list_conversations(db, {"limit": 50, "offset": 0})
    assert "conv_a" not in [c["id"] for c in listing["conversations"]]
    assert I.get_conversation(db, {"conversationId": "conv_a"}) == {}
    assert I.list_messages(db, {"conversationId": "conv_a"}) == {"messages": [], "count": 0}
    with pytest.raises(ValueError):
        I.rename_conversation(db, {"conversationId": "conv_a", "title": "nouveau"})


def test_le_nettoyage_cible_court_a_la_mise_en_corbeille(db):
    """§4.2 : liens, `doc_refs`, index de portée conversation et web — tous retirés.

    Le choix d'implémentation sur le point que la fiche laisse ouvert (§5.3
    liste la corbeille parmi ses décisions non tranchées) : le nettoyage court
    À LA mise en corbeille, pas à l'expiration ; la corbeille ne garde que la
    ligne de conversation et ses messages, ce qui suffit à la restaurer.
    """
    from sqlalchemy import text as T

    _conversation(db, "conv_a", messages=1)
    _piece_jointe(db, "art_piece", "conv_a", ["msg_conv_a_0"])
    db.add(DocRef(id="docref_1", conv_id="conv_a", doc_label="doc_1",
                  doc_index=1, artifact_id="art_piece"))
    db.commit()
    db.execute(T(
        "INSERT INTO artifact_search (artifact_id, scope_kind, scope_id, kind,"
        " card_revision, filename, title, summary, keywords, entities,"
        " extracted_preview) VALUES ('art_piece', 'conversation', 'conv_a',"
        " 'pdf', 0, 'fichier', 'titre', 'resume', 'mots', 'entites', 'apercu')"))
    db.execute(T(
        "INSERT INTO chunk_search (artifact_id, scope_kind, scope_id, text,"
        " context, chunk_id) VALUES ('art_piece', 'conversation', 'conv_a',"
        " 'passage', '', 'chk_1')"))
    db.execute(T(
        "INSERT INTO web_queries (id, conv_id, query_key)"
        " VALUES ('wq_1', 'conv_a', 'cle')"))
    db.execute(T(
        "INSERT INTO web_snapshots (id, public_id, conv_id, requested_url,"
        " final_url, status, content_type, title, content_sha256, text,"
        " extractor_version, scope_kind, scope_id, query_key, source_type,"
        " proposed_by, reused_count) VALUES ('ws1', 'web_abcdef123456', 'conv_a',"
        " 'https://x', 'https://x', 200, 'text/html', 'T', 'sha1', 'corps',"
        " 'v1', 'conversation', 'conv_a', 'cle', 'general', 'visiteur', 0)"))
    db.commit()

    C.mettre_en_corbeille(db, "conv_a")

    liens = db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "conversation").all()
    assert len(liens) == 1 and liens[0].removed_at is not None, \
        "le lien est retiré logiquement, pas détruit"
    refs = db.query(DocRef).filter(DocRef.conv_id == "conv_a").all()
    assert len(refs) == 1 and refs[0].removed_at is not None, \
        "le doc_N est retiré logiquement : un numéro de suppression n'est jamais recyclé"
    assert db.execute(T("SELECT COUNT(*) FROM artifact_search"
                        " WHERE scope_id = 'conv_a'")).scalar_one() == 0
    assert db.execute(T("SELECT COUNT(*) FROM chunk_search"
                        " WHERE scope_id = 'conv_a'")).scalar_one() == 0
    assert db.execute(T("SELECT COUNT(*) FROM web_queries"
                        " WHERE conv_id = 'conv_a'")).scalar_one() == 0
    assert db.execute(T("SELECT COUNT(*) FROM web_snapshots"
                        " WHERE conv_id = 'conv_a'")).scalar_one() == 0


def test_les_pieces_jointes_surviennent_a_la_mise_en_corbeille(db):
    """La conversation part, le registre des artefacts reste intact.

    Un lien retiré n'est pas un artefact supprimé : blob, artefact et fiche
    vivent au registre (§4.2, « images jointes ou générées préservées » ;
    et le verrou anti-purge pour les fichiers cités ailleurs).
    """
    _conversation(db, "conv_a")
    _piece_jointe(db, "art_piece", "conv_a", ["msg_conv_a_0"])
    db.add(ArtifactCard(id="card_piece", artifact_id="art_piece", revision=1,
                        is_current=1, title="Pièce"))
    db.commit()
    C.mettre_en_corbeille(db, "conv_a")
    art = db.query(Artifact).filter(Artifact.id == "art_piece").first()
    assert art is not None and art.deleted_at is None, \
        "l'artefact ne suit pas la corbeille de la conversation"
    blob = db.query(Blob).filter(Blob.id == "sha_art_piece").first()
    assert blob is not None, "le blob reste le sien"
    carte = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == "art_piece").first()
    assert carte is not None, "la fiche de l'artefact reste vivante"


# ---------------------------------------------------------------------------
# Migration additive — les bases existantes ne cassent pas
# ---------------------------------------------------------------------------

def test_migration_v7_est_additive_et_rejouable(tmp_path):
    """Deux colonnes nullables posées sur une base préexistante, puis rejouées.

    Une base « d'avant le lot » : le socle complet FOIS une table
    `conversations` au DDL d'AVANT (sans `deleted_at`). Elle migre, garde ses
    lignes EXACTES, et un second `apply_migrations` ne fait rien de plus : la
    migration est additive, idempotente, et ne réécrit aucune donnée.
    """
    fichier = tmp_path / "ancienne.db"
    engine = create_engine(f"sqlite:///{fichier}")
    av_conversations = [
        table for table in Base.metadata.sorted_tables
        if table.name != "conversations"
    ]
    Base.metadata.create_all(bind=engine, tables=av_conversations)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE conversations (id VARCHAR PRIMARY KEY,"
            " title VARCHAR, mode VARCHAR, created_at DATETIME,"
            " updated_at DATETIME)"))
        conn.execute(text(
            "INSERT INTO conversations (id, title, mode)"
            " VALUES ('conv_legacy', 'Ancienne', 'chat')"))
    apply_migrations(engine)
    apply_migrations(engine)  # rejouable : une base déjà au niveau ne redémarre rien

    with engine.begin() as conn:
        colonnes = {row[1] for row in
                    conn.execute(text("PRAGMA table_info(conversations)")).fetchall()}
    assert {"deleted_at", "purge_after"} <= colonnes
    with engine.begin() as conn:
        ligne = conn.execute(text(
            "SELECT id, title, deleted_at, purge_after FROM conversations"
            " WHERE id = 'conv_legacy'")).fetchone()
    assert ligne == ("conv_legacy", "Ancienne", None, None), \
        "aucune ligne préexistante n'est marquée ni modifiée par la migration"


# ---------------------------------------------------------------------------
# Purge à l'expiration
# ---------------------------------------------------------------------------

def test_balayer_corbeille_purge_seulement_les_echues(db):
    """Le balayage purge une conversation échue, jamais une en attente."""
    _conversation_complete(db, "conv_echue")
    _conversation_complete(db, "conv_attente")
    C.mettre_en_corbeille(db, "conv_echue")
    C.mettre_en_corbeille(db, "conv_attente")
    # L'échéance : remonte la seule conversation échue de huit jours en arrière.
    echue = db.query(Conversation).filter(Conversation.id == "conv_echue").first()
    echue.purge_after = echue.purge_after - timedelta(days=8)
    db.commit()

    purges = C.balayer_corbeille(db)
    assert purges == 1
    assert db.query(Conversation).filter(
        Conversation.id == "conv_echue").first() is None
    attente = db.query(Conversation).filter(Conversation.id == "conv_attente").first()
    assert attente is not None and attente.deleted_at is not None, \
        "l'attente reste en corbeille : elle n'est pas échue"


def test_balayer_est_silencieux_sans_echue(db):
    """Zéro échéance : zéro purge, zéro effet de bord. Le cas à zéro."""
    _conversation(db, "conv_a")
    C.mettre_en_corbeille(db, "conv_a")
    assert C.balayer_corbeille(db) == 0
    assert db.query(Conversation).filter(Conversation.id == "conv_a").first() is not None


def test_la_purge_refuse_une_conversation_active(db):
    """Purger un actif supprimerait sans jamais mettre en corbeille : refus ferme."""
    _conversation(db, "conv_a")
    with pytest.raises(LookupError) as attrape:
        C.purger_conversation(db, "conv_a")
    assert "CONVERSATION_NOT_TRASHED" in str(attrape.value)


def test_purge_acheve_les_donnees_conversationnelles_restantes(db):
    """À l'expiration, la purge détruit les données PROPRES, rien de plus.

    Messages, runs, snapshots, toolcalls, sources, pièces de message partent
    avec la discussion (§4.2, « retirés avec la discussion »). Les artefacts
    et les blobs, jamais.
    """
    _conversation_complete(db, "conv_a")
    art = _piece_jointe(db, "art_piece", "conv_a", ["msg_conv_a_0"])
    C.mettre_en_corbeille(db, "conv_a")
    echue = db.query(Conversation).filter(Conversation.id == "conv_a").first()
    echue.purge_after = echue.purge_after - timedelta(days=8)
    # autoflush=False sur toute la session du projet : la mutation d'échéance
    # doit être portée à la base AVANT le balayage, sinon la requête des échues
    # lit l'ancien calendrier.
    db.commit()
    C.balayer_corbeille(db)

    assert db.query(Conversation).filter(Conversation.id == "conv_a").first() is None
    assert db.query(Message).filter(Message.conv_id == "conv_a").first() is None
    assert db.query(Run).filter(Run.conv_id == "conv_a").first() is None
    assert db.query(MessageAttachment).all() == [], "les pièces de message suivent"
    restant = db.query(ArtifactLink).filter(ArtifactLink.scope_kind == "conversation").all()
    assert restant == [], "les liens conversationnels sont détruits avec elle"
    assert db.query(DocRef).filter(DocRef.conv_id == "conv_a").first() is None
    piece = db.query(Artifact).filter(Artifact.id == art).first()
    assert piece is not None and piece.deleted_at is None, \
        "l'artefact cité n'a jamais payé la purge de la discussion"


def test_zero_orphelin_apres_purge_avec_foreign_keys_ouvertes(tmp_path):
    """La vérification du contrat : aucune ligne orpheline ne subsiste après purge.

    La contrainte est VÉRIFIÉE en base, pas au code : `PRAGMA foreign_keys = ON`
    est posé sur la connexion, et `PRAGMA foreign_key_check` doit rester vide
    après la purge — messages, runs, snapshots, toolcalls, sources et pièces de
    message n'ayant plus de parent.
    """
    fichier = tmp_path / "orphelins.db"
    engine = create_engine(f"sqlite:///{fichier}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               autocommit=False, expire_on_commit=False)
    s = TestSession()
    # La session est ouverte : le PRAGMA doit être posé sur LA connexion brute
    # en usage, pas sur une autre — SQLAlchemy ignore le PRAGMA dans une
    # transaction déjà ouverte, donc il se pose AVANT tout usage.
    _artefact(s, "art_piece")
    _conversation_complete(s, "conv_orphelines", piece_art="art_piece")
    _piece_jointe(s, "art_piece", "conv_orphelines", ["msg_conv_orphelines_0"])
    s.commit()
    C.mettre_en_corbeille(s, "conv_orphelines")
    rapport = C.purger_conversation(s, "conv_orphelines", seulement_expiree=False)
    assert rapport["purged"] is True
    s.commit()

    # La mise en transaction de SQLAlchemy ferme la connexion à chaque commit :
    # le PRAGMA est posé sur LA connexion brute, donc avant toute relecture on
    # reprend la suivante (même connexion poolie, le PRAGMA y est toujours).
    verification = s.connection()
    verification.exec_driver_sql("PRAGMA foreign_keys = ON")
    assert verification.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1, \
        "le PRAGMA reste ouvert après les commits intermédiaires"
    assert verification.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == [], \
        "aucune violation de clé étrangère ne subsiste"
    for table in ("messages", "runs", "context_snapshots",
                  "toolcalls", "sources", "message_attachments"):
        restes = s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        assert restes == 0, f"{table} garde des lignes après la purge de sa discussion"
    assert s.query(Conversation).filter(
        Conversation.id == "conv_orphelines").first() is None
    s.close()

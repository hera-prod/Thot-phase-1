"""L'unicité du `doc_N` doit être une vérité de la BASE, pas une promesse de code.

Ces tests échouent tous si quelqu'un supprime `uq_doc_refs_conv_label`, ou revient
à un `max(doc_index) + 1` nu. Un test qui passe après la régression n'est pas un
test : chacun d'eux observe un fait que le code seul ne peut pas garantir.

Toutes les bases sont neuves, dans `tmp_path`. La base de l'utilisateur n'est ni
lue ni écrite — le schéma de session vient du `create_all` local, comme dans
`test_public_id_contract.py`.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import Artifact, ArtifactLink, Base, Blob, Conversation, DocRef
from backend.search import docrefs


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'docrefs.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _artifact(db, artifact_id: str, filename: str) -> None:
    db.add(Blob(
        id=f"blob_{artifact_id}",
        sha256=f"sha_{artifact_id}",
        relative_path=f"blobs/{artifact_id}.bin",
        size_bytes=1,
        mime_detected="text/plain",
    ))
    db.add(Artifact(
        id=artifact_id,
        blob_id=f"blob_{artifact_id}",
        kind="text",
        original_filename=filename,
        display_name=filename,
    ))


def _conversation_avec(session_factory, conv_id: str, nb_artefacts: int):
    """Une conversation, `nb_artefacts` fichiers liés, un DocRef par fichier."""
    db = session_factory()
    db.add(Conversation(id=conv_id, title=conv_id, mode="chat"))
    for i in range(nb_artefacts):
        artifact_id = f"{conv_id}_art{i}"
        _artifact(db, artifact_id, f"f{i}.txt")
        db.add(ArtifactLink(id=f"{conv_id}_lnk{i}", artifact_id=artifact_id,
                            scope_kind="conversation", scope_id=conv_id))
    db.commit()
    labels = [docrefs.allocate_doc_label(db, conv_id, f"{conv_id}_art{i}")
              for i in range(nb_artefacts)]
    db.commit()
    db.close()
    return labels


# --------------------------------------------------------------------------
# 1. La contrainte existe, et elle est bien là où le contrat la veut
# --------------------------------------------------------------------------
def test_la_base_refuse_deux_doc_label_pour_une_meme_conversation(session_factory):
    """Le refus vient de la base, pas d'un `if` dans le code."""
    db = session_factory()
    try:
        _artifact(db, "art_a", "a.txt")
        _artifact(db, "art_b", "b.txt")
        db.add(DocRef(id="dref_1", conv_id="conv_a", doc_label="doc_1",
                      doc_index=1, artifact_id="art_a"))
        db.commit()
        with pytest.raises(IntegrityError):
            db.add(DocRef(id="dref_2", conv_id="conv_a", doc_label="doc_1",
                          doc_index=1, artifact_id="art_b"))
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_l_unicite_survit_a_un_document_supprime(session_factory):
    """SANS filtre sur `removed_at` : un `doc_N` supprime garde son numero.

    Un index partiel `WHERE removed_at IS NULL` passerait le test précédent et
    échouerait celui-ci. C'est exactement le piège : la contrainte y semble
    présente, et libère en silence le numéro d'un document retiré.
    """
    db = session_factory()
    try:
        _artifact(db, "art_a", "a.txt")
        _artifact(db, "art_b", "b.txt")
        db.add(DocRef(id="dref_1", conv_id="conv_a", doc_label="doc_1",
                      doc_index=1, artifact_id="art_a",
                      removed_at=datetime.now(timezone.utc)))
        db.commit()
        with pytest.raises(IntegrityError):
            db.add(DocRef(id="dref_2", conv_id="conv_a", doc_label="doc_1",
                          doc_index=1, artifact_id="art_b"))
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_meme_label_dans_deux_conversations_differentes(session_factory):
    """L'unicité est POR CONVERSATION : `doc_1` reste un numéro local."""
    db = session_factory()
    try:
        _artifact(db, "art_a", "a.txt")
        db.add(DocRef(id="dref_1", conv_id="conv_a", doc_label="doc_1",
                      doc_index=1, artifact_id="art_a"))
        db.add(DocRef(id="dref_2", conv_id="conv_b", doc_label="doc_1",
                      doc_index=1, artifact_id="art_a"))
        db.commit()
        assert db.query(DocRef).count() == 2
    finally:
        db.close()


# --------------------------------------------------------------------------
# 2. L'allocation, répétée puis concurrente
# --------------------------------------------------------------------------
def test_deux_allocations_repetees_donnent_des_labels_distincts(session_factory):
    _conversation_avec(session_factory, "conv_rep", 2)
    db = session_factory()
    try:
        premier = docrefs.allocate_doc_label(db, "conv_rep", "conv_rep_art0")
        second = docrefs.allocate_doc_label(db, "conv_rep", "conv_rep_art1")
        troisieme = docrefs.allocate_doc_label(db, "conv_rep", "conv_rep_art0")
        db.commit()
        assert [premier, second] == ["doc_1", "doc_2"]
        assert troisieme == premier, "un artefact déjà nommé rend son label, pas un nouveau"
    finally:
        db.close()


def test_allocation_concurrente_ne_duplique_pas_le_label(session_factory):
    """Deux sessions, deux transactions, le MÊME `doc_1` proposé deux fois.

    L'entrelacement est forcé par un vrai hook SQLAlchemy : une seconde session
    commit son `doc_1` entre la lecture de `max` et l'INSERT de la première. Ce
    n'est pas une simulation de course — c'est la course, avec deux connexions
    et deux transactions réelles. Sans l'index unique, les deux lignes sont
    acceptées et `doc_1` désigne deux fichiers différents.
    """
    db = session_factory()
    concurrent = session_factory()
    try:
        _artifact(db, "art_1", "un.txt")
        _artifact(db, "art_2", "deux.txt")
        db.commit()

        arme = {"oui": False}

        def _commit_de_la_concurrente_avant_le_flush(_session, _ctx, _instances):
            if arme["oui"]:
                return
            arme["oui"] = True
            concurrent.add(DocRef(id="dref_concurrent", conv_id="conv_race",
                                 doc_label="doc_1", doc_index=1, artifact_id="art_2"))
            concurrent.commit()

        from sqlalchemy import event

        event.listen(db, "before_flush", _commit_de_la_concurrente_avant_le_flush)

        # Le lien de conversation est créé par l'appel lui-même, donc la
        # session concurrente peut écrire sans dépendre de `db`.
        label = docrefs.allocate_doc_label(db, "conv_race", "art_1")
        db.commit()
    finally:
        db.close()
        concurrent.close()

    assert label == "doc_2", "le doc_1 déjà pris par l'autre transaction n'est pas réutilisé"
    db = session_factory()
    try:
        labels = [str(r.doc_label) for r in db.query(DocRef).order_by(DocRef.doc_index).all()]
    finally:
        db.close()
    assert labels == ["doc_1", "doc_2"]


def test_cinq_allocations_par_fils_reels_restent_distinctes(session_factory, tmp_path):
    """La course réelle : N fils, N sessions, un `doc_N` chacun.

    N threads et pas une simulation, parce que c'est le seul endroit où l'on
    observe ce que fait vraiment SQLite quand deux transactions veulent écrire en
    même temps. On n'affirme que ce qui est vrai dans les deux cas : aucun
    doublon. Le nombre exact de labels attribués dépend du verrou SQLite, pas de
    notre code — mais l'UNICITÉ, elle, ne dépend que de la contrainte.
    """
    nb = 5
    _conversation_avec(session_factory, "conv_thr", nb)
    # Une base FICHIER : deux fils ne peuvent pas partager `:memory:`.
    chemin = tmp_path / "fils.db"
    moteur = create_engine(f"sqlite:///{chemin}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=moteur)
    fabrique = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)

    amorce = fabrique()
    amorce.add(Conversation(id="conv_fils", title="f", mode="chat"))
    for i in range(nb):
        _artifact(amorce, f"art{i}", f"f{i}.txt")
    amorce.commit()
    amorce.close()

    depart = threading.Barrier(nb, timeout=30)
    obtenus: dict[int, object] = {}
    erreurs: dict[int, str] = {}

    def _alloue(index: int) -> None:
        session = fabrique()
        try:
            depart.wait()
            obtenus[index] = docrefs.allocate_doc_label(session, "conv_fils", f"art{index}")
            session.commit()
        except Exception as exc:  # noqa: BLE001 — le test rapporte, il ne masque pas
            erreurs[index] = type(exc).__name__
        finally:
            session.close()

    fils = [threading.Thread(target=_alloue, args=(i,)) for i in range(nb)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()

    assert not erreurs, f"aucune allocation ne doit échouer : {erreurs}"
    assert len(set(obtenus.values())) == nb, f"labels dupliqués : {obtenus}"

    lecture = fabrique()
    try:
        lignes = lecture.execute(text(
            "SELECT doc_label, COUNT(*) FROM doc_refs WHERE conv_id='conv_fils'"
            " GROUP BY doc_label"
        )).fetchall()
    finally:
        lecture.close()
        moteur.dispose()
    assert lignes and all(compte == 1 for _, compte in lignes), lignes


def test_une_contrainte_non_pertinente_remonte_telle_quelle(session_factory, monkeypatch):
    """Une IntegrityError qui n'est PAS l'unicité du label doit remonter.

    Le test le distingue des collisions par une LECTURE (le label existe-t-il
    déjà ?), pas en comptant les retries. Donc une vraie faute — ici une clé
    primaire dupliquée — doit sortir immédiatement, jamais bouclée quatre fois
    pour finir en `RuntimeError` : ce serait transformer un bug en lenteur.
    """
    db = session_factory()
    try:
        _artifact(db, "art_a", "a.txt")
        _artifact(db, "art_b", "b.txt")
        # Un DocRef déjà attribué, mais à un AUTRE artefact : l'allocation de
        # `art_b` part donc bien d'une insertion, et non du court-circuit
        # « cet artefact a déjà un label ». `doc_2` est libre : seule la clé
        # primaire peut faire échouer l'insertion.
        db.add(DocRef(id="dref_fixe", conv_id="conv_bug", doc_label="doc_1",
                      doc_index=1, artifact_id="art_a"))
        db.commit()

        monkeypatch.setattr(
            docrefs.uuid, "uuid4", lambda: SimpleNamespace(hex="fixe"))

        with pytest.raises(IntegrityError):
            docrefs.allocate_doc_label(db, "conv_bug", "art_b")
    finally:
        db.rollback()
        db.close()


def test_les_essais_sont_bornes(session_factory, monkeypatch):
    """Un registre saturé s'annonce ; il ne boucle pas indéfiniment."""
    db = session_factory()
    try:
        _artifact(db, "art_a", "a.txt")
        _artifact(db, "art_b", "b.txt")
        db.add(DocRef(id="dref_1", conv_id="conv_saturee", doc_label="doc_1",
                      doc_index=1, artifact_id="art_a"))
        db.commit()

        def _prochain_toujours_1(_db, _conv_id):
            # Un registre qui propose sans cesse le MÊME numéro pris : la base
            # refuse, `_allouer` réessaie, et son budget doit finir par parler.
            return 1

        monkeypatch.setattr(docrefs, "_prochain_index", _prochain_toujours_1)
        with pytest.raises(RuntimeError) as echec:
            docrefs.allocate_doc_label(db, "conv_saturee", "art_b")
        assert str(docrefs._TENTATIVES_ALLOCATION) in str(echec.value)
    finally:
        db.rollback()
        db.close()


# --------------------------------------------------------------------------
# 3. Un numéro supprimé ne revient pas
# --------------------------------------------------------------------------
def test_un_document_supprime_garde_son_numero(session_factory):
    """Ni la suppression ni le retrait ne rendent `doc_1` disponible.

    Sans la contrainte SANS filtre sur `removed_at`, le retrait ferait aussi
    disparaître `doc_1` de l'index unique : la réallocation suivante le rendrait
    à un AUTRE fichier, et l'orchestrateur citerait `doc_1` pour deux documents
    différents selon le moment où on regarde.
    """
    _conversation_avec(session_factory, "conv_num", 2)

    db = session_factory()
    try:
        db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == "conv_num_art0"
        ).one().removed_at = datetime.now(timezone.utc)
        db.query(DocRef).filter(DocRef.artifact_id == "conv_num_art0").one().removed_at = \
            datetime.now(timezone.utc)
        db.commit()

        _artifact(db, "art_nouveau", "nouveau.txt")
        db.add(ArtifactLink(id="lnk_nouveau", artifact_id="art_nouveau",
                            scope_kind="conversation", scope_id="conv_num"))
        db.commit()

        assert docrefs.allocate_doc_label(db, "conv_num", "art_nouveau") == "doc_3"
        db.commit()
        labels = [str(r.doc_label) for r in db.query(DocRef)
                  .filter(DocRef.conv_id == "conv_num").order_by(DocRef.doc_index).all()]
        assert labels == ["doc_1", "doc_2", "doc_3"], "doc_1 est gardé, jamais recyclé"
    finally:
        db.close()


def test_reactivation_rend_son_propre_numero(session_factory):
    """Réactiver un document rend LE SIEN, pas le premier libre."""
    _conversation_avec(session_factory, "conv_react", 2)

    db = session_factory()
    try:
        db.query(DocRef).filter(DocRef.artifact_id == "conv_react_art0").one().removed_at = \
            datetime.now(timezone.utc)
        db.commit()
        assert docrefs.allocate_doc_label(db, "conv_react", "conv_react_art0") == "doc_1"
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# 4. La migration : présente, rejouable, et alignée sur `create_all`
# --------------------------------------------------------------------------
def _schema_doc_refs(engine) -> dict[tuple[str, str], str]:
    """Le schéma `doc_refs` tel que SQLite le stocke, DDL brut compris.

    Le DDL est NORMALISÉ (espaces repliés, parenthèses resserrées) parce que les
    deux chemins n'écrivent pas la même chaîne : l'ORM produit `doc_refs (a, b)`,
    la migration brute `doc_refs(a, b)`. Ce sont les mêmes objets. Comparer les
    chaînes brutes ferait échouer le test sur une typographie et le laisserait
    passer sur une contrainte manquante — l'inverse de ce qu'on veut.
    """
    with engine.connect() as connection:
        lignes = connection.execute(text(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='doc_refs'"
        )).fetchall()
    return {(row[0], row[1]): _ddl_normalise(row[2]) for row in lignes}


def _ddl_normalise(sql: object) -> str:
    return (re.sub(r"\s+", " ", str(sql))
            .replace("( ", "(").replace(" (", "(").replace(" )", ")").strip())


def _doc_refs_ancienne(url: str) -> Engine:
    """Une base EN SERVICE : `doc_refs` SANS l'index, migrations 1 à 5 déjà passées.

    La table est créée par le modèle PUIS son index retiré : c'est exactement
    l'état d'une base créée avant que la contrainte existe, et c'est le seul
    état que la migration doit savoir réparer (`create_all` ne rajoute rien à
    une table qui existe déjà). Les versions antérieures sont marquées
    appliquées parce que ce test éprouve la version 6, pas le socle.
    """
    moteur = create_engine(url)
    Base.metadata.create_all(bind=moteur)
    with moteur.begin() as connection:
        connection.execute(text("DROP INDEX uq_doc_refs_conv_label"))
        connection.execute(text(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR NOT NULL,"
            " applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))
        for version, nom in ((1, "toolcall_durability"), (2, "web_cache"),
                             (3, "espaces_memoire"), (4, "memoire_indexes"),
                             (5, "etat_ingestion")):
            connection.execute(text(
                "INSERT INTO schema_migrations(version, name) VALUES (:v, :n)"
            ), {"v": version, "n": nom})
    return moteur


def test_migration_cree_l_index_unique_et_est_rejouable(tmp_path):
    moteur = _doc_refs_ancienne(f"sqlite:///{tmp_path / 'mig.db'}")

    apply_migrations(moteur)
    apply_migrations(moteur)  # rejouer ne doit rien casser ni rien dupliquer

    schema = _schema_doc_refs(moteur)
    assert ("index", "uq_doc_refs_conv_label") in schema, schema
    with moteur.connect() as connection:
        versions = [row[0] for row in connection.execute(
            text("SELECT version FROM schema_migrations ORDER BY version"))]
        unicite = [row for row in connection.execute(
            text("PRAGMA index_list(doc_refs)")) if row[1] == "uq_doc_refs_conv_label"]
    # `PRAGMA index_list` : colonne 2 = « unique ».
    assert unicite and unicite[0][2] == 1, "l'index doit être UNIQUE, pas un simple index"
    # La 6 est enregistrée UNE fois, et les premières n'ont pas bougé. La liste
    # s'arrête à la DERNIÈRE migration enregistrée (la 7, corbeille des
    # conversations, est venue après ce test) : l'assertion porte sur son
    # préfixe, pas sur un gel du compteur — chaque migration future prolongerait
    # la liste de la même façon.
    assert versions[:6] == [1, 2, 3, 4, 5, 6]
    assert versions == sorted(set(versions)), "aucun doublon de version"
    moteur.dispose()


def test_migration_refuse_des_donnees_qui_violent_la_contrainte(tmp_path):
    """Une base sale échoue au lieu d'être réécrite.

    Le dépôt ne doit pas démarrer sur une base dont deux fichiers partagent un
    `doc_N` : mieux vaut un refus explicite qu'un registre réécrit sous les
    pieds de l'utilisateur. Aucune ligne n'est supprimée pour faire passer la
    migration — c'est la seule garantie qu'on peut donner sur ce point.
    """
    moteur = _doc_refs_ancienne(f"sqlite:///{tmp_path / 'sale.db'}")
    with moteur.begin() as connection:
        connection.execute(text(
            "INSERT INTO doc_refs (id, conv_id, doc_label, doc_index, artifact_id)"
            " VALUES ('d1', 'conv_sale', 'doc_1', 1, 'art_a')"
        ))
        connection.execute(text(
            "INSERT INTO doc_refs (id, conv_id, doc_label, doc_index, artifact_id)"
            " VALUES ('d2', 'conv_sale', 'doc_1', 1, 'art_b')"
        ))

    from backend.db.migrations import MigrationError

    with pytest.raises(MigrationError):
        apply_migrations(moteur)
    with moteur.connect() as connection:
        restantes = connection.execute(text("SELECT COUNT(*) FROM doc_refs")).scalar()
        versions = [row[0] for row in connection.execute(
            text("SELECT version FROM schema_migrations WHERE version = 6"))]
    assert restantes == 2, "aucune ligne n'a été supprimée pour faire passer la migration"
    assert versions == [], "une migration échouée ne s'enregistre pas"
    moteur.dispose()


def test_migration_et_create_all_donnent_le_meme_schema(tmp_path):
    """Les deux chemins doivent produire le MÊME `doc_refs`.

    Sinon `create_all` et la migration divergent en silence : une base neuve et
    une base existante ne sont pas le même produit. On compare le DDL brut
    stocké par SQLite, colonne par colonne et index par index — pas seulement
    la présence du nom.
    """
    neuf = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    Base.metadata.create_all(bind=neuf)
    ancien = _doc_refs_ancienne(f"sqlite:///{tmp_path / 'migration.db'}")
    apply_migrations(ancien)

    assert _schema_doc_refs(neuf) == _schema_doc_refs(ancien)
    neuf.dispose()
    ancien.dispose()


def test_init_db_installe_la_contrainte_sur_une_base_neuve(tmp_path, monkeypatch):
    """Le chemin réel de l'application aboutit au même schéma."""
    from backend.db import database

    moteur = create_engine(f"sqlite:///{tmp_path / 'init.db'}")
    monkeypatch.setattr(database, "engine", moteur)
    database.init_db()
    database.init_db()  # rejouable

    assert ("index", "uq_doc_refs_conv_label") in _schema_doc_refs(moteur)
    moteur.dispose()
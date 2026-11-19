"""L'état d'ingestion doit être une DONNÉE, pas une absence de ligne.

Le défaut que ces tests verrouillent est une absence, pas une valeur : l'état
d'ingestion d'un artefact se déduisait de « y a-t-il une observation `completed` ? ».
Un artefact déposé et jamais traité, un artefact dont l'extraction a échoué et un
artefact dont le contenu n'est pas du texte répondaient alors IDENTIQUEMENT, et le
lecteur disait la même chose des trois — « contenu non extrait ».

La Bible tranche (2.6) : « un fichier qui n'a pas été bien traité ne doit jamais être
considéré silencieusement comme correctement traité ». « Silencieusement » est le mot
qui décide de la forme de ce fichier : un état porté dans la donnée ne peut pas être
silencieux, parce qu'il se lit.

Trois blocs, trois questions :

  1. LE PORTAGE — l'état existe-t-il pour CHAQUE artefact, y compris jamais traité, et
     la migration est-elle sûre sur une base existante comme sur une base vide ?
  2. LA REPRISE — un arrêt en plein OCR laisse-t-il un `en_cours` que le démarrage
     rejoue, sans charger de modèle et sans boucler ?
  3. LA DÉTECTION — le rapport d'intégrité nomme-t-il les trois défauts d'extraction,
     sans les ré-accuser de ce qu'il n'a pas vérifié ?

Montage : base neuve et JETABLE, `SessionLocal` du projet redirigée, racines de données
jetables. Aucune migration n'est exécutée sur la base réelle : `tmp_path` à chaque fois.
Aucun octet, aucune ligne, aucun fichier du projet n'est touché. Aucun modèle, aucun GPU,
aucun réseau.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db import migrations
from backend.db.models import Artifact, ArtifactLink, Base, Blob, Conversation
from backend.ingestion import etat, reprise
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.search import observations as _obs_module
from backend.search import reader
from backend.services import depot_integrite as integrite
from backend.services import espaces as service


# ---------------------------------------------------------------------------
# Montage
# ---------------------------------------------------------------------------


def pdf_avec_texte(corps: str = "rapport") -> bytes:
    """Un PDF minimal dont la page porte du texte natif — donc extractible SANS modèle.

    Le texte passe par un VRAI contenu de flux, parce que `_texte_natif_pdf` lit la
    structure du PDF et non une chaîne qu'on aurait glissée dedans : un fichier dont le
    texte n'est pas dans le flux ne s'extrait pas, et le test testerait autre chose que
    le code de production.

    Une ligne par morceau de texte, et NON une seule longue ligne : le lecteur ne rend
    que ce qui tient dans la page, donc une ligne trop longue serait tronquée SILENCIEUS
    — et le test verrait « PDF sans texte » alors que le texte y est. C'est aussi pour
    cette raison qu'il faut plus de 200 caractères : en dessous, le code bascule sur
    l'OCR, ce qu'on veut précisément éviter ici.
    """
    morceaux = [f"{corps} {index:02d}" for index in range(8)]
    lignes = " ".join(
        f"/F1 12 Tf 1 0 0 1 40 {720 - 20 * index:n} Tm ({morceau}) Tj"
        for index, morceau in enumerate(morceaux)
    )
    flux = ("BT " + lignes + " ET").encode("latin-1")
    return (
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length "
        + str(len(flux)).encode()
        + b">>stream\n"
        + flux
        + b"\nendstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF " + corps.encode()
    )


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Base neuve, `SessionLocal` du projet redirigée, racines de données jetables.

    L'ingestion est NEUTRALISÉE, comme dans les suites de dépôt : sans cela, déposer un
    fichier déclencherait un vrai fil d'extraction dans un fil de test, qui écrirait son
    état `en_cours` puis `extrait` pendant qu'on mesure — donc des courses, et des
    tests qui passent ou échouent selon la machine. Les trois tests qui MESURENT
    l'extraction appellent `_ingérer_un` directement, en synchronisé.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'etat.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids, **kw: None)
    return engine, sessions


def _espace(db, nom: str = "Atelier") -> str:
    return str(service.create_workspace(db, nom).id)


def _verser(db, dest: dict, nom: str, octets: bytes, request_id: str = "etat") -> str:
    """Le VRAI chemin de dépôt : staging puis promotion. Un artefact fabriqué à la main
    ne prouverait rien du contrat de dépôt."""
    item = A.stage_picker_bytes(
        db, {"request_id": request_id, "filename": nom, "bytes": octets}
    )["items"][0]
    (artifact_id,) = A.attachments_prepare(
        db,
        {
            **dest,
            "stageItemIds": [item["stage_item_id"]],
            "names": [nom],
        },
    )["artifact_ids"]
    return artifact_id


def _verser_texte(
    db, dest: dict, nom: str = "table.csv", request_id: str = "etat"
) -> str:
    """Un dépôt SANS modèle : un CSV est du texte, et son extraction n'en demande aucun."""
    return _verser(db, dest, nom, ("contenu de " + nom).encode("utf-8"), request_id)


def _fiche(db, artifact_id: str) -> None:
    """Fiche + ligne d'index : ce que l'ingestion fait après la promotion.

    Sans elle, le dépôt serait aussi `index_manquant` — et un test qui vérifie les
    anomalies d'EXTRACTION measuringait en réalité l'absence de fiche.
    """
    from backend.search import cards

    artefact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
    cards.ensure_card_on_import(db, artefact)
    db.commit()


def _etat_de(sessions, artifact_id: str) -> etat.Etat:
    db = sessions()
    try:
        return etat.lire(db.query(Artifact).filter(Artifact.id == artifact_id).one())
    finally:
        db.close()


def _poser(
    sessions,
    artifact_id: str,
    valeur: str,
    raison: str = "",
    il_y_a: timedelta | None = None,
) -> None:
    db = sessions()
    try:
        marque = etat.utc_naif() - (il_y_a or timedelta(0))
        db.query(Artifact).filter(Artifact.id == artifact_id).update(
            {
                "ingest_state": valeur,
                "ingest_reason": raison or None,
                "ingest_updated_at": marque,
            },
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


def _observations(sessions, artifact_id: str, outil: str = "attachment_ingest") -> int:
    db = sessions()
    try:
        from backend.db.models import Observation

        return (
            db.query(Observation)
            .filter(
                Observation.artifact_id == artifact_id,
                Observation.tool_name == outil,
            )
            .count()
        )
    finally:
        db.close()


# ---------- 1. LE PORTAGE : l'état existe pour chaque artefact ----------


def test_un_artefact_depose_jamais_ingere_a_un_etat_valant(base, monkeypatch):
    """Question 5 du plan : que vaut l'état d'un dépôt jamais ingéré ? UNE VALEUR.

    La réponse est `depose` avec son motif, écrits DANS la promotion. Ce n'est pas
    « une absence d'observation » : c'est une donnée. Un lecteur peut donc dire à
    l'utilisateur « extraction jamais demandée », ce qui est vrai et actionnable,
    au lieu de « contenu non extrait », qui ne l'est pas.

    L'ingestion est neutralisée ICI pour que la question porte bien sur le dépôt seul :
    sinon l'artefact serait déjà passé par `extrait` et la question n'aurait plus d'objet.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser_texte(db, {"workspaceId": espace}, "note.csv", "portage")

    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_DEPOSE
    assert etat_lu.raison, "un état sans motif n'explique rien à l'écran"
    assert etat_lu.est_terminal is False
    assert etat_lu.est_reprenable is True
    assert _observations(sessions, artifact_id) == 0, (
        "le test porte sur le dépôt, pas sur l'ingestion"
    )


def test_la_migration_ajoute_l_etat_sur_une_base_vide(tmp_path):
    """Base VIDE : `create_all` puis la migration doivent passer, dans cet ordre.

    `apply_migrations` est appelé SANS filet par `init_db` (database.py:117) : une
    migration qui échoue sur une base neuve empêche toute première exécution. On
    vérifie donc les deux chemins — celui de `create_all` et celui du `ALTER`.
    """
    fichier = tmp_path / "neuve.db"
    neuf = create_engine(f"sqlite:///{fichier}")
    Base.metadata.create_all(bind=neuf)
    migrations.apply_migrations(neuf)
    with neuf.connect() as conn:
        info = list(conn.execute(sql_text("PRAGMA table_info(artifacts)")))
    colonnes = {row[1] for row in info}
    not_null = {row[1]: row[3] for row in info}
    defauts = {row[1]: row[4] for row in info}
    assert {"ingest_state", "ingest_reason", "ingest_updated_at"} <= colonnes
    assert {
        "ingest_attempts",
        "ingest_last_attempt_at",
        "ingest_next_attempt_at",
        "ingest_heartbeat_at",
    } <= colonnes, "le backoff et le battement sont portés par la même table"
    # NOT NULL + défaut : c'est la garantie qu'aucun artefact ne peut être sans état,
    # ET que le dépôt écrit en SQL brut continue de passer. Une colonne sans l'un des
    # deux laisserait soit un artefact sans état, soit un dépôt rejeté sur base neuve.
    assert not_null["ingest_state"] == 1
    assert not_null["ingest_attempts"] == 1
    assert defauts["ingest_attempts"] == "0"
    # Le DÉFAUT est `inconnu`, pas `depose` : le dépôt écrit `depose` lui-même parce
    # qu'il sait qu'une extraction est due, et une écriture qui omet la colonne ne le
    # sait pas. C'est ce qui rend le backfill de la migration atomique — SQLite remplit
    # les lignes déjà présentes avec le défaut du DDL.
    assert defauts["ingest_state"] == "'inconnu'"
    assert [
        v[0]
        for v in list(
            neuf.connect().execute(
                sql_text("SELECT version FROM schema_migrations ORDER BY version")
            )
        )
    ][:6] == [1, 2, 3, 4, 5, 6], (
        "le registre porte les migrations appliquées, v7 comprise"
    )


def _base_avant_le_releve(tmp_path, nom: str) -> tuple:
    """Une base VRAIMENT antérieure au relevé : schéma complet, colonnes d'état ABSENTES.

    On part de `create_all` puis on RETIRE les trois colonnes — c'est la seule façon
    d'obtenir le schéma d'avant sans dupliquer dix-sept `CREATE TABLE` dans un test.
    Le retrait est fait en SQL brut parce que c'est exactement ce qu'aurait subi la
    base de l'utilisateur avant cette migration, et que le DDL qui l'a produit n'est
    plus dans le dépôt.
    """
    moteur = create_engine(f"sqlite:///{tmp_path / nom}")
    Base.metadata.create_all(bind=moteur)
    with moteur.begin() as conn:
        # L'index d'abord : SQLite refuse de retirer une colonne indexée, et ce refus
        # est la preuve que la colonne était bien celle du modèle.
        conn.exec_driver_sql("DROP INDEX IF EXISTS idx_artifacts_ingest_state")
        for colonne in ("ingest_updated_at", "ingest_reason", "ingest_state"):
            conn.exec_driver_sql(f"ALTER TABLE artifacts DROP COLUMN {colonne}")
        conn.exec_driver_sql(
            "INSERT INTO blobs (id, sha256, relative_path, size_bytes, mime_detected,"
            " state) VALUES ('b_avant', 's_avant', 'blobs/s_avant.bin', 3,"
            " 'application/pdf', 'ready')"
        )
        conn.exec_driver_sql(
            "INSERT INTO artifacts (id, blob_id, kind, original_filename, display_name,"
            " created_at) VALUES ('art_avant', 'b_avant', 'pdf', 'Contrat.pdf',"
            " 'Contrat.pdf', '2026-01-01 00:00:00')"
        )
    return moteur


def test_la_migration_backfill_dit_inconnu_et_pas_depose(tmp_path):
    """Une base ANCIENNE, peuplée : le backfill dit `inconnu`, jamais `depose`.

    `inconnu` est la seule réponse honnête pour un artefact antérieur au relevé : il a
    peut-être déjà été extrait. Écrire `depose` fabriquerait une dette sur tout un
    patrimoine, et `extrait` fabriquerait une réussite — deux mensonges, dont le
    premier ferait crier « dépôt dégradé » au premier démarrage suivant la migration.
    """
    moteur = _base_avant_le_releve(tmp_path, "ancienne.db")
    migrations.apply_migrations(moteur)

    with moteur.connect() as conn:
        lignes = list(conn.execute(sql_text("SELECT id, ingest_state FROM artifacts")))
        index = list(
            conn.execute(
                sql_text(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND name='idx_artifacts_ingest_state'"
                )
            )
        )
    assert lignes == [("art_avant", etat.ETAT_INCONNU)]
    assert len(index) == 1, (
        "l'index du balayage de reprise doit exister sur l'ancienne base"
    )


def test_la_migration_est_rejouable_sur_une_base_peuplee(tmp_path):
    """Rejouée deux fois sur la même base : rien ne casse, rien ne bouge.

    `init_db` fait `create_all` PUIS `apply_migrations`, et les deux chemins se
    croisent dans un ordre quelconque selon la base. Une migration non idempotente
    ferait échouer le DEUXIÈME démarrage — pas le premier, qui est celui qu'on teste
    d'ordinaire.

    On force le rejeu en retirant la version 5 de `schema_migrations`, ce qui reproduit
    ce que voit une base dont l'enregistrement a été perdu : `apply_migrations` la
    rejoue, et elle doit pouvoir le faire sur des colonnes déjà là et déjà remplies.
    """
    moteur = _base_avant_le_releve(tmp_path, "rejouee.db")
    migrations.apply_migrations(moteur)
    with moteur.begin() as conn:
        conn.exec_driver_sql("DELETE FROM schema_migrations WHERE version = 5")
    migrations.apply_migrations(moteur)

    with moteur.connect() as conn:
        lignes = list(conn.execute(sql_text("SELECT id, ingest_state FROM artifacts")))
        versions = [
            row[0]
            for row in conn.execute(
                sql_text("SELECT version FROM schema_migrations ORDER BY version")
            )
        ]
    assert lignes == [("art_avant", etat.ETAT_INCONNU)]
    # Le registre porte toutes les migrations APPLIQUÉES, y compris celles
    # venues après l'écriture de ce test (la 7, corbeille des conversations —
    # additive, elle ne touche ni `artifact_ingestion` ni `web_*`) : l'écriture
    # utile est le PRÉFIXE [1..6] sans doublon, pas un gel du compteur.
    assert versions[:6] == [1, 2, 3, 4, 5, 6], (
        "et une seule fois : pas de doublon de version"
    )
    assert versions == sorted(set(versions)), "aucune version dupliquée"


def test_un_ecriture_brute_qui_oublie_la_colonne_passe_quand_meme(tmp_path):
    """Le dépôt écrit en SQL brut : la colonne a un DÉFAUT, sinon il casserait.

    C'est la raison du `server_default` dans le modèle : une base neuve dont la colonne
    est NOT NULL sans défaut rejetterait l'INSERT du dépôt, alors qu'une base migrée
    l'accepterait. Deux bases, deux comportements — et le dépôt est le chemin par
    lequel TOUT le projet passe.
    """
    fichier = tmp_path / "brute.db"
    moteur = create_engine(f"sqlite:///{fichier}")
    brut = sqlite3.connect(fichier)
    Base.metadata.create_all(bind=moteur)
    migrations.apply_migrations(moteur)
    with brut:
        brut.execute(
            "INSERT INTO blobs (id, sha256, relative_path, size_bytes, mime_detected,"
            " state) VALUES ('b2', 's2', 'blobs/s2.bin', 3, 'application/pdf', 'ready')"
        )
        brut.execute(
            "INSERT INTO artifacts (id, blob_id, kind, original_filename, display_name)"
            " VALUES ('a2', 'b2', 'pdf', 'Y.pdf', 'Y.pdf')"
        )
        ligne = brut.execute(
            "SELECT ingest_state FROM artifacts WHERE id='a2'"
        ).fetchone()
    brut.close()
    assert ligne == ("inconnu",), "le défaut dit ce qu'on ne sait pas, pas une dette"


def test_marquer_refuse_un_etat_hors_vocabulaire(base):
    """Law 4 : un état hors vocabulaire s'arrête, avec le nom.

    Law 4 parce qu'un état écrit à côté ne serait pas un état : le rapport d'intégrité
    le lirait comme un défaut sans savoir lequel, et l'écran ne pourrait pas le nommer.
    mieux vaut un appel qui échoue qu'une donnée que personne ne peut interpréter.

    La date est vérifiée dans la foulée, parce qu'elle est ce qui rend un `en_cours`
    rattrapable : un état `en_cours` sans horodatage est un état que le balayage de
    reprise ne peut pas juger périmé, donc un état qu'il laisserait pour toujours.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Vocabulaire")
        cible = _verser_texte(db, {"workspaceId": espace}, "vocal.csv", "vocabulaire")

    with sessions() as db:
        with pytest.raises(etat.EtatInconnu) as attr:
            etat.marquer(db, cible, "a-peu-pres-bon")
    assert "a-peu-pres-bon" in str(attr.value)

    with sessions() as db:
        marque = etat.marquer(db, cible, etat.ETAT_EXTRAIT, "texte obtenu")
        lu = etat.lire(db.query(Artifact).filter(Artifact.id == cible).one())
    assert marque == etat.ETAT_EXTRAIT
    assert lu.maj_le is not None and lu.maj_le.tzinfo is None, (
        "UTC naïf, comparable au func.now()"
    )


# ---------- 2. LA REPRISE ----------


def test_un_en_cours_abandonne_est_repris_au_demarrage(base, monkeypatch):
    """Un arrêt en plein OCR laisse un `en_cours` horodaté — et il est rejoué.

    Le seuil est testé par ses DEUX côtés : vieux de deux heures, il est repris ; vieux
    de dix secondes, il ne l'est pas, parce qu'une autre instance s'en occupe. C'est le
    paramètre n°1 de la tâche, et il n'a de sens que s'il écarte ET admet.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        ancien = _verser_texte(
            db, {"workspaceId": espace}, "scan1.csv", "repris-ancien"
        )
        recent = _verser_texte(
            db, {"workspaceId": espace}, "scan2.csv", "repris-recent"
        )
    _poser(
        sessions,
        ancien,
        etat.ETAT_EN_COURS,
        "extraction demandée",
        il_y_a=timedelta(hours=2),
    )
    _poser(
        sessions,
        recent,
        etat.ETAT_EN_COURS,
        "extraction demandée",
        il_y_a=timedelta(seconds=10),
    )

    reels: list[tuple[list[str], bool]] = []

    def _faux(ids, *, modeles_autorises=True):
        reels.append((list(ids), modeles_autorises))

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", _faux)

    bilan = reprise.balayer_reprise()

    assert bilan["ok"] is True
    assert bilan["repris"] == [ancien]
    assert [motif["artifact_id"] for motif in bilan["ignores"]] == [recent]
    assert "encore récente" in bilan["ignores"][0]["raison"]
    # Le paramètre n°2 : aucun modèle au démarrage, et c'est VÉRIFIÉ, pas déclaré.
    assert reels == [([ancien], False)]


def test_le_balayage_ne_boucle_pas_et_ne_demande_aucun_modele(base, monkeypatch):
    """Un `depose` ne doit pas finir en boucle de reprises successives.

    Le fil d'extraction est laissé Alive ici : le but est de vérifier que la PASSE ne se
    rejoue pas elle-même. Un artefact qu'on relancerait en boucle consommerait du temps
    machine à chaque démarrage, et le journal se remplirait de la même ligne.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        cible = _verser_texte(db, {"workspaceId": espace}, "tableau.csv", "boucle")
    appels: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest,
        "lancer_ingestion",
        lambda ids, **kw: appels.append(list(ids)),
    )

    premiere = reprise.balayer_reprise()
    # UNE relance par passe, jamais deux : c'est ce que « borné » veut dire. La base
    # n'a pas bougé entre les deux passes — `lancer_ingestion` est factice ici — donc
    # c'est bien la PASSE qui est bornée, et non la base.
    assert premiere["repris"] == [cible], "une passe ne relance pas deux fois le même"
    assert len(appels) == 1 and appels[0] == [cible]

    seconde = reprise.balayer_reprise()
    assert seconde["eligibles"] == 1 and len(appels) == 2, (
        "chaque passe solde ce qu'elle voit"
    )

    # Un état TERMINAL sort de la passe : la dette est soldée et ne revient pas.
    # C'est ce qui empêche une base de reboucler sur un fichier récalcitrant.
    _poser(sessions, cible, etat.ETAT_EXTRAIT, "texte obtenu")
    assert reprise.balayer_reprise()["repris"] == []
    assert reprise.balayer_reprise()["eligibles"] == 0


def test_un_inconnu_sans_observation_est_repris_mais_pas_avec(base, monkeypatch):
    """Le backfill `inconnu` ne doit pas relancer tout un patrimoine déjà extrait.

    C'est la condition qui rend la migration SÛRE sur une base peuplée : sans elle, le
    premier démarrage suivant la migration rejouerait mille fichiers pour rien. Deux
    cas, donc deux comportements, et c'est l'observation qui les sépare.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        jamais_regarde = _verser_texte(
            db, {"workspaceId": espace}, "ancien1.csv", "inc1"
        )
        deja_regarde = _verser_texte(db, {"workspaceId": espace}, "ancien2.csv", "inc2")
    _poser(sessions, jamais_regarde, etat.ETAT_INCONNU, "")
    _poser(sessions, deja_regarde, etat.ETAT_INCONNU, "")
    # Une observation d'ingestion RÉUSSIE : le fichier a déjà été extrait.
    from backend.search.observations import record_tool_observation

    db = sessions()
    try:
        record_tool_observation(
            deja_regarde,
            "attachment_ingest",
            None,
            "texte déjà extrait de ce fichier",
            ok=True,
        )
    finally:
        db.close()

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids, **kw: None)
    bilan = reprise.balayer_reprise()

    assert bilan["repris"] == [jamais_regarde]


def test_le_balayage_reste_borne(base, monkeypatch):
    """Cinquante par passe, pas plus : le démarrage ne lance pas mille extractions.

    La borne est un compromis (« tout rattraper » contre « démarrer vite ») ; le test
    la verrouille pour qu'elle ne devienne pas un `LIMIT` qu'on oublie d'enlever, ni un
    nombre qu'on relève parce qu'un test de performance le demande.
    """
    _, sessions = base
    db = sessions()
    try:
        espace = _espace(db)
        for index in range(6):
            _verser(
                db,
                {"workspaceId": espace},
                f"piece{index}.pdf",
                pdf_avec_texte(f"pièce {index}"),
                f"borne-{index}",
            )
    finally:
        db.close()
    vus: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda ids, **kw: vus.append(list(ids))
    )

    bilan = reprise.balayer_reprise(limite=4)

    assert bilan["eligibles"] == 4 and len(bilan["repris"]) == 4
    assert len(vus) == 1 and len(vus[0]) == 4


def test_le_balayage_ne_leve_pas_et_journalise(base, monkeypatch, caplog):
    """Une base qui ne répond pas donne un bilan et une ligne de journal, pas une erreur.

    Un rattrapage qui casse le démarrage transforme un fichier illisible en application
    indisponible. Et un rattrapage SILENCIEUX est le défaut qu'on répare : la ligne de
    journal doit donc exister, sinon on a reconduit le problème sous une autre forme.
    """
    _, sessions = base

    def _casse(*args, **kwargs):
        raise sqlite3.OperationalError("base verrouillée")

    monkeypatch.setattr(reprise, "_candidats", _casse)
    caplog.set_level("WARNING", logger="backend.ingestion.reprise")
    bilan = reprise.demarrer_reprise()

    assert bilan["eligibles"] == 0 and bilan["repris"] == []
    assert any("balayage impossible" in record.message for record in caplog.records), (
        "un rattrapage impossible doit se lire dans le journal, pas se déduire de "
        "l'absence de ligne"
    )


# ---------- 3. LA DÉTECTION ----------


def _depot_sain(sessions, espace: str, nb: int = 1, prefixe: str = "sain"):
    """Des fichiers versés ET indexés : le dépôt est sain, et seul l'extestion varie."""
    db = sessions()
    try:
        ids = [
            _verser(
                db,
                {"workspaceId": espace},
                f"piece{index}.pdf",
                pdf_avec_texte(f"{prefixe} {index}"),
                f"{prefixe}-{index}",
            )
            for index in range(nb)
        ]
        for artifact_id in ids:
            _fiche(db, artifact_id)
        return ids
    finally:
        db.close()


def test_un_depot_jamais_ingere_ne_signale_aucune_extraction_absente(base, monkeypatch):
    """Un dépôt dont l'ingestion n'a pas encore eu lieu n'est PAS un dépôt abîmé.

    Ce test verrouille le refus le plus difficile à tenir : `depose` et `en_cours` sont
    des DETTES, pas des défauts. Les signaler ferait crier « dépôt dégradé » sur une base
    parfaitement saine — et un rapport qui crie au loup perd sa crédibilité bien avant
    d'avoir trouvé un vrai problème.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Sain")
        deposes = _depot_sain(sessions, espace, 3, "jamais")
        db.commit()

    db = sessions()
    try:
        rapport = integrite.verifier_depot(db, espace)
    finally:
        db.close()
    for type_ in ("extraction_absente", "extraction_binaire", "extraction_tronquee"):
        assert rapport["par_type"][type_] == 0, type_
    assert rapport["sain"] is True
    assert rapport["mesure"]["artefacts_vivants"] == len(deposes) == 3


def test_les_trois_defauts_d_extraction_sont_signales_avec_leur_motif(
    base, monkeypatch
):
    """`vide`, `binaire_refuse` et `tronque` sortent séparément, motif à l'appui.

    Séparément, et c'est le sujet : trois causes qui appellent trois actions
    différentes. Un rapport qui les confondrait en « extraction absente » ferait
    réessayer un fichier binaire, qui n'en a pas besoin, et ne rien dire d'un fichier
    qu'on n'a pas encore regardé.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Extraction")
        vide = _verser(
            db,
            {"workspaceId": espace},
            "photo.png",
            b"\x89PNG\r\n\x1a\n" + b"\0" * 40,
            "vide",
        )
        binaire = _verser(
            db,
            {"workspaceId": espace},
            "classeur.xlsx",
            b"PK\x03\x04" + b"\x01" * 40,
            "binaire",
        )
        tronque = _verser(
            db, {"workspaceId": espace}, "long.pdf", pdf_avec_texte("long"), "tronque"
        )
        for artifact_id in (vide, binaire, tronque):
            _fiche(db, artifact_id)
    _poser(sessions, vide, etat.ETAT_VIDE, "aucun texte reconnu en mode ocr")
    _poser(sessions, binaire, etat.ETAT_BINAIRE, "signature d'archive ZIP")
    _poser(
        sessions,
        tronque,
        etat.ETAT_TRONQUE,
        "Extrait tronqué à 2000 caractères sur 50000",
    )

    db = sessions()
    try:
        rapport = integrite.verifier_depot(db, espace)
    finally:
        db.close()

    # UN état, UNE anomalie : le binaire ne doit apparaître QUE dans la sienne, sinon
    # le total gonfle et le rapport annonce un dépôt plus abîmé qu'il ne l'est.
    assert rapport["par_type"]["extraction_absente"] == 1  # le `vide` seul
    assert rapport["par_type"]["extraction_binaire"] == 1
    assert rapport["par_type"]["extraction_tronquee"] == 1
    assert rapport["total"] == 3
    (absente,) = rapport["anomalies"]["extraction_absente"]
    assert absente["artifact_id"] == vide and absente["ingest_state"] == etat.ETAT_VIDE
    assert "aucun texte reconnu" in absente["detail"]
    (refuse,) = rapport["anomalies"]["extraction_binaire"]
    assert refuse["artifact_id"] == binaire and "ZIP" in refuse["detail"]
    assert rapport["anomalies"]["extraction_tronquee"][0]["artifact_id"] == tronque


def test_un_inconnu_n_est_jamais_accuse(base, monkeypatch):
    """`inconnu` n'est pas un défaut : c'est une absence d'information.

    Un détecteur qui accuse ce qu'il n'a pas vérifié est le premier à fabriquer le
    mensonge qu'il prétend poursuivre. Le test est ici parce que `inconnu` est l'état
    de TOUT le patrimoine existant après la migration : si le rapport l'accusait, le
    premier démarrage suivant la migration annoncerait un dépôt dégradé pour rien.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Legacy")
        heritage = _verser(
            db, {"workspaceId": espace}, "avant.pdf", pdf_avec_texte("avant"), "legacy"
        )
        _fiche(db, heritage)
    _poser(sessions, heritage, etat.ETAT_INCONNU, "")

    db = sessions()
    try:
        rapport = integrite.verifier_depot(db, espace)
    finally:
        db.close()
    assert (
        sum(
            rapport["par_type"][t]
            for t in ("extraction_absente", "extraction_binaire", "extraction_tronquee")
        )
        == 0
    )


def test_les_defauts_d_extraction_ne_sont_pas_repares(base, monkeypatch):
    """Les trois sont SIGNALÉS et jamais réparés — la raison du refus est donnée.

    Les réparer demanderait de relancer une extraction : du temps machine, et du contenu
    neuf. Une maintenance qui fabrique du texte est une maintenance qui invente. Le
    test vérifie aussi que rien n'a bougé en base après le refus.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Non réparable")
        casse = _verser(
            db,
            {"workspaceId": espace},
            "binaire.xlsx",
            b"PK\x03\x04" + b"\x01" * 40,
            "casse",
        )
        _fiche(db, casse)
    _poser(sessions, casse, etat.ETAT_BINAIRE, "signature d'archive ZIP")

    db = sessions()
    try:
        reparation = integrite.reparer_depot(db, espace)
    finally:
        db.close()

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    (signalee,) = reparation["signales"]["extraction_binaire"]
    assert (
        signalee["raison_du_refus"] == integrite.RAISONS_DE_REFUS["extraction_binaire"]
    )
    assert reparation["apres"]["par_type"]["extraction_binaire"] == 1
    assert _etat_de(sessions, casse).etat == etat.ETAT_BINAIRE


def test_les_detecteurs_d_extraction_ne_ajoutent_aucune_lecture(base, monkeypatch):
    """Cinq lectures, toujours. L'état d'ingestion ne vaut pas une sixième.

    `LECTURES_PAR_VERIFICATION` est une promesse testée ailleurs ; ce test la garde
    pour les trois détecteurs ajoutés. Ré-analyser les octets comme le ferait
    `detecter_ingestions_binaire_ou_tronquees` coûterait quatre lectures de plus PAR
    VÉRIFICATION — sur le chemin du démarrage, où `init_db` signale le dépôt dégradé.
    """
    from sqlalchemy import event

    engine, sessions = base
    with sessions() as db:
        espace = _espace(db, "Coût")
        _depot_sain(sessions, espace, 2, "cout")
        db.commit()
    db = sessions()
    try:
        db.execute(sql_text("SELECT 1"))  # réchauffe la connexion, pas un compte
        vues: list[str] = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda conn, cur, statement, *a: vues.append(statement),
        )
        integrite.verifier_depot(db, espace)
    finally:
        db.close()
    assert len(vues) == integrite.LECTURES_PAR_VERIFICATION == 5


def test_un_detecteur_ne_leve_jamais_meme_base_degradee(base, monkeypatch):
    """Le CONTRAT du module tient pour les nouveaux détecteurs comme pour les anciens.

    Une table d'état illisible ne doit faire tomber AUCUN rapport : c'est précisément
    dans une base dégradée qu'un rapport est utile, et une exception ramènerait
    l'appelant à ne rien savoir.
    """
    engine, sessions = base
    with sessions() as db:
        espace = _espace(db, "Dégradé")
        _depot_sain(sessions, espace, 1, "degrade")
        db.commit()
    with engine.begin() as conn:  # base JETABLE de la suite
        conn.exec_driver_sql("DROP TABLE artifacts")

    db = sessions()
    try:
        rapport = integrite.verifier_depot(db, espace)
    finally:
        db.close()
    assert rapport["ok"] is True and rapport["sain"] is False
    assert any("artifacts" in d for d in rapport["diagnostics"])


# ---------- 4. LES LECTEURS DISENT POURQUOI ----------


def test_la_lecture_ciblee_dit_pourquoi_il_n_y_a_pas_de_texte(base, monkeypatch):
    """Le même fichier, deux états opposés, deux messages DIFFÉRENTS.

    Avant, les deux renvoyaient « Contenu non extrait : utilise doc_ocr ». L'orchestrateur
    ne pouvait donc pas distinguer « on n'a pas encore essayé » de « on a essayé et le
    fichier n'est pas du texte » — et il répondait en ordonnant de réessayer un
    fichier qu'aucune insistance ne ramènera jamais.
    """
    _, sessions = base
    with sessions() as db:
        conversation = "conv_reprise"
        db.add(Conversation(id=conversation, title="t", mode="chat"))
        db.commit()
        jamais = _verser(
            db,
            {"conversationId": conversation},
            "jamais.pdf",
            pdf_avec_texte("jamais"),
            "msg-jamais",
        )
        binaire = _verser(
            db,
            {"conversationId": conversation},
            "classeur.xlsx",
            b"PK\x03\x04" + b"\x01" * 40,
            "msg-binaire",
        )
        for artifact_id in (jamais, binaire):
            _fiche(db, artifact_id)
    _poser(sessions, jamais, etat.ETAT_DEPOSE, "déposé, extraction en attente")
    _poser(sessions, binaire, etat.ETAT_BINAIRE, "signature d'archive ZIP")

    from backend.search import scope as _scope

    with sessions() as db:
        autorises = _scope.resolve_allowed_artifacts(conversation)
        message_jamais = reader.read_payload(db, jamais, autorises)["message"]
        message_binaire = reader.read_payload(db, binaire, autorises)["message"]

    assert "jamais" in message_jamais or "demandée" in message_jamais
    assert "ZIP" in message_binaire
    assert message_jamais != message_binaire
    # La consigne d'action reste : le motif complète, il ne remplace pas le geste.
    assert "doc_ocr" in message_jamais or "ask_files" in message_jamais


def test_une_erreur_d_ingestion_ne_produit_pas_d_etat_favorable(base, monkeypatch):
    """Un échec d'écriture d'observation remonte ET laisse un état nommé.

    L'ordre de la Phase 1 est observation → état. Si l'observation ne peut pas être
    écrite, l'état est tout de même consigné avant que l'erreur ne remonte : la panne
    d'écriture est connue, et le fichier ne doit pas rester `en_cours` pour elle.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Échec")
        artifact_id = _verser(
            db, {"workspaceId": espace}, "casse.pdf", b"%PDF-1.7 tronque", "echec"
        )
        db.commit()

    def _echec(*args, **kwargs):
        raise _obs_module.ObservationNonÉcrite("disque plein")

    # On patche le module SOURCE et non l'attribut importé : `_ingérer_un` fait son
    # `from ... import record_extraction_observation` À CHAQUE APPEL, donc c'est là
    # que le nom est résolu. Le chemin d'extraction est append-only : il passe par
    # `record_extraction_observation`, jamais par `record_tool_observation`.
    monkeypatch.setattr(_obs_module, "record_extraction_observation", _echec)
    # L'observation qui ne s'écrit pas REMONTE : c'est son contrat, et l'avaler
    # rendrait l'échec de l'écriture indiscernable d'une absence. Ce qu'on vérifie
    # ici, c'est que l'ÉTAT, lui, a survécu à cette remontée.
    with pytest.raises(_obs_module.ObservationNonÉcrite):
        attachment_ingest._ingérer_un(artifact_id, modeles_autorises=False)

    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_ECHEC
    assert "blob" in etat_lu.raison or "impossible" in etat_lu.raison


def test_le_refus_de_modele_laisse_une_dette_nommee(base, monkeypatch):
    """Refus de modèle : le fichier reste `depose` AVEC SON MOTIF, pas `echec`.

    `echec` dirait « quelque chose est cassé » alors que rien ne l'est — il n'y a juste
    pas de machine autorisée. Et un `depose` sans motif serait indécidable, donc aussi
    muet qu'un `echec` : c'est le motif qui fait la différence entre une dette et un
    oubli.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Sans modèle")
        image = _verser(
            db,
            {"workspaceId": espace},
            "photo.png",
            b"\x89PNG\r\n\x1a\n" + b"\0" * 64,
            "sans-modele",
        )
        db.commit()

    attachment_ingest._ingérer_un(image, modeles_autorises=False)

    etat_lu = _etat_de(sessions, image)
    assert etat_lu.etat == etat.ETAT_DEPOSE
    assert "modèle" in etat_lu.raison
    assert etat_lu.est_reprenable is True


def test_ressource_occupee_laisse_depose_et_non_echec(base, monkeypatch):
    """ResourceBusy est une dette de ressource, pas une panne du fichier."""
    from backend.inference.resource_coordinator import ResourceBusy

    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Ressource occupée")
        image = _verser(
            db,
            {"workspaceId": espace},
            "photo2.png",
            b"\x89PNG\r\n\x1a\n" + b"\0" * 64,
            "resource-busy",
        )
        db.commit()

    def occupe(*args, **kwargs):
        raise ResourceBusy("un run image tient le bail")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", occupe)
    attachment_ingest._ingérer_un(image, modeles_autorises=True)

    etat_lu = _etat_de(sessions, image)
    assert etat_lu.etat == etat.ETAT_DEPOSE
    assert "ressource occupée" in etat_lu.raison
    assert etat_lu.est_reprenable is True


def test_une_extraction_sans_modele_reussit_sur_un_pdf_a_texte(base, monkeypatch):
    """Le refus est posé AU POINT EXACT du besoin, pas avant : le PDF natif passe.

    Refuser d'emblée tout PDF « parce qu'il pourrait demander de l'OCR » laisserait sans
    texte un fichier dont le texte EST dans le fichier — le défaut inverse, et tout
    aussi grave.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db, "Natif")
        rapport = _verser(
            db,
            {"workspaceId": espace},
            "rapport.pdf",
            pdf_avec_texte("compte rendu de la réunion du mardi"),
            "natif",
        )
        db.commit()

    monkeypatch.setattr(_obs_module, "record_tool_observation", lambda *a, **kw: None)
    attachment_ingest._ingérer_un(rapport, modeles_autorises=False)

    etat_lu = _etat_de(sessions, rapport)
    assert etat_lu.etat == etat.ETAT_EXTRAIT
    assert etat_lu.a_texte is True

"""Boucle complète : un fichier, des questions, un brief.

C'est le test que la chaîne de pièces jointes ne prouve pas. Il traverse
l'enchaînement réel :

    fichier déposé → stagé → promu en artefact → ingéré → l'orchestrateur le voit
    → appelle ask_files → l'outil LIT LA REPRÉSENTATION D'INGESTION
    → le résultat revient → le brief est construit, validé et rendu

`ask_files` ne relit plus le fichier et n'appelle aucun modèle : c'est la Bible
§2.5 l. 163 (« il s'appuie sur le fichier d'ingestion associé au document ») et
§2.6 l. 189 (« sans recommencer systématiquement l'analyse complète à chaque
question »). Le chemin vivant demande donc à lire l'INGESTION, ce qui change la
nature de ce fichier : les sept tests qui pinnaient l'ancien routage
vision/OCR/repli ont été réécrits pour attester le nouveau contrat, et les
pièges de modèle (`aucun_modele`) rendent tout retour au modèle BRUYANT.

Le LLM est simulé (monkeypatch des exécuteurs), ce qui teste la *MÉCANIQUE* de la
boucle et non la qualité du modèle. C'est exactement ce qu'on veut ici : un
échec doit signifier « un maillon est cassé », pas « le modèle a mal répondu ».

Trois niveaux, du plus petit au plus large :
  - l'outil lit-il vraiment ce que l'ingestion a écrit ?
  - l'orchestrateur reçoit-il un résultat exploitable ?
  - le brief tient-il debout une fois la preuve validée ?
"""

from __future__ import annotations

import sys
import uuid
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.db.database import SessionLocal  # noqa: E402
from backend.inference import brief as B  # noqa: E402
from backend.inference import loop as L  # noqa: E402
from backend.ingestion import attachment_ingest, etat as ETAT  # noqa: E402
from backend.ipc import attachments as A  # noqa: E402
from backend.ipc.base import list_messages  # noqa: E402


def _text(q: str):
    """`text()` de SQLAlchemy : les requetes de ce fichier passent par le SQL
    textuel, parce que les ecritures doivent rester explicites et lisibles."""
    from sqlalchemy import text as _t

    return _t(q)


# ── Fabriques ────────────────────────────────────────────────────────────────


def pdf_avec_texte(montant: str = "1 200,00") -> bytes:
    """PDF RÉEL, texte RÉELLEMENT extractible : c'est `pymupdf` qui l'écrit ET qui le
    relit.

    Le PDF d'avant — un en-tête, un catalogue, du texte en clair dans le corps du
    fichier — n'a pas de flux de contenu : l'extraction n'y trouvait rien,
    l'ingestion aboutissait à `vide`, et le test ne vérifiait qu'un motif d'absence
    au lieu d'une réponse. Fabriquer un fichier que l'extraction ne sait pas lire,
    puis conclure qu'il est illisible, prouve qu'on ne sait pas lire.
    """
    import pymupdf

    texte = (
        f"Contrat de prestation. Montant total : {montant} euros. "
        "Signataire : Atelier Beziers. Date : 12 mars 2026. "
        "La prestataire s'engage a livrer les fournitures. "
    ) * 12
    document = pymupdf.open()
    try:
        document.new_page().insert_textbox(
            pymupdf.Rect(50, 50, 545, 780), texte, fontsize=9
        )
        return document.tobytes()
    finally:
        document.close()


def xlsx_avec_feuille(valeurs: list[list]) -> bytes:
    """Classeur RÉEL, écrit par `openpyxl`.

    L'ancienne fabrique composait un ZIP à la main : ce n'était pas un classeur
    valide, et l'extraction retombait sur un lecteur de secours qui lisait les
    OCTETS du fichier. Le test passait donc grâce au défaut qu'il croyait couvrir.
    """
    _openpyxl = pytest.importorskip("openpyxl")
    classeur = _openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Budget"
    feuille.append(["Poste", "Valeur"])
    for ligne in valeurs:
        feuille.append(list(ligne))
    tampon = BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()


def _tableur_de(texte: str) -> bytes:
    """Un `.csv` RÉEL dont la représentation structurée dépassera le plafond.

    La représentation (tâche 4.5) ne connaît que des DONNÉES à en-tête et
    lignes naturalisées : un csv réduit à UNE ligne dégénérée n'est pas un
    tableur — l'aplatissement TSV d'avant en rendait les octets parce qu'il
    copiait le fichier ; elle le dit « lisible mais vide ». Le texte du contrat
    est donc étalé sur des lignes de tableur réelles, en volume suffisant pour
    dépasser les 16 000 caractères conservés. Aucun mock, aucun modèle : une
    extraction csv ne demande AUCUN moteur.
    """
    lignes = ["Poste;Detail"]
    lignes += [f"L-{i:05d};{texte.strip()}" for i in range(250)]
    return ("\n".join(lignes) + "\n").encode("utf-8")


# ── Utilitaires ──────────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def conversation():
    """Une conversation neuve, avec un message utilisateur et sa pièce jointe.

    C'est le point de départ réaliste : l'orchestrateur ne voit un fichier que
    s'il est lié à un message du tour.
    """
    session = SessionLocal()
    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    session.execute(
        _text(
            "INSERT INTO conversations (id, title, mode, created_at, updated_at)"
            " VALUES (:c, 'test', 'chat', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"c": conv_id},
    )
    session.execute(
        _text(
            "INSERT INTO messages (id, conv_id, role, content, created_at)"
            " VALUES (:m, :c, 'user', ' lis le contrat et donne le montant',"
            " CURRENT_TIMESTAMP)"
        ),
        {"m": msg_id, "c": conv_id},
    )
    session.commit()
    session.close()
    return conv_id, msg_id


def _promettre(db, conv_id: str, msg_id: str, data: bytes, nom: str) -> str:
    """Dépose → stagé → promu → lié au message. Renvoie l'artifact_id."""
    item = A.stage_picker_bytes(
        db,
        {"requestId": f"boucle_{uuid.uuid4().hex[:8]}", "filename": nom, "bytes": data},
    )["items"][0]
    artefact = A.attachments_prepare(
        db,
        {
            "conversationId": conv_id,
            "stageItemIds": [item["stage_item_id"]],
            "names": [nom],
        },
    )["artifact_ids"][0]
    db.execute(
        _text(
            "INSERT INTO message_attachments (id, message_id, artifact_id, ordinal, created_at)"
            " VALUES (:id, :m, :a, 0, CURRENT_TIMESTAMP)"
        ),
        {"id": f"mat_{uuid.uuid4().hex[:12]}", "m": msg_id, "a": artefact},
    )
    db.commit()
    return artefact


def _promettre_puis_ingérer(
    db, conv_id: str, msg_id: str, data: bytes, nom: str
) -> str:
    """Dépôt par le vrai chemin, puis INGESTION RÉELLE, exécutée dans le fil.

    C'est désormais ce que `ask_files` interroge : sans ingestion, le test ne
    vérifierait qu'un motif d'absence — il passerait au vert en ne prouvant rien du
    nouveau contrat. `_ingérer_un` est pilotée ici parce que le dépôt la lance dans
    un thread, et qu'un thread survit au test qui l'a créé pour venir écrire dans la
    base du suivant.
    """
    artefact = _promettre(db, conv_id, msg_id, data, nom)
    attachment_ingest._ingérer_un(artefact)
    return artefact


@pytest.fixture(autouse=True)
def ingestion_par_le_test(monkeypatch):
    """Le dépôt ne lance pas son thread d'ingestion : ce fichier la pilote.

    Sans cela, l'ingestion partirait en arrière-plan et l'état lu par le test serait
    celui du thread — donc aléatoire. Le déclencheur est coupé au moment du dépôt,
    jamais pendant une extraction.
    """
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None)


@pytest.fixture()
def aucun_modele(monkeypatch):
    """Les quatre exécuteurs de la boucle deviennent des pièges qui ÉCHOUENT.

    Ce sont eux que le chemin vivant empruntait pour faire lire le fichier par un
    modèle : `_execute_vision` (un VLM sur les pixels), `_execute_doc_ocr` (un modèle
    OCR), `_execute_read_document` puis `_execute_read_text` (le repli). Un `assert`
    sur le résultat ne prouverait pas leur absence — une assertion indirecte peut
    passer pour une raison fausse. Une exception, elle, casse le test.
    """

    def _piege(nom: str):
        def _appele(*args, **kwargs):
            raise AssertionError(
                f"ask_files a appelé {nom} : il doit répondre depuis la représentation "
                f"d'ingestion, pas faire relire le fichier par un modèle"
            )

        return _appele

    for nom in (
        "_execute_vision",
        "_execute_doc_ocr",
        "_execute_read_text",
        "_execute_read_document",
    ):
        monkeypatch.setattr(L, nom, _piege(nom))
    return _piege


# ══════════════════════════════════════════════════════════════════════════════
# 1. L'OUTIL EXTRAIT-IL VRAIMENT ?
# ══════════════════════════════════════════════════════════════════════════════


class TestExtraction:
    def test_doc_public_est_attribue_au_fichier_depose(
        self, db, conversation, aucun_modele
    ):
        """`doc_1` doit exister ET désigner le fichier que nous venons de déposer.

        C'est le pivot de tout : si le label ne pointe pas sur le bon artefact, la
        preuve citée par le générateur désignera le mauvais fichier — silencieusement.

        CE QUE CE TEST AFFIRME AUJOURD'HUI, et n'affirmait pas avant : la réponse
        vient de l'ingestion, et AUCUN modèle n'a été mis en jeu pour la produire.
        Instrumenter les exécuteurs de lecture ne prouverait plus rien — ils ne sont
        plus sur le chemin — donc c'est un PIÈGE qui est posé, et qui casse le test
        si l'un d'eux est appelé. Un PDF sans couche texte nominationait l'OCR et le
        repli « dans le bon ordre » : on ne vérifiait qu'un aiguillage, pas la
        lecture. C'est `pdf_avec_texte()` — un vrai PDF — qui rend la question
        entière : son texte est déjà dans la représentation d'ingestion.
        """
        conv, msg = conversation
        artefact = _promettre_puis_ingérer(
            db, conv, msg, pdf_avec_texte(), "contrat.pdf"
        )

        from backend.search import docrefs as _docrefs

        assert _docrefs.resolve_doc_label(db, conv, "doc_1") == artefact, (
            "doc_1 ne désigne pas le fichier déposé"
        )

        r = L._execute_ask_files(
            {
                "args": {
                    "questions": ["Quel est le montant ?"],
                    "resource_ids": ["doc_1"],
                }
            },
            None,
            conv_id=conv,
            request_id="req_b1",
        )
        assert r["ok"] is True
        assert "1 200,00" in r["result"], (
            f"le texte ingéré ne revient pas dans le résultat :\n{r['result']}"
        )
        assert r["public_ids"] == ["doc_1"], "le tour doit nommer le fichier qu'il a lu"
        assert r["evidence_ids"], "un extrait cité sans preuve n'est pas une réponse"
        assert all(e.startswith("doc_1:") for e in r["evidence_ids"])

    def test_question_sans_fichier_refusee(self, db, conversation):
        conv, _ = conversation
        r = L._execute_ask_files(
            {"args": {"questions": ["Quel montant ?"], "resource_ids": []}},
            None,
            conv_id=conv,
            request_id="req_b2",
        )
        assert r["ok"] is False
        assert "[MISSING_REQUIRED]" in r["result"] or "ressource" in r["result"].lower()

    def test_label_public_inconnu_refuse(self, db, conversation):
        conv, _ = conversation
        r = L._execute_ask_files(
            {"args": {"questions": ["Quel montant ?"], "resource_ids": ["doc_99"]}},
            None,
            conv_id=conv,
            request_id="req_b3",
        )
        assert r["ok"] is False, (
            "un doc_ inexistant ne doit pas produire une reponse vide mais un refus"
        )
        # Cette conversation ne porte aucun document autorisé : le périmètre est vide,
        # donc ask_files refuse avant même de qualifier l'id (RESOURCE_UNAVAILABLE).
        assert "[RESOURCE_UNAVAILABLE]" in r["result"]

    def test_un_fichier_illisible_se_dis_tel_quil_est(
        self, db, conversation, aucun_modele
    ):
        """Un fichier que rien n'a pu lire ne produit NI réponse NI absence de contenu.

        AVANT, ce test exigeait `ok=False` et le motif `[READ_FAILED]` — c'est-à-dire
        le motif de la DERNIÈRE voie essayée. Il pinnait donc un routage
        (vision → OCR → lecture → repli texte) que la Bible §2.6 l. 189 interdit :
        chaque question relançait l'analyse complète du fichier.

        AUJOURD'HUI, l'appel RÉUSSIT parce que l'outil a fait son travail — dire la
        vérité sur le fichier — et la vérité, c'est le MOTIF DE L'ÉTAT. C'est ce qui
        change pour l'orchestrateur : il ne reçoit plus « le fichier ne contient
        rien », il reçoit « l'extraction a échoué, voici pourquoi », donc il ne
        relance pas un fichier qu'aucune insistance ne ramènera. L'absence de preuve
        reste, elle aussi, un fait : on ne fabrique pas de citation.
        """
        conv, msg = conversation
        artefact = _promettre(db, conv, msg, pdf_avec_texte(), "contrat.pdf")
        db.execute(
            _text(
                "UPDATE artifacts SET ingest_state = :e, ingest_reason = :r"
                " WHERE id = :a"
            ),
            {
                "e": ETAT.ETAT_ECHEC,
                "r": "extraction impossible — pdftotext: fichier absent",
                "a": artefact,
            },
        )
        db.commit()

        r = L._execute_ask_files(
            {"args": {"questions": ["Quel montant ?"], "resource_ids": ["doc_1"]}},
            None,
            conv_id=conv,
            request_id="req_b4",
        )
        assert r["ok"] is True, (
            "l'appel a réussi : l'outil a rapporté l'état du fichier"
        )
        assert ETAT.PHRASES[ETAT.ETAT_ECHEC] in r["result"], (
            f"le motif de l'état d'ingestion doit être rendu :\n{r['result']}"
        )
        assert "pdftotext" in r["result"], "la raison consignée au dépôt doit suivre"
        assert "1 200,00" not in r["result"], (
            "une réponse a été inventée pour un fichier illisible"
        )
        assert "information absente du document" not in r["result"], (
            "un fichier illisible ne doit pas se lire comme un document sans le contenu"
        )
        assert r["evidence_ids"] == [], "une absence n'a pas de preuve à citer"

    def test_aucune_seconde_tentative_ne_rejoue_le_fichier(
        self, db, conversation, aucun_modele
    ):
        """Pas de repli, pas de seconde passe : il n'y a plus rien à réessayer.

        AVANT, ce test affirmait l'OPPOSÉ, et c'était vrai : l'échec de l'OCR
        déclenchait une DEUXIÈME tentative (`_execute_read_document`), vérifiée par un
        spy. C'était la garantie qu'un PDF dont l'OCR échoue serait un jour rouvert
        autrement que par une relance manuelle.

        AUJOURD'HUI cette garantie n'a plus d'objet : `ask_files` ne refait pas
        l'extraction, donc il n'a plus de second essai à tenter. Ce qui remplace la
        relance, c'est l'état d'ingestion — horodaté, rejouable, et visible dans le
        rapport d'intégrité. Le test vérifie donc l'absence de second essai (une
        exception ici le casserait), l'absence de nouvelle observation, et le motif
        que l'orchestrateur reçoit à la place.
        """
        conv, msg = conversation
        artefact = _promettre(db, conv, msg, pdf_avec_texte(), "contrat.pdf")
        db.execute(
            _text("UPDATE artifacts SET ingest_state = :e WHERE id = :a"),
            {"e": ETAT.ETAT_VIDE, "a": artefact},
        )
        db.commit()
        avant = db.execute(
            _text("SELECT COUNT(*) FROM observations WHERE artifact_id = :a"),
            {"a": artefact},
        ).scalar()

        r = L._execute_ask_files(
            {"args": {"questions": ["Montant ?"], "resource_ids": ["doc_1"]}},
            None,
            conv_id=conv,
            request_id="req_repli",
        )

        assert ETAT.PHRASES[ETAT.ETAT_VIDE] in r["result"], (
            f"l'orchestrateur doit savoir que l'extraction n'a rien reconnu :\n{r['result']}"
        )
        apres = db.execute(
            _text("SELECT COUNT(*) FROM observations WHERE artifact_id = :a"),
            {"a": artefact},
        ).scalar()
        assert apres == avant, "une nouvelle observation : le fichier a été rouvert"

    def test_plusieurs_questions_une_seule_execution(
        self, db, conversation, aucun_modele
    ):
        """Regrouper évite de recharger la représentation trois fois.

        AVANT, le test comptait les appels à `_execute_read_document` : « au plus deux
        extractions pour trois questions ». Le compte était sur l'extracteur, donc il
        mesurait la bonne chose tant que l'extraction était une opération coûteuse.
        Il ne mesure plus rien aujourd'hui, puisque l'extraction n'est plus appelée
        du tout.

        Le test vérifie donc la propriété qui reste : les trois questions portent sur
        la MÊME représentation, lue une fois, et la réponse ne change pas de document
        entre elles. Une lecture par question donnerait trois horodatages d'observation
        différents — c'est ce que le test détecte.
        """
        conv, msg = conversation
        artefact = _promettre_puis_ingérer(
            db, conv, msg, pdf_avec_texte(), "contrat.pdf"
        )
        avant = db.execute(
            _text("SELECT COUNT(*) FROM observations WHERE artifact_id = :a"),
            {"a": artefact},
        ).scalar()

        r = L._execute_ask_files(
            {
                "args": {
                    "questions": ["Montant ?", "Signataire ?", "Date ?"],
                    "resource_ids": ["doc_1"],
                }
            },
            None,
            conv_id=conv,
            request_id="req_b5",
        )
        assert "mode matrice" in r["result"]
        apres = db.execute(
            _text("SELECT COUNT(*) FROM observations WHERE artifact_id = :a"),
            {"a": artefact},
        ).scalar()
        assert apres == avant, (
            f"{apres - avant} nouvelles lectures pour trois questions"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. L'ORCHESTRATEUR REÇOIT-IL QUOI QU'IL PUISSE UTILISER ?
# ══════════════════════════════════════════════════════════════════════════════


class TestReceptionOrchestrateur:
    def test_les_preuves_sont_exposees_pour_le_brief(
        self, db, conversation, aucun_modele
    ):
        """`ask_files` doit rendre des `public_ids` ET des `evidence_ids` : sans eux
        l'orchestrateur ne peut rien citer dans son brief.

        AVANT, les identifiants venaient d'une lecture SIMULÉE qui les rendait en dur
        (`public_ids: ["doc_1"]`), donc le test ne prouvait qu'un copier-coller : il
        aurait continué de passer si l'outil avait cessé de nommer ses fichiers.
        AUJOURD'HUI, rien n'est rendu en dur : les `doc_N` sont ceux que l'outil a
        RÉSOLUS dans CETTE conversation, et les preuves pointent des fragments
        PERSISTÉS de l'ingestion. Un `doc_1` qui désignerait un autre fichier ferait
        échouer le test.
        """
        conv, msg = conversation
        _promettre_puis_ingérer(db, conv, msg, pdf_avec_texte(), "contrat.pdf")

        r = L._execute_ask_files(
            {"args": {"questions": ["Quel montant ?"], "resource_ids": ["doc_1"]}},
            None,
            conv_id=conv,
            request_id="req_b6",
        )
        assert r.get("public_ids"), (
            "aucune preuve publique : le brief ne pourra rien citer"
        )
        assert "doc_1" in r["public_ids"]
        assert r.get("evidence_ids"), (
            "aucune preuve exploitable : le brief n'aura rien à citer"
        )
        assert all(e.startswith("doc_1:") for e in r["evidence_ids"]), (
            f"une preuve ne pointe pas un fragment du fichier lu : {r['evidence_ids']}"
        )

    def test_le_resultat_tient_dans_le_budget_de_texte(
        self, db, conversation, aucun_modele
    ):
        """Un document de plusieurs dizaines de kilo-octets ne doit pas saturer le
        contexte du modèle.

        AVANT, ce test simulait un résultat géant en patchant la lecture : il
        mesurait donc la COUPURE de l'ancien chemin (`[:800]` par ligne), pas celle
        qui existe. Il ne voyait rien du plafond de conservation, qui est la seule
        borne réelle : le texte servi vient de l'ingestion, et elle est bornée par la
        DONNÉE. Le test dépose donc un vrai document qui dépasse ce plafond, et
        vérifie les deux choses qui doivent rester vraies : le résultat tient dans le
        budget du tour, et il DIT qu'il est borné.
        """
        from backend.inference.tool_executor import TOOL_RESULT_MAX_CHARS

        conv, msg = conversation
        gros = (
            "Contrat de prestation. Montant total : 1 200,00 euros. "
            "Signataire : Atelier Beziers. "
        )
        artefact = _promettre_puis_ingérer(
            db, conv, msg, _tableur_de(gros), "rapport.csv"
        )

        r = L._execute_ask_files(
            {
                "args": {
                    "questions": [
                        "Quel est le montant ?",
                        "Qui est le signataire ?",
                        "Quelle est la date ?",
                    ],
                    "resource_ids": ["doc_1"],
                }
            },
            None,
            conv_id=conv,
            request_id="req_b7",
        )
        assert len(r["result"]) <= TOOL_RESULT_MAX_CHARS + 400, (
            f"résultat de {len(r['result'])} caractères : le plafond ne s'applique pas"
        )
        assert ETAT.PHRASES[ETAT.ETAT_TRONQUE] in r["result"], (
            "un résultat servi sur un texte borné doit dire son motif "
            f"d'indexation tronquée :\n{r['result'][:400]}"
        )

    def test_echec_reste_tracable(self, db, conversation, aucun_modele):
        """Un échec doit rester visible dans le tour, pas être avalé : c'est ce qui
        permet au brief de dire POURQUOI il n'a pas la réponse.

        AVANT, la trace était le MOTIF DE LA DERNIÈRE VOIE (`[READ_FAILED]`), donc le
        motif d'un chemin d'exécution — celui qui a échoué en dernier. Il disparaissait
        dès qu'un chemin réussissait, et il mentait sur la nature du problème : une
        `KeyError` se lisait comme un délai dépassé.

        AUJOURD'HUI, la trace est l'ÉTAT D'INGESTION : il survit aux questions, il ne
        dépend d'aucun ordre d'essai, et il est le même mot que celui du rapport
        d'intégrité et de l'écran. Le tour nomme en plus le fichier (`public_ids`),
        même quand sa lecture a échoué : la discussion avait le droit de le nommer,
        et c'est ce droit qui permet au brief de dire « le fichier que vous avez
        nommé n'a pas pu être lu ».
        """
        conv, msg = conversation
        artefact = _promettre(db, conv, msg, pdf_avec_texte(), "contrat.pdf")
        db.execute(
            _text(
                "UPDATE artifacts SET ingest_state = :e, ingest_reason = :r"
                " WHERE id = :a"
            ),
            {
                "e": ETAT.ETAT_BINAIRE,
                "a": artefact,
                "r": "Contenu binaire refusé : ce fichier n'est pas du texte",
            },
        )
        db.commit()

        r = L._execute_ask_files(
            {"args": {"questions": ["Montant ?"], "resource_ids": ["doc_1"]}},
            None,
            conv_id=conv,
            request_id="req_b8",
        )
        assert ETAT.PHRASES[ETAT.ETAT_BINAIRE] in r["result"], (
            "le motif de l'état doit remonter : sans lui, l'orchestrateur conclut "
            f"« le document ne contient rien » :\n{r['result']}"
        )
        assert "n'est pas du texte" in r["result"], "la raison consignée doit suivre"
        assert r["public_ids"] == ["doc_1"], (
            "le fichier nommé par la discussion reste nommé dans le tour"
        )
        assert r["evidence_ids"] == [], "rien n'a été lu : aucune preuve à fabriquer"


# ══════════════════════════════════════════════════════════════════════════════
# 3. LE BRIEF TIENT-IL DEBOUT ?
# ══════════════════════════════════════════════════════════════════════════════


class TestBrief:
    def test_brief_valide_avec_une_preuve_reelle(self, db, conversation, aucun_modele):
        """Le handoff v1.2 : le montant lu ce tour, tel que le backend le recevra.

        La preuve n'est plus une chaîne SIMULÉE : un fragment RÉELLEMENT PERSISTÉ
        par l'ingestion porte le rang que `ask_files` a émis. Depuis la signature
        v1.2 (`{"brief"}`, fiche §2.2), la validation ne juge plus la preuve : le
        test vérifie que le brief TEXTUEL reçu court, et que le rang éparpillé
        par l'outil revient cité dans ce texte.
        """
        conv, msg = conversation
        artefact = _promettre_puis_ingérer(
            db, conv, msg, pdf_avec_texte(), "contrat.pdf"
        )

        r = L._execute_ask_files(
            {"args": {"questions": ["Quel montant ?"], "resource_ids": ["doc_1"]}},
            None,
            conv_id=conv,
            request_id="req_b9",
        )
        preuves = sorted(r.get("evidence_ids") or [])
        assert preuves, "ask_files doit exposer les preuves vues dans le tour"
        rang = int(preuves[0].split(":")[1])
        fragments = db.execute(
            _text("SELECT COUNT(*) FROM artifact_chunks WHERE artifact_id = :a"),
            {"a": artefact},
        ).scalar()
        assert fragments, (
            "l'ingestion n'a produit aucun fragment : la preuve est inventée"
        )
        assert rang < fragments, "le rang cité ne désigne aucun fragment du fichier lu"

        args = {
            "brief": (
                "Le montant de la facture est de 1 200,00 €, "
                "lu dans contrat.pdf ce tour (preuve : " + preuves[0] + ")."
            )
        }
        ok, code, normalise = B.validate_submit_brief(args)
        assert ok, f"brief refusé : {code} — {normalise.get('message', '')}"
        rendu = B.render_brief(normalise)
        assert "1 200,00" in rendu, "le montant extrait doit apparaitre dans le brief"
        assert preuves[0] in rendu, (
            "en v1.2 le rendu EST le texte soumis : la provenance émise par l'outil "
            "y vit ; c'est le GÉNÉRATEUR (prompt) qui interdit les ids en sortie utilisateur"
        )

    def test_le_brief_ne_recopie_pas_une_preuve_que_le_fichier_na_pas_rendue(
        self, db, conversation
    ):
        """Une citation qui n'a pas été émise CE tour ne peut pas exister au schéma.

        Auparavant refusée EVIDENCE_UNKNOWN ; en v1.2 la fenêtre est fermée
        différemment : la structure des preuves est révoquée (fiche §2.3), et la
        règle qui reste est à la SOURCE — un rang non persisté n'est jamais émis
        (`_chunk_evidence_id` vaut None, verrouillé in test_public_id_contract).
        """
        ok, code, _ = B.validate_submit_brief(
            {
                "brief": "Le montant est de 99 999 €.",
                "facts": [{"evidence_ids": ["doc_1:7"]}],
            }
        )
        assert ok is False and code == "UNKNOWN_ARG"

    def test_brief_refuse_un_contenu_vide(self, db, conversation):
        conv, _ = conversation
        ok, code, _ = B.validate_submit_brief({"brief": "   "})
        assert ok is False and code == "EMPTY_BRIEF"

    def test_brief_signale_un_echec_de_lecture(self, db, conversation, aucun_modele):
        """Le cas de q-r.md : la lecture a échoué, le brief doit le DIRE, pas le cacher
        derrière un fait affirmatif.

        AVANT, l'échec était celui des DEUX VOIES (`[OCR_FAILED]` puis `[READ_FAILED]`)
        et le test exigeait `ok=False`. Il pinnait donc le fait qu'un échec d'outil
        rende le tour en échec — ce qui est vrai d'une panne, et FAUX d'un fichier
        illisible : un fichier que rien ne peut lire ne se réessaie pas, donc le tour
        n'a rien à retenter. Le retry de la boucle aurait transformé une constatation
        en série de reprises.

        AUJOURD'HUI, l'échec est un ÉTAT, il est rendu tel quel, et le brief TEXTUEL
        doit pouvoir le décrire — c'est la fiche §4.3 : « une sortie dégradée est un
        état de traitement, pas une raison de masquer ».
        """
        conv, msg = conversation
        artefact = _promettre(db, conv, msg, pdf_avec_texte(), "contrat.pdf")
        db.execute(
            _text(
                "UPDATE artifacts SET ingest_state = :e, ingest_reason = :r"
                " WHERE id = :a"
            ),
            {
                "e": ETAT.ETAT_ECHEC,
                "r": "extraction impossible — worker OCR absent",
                "a": artefact,
            },
        )
        db.commit()

        r = L._execute_ask_files(
            {"args": {"questions": ["Montant ?"], "resource_ids": ["doc_1"]}},
            None,
            conv_id=conv,
            request_id="req_b10",
        )
        assert "worker OCR absent" in r["result"], (
            f"le motif doit être celui de l'état, pas celui d'une voie d'exécution :\n{r['result']}"
        )
        args = {
            "brief": "La lecture du document a échoué : extraction impossible — "
            "worker OCR absent. Aucun montant n'a pu être obtenu."
        }
        ok, code, normalise = B.validate_submit_brief(args)
        assert ok, f"un tour d'échec doit pouvoir être décrit : {code}"
        assert "échoué" in B.render_brief(normalise)


# ══════════════════════════════════════════════════════════════════════════════
# 4. LE PARCOURS ENTIER
# ══════════════════════════════════════════════════════════════════════════════


class TestParcoursEntier:
    def test_tableur_depose_lu_puis_cite(self, db, conversation, aucun_modele):
        """Le même parcours avec un TABLEUR, pas un PDF : le chemin de promotion
        diffère (conteneur ZIP, type `sheet`) et c'est ce qui doit être vérifié.

        CE QUI CHANGE, et qui rend le test beaucoup plus fort : avant, la lecture était
        SIMULÉE et rendait « Atelier 1200 » en dur. Le test prouvait donc qu'un
        `doc_N` était cité — pas qu'un TABLEUR était lu. La fabrique, elle, composait
        un ZIP qui n'était pas un classeur valide : la chaîne des deux mocks se
        validaient mutuellement, et le test passait sans qu'aucun tableur n'ait
        jamais été extrait.

        AUJOURD'HUI, le classeur est ÉCRIT par `openpyxl` et EXTRACTED par
        l'ingestion. Le contenu que le brief cite vient donc d'un tableur réellement
        aplati en texte — la Bible §2.6 l. 213 : « un tableur doit recevoir une
        représentation textuelle de ses données », pas d'une description.
        """
        conv, msg = conversation
        artefact = _promettre_puis_ingérer(
            db,
            conv,
            msg,
            xlsx_avec_feuille(
                [["Atelier", 1200]] + [[f"Poste {i}", i * 10] for i in range(1, 20)]
            ),
            "budget.xlsx",
        )
        genre = db.execute(
            _text("SELECT kind FROM artifacts WHERE id=:a"), {"a": artefact}
        ).scalar()
        assert genre == "sheet", f"kind={genre}"

        r = L._execute_ask_files(
            {
                "args": {
                    "questions": ["Quel est le montant alloué à l'atelier ?"],
                    "resource_ids": ["doc_1"],
                }
            },
            None,
            conv_id=conv,
            request_id="req_b11",
        )
        assert r["ok"] is True, f"le tableur n'a pas été lu :\n{r['result']}"
        assert "Atelier" in r["result"] and "1200" in r["result"], (
            f"la donnée du tableur ne revient pas :\n{r['result']}"
        )
        assert r["evidence_ids"], "une donnée lue sans preuve n'est pas citable"

        ok, code, normalise = B.validate_submit_brief(
            {
                "brief": (
                    "Le budget alloué à l'atelier est de 1 200 €, lu dans budget.xlsx "
                    "ce tour (preuve : "
                    + ", ".join(sorted(r.get("evidence_ids") or []))
                    + ")."
                )
            },
        )
        assert ok, f"brief du tableur refusé : {code}"

    def test_l_historique_revoit_la_piece_jointe(self, db, conversation, monkeypatch):
        """Après le tour, la pièce jointe doit être VISIBLE dans l'historique —
        c'est ce que l'utilisateur voit en rouvrant la conversation."""
        conv, msg = conversation
        _promettre(db, conv, msg, pdf_avec_texte("final"), "contrat.pdf")
        messages = list_messages(db, {"conversationId": conv})["messages"]
        assert len(messages) == 1
        assert len(messages[0]["attachments"]) == 1, (
            "la piece jointe n'apparait pas dans l'historique du tour"
        )
        att = messages[0]["attachments"][0]
        assert att["kind"] == "pdf"
        assert att["id"].startswith("art_")

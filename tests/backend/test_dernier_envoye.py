"""« la dernière image que je t'ai envoyée » — retrouver un fichier SANS se souvenir de son nom.

Bible §2.5, l. 147 : `find_files` « doit permettre de retrouver un fichier à partir de son
nom, de son contenu ou d'une référence donnée par l'utilisateur, comme “la dernière image
que j'ai envoyée” ». L. 153 et l. 167 le répètent en exemple. Retrouver un fichier « ne doit
pas exiger que l'utilisateur se souvienne de son nom exact ».

CE QUE CES TESTS PROUVENT, et pourquoi ce ne sont pas des tests de mots :

1. L'ORDRE TOTAL des pièces jointes d'une discussion. « La dernière » n'est une réponse
   que s'il existe un ordre où « dernière » a un sens. Cet ordre est construit sur
   `(Message.created_at, MessageAttachment.ordinal, MessageAttachment.id)` : les deux
   premiers termes portent l'intention de l'utilisateur, le troisième ne porte que la
   STABILITÉ. Le test 2 mesure la stabilité en REJOUANT l'appel, et compare le gagnant à
   ce que la règle désigne — pas à ce qui sort du hasard d'un plan d'exécution.

2. LES DEUX restrictions que la demande porte à la fois. « La dernière image » restreint
   le TYPE, « que je t'ai envoyée » restreint la DIRECTION (envoyé par l'utilisateur,
   donc dans cette discussion). Un ordinal qui ignorerait l'un des deux rendrait un
   fichier que personne n'a demandé : le test 3 place un PDF en dernier, le test 4 place
   une image PRODUITE par l'application après celle de l'utilisateur.

3. L'HONNÊTET quand la demande ne se résout pas. « La deuxième image » d'une seule image
   ne doit pas rendre cette image : le test 6 vérifie le MOTIF rendu, pas seulement
   l'absence de résultat.

Montage : base temporaire, `SessionLocal` du projet redirigée, racines de données
jetables, ingestion neutralisée. Les fichiers entrent par le VRAI chemin de promotion
(`stage_picker_bytes` puis `attachments_prepare`), parce qu'un artefact fabriqué à la main
ne prouverait rien du contrat de dépôt. Les pièces jointes de discussion sont écrites
comme `backend/main.py` les écrit : une ligne `Message` par tour, une `MessageAttachment`
par pièce, `ordinal` = rang dans le tour. Aucun octet ni aucune ligne du projet n'est
touché.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Conversation,
    Message,
    MessageAttachment,
)
from backend.ingestion import attachment_ingest
from backend.inference import loop as L
from backend.inference.media_policy import (
    has_explicit_reference,
    index_reference,
    needs_reinspection,
    parse_media_reference,
    pick_target,
    reference_fichier,
)
from backend.ipc import attachments as A
from backend.search import cards as Cards
from backend.search import fts as Fts
from backend.services import espaces as service
from backend.tools.base import ToolContext
from backend.tools.find_files import FindFilesTool

# Un PNG 1×1 minimal : `sniff_mime` doit en reconnaître la signature, sinon le fichier est
# REFUSÉ et le test mesurerait un refus au lieu d'un ordinal.
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def pdf_bytes(marqueur: str = "rapport") -> bytes:
    """PDF minimal mais structurallement valide (en-tête + catalogue + EOF)."""
    return (
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF " + marqueur.encode()
    )


BASE_TEMPORAIRE = "2026-10-05"


def _instant(hh: int = 9, mm: int = 0, jour: int = 5) -> datetime:
    """Un horodatage choisi. Le fixer à la main est ce qui permet de forcer l'ÉGALITÉ
    de deux horodatages — un `server_default` ne laisserait pas choisir lequel."""
    return datetime(2026, 10, jour, hh, mm, 0, tzinfo=timezone.utc)


@pytest.fixture()
def sessions(tmp_path, monkeypatch):
    """Base neuve et jetable, index FTS créé, ingestion neutralisée, racines isolées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dernier-envoye.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts
    _ensure_fts(engine)  # sinon la fiche n'a nulle part où s'indexer
    fabrique = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", fabrique)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda artifact_ids: None)
    return fabrique


# ---------- Montage : promotion réelle + messages comme `main.py` les écrit ----------

def _promouvoir(db, dest: dict, nom: str, donnees: bytes, request_id: str,
                fiche: str = "") -> str:
    """Fait entrer un fichier par la VRAI promotion, puis lui donne une fiche indexée.

    La promotion crée l'artefact, son blob et son lien de scope. La fiche et la ligne FTS
    suivent : sans elles, le mode recherche ne verrait rien, et un test de l'ordinal
    confondrait « invisible à l'ordre » avec « invisible à la recherche ».
    """
    jeton = A.stage_picker_bytes(db, {"request_id": request_id, "filename": nom,
                                      "bytes": donnees})["items"][0]["stage_item_id"]
    (artifact_id,) = A.attachments_prepare(db, {**dest, "stageItemIds": [jeton],
                                                 "names": [nom]})["artifact_ids"]
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
    card = Cards.ensure_card_on_import(db, artifact)
    if fiche:
        card.summary = fiche
    db.commit()
    for lien in (db.query(ArtifactLink)
                 .filter(ArtifactLink.artifact_id == artifact_id,
                         ArtifactLink.removed_at.is_(None)).all()):
        Fts.index_card_row(db, artifact, card, lien.scope_kind, lien.scope_id, "")
    db.commit()
    return artifact_id


def _tour(db, conv_id: str, created_at: datetime, role: str = "user",
          msg_id: str = "") -> str:
    """Un message de discussion, à l'horodatage choisi. Retourne son id."""
    msg_id = msg_id or f"msg_{uuid.uuid4().hex[:12]}"
    db.add(Message(id=msg_id, conv_id=conv_id, role=role, content="tour", status="completed",
                   created_at=created_at))
    db.commit()
    return msg_id


def _rattacher(db, msg_id: str, artifact_id: str, ordinal: int = 0,
               matt_id: str = "") -> str:
    """`MessageAttachment` : « ce message montre ce fichier », comme `main.py`."""
    matt_id = matt_id or f"matt_{msg_id}_{ordinal}"
    db.add(MessageAttachment(id=matt_id, message_id=msg_id, artifact_id=artifact_id,
                             ordinal=ordinal, created_at=None))
    db.commit()
    return matt_id


def _chat(sessions, nom: str, avec_espace: bool = True) -> tuple[str, str | None]:
    """Discussion, seule ou membre d'un espace. Retourne (conversation, espace)."""
    db = sessions()
    try:
        espace = str(service.create_workspace(db, nom).id) if avec_espace else None
        conv_id = f"conv_{uuid.uuid4().hex[:10]}"
        db.add(Conversation(id=conv_id, title=nom, mode="chat", workspace_id=espace))
        db.commit()
        return conv_id, espace
    finally:
        db.close()


def _nom_de(db, nom: str) -> str:
    return str(db.query(Artifact).filter(Artifact.original_filename == nom).one().id)


def _appeler(conversation: str, arguments: dict):
    ctx = ToolContext(run_id="r1", request_id=conversation, conv_id=conversation)
    return asyncio.run(FindFilesTool().call(dict(arguments), ctx))


def _charge(resultat) -> dict:
    return json.loads(resultat.content)


def _seul(resultat) -> dict:
    """Le résultat unique attendu, ou une erreur qui dit lequel n'a pas été rendu."""
    charge = _charge(resultat)
    assert charge["results"], f"aucun résultat : {resultat.display_summary}"
    assert len(charge["results"]) == 1, f"{len(charge['results'])} résultats : un ordinal rend UN fichier"
    return charge["results"][0]


# ---------- 1. « la dernière image » rend le bon fichier ----------

def test_la_derniere_image_rend_la_bonne_piece_jointe(sessions):
    """Trois pièces jointes, images et PDF mêlés : « la dernière image » est la 3e.

    Le PDF est au MILIEU, pas à la fin : s'il était le dernier, ce test mesurerait la
    restriction de type du test 3 au lieu de l'ordre, et les deux fautes seraient
    confondues.
    """
    conv_id, espace = _chat(sessions, "Menus")
    db = sessions()
    try:
        _promouvoir(db, {"workspaceId": str(espace)}, "Menu_du_depot.pdf", pdf_bytes("depot"), "menu-d")
        _rattacher(db, _tour(db, conv_id, _instant(9, 0)), _nom_de(db, "Photo_plat.png")
                   if _promouvoir(db, {"conversationId": conv_id}, "Photo_plat.png", PNG_1PX, "menu-1")
                   else "")
        _promouvoir(db, {"conversationId": conv_id}, "Facture.pdf", pdf_bytes("facture"), "menu-2")
        _rattacher(db, _tour(db, conv_id, _instant(9, 10)), _nom_de(db, "Facture.pdf"))
        _promouvoir(db, {"conversationId": conv_id}, "Menu_restaurant.png", PNG_1PX, "menu-3")
        _rattacher(db, _tour(db, conv_id, _instant(9, 20)), _nom_de(db, "Menu_restaurant.png"))
    finally:
        db.close()

    resultat = _appeler(conv_id, {"query": "la dernière image que je t'ai envoyée"})

    assert resultat.ok
    charge = _charge(resultat)
    assert charge["mode"] == "reference"
    rendu = _seul(resultat)
    assert rendu["name"] == "Menu_restaurant.png"
    assert rendu["type"] == "image"
    assert rendu["origin"] == "conversation"
    # Les identifiants voyagent HORS du contenu : c'est eux que la boucle alimente, et le
    # contenu de `call()` est tronqué sans qu'ils disparaissent (voir le test 7).
    assert resultat.public_ids == [rendu["public_id"]]
    assert charge["reference"]["envoyee_par_l_utilisateur"] is True
    assert charge["reference"]["position"] == "dernier"


# ---------- 2. Deux horodatages identiques : départage DÉTERMINISTE et stable ----------

def test_deux_pieces_de_meme_horodatage_sont_departagees_stablement(sessions):
    """Même horodatage, deux messages : le départage est REJOUÉ, pas supposé.

    WHY ce cas n'est pas rare : `Message.created_at` est écrit par
    `server_default=func.now()`, que SQLite résout à la SECONDE. Deux tours écoulés dans
    la même seconde produisent donc réellement deux horodatages égaux, et c'est
    précisément le cas où « la dernière » n'a pas de sens.

    Le test vérifie deux choses distinctes : (a) huit appels successifs rendent le MÊME
    `doc_N` — la stabilité, MESURÉE et non supposée ; (b) le gagnant est celui que la règle
    désigne, donc que la réponse ne doit rien à l'ordre d'exécution de SQLite.
    """
    conv_id, espace = _chat(sessions, "Ex_aequo")
    db = sessions()
    try:
        _promouvoir(db, {"workspaceId": str(espace)}, "Du_depot.pdf", pdf_bytes("depot"), "ex-0")
        _promouvoir(db, {"conversationId": conv_id}, "A_alpha.png", PNG_1PX, "ex-a")
        _promouvoir(db, {"conversationId": conv_id}, "B_beta.png", PNG_1PX, "ex-b")
        # Deux messages DISTINCTS, même horodatage à la seconde, une pièce chacun : les
        # termes 1 et 2 de l'ordre total sont donc identiques, et seule la pièce jointe
        # les départage. Les ids sont posés à l'inverse de l'ordre d'écriture, pour que le
        # test distingue « l'ordre du INSERT » de « l'ordre de l'identifiant ».
        _rattacher(db, _tour(db, conv_id, _instant(10, 30), msg_id="msg_a"), _nom_de(db, "A_alpha.png"),
                   matt_id="matt_zz")
        _rattacher(db, _tour(db, conv_id, _instant(10, 30), msg_id="msg_b"), _nom_de(db, "B_beta.png"),
                   matt_id="matt_aa")
        # La règle, écrite par le TEST, DIT quel est le gagnant : le DERNIER élément de
        # l'ordre croissant `(created_at, ordinal, id)`. On ne reprend pas le code de
        # l'outil — on écrit la règle, sinon le test ne prouverait que sa propre.
        gagnante = max(
            (ligne for ligne in db.query(MessageAttachment.id, Message.created_at,
                                         MessageAttachment.ordinal, Artifact.original_filename)
             .join(Artifact, Artifact.id == MessageAttachment.artifact_id)
             .join(Message, Message.id == MessageAttachment.message_id).all()),
            key=lambda ligne: (ligne[1], ligne[2], ligne[0]),
        )
        attendu_nom = str(gagnante[3])
        db.commit()
    finally:
        db.close()

    rendus = [_appeler(conv_id, {"query": "la dernière image"}) for _ in range(8)]
    noms = [_seul(r)["name"] for r in rendus]
    doc_ids = [tuple(r.public_ids) for r in rendus]

    assert len(set(doc_ids)) == 1, f"huit appels, {len(set(doc_ids))} réponses : l'ordre n'est pas total"
    assert set(noms) == {attendu_nom}, (
        f"le gagnant est {noms[0]}, la règle désigne {attendu_nom}")
    # Les ids sont posés à l'INVERSE de l'ordre d'écriture : un ordre par INSERT rendrait
    # l'autre fichier. Ce test distingue donc la règle du hasard du plan d'exécution.
    assert noms[0] == "A_alpha.png", "c'est l'ordre total qui départage, pas l'écriture"
    # Et l'IGNORANCE est déclarée : le terme 3 ne dit rien de l'intention de l'utilisateur.
    assert all(_charge(r)["ambiguous"] is True for r in rendus)
    assert all("Ambiguïté" in r.display_summary for r in rendus)


def test_les_pieces_sans_egalite_ne_sont_jamais_signalees_ambigues(sessions):
    """Le contre-exemple : sans égalité d'horodatage, rien n'est signalé.

    Un test qui ne vérifierait que le cas ambigu passerait avec un outil qui signale
    TOUJOURS une ambiguïté. Celui-ci mesure l'autre moitié : deux tours distincts, une
    réponse certaine.
    """
    conv_id, _ = _chat(sessions, "Sans_egalite", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Une.png", PNG_1PX, "sans-1")
        _promouvoir(db, {"conversationId": conv_id}, "Deux.png", PNG_1PX, "sans-2")
        _rattacher(db, _tour(db, conv_id, _instant(11, 1)), _nom_de(db, "Une.png"), matt_id="matt_u")
        _rattacher(db, _tour(db, conv_id, _instant(11, 2)), _nom_de(db, "Deux.png"), matt_id="matt_d")
        db.commit()
    finally:
        db.close()

    resultat = _appeler(conv_id, {"query": "la dernière image"})

    assert _charge(resultat)["ambiguous"] is False
    assert "Ambiguïté" not in resultat.display_summary
    assert _seul(resultat)["name"] == "Deux.png"


# ---------- 3. La restriction de TYPE ----------

def test_un_pdf_n_est_pas_rendu_pour_la_derniere_image(sessions):
    """La dernière PIÈCE est un PDF : « la dernière image » ne doit pas le rendre.

    C'est l'erreur que le seul tri commettrait, et elle est INVISIBLE sans ce montage :
    sans restriction de type, la réponse est la dernière pièce, donc fausse.
    """
    conv_id, _ = _chat(sessions, "Melange", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Photo_vieille.png", PNG_1PX, "mel-1")
        _promouvoir(db, {"conversationId": conv_id}, "Contrat.pdf", pdf_bytes("contrat"), "mel-2")
        _rattacher(db, _tour(db, conv_id, _instant(12, 0)), _nom_de(db, "Photo_vieille.png"), matt_id="m1")
        _rattacher(db, _tour(db, conv_id, _instant(12, 5)), _nom_de(db, "Contrat.pdf"), matt_id="m2")
        db.commit()
    finally:
        db.close()

    image = _appeler(conv_id, {"query": "la dernière image"})
    pdf = _appeler(conv_id, {"query": "le dernier pdf"})

    assert _seul(image)["name"] == "Photo_vieille.png", "un pdf rendu pour « la dernière image »"
    assert "Contrat" not in image.display_summary
    assert _seul(pdf)["name"] == "Contrat.pdf", "une image rendue pour « le dernier pdf »"
    assert "Photo_vieille" not in pdf.display_summary


def test_les_synonymes_de_type_sont_le_meme_type(sessions):
    """« photo » est « image » : le vocabulaire est une fonction du mot, pas une liste.

    Une liste de couples figés laisserait passer « la dernière photo » sans la
    restriction de type, et l'outil rendrait alors un PDF.
    """
    conv_id, _ = _chat(sessions, "Synonymes", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Carte.png", PNG_1PX, "syn-1")
        _promouvoir(db, {"conversationId": conv_id}, "Contrat.pdf", pdf_bytes("c"), "syn-2")
        _rattacher(db, _tour(db, conv_id, _instant(13, 0)), _nom_de(db, "Carte.png"), matt_id="m1")
        _rattacher(db, _tour(db, conv_id, _instant(13, 1)), _nom_de(db, "Contrat.pdf"), matt_id="m2")
        db.commit()
    finally:
        db.close()

    assert _seul(_appeler(conv_id, {"query": "la dernière photo"}))["name"] == "Carte.png"


# ---------- 4. La restriction de DIRECTION : « que je t'ai envoyée » ----------

def test_que_je_t_ai_envoyee_exclut_ce_qui_vient_d_ailleurs(sessions):
    """La dernière image de la discussion n'est pas celle que l'utilisateur a envoyée.

    Le second fichier est une image PRODUITE par l'application : elle est jointe à un
    message de rôle `assistant` (`main.py`, finalisation de trace), donc elle EST une
    pièce jointe de la discussion — et elle n'est PAS « envoyée par l'utilisateur ». C'est
    la seule direction possible d'un écart de cet ordre.
    """
    conv_id, _ = _chat(sessions, "Direction", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Envoyee.png", PNG_1PX, "dir-1")
        _promouvoir(db, {"conversationId": conv_id}, "Generee.png", PNG_1PX, "dir-2")
        _rattacher(db, _tour(db, conv_id, _instant(14, 0)), _nom_de(db, "Envoyee.png"), matt_id="m1")
        _rattacher(db, _tour(db, conv_id, _instant(14, 1), role="assistant"), _nom_de(db, "Generee.png"),
                   matt_id="m2")
        db.commit()
    finally:
        db.close()

    sans_direction = _appeler(conv_id, {"query": "la dernière image"})
    avec_direction = _appeler(conv_id, {"query": "la dernière image que je t'ai envoyée"})

    assert _seul(sans_direction)["name"] == "Generee.png", (
        "sans restriction de direction, la dernière pièce jointe de la discussion compte")
    assert _seul(avec_direction)["name"] == "Envoyee.png", (
        "« que je t'ai envoyée » doit exclure une image que l'utilisateur n'a pas envoyée")
    assert "Generee" not in avec_direction.display_summary


# ---------- 5. Le dépôt n'est pas concerné, et c'est DIT ----------

def test_le_depot_n_est_pas_concerne_par_un_ordinal_de_discussion(sessions):
    """Un fichier du dépôt plus RÉCENT que toutes les pièces n'est pas « la dernière ».

    Le dépôt n'a pas d'ordre d'AJOUT : personne ne l'a joint à un message. Le traiter
    comme une pièce jointe de plus ferait de « la dernière image » une recherche dans tout
    ce que la discussion peut lire — c'est-à-dire exactement ce que la demande ne dit pas.

    Le test montre les DEUX faces : le dépôt est invisible pour l'ordinal, et il reste
    visible par la liste. Ce n'est donc pas une absence de fichier, c'est une absence
    D'ORDRE — et le rendu doit le dire, sinon le modèle lirait un « fichier introuvable ».
    """
    conv_id, espace = _chat(sessions, "Avec_depot")
    db = sessions()
    try:
        _promouvoir(db, {"workspaceId": str(espace)}, "Image_du_depot.png", PNG_1PX, "dep-1")
        _promouvoir(db, {"conversationId": conv_id}, "Image_de_la_discussion.png", PNG_1PX, "dep-2")
        # Trois jours plus tard que tout le reste : le dépôt est donc le plus récent.
        _rattacher(db, _tour(db, conv_id, _instant(15, 0, jour=8)), _nom_de(db, "Image_de_la_discussion.png"),
                   matt_id="m1")
        db.commit()
    finally:
        db.close()

    resultat = _appeler(conv_id, {"query": "la dernière image que je t'ai envoyée"})
    charge = _charge(resultat)

    assert "Image_du_depot.png" not in json.dumps(charge, ensure_ascii=False), (
        "un fichier du dépôt rendu pour une référence de discussion")
    assert _seul(resultat)["name"] == "Image_de_la_discussion.png"

    liste = _charge(_appeler(conv_id, {"types": ["image"]}))["results"]
    du_depot = [r for r in liste if r["origin"] == "workspace"]
    assert [r["name"] for r in du_depot] == ["Image_du_depot.png"], (
        "le dépôt doit rester visible PAR LA LISTE : c'est l'absence d'ordre, pas de fichier")

    assert "dépôt de l'espace n'est pas concerné" in resultat.display_summary


# ---------- 6. L'ambiguïté de la DEMANDE est signalée ----------

def test_la_deuxieme_image_nexiste_pas_ne_rend_pas_la_premiere(sessions):
    """Une seule image, « la deuxième » : motif rendu, résultat VIDE.

    Le défaut par défaut serait de rendre l'image disponible « pour être utile » : le
    modèle annoncerait alors un fichier que l'utilisateur n'a pas demandé, et transmettrait
    son `doc_N` à `ask_files`.
    """
    conv_id, _ = _chat(sessions, "Une_seule", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Unique.png", PNG_1PX, "one-1")
        _rattacher(db, _tour(db, conv_id, _instant(16, 0)), _nom_de(db, "Unique.png"), matt_id="m1")
    finally:
        db.close()

    resultat = _appeler(conv_id, {"query": "la deuxième image"})

    charge = _charge(resultat)
    assert charge["ok"] is True, "une référence impossible n'est pas une erreur d'appel"
    assert charge["results"] == [], "aucun fichier rendu : « la 2e » n'existe pas"
    assert "Unique" not in resultat.display_summary
    assert "2e" in resultat.display_summary, f"le motif doit nommer la position : {resultat.display_summary}"
    assert "Vérifie avec l'utilisateur" in resultat.display_summary
    assert resultat.public_ids == []


def test_le_type_absent_est_nomme_comme_tel(sessions):
    """Des fichiers existent, mais pas du type demandé : il faut le dire lesquels.

    Conflater ce cas avec « la discussion est vide » ferait conclure à l'orchestrateur que
    l'utilisateur n'a rien envoyé — donc qu'il a tort — au lieu de lui rendre son pdf.
    """
    conv_id, _ = _chat(sessions, "Pas_d_image", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Premier.pdf", pdf_bytes("p"), "nod-1")
        _promouvoir(db, {"conversationId": conv_id}, "Deuxieme.pdf", pdf_bytes("d"), "nod-2")
        _rattacher(db, _tour(db, conv_id, _instant(17, 0)), _nom_de(db, "Premier.pdf"), matt_id="m1")
        _rattacher(db, _tour(db, conv_id, _instant(17, 30)), _nom_de(db, "Deuxieme.pdf"), matt_id="m2")
    finally:
        db.close()

    resultat = _appeler(conv_id, {"query": "la dernière image que je t'ai envoyée"})

    assert _charge(resultat)["results"] == []
    assert "aucun fichier de type image" in resultat.display_summary
    assert "pdf" in resultat.display_summary, "les types présents doivent être dits"


# ---------- 7. PARTIE A : les `doc_N` hors du contenu tronqué ----------

def test_public_ids_survivent_a_la_troncature_du_contenu(sessions, monkeypatch):
    """`FindFilesTool.execute` remplit `public_ids` : sans quoi la migration perd les ids.

    `BaseTool.call()` tronque `content` à `max_output_chars` et couper un JSON en plein
    milieu le rend ILLISIBLE. La boucle doit pouvoir citer une réponse tronquée, donc les
    identifiants voyagent sur un champ à part — comme le fait déjà `SearchInfoTool`. Le
    plafond est abaissé pour vérifier le MARQUAGE et la survie des ids, sans fabriquer six
    kilo-octets de contenu.
    """
    conv_id, _ = _chat(sessions, "Troncature", avec_espace=False)
    db = sessions()
    try:
        for i in range(3):
            _promouvoir(db, {"conversationId": conv_id}, f"Doc_{i}.pdf", pdf_bytes(f"d{i}"), f"tro-{i}")
        for i in range(3):
            _rattacher(db, _tour(db, conv_id, _instant(18, i), msg_id=f"msg_{i}"),
                       _nom_de(db, f"Doc_{i}.pdf"), matt_id=f"matt_{i}")
    finally:
        db.close()

    complet = _appeler(conv_id, {"query": "le dernier pdf"})
    monkeypatch.setattr(FindFilesTool, "max_output_chars", 40)
    tronque = _appeler(conv_id, {"query": "le dernier pdf"})

    assert complet.ok and tronque.ok
    assert tronque.truncated is True, "le plafond abaissé doit se voir"
    assert tronque.public_ids, "une réponse tronquée doit rester citable"
    assert tronque.public_ids == complet.public_ids
    # Sanité du marqueur : les deux nombres sont RÉELS, sinon la troncature mentirait.
    assert f"[Tronqué : " in tronque.content


def test_le_mode_liste_remplit_aussi_public_ids(sessions):
    """Le mode liste avait un `"public_ids": []` en dur : la boucle n'avait aucun id.

    C'est le chemin le plus emprunté par l'orchestrateur (`type` + `recency`), et il
    livrait un résultat sans identifiant — donc impossible à transmettre à `ask_files`.
    """
    conv_id, espace = _chat(sessions, "Liste_ids")
    db = sessions()
    try:
        _promouvoir(db, {"workspaceId": str(espace)}, "Du_depot.pdf", pdf_bytes("d"), "lst-1")
        _promouvoir(db, {"conversationId": conv_id}, "De_la_discussion.pdf", pdf_bytes("c"), "lst-2")
    finally:
        db.close()

    resultat = _appeler(conv_id, {"types": ["pdf"]})

    assert resultat.public_ids == ["doc_1", "doc_2"], resultat.public_ids
    assert sorted(r["name"] for r in _charge(resultat)["results"]) == ["De_la_discussion.pdf", "Du_depot.pdf"]


# ---------- 8. PARTIE B : le chemin de la boucle délègue ----------

def test_le_chemin_migre_vit_le_depot_et_rend_les_identifiants(sessions):
    """L'adaptateur rend ce que `call()` rend — dépôt compris.

    MESURÉ avant la migration : deux fichiers, un mot commun, un versé au dépôt et un
    joint à la discussion. `FindFilesTool.call()` en rendait 2 avec `public_ids` VIDE ;
    `_execute_find_files` en rendait 1 — le dépôt INVISIBLE — avec `public_ids` vide
    aussi. Le test rejoue les deux portes sur la même base et compare.
    """
    conv_id, espace = _chat(sessions, "Parite_find_files")
    fiche = "Le budget du trimestre s'élève à 12 000 euros."
    db = sessions()
    try:
        _promouvoir(db, {"workspaceId": str(espace)}, "Budget_depot.pdf", pdf_bytes("depot"), "pf-1", fiche)
        _promouvoir(db, {"conversationId": conv_id}, "Budget_joint.pdf", pdf_bytes("joint"), "pf-2", fiche)
    finally:
        db.close()

    reference = _appeler(conv_id, {"query": "budget"})
    rendu = L._execute_find_files({"args": {"query": "budget"}}, conv_id=conv_id)

    assert reference.ok and rendu["ok"] is True
    assert rendu["result"] == (reference.display_summary or reference.content), (
        "le chemin migré ne rend pas ce que rend `call()`")
    assert rendu["public_ids"] == reference.public_ids == ["doc_1", "doc_2"]
    assert "Budget_depot.pdf" in rendu["result"], "le dépôt doit être nommé"
    assert "dépôt de l'espace" in rendu["result"], "l'ORIGINE est dite, pas seulement codée"


def test_le_chemin_migre_resout_la_reference_de_discussion(sessions):
    """PARTIE B appliquée à la partie C : l'ordinal passe aussi par l'adaptateur.

    C'est par là que l'orchestrateur arrive à « la dernière image » : si la référence ne
    vivait que dans `FindFilesTool`, la promesse de la Bible ne vaudrait que pour les
    tests d'outils.
    """
    conv_id, _ = _chat(sessions, "Adaptateur_ref", avec_espace=False)
    db = sessions()
    try:
        _promouvoir(db, {"conversationId": conv_id}, "Avant.png", PNG_1PX, "ad-1")
        _promouvoir(db, {"conversationId": conv_id}, "Apres.png", PNG_1PX, "ad-2")
        _rattacher(db, _tour(db, conv_id, _instant(19, 0), msg_id="msg_a"), _nom_de(db, "Avant.png"),
                   matt_id="matt_a")
        _rattacher(db, _tour(db, conv_id, _instant(19, 2), msg_id="msg_b"), _nom_de(db, "Apres.png"),
                   matt_id="matt_b")
    finally:
        db.close()

    arguments = {"query": "la dernière image que je t'ai envoyée"}
    reference = _appeler(conv_id, arguments)
    rendu = L._execute_find_files({"args": arguments}, conv_id=conv_id)

    assert rendu["ok"] is True
    assert rendu["result"] == (reference.display_summary or reference.content)
    assert rendu["public_ids"] == reference.public_ids
    assert "Apres.png" in rendu["result"]
    assert "Avant.png" not in rendu["result"]


# ---------- 9. Un ordinal, deux lecteurs : le même fichier ----------

def test_media_policy_et_find_files_designent_le_meme_fichier():
    """R2 (ré-inspection) et `find_files` lisent le MÊME ordinal.

    Ils lisaient deux listes séparées : « la dernière image » pouvait donc désigner un pdf
    pour la ré-inspection et une image pour l'identification, dans le même tour. Le
    registre porte un `kind` par entrée, donc la restriction de type y est applicable.

    Le PDF est placed EN DERNIER : c'est la seule disposition où « prendre le dernier »
    et « prendre la dernière image » ne donnent pas la même réponse. Avec le PDF en
    tête, ce test passerait même avec un `pick_target` inchangé — il a été vérifié.
    """
    registre = [
        {"id": "art_img1", "kind": "image", "name": "Une.png", "available": True},
        {"id": "art_img2", "kind": "image", "name": "Deux.png", "available": True},
        {"id": "art_pdf", "kind": "pdf", "name": "Contrat.pdf", "available": True},
    ]

    cible = pick_target("la dernière image que je t'ai envoyée", registre)
    reference = parse_media_reference("la dernière image que je t'ai envoyée")

    assert cible is not None and cible["id"] == "art_img2", (
        "la ré-inspection doit viser la même image que `find_files`, pas le dernier fichier")
    # Et la couche A sait que la demande POINTE un média : sans cela, rien ne déclencherait
    # la ré-inspection et « la dernière image » resterait sans effet de bord.
    assert has_explicit_reference("la dernière image que je t'ai envoyée") is True
    assert needs_reinspection("la dernière image que je t'ai envoyée") is True
    assert reference is not None
    assert reference.types == ("image",)
    assert reference.sent_by_user is True
    assert index_reference(reference, 2) == 1


def test_le_vocabulaire_exporte_reste_lisible_par_le_parseur():
    """Chaque collocation ORDINALE d'`EXPLICIT_REFERENCE_TERMS` reste une vraie référence.

    Cette constante est la seule lisibilité du vocabulaire pour un lecteur. Si elle
    dérive du parseur — un mot d'ordinalité ajouté à la table sans être listé, ou
    l'inverse — elle devient un mensonge affiché. Ce test fait passer chaque collocation
    par la MÊME porte que le code, donc la dérive se voit au lieu d'être supposée absente.
    """
    from backend.inference.media_policy import EXPLICIT_REFERENCE_TERMS

    deixis = ("cette image", "cette photo", "ce pdf", "ce document", "celle-ci", "celle-là",
              "dans l'image", "sur l'image", "dans la photo", "dans le document")
    non_lues = [terme for terme in EXPLICIT_REFERENCE_TERMS
                if terme not in deixis and reference_fichier(f"montre-moi {terme}") is None]

    assert non_lues == [], f"collocation d'ordinalité que le parseur ne reconnaît pas : {non_lues}"


def test_un_chiffre_nue_est_jamais_un_ordinal():
    """« envoyée en 2 minutes » ne devient pas « la 2e ».

    Accepter un chiffre nu ferait dire à `find_files` que « la dernière image » est la
    deuxième de la discussion parce que l'utilisateur a mentionné « 2 » plus loin.
    """
    sans_suffixe = parse_media_reference("la dernière image envoyée en 2 minutes")
    avec_suffixe = parse_media_reference("montre la 2ème image")

    assert sans_suffixe is not None
    assert (sans_suffixe.position, sans_suffixe.rang) == ("dernier", 0)
    assert avec_suffixe is not None and avec_suffixe.rang == 2
    assert parse_media_reference("trouve le fichier budget") is None


def test_un_ordinal_sans_nom_de_fichier_ne_demande_aucun_fichier():
    """« la dernière fois » désigne une conversation, pas un fichier.

    L'ordinal seul ne suffit pas : sans cette porte, toute phrase contenant « dernière »
    déclencherait une ré-inspection et, pour `find_files`, une recherche d'ordinal. Les deux
    consommateurs lisent la MÊME porte, sinon ils ouvriraient sur des fichiers différents.
    """
    for phrase in ("la dernière fois que tu as répondu", "le dernier résultat",
                   "trouve le fichier budget", ""):
        assert reference_fichier(phrase) is None, phrase
        assert has_explicit_reference(phrase) is False, phrase
    assert reference_fichier("le dernier document") is not None
    assert reference_fichier("la dernière capture") is not None
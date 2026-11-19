"""Le dépôt d'un espace donne aux OUTILS ce que le contrat lui promet.

La matrice de permissions (`01_architecture_registre_index.md`, §Permissions) dit
qu'un chat d'espace accède à « fichiers de cette conversation + fichiers
utilisateurs de l'espace ». L'implémentation n'en honours que la moitié : un
fichier versé au dépôt était invisible pour `find_files` et `search_info`, alors
que `docrefs` savait déjà le nommer. Deux défauts, tous deux testés ici :

1. l'index FTS ne garde qu'une ligne par LIEN, donc un fichier de dépôt n'existe
   que sous la portée `workspace` — l'interroger sous la seule portée
   `conversation` le rend non candidat, quelle que soit la taille du dépôt ;
2. un fichier sans `doc_N` était ÉCARTÉ au lieu d'être nommé. Or c'est justement
   là que l'allocation paresseuse doit avoir lieu : la discussion choisit ce
   fichier, elle ne l'a pas choisi au moment du versement.

Le troisième test est le plus important : il doit ÉCHOUER si une portée est
élargie par erreur. Un dépôt qui fuite vers un autre espace ne se voit pas dans
lesLogs — il se voit dans ce test.

Montage : base temporaire, `SessionLocal` du projet redirigée, racines de données
jetables, ingestion neutralisée. Les fichiers entrent par le VRAI chemin de dépôt
(`stage` puis `attachments_prepare`), parce qu'un artefact fabriqué à la main ne
prouverait rien du contrat de dépôt. Aucun octet ni aucune ligne du projet n'est
touché.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.config.defaults import TOOL_LIMITS
from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Conversation,
    DocRef,
    Observation,
)
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.search import cards as Cards
from backend.search import fts as Fts
from backend.services import espaces as service
from backend.tools.base import ToolContext
from backend.tools.find_files import FindFilesTool
from backend.tools.ask_files import AskFilesTool
from backend.tools.search_info import SearchInfoTool

# Un appel de dépôt accepte huit pièces : un dépôt de cinquante se verse donc par
# lots, comme l'interface le fait.
_LOTS = 8


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


@pytest.fixture()
def base_temporaire(tmp_path, monkeypatch):
    """Base neuve et jetable, index FTS créé, ingestion neutralisée, racines isolées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'depot-outils.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts
    _ensure_fts(engine)  # sinon la fiche n'a nulle part où s'indexer
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda artifact_ids: None)
    return sessions


# ---------- Montage : dépôt réel, fiches indexées ----------

def _espace(db, nom: str) -> str:
    return str(service.create_workspace(db, nom).id)


def _conversation(db, conv_id: str, workspace_id: str | None) -> str:
    db.add(Conversation(id=conv_id, title="Discussion", mode="chat", workspace_id=workspace_id))
    db.commit()
    return conv_id


def _verser(db, dest: dict, jetons: list[str], noms: list[str]) -> list[str]:
    """Promeut des fichiers stagés vers une discussion OU vers le dépôt d'un espace."""
    ids = []
    for debut in range(0, len(jetons), _LOTS):
        tranche = jetons[debut:debut + _LOTS]
        reponse = A.attachments_prepare(db, {**dest, "stageItemIds": tranche,
                                             "names": noms[debut:debut + _LOTS]})
        ids.extend(reponse["artifact_ids"])
    return ids


def _depot(db, workspace_id: str, noms: list[str], prefixes: list[str],
           request_id: str = "depot-outils") -> list[str]:
    """Verse `noms` dans le dépôt de l'espace et indexe une fiche pour chacun."""
    jetons = [
        A.stage_picker_bytes(db, {"request_id": request_id, "filename": nom,
                                  "bytes": pdf_bytes(prefixe)})["items"][0]["stage_item_id"]
        for nom, prefixe in zip(noms, prefixes)
    ]
    ids = _verser(db, {"workspaceId": workspace_id}, jetons, list(noms))
    for artifact_id in ids:
        _fiche(db, artifact_id)
    return ids


def _fiche(db, artifact_id: str, resume: str = "") -> None:
    """Fiche minimale + ligne FTS, comme le fait l'ingestion après promotion.

    L'ingestion est neutralisée ici : sans elle, le fichier du dépôt n'aurait
    aucune fiche, donc aucune ligne FTS, donc rien à trouver — on testerait
    l'absence d'analyse, pas la portée.
    """
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
    card = Cards.ensure_card_on_import(db, artifact)
    if resume:
        card.summary = resume
        db.flush()
        liens = (db.query(ArtifactLink)
                 .filter(ArtifactLink.artifact_id == artifact_id,
                         ArtifactLink.removed_at.is_(None))
                 .all())
        for lien in liens:
            Fts.index_card_row(db, artifact, card, lien.scope_kind, lien.scope_id, "")
    db.commit()


def _contenu_lu(db, artifact_id: str, texte: str) -> None:
    """Observation OCR + fragment PERSISTÉ : ce que `ask_files` sait lire et prouver."""
    db.add(Observation(id=f"obs_{artifact_id}", artifact_id=artifact_id, tool_name="doc_ocr",
                       status="completed", payload_ref=texte))
    db.add(ArtifactChunk(id=f"chk_{artifact_id}", artifact_id=artifact_id, card_revision=1,
                         ordinal=0, text=texte, context=""))
    db.commit()


def _appeler(outil, arguments: dict, conv_id: str):
    ctx = ToolContext(run_id="r1", request_id=conv_id, conv_id=conv_id)
    return asyncio.run(outil.call(arguments, ctx))


def _charge(resultat) -> dict:
    return json.loads(resultat.content)


# ---------- 1. find_files mode liste : le fichier du dépôt est nommé ----------

def test_find_files_en_liste_nomme_le_fichier_du_depot(base_temporaire):
    """La moitié espace du contrat : un fichier versé au dépôt devient un `doc_N`.

    Sans ce `doc_1`, l'orchestrateur ne peut rien demander de ce fichier : il n'a
    que des identifiants à recopier, jamais à inventer.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = _espace(db, "Veille")
        conversation = _conversation(db, "conv_veille", espace)
        (versé,) = _depot(db, espace, ["Note_de_reunion.pdf"], ["note-veille"])
    finally:
        db.close()

    resultat = _appeler(FindFilesTool(), {"types": ["pdf"]}, conversation)
    charge = _charge(resultat)

    assert resultat.ok and charge["ok"] is True
    assert charge["mode"] == "list"
    assert [r["name"] for r in charge["results"]] == ["Note_de_reunion.pdf"]
    assert charge["results"][0]["public_id"] == "doc_1"
    assert charge["results"][0]["origin"] == "workspace"
    # L'origine est DITE, pas seulement codée : un fichier du dépôt n'est pas
    # « joint à ce message », et le modèle doit pouvoir le lire.
    assert "dépôt de l'espace" in resultat.display_summary
    assert "cette discussion" not in resultat.display_summary

    db = sessions()
    try:
        lignes = db.query(DocRef).filter(DocRef.conv_id == conversation).all()
        assert [(r.doc_label, r.artifact_id) for r in lignes] == [("doc_1", versé)]
    finally:
        db.close()


# ---------- 2. find_files mode recherche : la portée FTS corrigée ----------

def test_find_files_par_mot_trouve_le_fichier_du_depot(base_temporaire):
    """Le fichier du dépôt n'existe que sous la portée `workspace` de l'index.

    C'est la preuve du premier défaut : interroger la seule portée
    `conversation` ne le rend pas CANDIDAT, si grand que soit le dépôt. Le test
    échouerait si la fusion des deux portées disparaissait.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = _espace(db, "Finance")
        conversation = _conversation(db, "conv_finance", espace)
        (versé,) = _depot(db, espace, ["Budget_T3.pdf"], ["budget-finance"])
        _fiche(db, versé, resume="Le budget du trimestre s'élève à 12 000 euros.")

        # Fait brut : une seule ligne FTS, et elle est portée `workspace`.
        lignes = db.execute(sql_text(
            "SELECT scope_kind, scope_id FROM artifact_search WHERE artifact_id = :a"
        ), {"a": versé}).all()
        assert [tuple(l) for l in lignes] == [("workspace", espace)]

        # Ce que l'appelait d'avant : rien. Le dépôt n'est pas une portée conversation.
        page = Fts.search(db, query="budget", scope_kind="conversation",
                          scope_id=conversation, allowed_ids={versé})
        assert page["ok"] and page["results"] == []
    finally:
        db.close()

    resultat = _appeler(FindFilesTool(), {"query": "budget"}, conversation)
    charge = _charge(resultat)

    assert resultat.ok and charge["ok"] is True
    assert charge["mode"] == "semantic"
    assert [r["name"] for r in charge["results"]] == ["Budget_T3.pdf"]
    assert charge["results"][0]["public_id"] == "doc_1"
    assert charge["results"][0]["origin"] == "workspace"


def test_search_info_trouve_le_fichier_du_depot(base_temporaire):
    """`search_info` rend l'INFORMATION d'un fichier du dépôt, pas son contenu brut.

    Même portée, même défaut : l'outil interrogeait une seule portée et perdait la
    moitié espace du contrat.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = _espace(db, "Veille")
        conversation = _conversation(db, "conv_veille", espace)
        (versé,) = _depot(db, espace, ["Budget_T3.pdf"], ["budget-recherche"])
        _fiche(db, versé, resume="Le budget du trimestre s'élève à 12 000 euros.")
    finally:
        db.close()

    resultat = _appeler(SearchInfoTool(), {"queries": ["budget du trimestre"]}, conversation)
    charge = _charge(resultat)

    assert resultat.ok and charge["ok"] is True
    (entree,) = charge["by_query"]
    assert [r["name"] for r in entree["results"]] == ["Budget_T3.pdf"]
    assert entree["results"][0]["public_id"] == "doc_1"
    assert entree["results"][0]["origin"] == "workspace"
    assert "dépôt de l'espace" in resultat.display_summary


# ---------- 3. SÉCURITÉ : jamais le dépôt d'un autre espace ----------

def test_le_depot_d_un_autre_espace_reste_invisible(base_temporaire):
    """Deux espaces, un mot commun aux deux dépôts : seul le sien est rendu.

    Les deux fichiers portent un nom contenant le même mot, pour que la seule
    différence qui les sépare soit l'espace. Une portée élargie par erreur —
    « interrogeons tous les workspaces » — se verrait ici immédiatement.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace_a = _espace(db, "Alpha")
        conversation_a = _conversation(db, "conv_alpha", espace_a)
        (alpha,) = _depot(db, espace_a, ["Alpha_rapt.pdf"], ["alpha-rapt"],
                          request_id="depot-alpha")

        espace_b = _espace(db, "Beta")
        conversation_b = _conversation(db, "conv_beta", espace_b)
        (beta,) = _depot(db, espace_b, ["Beta_rapt.pdf"], ["beta-rapt"],
                         request_id="depot-beta")
        _fiche(db, beta, resume="Le rapport confidentiel de Beta.")
    finally:
        db.close()

    liste = _appeler(FindFilesTool(), {"types": ["pdf"]}, conversation_a)
    assert [r["name"] for r in _charge(liste)["results"]] == ["Alpha_rapt.pdf"]
    assert "Beta" not in liste.display_summary

    recherche = _appeler(FindFilesTool(), {"query": "rapt"}, conversation_a)
    charge = _charge(recherche)
    assert [r["name"] for r in charge["results"]] == ["Alpha_rapt.pdf"]
    assert beta not in json.dumps(charge)
    assert "Beta" not in recherche.display_summary

    info = _appeler(SearchInfoTool(), {"queries": ["rapt"]}, conversation_a)
    assert beta not in info.content
    assert "Beta" not in info.display_summary

    # Et le symétrique : Beta ne voit pas Alpha. La fuite n'a pas de sens unique.
    retour = _appeler(FindFilesTool(), {"query": "rapt"}, conversation_b)
    assert [r["name"] for r in _charge(retour)["results"]] == ["Beta_rapt.pdf"]
    assert alpha not in json.dumps(_charge(retour))


# ---------- 4. ask_files : la chaîne complète passe ----------

def test_ask_files_lit_un_fichier_du_depot_avec_son_doc_N(base_temporaire):
    """Le `doc_N` du dépôt n'est pas un id décoratif : il se lit, et se prouve.

    Toute la chaîne est vérifiée : `find_files` nomme → `docrefs` résout sans
    lien de conversation → le périmètre accepte → le contenu répond → la preuve
    pointe un fragment RÉELLEMENT persisté.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = _espace(db, "Compta")
        conversation = _conversation(db, "conv_compta", espace)
        (versé,) = _depot(db, espace, ["Budget_T3.pdf"], ["budget-lecture"])
        _contenu_lu(db, versé, "Le budget du T2 s'élève à 12 000 euros.")
    finally:
        db.close()

    trouve = _charge(_appeler(FindFilesTool(), {"query": "budget"}, conversation))
    doc_id = trouve["results"][0]["public_id"]

    reponse = _appeler(AskFilesTool(), {"questions": ["Quel est le budget du T2 ?"],
                                        "resource_ids": [doc_id]}, conversation)

    assert reponse.ok and _charge(reponse)["ok"] is True
    (groupe,) = _charge(reponse)["answers"]
    (reponse_fichier,) = groupe["by_file"]
    assert reponse_fichier["artifact_id"] == doc_id
    assert "12 000" in reponse_fichier["answer"]
    # Une preuve sans fragment derrière elle ne vaut rien : celle-ci en a un.
    assert reponse_fichier["evidence_id"] == f"{doc_id}:0"


# ---------- 5. Un chat simple hors espace ne voit rien ----------

def test_un_chat_simple_ne_voit_aucun_depot(base_temporaire):
    """Ni le sien — il n'en a pas — ni celui d'un espace qu'il ne rejoint pas."""
    sessions = base_temporaire
    db = sessions()
    try:
        # Un dépôt existe, dans un espace auquel ce chat n'appartient pas.
        _depot(db, _espace(db, "Reserve"), ["Confidentiel.pdf"], ["confidentiel"],
               request_id="depot-reserve")
        solo = _conversation(db, "conv_solo", None)
    finally:
        db.close()

    liste = _appeler(FindFilesTool(), {"types": ["pdf"]}, solo)
    charge = _charge(liste)
    assert charge["registry_size"] == 0 and charge["results"] == []
    assert "aucun fichier n'est enregistré" in liste.display_summary

    recherche = _appeler(FindFilesTool(), {"query": "confidentiel"}, solo)
    assert _charge(recherche)["results"] == []
    assert "Confidentiel" not in recherche.display_summary

    info = _appeler(SearchInfoTool(), {"queries": ["confidentiel"]}, solo)
    assert _charge(info)["by_query"] == []
    assert "Confidentiel" not in info.display_summary

    db = sessions()
    try:
        # Rien n'a été nommé pour une discussion qui n'a rien à nommer.
        assert db.query(DocRef).filter(DocRef.conv_id == "conv_solo").count() == 0
    finally:
        db.close()


# ---------- 6. Le contexte de l'orchestrateur ne grossit pas ----------

def test_un_depot_de_cinquante_fichiers_n_alloue_pas_cinquante_labels(base_temporaire):
    """Chercher UN fichier ne coûte pas un numéro par fichier du dépôt.

    Le contexte de l'orchestrateur est la ressource rare. Si l'appel nombait le
    dépôt entier, les conversations d'un espace bien garni deviendraient
    impossibles à tenir dans une fenêtre — et le `doc_N` deviendrait une preuve
    d'existence du dépôt plutôt qu'une adresse.

    Le mécanisme de troncature est celui qui existe déjà (`render_limit`) : on ne
    lui en ajoute pas un second.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = _espace(db, "Archives")
        conversation = _conversation(db, "conv_archives", espace)
        noms = [f"piece{index:02d}.pdf" for index in range(1, 51)]
        _depot(db, espace, noms, [f"piece-{index}" for index in range(1, 51)],
               request_id="depot-archives")
    finally:
        db.close()

    def _labels() -> list[str]:
        db = sessions()
        try:
            return [r.doc_label for r in
                    db.query(DocRef).filter(DocRef.conv_id == conversation)
                    .order_by(DocRef.doc_index.asc()).all()]
        finally:
            db.close()

    assert _labels() == []  # verser ne nomme rien

    precis = _appeler(FindFilesTool(), {"query": "piece07"}, conversation)
    charge = _charge(precis)
    assert [r["name"] for r in charge["results"]] == ["piece07.pdf"]
    assert len(_labels()) == 1  # un fichier cherché, un numéro

    # Le mode liste rend tout de même la page bornée par le mécanisme existant,
    # et ne nomme que les lignes qu'il affiche : 1 (la recherche précise) + 5.
    liste = _appeler(FindFilesTool(), {"types": ["pdf"]}, conversation)
    assert len(_charge(liste)["results"]) == int(TOOL_LIMITS["render_limit"])
    assert len(_labels()) == 1 + int(TOOL_LIMITS["render_limit"])

    # Relire ne coûte rien de plus : les numéros déjà pris sont réutilisés.
    _appeler(FindFilesTool(), {"query": "piece07"}, conversation)
    _appeler(FindFilesTool(), {"types": ["pdf"]}, conversation)
    assert len(_labels()) == 1 + int(TOOL_LIMITS["render_limit"])


# ---------- 7. Un fichier du dépôt ne reçoit aucun lien de conversation ----------

def test_le_fichier_du_depot_ne_recoit_aucun_lien_de_conversation(base_temporaire):
    """L'emploi d'un `doc_N` ne vaut pas rattachement.

    Un lien `conversation` ferait du fichier une pièce jointe de cette
    discussion : il disparaîtrait avec elle. Le dépôt, lui, doit survivre à ses
    discussions. C'est vérifié APRÈS usage — nommer et lire ne doivent rien
    laisser derrière eux.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = _espace(db, "Partage")
        conversation = _conversation(db, "conv_partage", espace)
        (versé,) = _depot(db, espace, ["Contrat_cadre.pdf"], ["contrat-partage"])
        _contenu_lu(db, versé, "Le contrat-cadre court jusqu'en décembre.")
    finally:
        db.close()

    trouve = _charge(_appeler(FindFilesTool(), {"query": "contrat"}, conversation))
    doc_id = trouve["results"][0]["public_id"]
    _appeler(AskFilesTool(), {"questions": ["Jusqu'à quand court le contrat ?"],
                              "resource_ids": [doc_id]}, conversation)

    db = sessions()
    try:
        liens = db.query(ArtifactLink).filter(ArtifactLink.artifact_id == versé).all()
        assert [(l.scope_kind, l.scope_id) for l in liens] == [("workspace", espace)]
        assert all(l.removed_at is None for l in liens)
        # Le DocRef existe — c'est un EMPLOI — mais il ne crée aucun lien.
        assert [r.doc_label for r in
                db.query(DocRef).filter(DocRef.artifact_id == versé).all()] == [doc_id]
    finally:
        db.close()
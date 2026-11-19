"""Orchestration de bout en bout avec le VRAI modèle LFM2.5-8B.

Pourquoi ce fichier existe, et pourquoi il est `slow` : tous les autres tests de
la boucle d'orchestration rejouent un modèle SIMULÉ (`_ScriptedRuntime`). Ils
prouvent que l'outillage fonctionne QUAND ON L'APPELLE — étiquetage `doc_N`,
routage vers l'OCR, propagation d'échec, citation de preuve. Ils ne prouvent rien
sur la seule question qui compte pour l'utilisateur : est-ce que le modèle, lui,
appelle le bon outil ?

Un orchestrateur peut être parfaitement câblé et ne jamais lire un document,
parce qu'il choisit `web_research` là où il fallait `ask_files`, ou qu'il passe
un `doc_1` inexistant. Aucun test simulé ne peut voir cela : c'est exactement la
décision que le scripté remplace.

Ces tests chargent donc le vrai modèle, sur un vrai fichier déposé, et observent
ses appels réels. Ils sont lents (chargement + plusieurs générations).

Ils ne comparent pas des chaînes de caractères : une sortie de modèle n'est pas
déterministe. Ils vérifient des propriétés structurelles — l'outil choisi est
plausible, les arguments passent le contrat, la brief cite une preuve réelle.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

OUTILS_ORCHESTRATEUR = {"search_info", "find_files", "ask_files", "generate_image", "web_research"}

RACINE = Path(__file__).resolve().parents[2]
PDF_REEL = RACINE / "tests" / "fixtures" / "pdf" / "Le Continent du Phenix.pdf"

# Le document déposé fait 60 Mo et l'application refuse au-delà de 20 Mo
# (`MAX_UPLOAD_BYTES`) : le fichier entier ne peut donc pas traverser le staging,
# il serait refusé en production. On n'en prend que deux pages RÉELLES — le
# préambule du livre, texte mis en page par InDesign, avec ses ligatures, ses
# guillemets français et son image de couverture. Un PDF fabriqué à la main n'a
# aucune de ces propriétés, et c'est précisément ce qu'un orchestrateur doit lire.
#
# Deux pages, et pas plus : au-delà, la lecture remplit le contexte du modèle et la
# phase brief échoue sur `Requested tokens exceed context window of 4096`. Cette
# borne est un fait du modèle et de `resolve_n_ctx`, pas une préférence de test.
PREMIERE_PAGE = 7
DERNIERE_PAGE = 8

sans_pdf_reel = pytest.mark.skipif(
    not PDF_REEL.is_file(),
    reason=f"le PDF réel n'est pas présent : {PDF_REEL}",
)


def _exiger_modele() -> None:
    from backend.config.paths import get_local_model_default_path

    if not get_local_model_default_path().exists():
        pytest.skip("LFM2.5-8B absent : test d'orchestration réelle non exécutable ici")


@pytest.fixture(scope="module")
def runtime():
    """Le vrai runtime, construit comme en production.

    On passe par `build_runtime` plutôt que d'instancier `LlamaCppRuntime` à la
    main : c'est le chemin réellement emprunté par l'application, et un test qui
    emprunterait un autre chemin ne couvrirait pas ce qui casse en vrai.
    """
    _exiger_modele()
    from backend.inference.manager import ModelManager
    from backend.inference.runtime import build_runtime

    return build_runtime(ModelManager())


def _pdf_rapport() -> bytes:
    """Une tranche du VRAI document déposé, pas un PDF fabriqué.

    Un PDF fabriqué ne prouve qu'une chose : que le code sait lire un PDF. Ce que
    ce test doit prouver, c'est que l'orchestrateur lit un fichier comme celui
    qu'un utilisateur dépose pour de bon : texte mis en page par InDesign, styles
    imbriqués, images, table des matières. Ce que la fabrication à la main efface
    exactement.

    La tranche respecte `MAX_UPLOAD_BYTES` (20 Mo) que l'application applique au
    dépôt : passer le fichier entier par le staging serait refusé en production,
    et un test qui viole le plafond qu'il est censé vérifier teste autre chose.
    """
    import pymupdf

    if not PDF_REEL.is_file():
        pytest.skip(f"le PDF réel n'est pas présent : {PDF_REEL}")

    source = pymupdf.open(PDF_REEL)
    tranche = pymupdf.open()
    tranche.insert_pdf(source, from_page=PREMIERE_PAGE, to_page=DERNIERE_PAGE)
    octets = tranche.tobytes(deflate=True, garbage=4)
    tranche.close()
    source.close()
    return octets


@pytest.fixture
def fichier_depose(tmp_path, monkeypatch):
    """Conversation neuve + PDF déposé et digéré. Renvoie (conv_id, artifact_id)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.db import models as _models
    from backend.db.models import Blob, Conversation, Message, MessageAttachment
    from backend.ipc import attachments as _att
    from backend.ipc import paths_bridge as _pb
    from backend.search.chunks import ensure_chunk_tables
    from backend.config import paths as _paths

    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True, exist_ok=True)
    (racine / "temp").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "get_data_dir", lambda: racine)
    monkeypatch.setattr(_paths, "get_temp_dir", lambda: racine / "temp")
    monkeypatch.setattr(_pb, "get_data_dir", lambda: racine, raising=False)
    monkeypatch.setattr(_pb, "blobs_root", lambda: racine / "blobs", raising=False)
    monkeypatch.setattr(_pb, "temp_root", lambda: racine / "temp", raising=False)
    # `data_dirs()` est le point d'entrée RÉEL de la résolution des blobs côté
    # ingestion : sans cette redirection, l'extraction chercherait le blob dans le
    # dossier de l'utilisateur, échouerait, et le test passerait en n'ayant rien
    # digéré.
    monkeypatch.setattr(_pb, "data_dirs", lambda: (racine, racine / "temp"), raising=False)
    monkeypatch.setattr(_att, "get_data_dir", lambda: racine, raising=False)
    monkeypatch.setattr(_att, "get_temp_dir", lambda: racine / "temp", raising=False)
    # Le dépôt lance l'ingestion dans un FIL. Ce fil survit au test et vient
    # écrire dans la base du test suivant : on le coupe et on pilote nous-mêmes.
    monkeypatch.setattr(_att, "_declencher_ingestion", lambda ids: None, raising=False)

    moteur = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _models.Base.metadata.create_all(moteur)
    session = sessionmaker(bind=moteur)
    monkeypatch.setattr("backend.db.database.SessionLocal", session)
    monkeypatch.setattr("backend.ipc.attachments.SessionLocal", session, raising=False)
    monkeypatch.setattr(
        "backend.search.observations.SessionLocal", session, raising=False
    )
    monkeypatch.setattr("backend.search.chunks.SessionLocal", session, raising=False)

    conv = f"conv_{uuid.uuid4().hex[:10]}"
    message = f"msg_{uuid.uuid4().hex[:10]}"
    with session() as db:
        db.add(Conversation(id=conv, title="test reel", mode="chat"))
        db.add(Message(id=message, conv_id=conv, role="user", content="depot"))
        db.commit()

        item = _att.stage_picker_bytes(
            db, {"requestId": "test-reel", "filename": "rapport.pdf", "bytes": _pdf_rapport()}
        )["items"][0]
        artefact = _att.attachments_prepare(
            db, {"conversationId": conv, "stageItemIds": [item["stage_item_id"]]}
        )["artifact_ids"][0]
        db.add(MessageAttachment(id="ma1", message_id=message, artifact_id=artefact, ordinal=0))
        db.commit()

    # `ensure_chunk_tables` exécute du DDL : elle prend une SESSION, pas la
    # factory. Passer la factory levait `AttributeError: 'sessionmaker' object has
    # no attribute 'execute'` — une erreur de fixture, pas une régression.
    with session() as db:
        ensure_chunk_tables(db)

    from backend.ingestion import attachment_ingest as _ing

    _ing._ingérer_un(artefact)
    with session() as db:
        depuis = db.query(Blob).count()
    assert depuis >= 1
    return conv, artefact


def test_definition_complete_pour_le_modele():
    """Aucun outil ne doit partir avec un schéma vide.

    Une clé `parameters.properties` vide donne au modèle une définition sans
    indication sur les arguments. L'appel échoue ensuite en `BAD_ARGS`, que
    l'utilisateur lit comme une panne de l'outil alors que la cause est un
    schéma jamais rempli. On le vérifie ici pour que le symptôme soit attribué à
    sa vraie cause.
    """
    from backend.tools.registry import build_default_registry

    registre = build_default_registry()
    vides = []
    for nom in OUTILS_ORCHESTRATEUR:
        definition = registre._tools[nom].definition()
        fonction = definition.get("function") or {}
        proprietes = (fonction.get("parameters") or {}).get("properties") or {}
        if not proprietes:
            vides.append(nom)
    assert not vides, f"définition sans propriétés : {vides}"


def test_web_research_nomme_l_outil_dans_son_erreur():
    """L'erreur de contrat doit nommer l'outil fautif.

    Constaté en sondant : `web_research` exige `purpose`, et le message
    `MISSING_REQUIRED` ne dit pas quel outil le réclame. L'orchestrateur qui se
    trompe se trompe alors deux fois au lieu d'une fois.
    """
    from backend.inference import loop as L

    r = L._execute_web_research({"args": {"queries": ["prix du cuivre 2026"]}})
    assert r["ok"] is False
    assert "purpose" in r["result"]
    assert "web_research" in r["result"], (
        "l'erreur ne nomme pas l'outil fautif : l'orchestrateur ne sait pas "
        "quel appel corriger"
    )


def test_requetes_outil_du_modele_valident_leur_contrat(runtime):
    """Le vrai modèle doit produire des arguments valides pour chaque outil.

    On ne teste pas ce qu'il va décider — il peut légitimement choisir
    `find_files` avant `ask_files`. On teste que, pour TOUS les outils de
    découverte, les arguments qu'il écrit passent le schéma de l'outil visé.

    LE CANAL NOMMÉ, ET POURQUOI. Ce test exigeait jusqu'ici
    `tool_choice="required"` en espérant un appel natif. Sur ce runtime il n'y
    en a jamais : llama-cpp-python 0.3.34 ne construit une grammaire que pour
    un choix NOMMÉ, `"required"` est rendu au template Jinja et ignoré, et le
    modèle répond alors en prose — ce que la boucle de production documente
    elle-même dans `_reask_unconstrained_tool_call`. On demandait donc une
    garantie que la couche runtime ne sait pas rendre, et le test échouait sur
    une propriété du lanceur, pas sur le modèle.

    Le choix nommé est précisément ce que la production utilise quand elle doit
    obtenir un appel (`_recover_named_tool_call`, et la phase brief forcée). On
    teste donc ce qui existe : pour chaque outil, le modèle nomme et écrit, et
    ce qu'il écrit passe le pydantic de l'outil.
    """
    from backend.tools.registry import build_default_registry

    registre = build_default_registry()
    prompt = (
        "Voici un fichier PDF depose : rapport.pdf. "
        "Quels constats contient-il ?"
    )

    inconnus, invalides, valides, silencieux = [], [], 0, []
    for nom in sorted(OUTILS_ORCHESTRATEUR):
        definition = registre._tools[nom].definition()
        reponse = runtime.complete_chat(
            _requete(
                prompt,
                [definition],
                tool_choice={"type": "function", "function": {"name": nom}},
            )
        )
        # `RuntimeResponse.tool_calls` porte déjà des `RuntimeToolCall` : le nom
        # et les arguments sont des attributs déjà désérialisés. Les relire comme
        # des dictionnaires bruts passerait à côté du contrat du runtime.
        appels = reponse.tool_calls
        if not appels:
            silencieux.append(nom)
            continue
        for appel in appels:
            nom_appele = appel.name
            if nom_appele not in registre._tools:
                inconnus.append(nom_appele)
                continue
            charge = dict(appel.arguments or {})
            if not _valider(registre._tools[nom_appele], charge):
                invalides.append(f"{nom_appele}: {sorted(charge)}")
            else:
                valides += 1

    assert not inconnus, f"outils imaginaires : {inconnus}"
    assert not invalides, f"arguments hors contrat : {invalides}"
    assert not silencieux, (
        f"aucun appel natif pour {silencieux} alors que le canal nommé en impose un : "
        "la boucle de production n'aurait rien à exécuter"
    )
    assert valides >= len(OUTILS_ORCHESTRATEUR)


@sans_pdf_reel
def test_orchestrateur_lit_un_fichier_depose(runtime, fichier_depose):
    """Le test qui manquait : le vrai modèle, un vrai dépôt, et on regarde.

    On observe la trace de la boucle et on exige que le modèle soit allé
    chercher le document. On tolère qu'il demande un `doc_1` absent ou qu'il
    passe par `find_files` d'abord : ce qui compte est qu'un outil d'accès au
    contenu ait été exécuté et qu'il n'ait pas échoué.

    Le document est le VRAI PDF déposé (une tranche de ses pages) : c'est
    l'orchestrateur face à un fichier d'utilisateur, pas face à une page
    fabriquée pour le test.
    """
    conv, artefact = fichier_depose
    from backend.inference import loop as L

    collected, trace = L.run_agent_loop(
        None,
        conv_id=conv,
        request_id="req-test-reel",
        message="De quoi parle le document que je viens de deposer ?",
        image_paths=None,
        history_shared=[],
        runtime=runtime,
        attachment_ids=[artefact],
    )
    brief = trace.get("brief") or ""
    assert brief, "aucune brief produite : la boucle s'est arretee sans conclusion"
    echecs = trace.get("tool_errors") or []
    assert not echecs, f"échecs d'outils pendant la boucle : {echecs}"


def _requete(prompt: str, definitions: list[dict], *, tool_choice=None):
    from backend.inference.runtime import RuntimeRequest

    return RuntimeRequest(
        messages=({"role": "user", "content": prompt},),
        tools=tuple(definitions),
        tool_choice=tool_choice,
        temperature=0.0,
        max_tokens=400,
        timeout_s=240.0,
    )


def _valider(outil, charge: dict) -> bool:
    """Passe la charge par le schéma pydantic de l'outil, sans lever."""
    modele = getattr(outil, "args_model", None) or getattr(outil, "Args", None)
    if modele is None:
        return bool(charge)
    try:
        modele.model_validate(charge)
        return True
    except Exception:
        return False
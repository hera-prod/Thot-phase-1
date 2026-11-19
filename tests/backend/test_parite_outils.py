"""La parité entre `Tool.call()` et le chemin VIVANT de la boucle.

Deux implémentations coexistaient pour `search_info`, `find_files` et `ask_files` : la
classe `Tool` (`backend/tools/`), riche, et les exécuteurs `loop._execute_*`, dégradés.
Ce module existe pour que la question « le chemin vivant a-t-il perdu quelque chose ? »
ne se réponde plus à l'intuition mais par une comparaison.

OÙ EN EST LA MIGRATION : les trois exécuteurs sont désormais des ADAPTATEURS — une
ligne chacun, `_resultat_pour_la_boucle(outil, ...)` — donc il n'y a plus deux
implémentations à comparer. La parité reste mesurée ici pour une raison qui n'a pas
disparu : le pont recopie `evidence_ids` et `public_ids` du `ToolResult`, et une
régression de copie ne se verrait dans aucun test qui ne comparerait que le texte.

CE QUE LA PARITÉ COMPARE : ce que le MODÈLE et la BOUCLE voient, jamais l'identité
d'objet. Un `ToolResult` de `call()` et le `dict` de tour de l'adaptateur sont deux
représentations différentes de la même exécution ; les comparer champ par champ
(comme `content`) ne prouverait rien. Ce qui doit être identique, c'est :
`ok`, le texte rendu, le message d'erreur et son CODE, la troncature, et les
identifiants (`evidence_ids`, `public_ids`).

CE QUE LE PREMIER TEST GARDE : la régression centrale de la Bible §2.5 (l. 143).
`search_info` doit chercher « dans un périmètre qui va au-delà de la seule
discussion — fichiers joints, dépôt de l'espace, mémoire, résultats web déjà
ingérés ». L'ancien chemin imposait `scope="conversation"`, donc un fichier versé
au dépôt de l'espace n'était pas CANDIDAT à la recherche, quelle que soit la
taille du dépôt. Le test prepare un artefact de dépôt ET un artefact de discussion,
puis montre que le chemin migré voit les deux et que le mécanisme d'avant n'en
voyait qu'un.

Montage : base temporaire, `SessionLocal` du projet redirigée, racines de données
jetables, ingestion neutralisée. Les fichiers entrent par le VRAI chemin de dépôt
(`stage` puis `attachments_prepare`), parce qu'un artefact fabriqué à la main ne
prouverait rien du contrat de dépôt. Aucun octet ni aucune ligne du projet n'est
touché.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Conversation,
    Observation,
)
from backend.ingestion import attachment_ingest
from backend.inference import loop as L
from backend.ipc import attachments as A
from backend.search import cards as Cards
from backend.search import fts as Fts
from backend.search import scope as Scope
from backend.services import espaces as service
from backend.tools.ask_files import AskFilesTool
from backend.tools.base import BaseTool, ToolContext, ToolResult, ToolStatus
from backend.tools.search_info import SearchInfoTool

# Un appel de dépôt accepte huit pièces : au-delà, il faut lots.
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
    engine = create_engine(f"sqlite:///{tmp_path / 'parite-outils.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)  # sinon la fiche n'a nulle part où s'indexer
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda artifact_ids: None
    )
    return sessions


# ---------- Montage : espace + dépôt + discussion, par le vrai chemin ----------


def _conversation(db, conv_id: str, workspace_id: str | None) -> str:
    db.add(
        Conversation(
            id=conv_id, title="Discussion", mode="chat", workspace_id=workspace_id
        )
    )
    db.commit()
    return conv_id


def _verser(db, dest: dict, noms: list[str], request_id: str) -> list[str]:
    """Promeut des fichiers stagés vers une discussion OU vers le dépôt d'un espace."""
    ids: list[str] = []
    for debut in range(0, len(noms), _LOTS):
        tranche = noms[debut : debut + _LOTS]
        jetons = [
            A.stage_picker_bytes(
                db,
                {
                    "request_id": request_id,
                    "filename": nom,
                    "bytes": pdf_bytes(f"{nom}-jeton"),
                },
            )["items"][0]["stage_item_id"]
            for nom in tranche
        ]
        reponse = A.attachments_prepare(
            db, {**dest, "stageItemIds": jetons, "names": tranche}
        )
        ids.extend(reponse["artifact_ids"])
    return ids


def _fiche(db, artifact_id: str, resume: str) -> None:
    """Fiche + ligne FTS sous CHAQUE lien actif, comme le fait l'ingestion.

    Le contenu va dans `card.summary` : c'est là que `fts.search` le range, dans la
    fiche IMBRIQUÉE (`{"card": {"summary": …}}`). C'est cette forme que l'ancien
    chemin lisait à plat — et qui lui rendait donc un extrait vide.
    """
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
    card = Cards.ensure_card_on_import(db, artifact)
    card.summary = resume
    db.flush()
    liens = (
        db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == artifact_id, ArtifactLink.removed_at.is_(None)
        )
        .all()
    )
    for lien in liens:
        Fts.index_card_row(db, artifact, card, lien.scope_kind, lien.scope_id, "")
    db.commit()


def _fragment_lu(db, artifact_id: str, texte: str) -> None:
    """Fragment PERSISTÉ + observation : ce que `ask_files` sait relire et prouver."""
    db.add(
        Observation(
            id=f"obs_{artifact_id}",
            artifact_id=artifact_id,
            tool_name="doc_ocr",
            status="completed",
            payload_ref=texte,
        )
    )
    db.add(
        ArtifactChunk(
            id=f"chk_{artifact_id}",
            artifact_id=artifact_id,
            card_revision=1,
            ordinal=0,
            text=texte,
            context="",
        )
    )
    db.commit()


def _depot_et_discussion(
    sessions, resume: str = "Le budget du trimestre s'élève à 12 000 euros."
):
    """Un chat d'espace, UN fichier versé au dépôt, UN fichier joint à la discussion.

    Le fichier du dépôt n'a qu'une FICHE indexée ; le fichier joint a aussi un
    FRAGMENT PERSISTÉ. C'est volontaire : la recherche se nourrit de la fiche,
    `ask_files` ne peut répondre que d'un fragment — et les deux fichiers doivent
    donc être visibles par la recherche tout en n'étant pas lisibles de la même
    façon. Un montage où les deux seraient interchangeables ne prouverait rien.
    """
    db = sessions()
    try:
        espace = str(service.create_workspace(db, "Parité").id)
        conversation = _conversation(db, "conv_parite", espace)
        (versé,) = _verser(
            db, {"workspaceId": espace}, ["Note_depot.pdf"], "parite-depot"
        )
        _fiche(db, versé, resume)
        (joint,) = _verser(
            db, {"conversationId": conversation}, ["Piece_jointe.pdf"], "parite-conv"
        )
        _fiche(db, joint, resume)
        _fragment_lu(db, joint, resume)
        return conversation, versé, joint
    finally:
        db.close()


# ---------- La comparaison elle-même ----------


def _via_call(outil, arguments: dict, conv_id: str):
    """L'exécution de référence : la porte RICHE, telle que l'appellent les tests d'outils."""
    ctx = ToolContext(run_id="r1", request_id=conv_id, conv_id=conv_id)
    return asyncio.run(outil.call(dict(arguments), ctx))


def _ce_que_voit_la_boucle(resultat) -> tuple[bool, str]:
    """La projection que la boucle applique à un `ToolResult` : (ok, texte). Pur."""
    if not resultat.ok:
        return False, resultat.error or resultat.display_summary or ""
    return True, resultat.display_summary or resultat.content


def _adaptateur_de(outil):
    """Le même pont que `_execute_search_info`, pour un outil quelconque.

    `_execute_search_info` N'EST PAS ce pont : c'est un adaptateur qui construit son
    outil. Ici on veut mesurer le pont lui-même — `_resultat_pour_la_boucle` — sur
    `AskFilesTool`, que `_execute_ask_files` appelle désormais directement (voir le
    bloc de commentaire devant cet exécuteur). Le pont reste mesuré à part : c'est
    lui qui recopie `evidence_ids` et `public_ids`, et une régression de copie ne se
    verrait pas dans un test qui ne comparerait que le texte rendu.
    """

    def _pont(tool_call: dict, conv_id: str | None = None) -> dict:
        return L._resultat_pour_la_boucle(
            outil, tool_call, conv_id, time.perf_counter()
        )

    return _pont


def _comparer(outil, adaptateur, arguments: dict, conv_id: str) -> tuple:
    """Le même appel par les deux portes, ramené à ce qui doit être IDENTIQUE.

    `latency_ms` et `duration_ms` sont exclus : ils mesurent deux choses différentes
    (l'appel de tour, pont sync compris, contre le seul `execute`) et ne peuvent pas
    être égaux. Tout le reste doit l'être.
    """
    reference = _via_call(outil, arguments, conv_id)
    rendu = adaptateur({"args": dict(arguments)}, conv_id=conv_id)
    return reference, rendu


def _code(message: str) -> str:
    """Le code `[CODE]` en tête d'un message d'erreur — ce que la boucle relit. Pur."""
    message = str(message or "")
    if not message.startswith("["):
        return ""
    return message[1 : message.index("]")] if "]" in message else ""


# ---------- 1. PARITÉ : search_info ----------


def test_parite_search_info_appel_valide(base_temporaire):
    """Une recherche qui aboutit : rendu et identifiants identiques.

    On compare le TEXTE RENDU (`display_summary`), pas le JSON : c'est le rendu que
    le modèle lit et que la boucle range dans `tool_results`. Le JSON, lui, est un
    canal interne — et il est tronqué, donc pas comparable octet à octet.
    """
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()
    arguments = {"queries": ["budget du trimestre"], "types": ["pdf"]}

    reference, rendu = _comparer(outil, L._execute_search_info, arguments, conversation)

    assert reference.ok and rendu["ok"] is True
    assert _ce_que_voit_la_boucle(reference) == (True, rendu["result"]), (
        "le chemin migré ne rend pas ce que rend `call()`"
    )
    assert rendu["evidence_ids"] == reference.evidence_ids
    assert rendu["public_ids"] == reference.public_ids
    assert reference.public_ids, "la recherche doit nommer au moins un fichier"
    # Le contenu utile, pas seulement l'égalité de forme : la fiche est lue dans
    # sa forme IMBRIQUÉE, donc l'extrait est PRÉSENT.
    assert "budget du trimestre" in rendu["result"].lower()
    assert rendu["evidence_ids"], "un extrait cité sans preuve n'est pas une réponse"


def test_parite_search_info_appel_vide(base_temporaire):
    """Appel vide : `EMPTY_CALL` des deux côtés, jamais un `BAD_ARGS` générique."""
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()

    reference, rendu = _comparer(outil, L._execute_search_info, {}, conversation)

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "EMPTY_CALL"
    assert rendu["result"] == (reference.error or reference.display_summary)
    assert _code(rendu["result"]) == "EMPTY_CALL"


def test_parite_search_info_requete_vide(base_temporaire):
    """`queries: []` : le validateur tranche, et les deux portes rendent son code."""
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()

    reference, rendu = _comparer(
        outil, L._execute_search_info, {"queries": []}, conversation
    )

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "EMPTY_QUERY"
    assert _code(rendu["result"]) == "EMPTY_QUERY"


def test_parite_search_info_argument_inconnu(base_temporaire):
    """Clé inconnue : `UNKNOWN_ARG`, avec les arguments autorisés dans le message.

    Un `BAD_ARGS` générique ferait perdre au modèle la faute précise — c'est
    exactement ce que `call()` refuse, donc l'adaptateur doit le refuser aussi.
    """
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()

    reference, rendu = _comparer(
        outil,
        L._execute_search_info,
        {"queries": ["budget"], "portee": "depot"},
        conversation,
    )

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "UNKNOWN_ARG"
    assert _code(rendu["result"]) == "UNKNOWN_ARG"
    assert rendu["result"] == (reference.error or reference.display_summary)


def test_parite_search_info_enum_invalide(base_temporaire):
    """`type` hors liste : `ENUM_INVALID` des deux côtés."""
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()

    reference, rendu = _comparer(
        outil,
        L._execute_search_info,
        {"queries": ["budget"], "types": ["video"]},
        conversation,
    )

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "ENUM_INVALID"
    assert _code(rendu["result"]) == "ENUM_INVALID"


def test_parite_search_info_requete_trop_longue(base_temporaire):
    """Plus de trois requêtes : `TOO_MANY` des deux côtés."""
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()

    reference, rendu = _comparer(
        outil, L._execute_search_info, {"queries": ["a", "b", "c", "d"]}, conversation
    )

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "TOO_MANY"
    assert _code(rendu["result"]) == "TOO_MANY"


def test_parite_search_info_timeout(base_temporaire, monkeypatch):
    """Le délai de l'outil est celui de `call()`, et il devient un `TimeoutError`.

    On rend `execute` plus lent que `timeout_s` : sans le `asyncio.timeout` de
    `call()`, l'appel déborderait le délai de l'outil sans jamais s'interrompre.
    Le pont sync ne peut pas « court-circuiter » ce délai : il passe par `call()`,
    donc il le rend, et il le rend de la même façon.

    On instrumente la CLASSE, pas l'instance : l'adaptateur construit son propre
    outil, et un patch d'instance ne le toucherait pas — le test passerait alors
    pour une raison fausse.
    """
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()
    monkeypatch.setattr(SearchInfoTool, "timeout_s", 0.05)

    async def _lent(self, args, ctx):  # noqa: ARG001 — signature de méthode
        await asyncio.sleep(5)
        raise AssertionError("execute n'aurait pas dû être terminé")

    monkeypatch.setattr(SearchInfoTool, "execute", _lent)

    reference, rendu = _comparer(
        outil, L._execute_search_info, {"queries": ["budget"]}, conversation
    )

    assert reference.ok is False and rendu["ok"] is False
    assert "timeout" in (reference.display_summary or "").lower()
    assert rendu["result"] == (reference.error or reference.display_summary)
    # Le motif reste VISIBLE : un échec qui ne dit pas pourquoi se lit comme une
    # absence, et l'orchestrateur n'en demandera rien d'utile ensuite. La boucle
    # s'appuie d'ailleurs sur ce mot pour retenter (`loop.py`, `execute_discovery`).
    assert "timeout" in rendu["result"].lower()


def test_parite_search_info_panne_non_timeout_nommee(base_temporaire, monkeypatch):
    """Le journal des fenêtres internes : seul le délai VRAI est un TIMEOUT.

    Une panne du corps qui n'est PAS un dépassement de délai (ici une `KeyError`
    côté moteur FTS) est nommée honnêtement — le filet `except Exception` de
    `execute` a disparu (précédent `ask_files.py`), donc la panne atteint
    `BaseTool.call` qui écrit `[INTERNAL_ERROR] {type}`. L'ancien filet la
    déguisait en `[TIMEOUT] délai dépassé` : un motif FAUX que la boucle
    relançait et que l'utilisateur lisait comme un ralentissement.
    """

    def _panne(db, *args, **kwargs):  # noqa: ANN001
        raise KeyError("panne de moteur de test")

    monkeypatch.setattr("backend.search.fts.ensure_fts", _panne)
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()

    reference, rendu = _comparer(
        outil, L._execute_search_info, {"queries": ["budget"]}, conversation
    )

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "INTERNAL_ERROR"
    assert "KeyError" in (reference.error or ""), "le filet nomme le TYPE de la panne"
    assert "délai dépassé" not in (reference.error or ""), "aucun TIMEOUT fabriqué"
    assert _code(rendu["result"]) == "INTERNAL_ERROR"


def test_parite_search_info_troncature(base_temporaire, monkeypatch):
    """Au-delà de `max_output_chars`, la troncature est ANNONCÉE et identique.

    On abaisse le plafond pour ne pas fabriquer six kilo-octets de contenu : ce qui
    est vérifié est le MARQUAGE, et surtout que ses deux nombres sont RÉELS. Une
    troncature muette ferait croire au modèle que la page a été lue en entier ; une
    troncature annoncée faux mentirait tout autant.
    """
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = SearchInfoTool()
    monkeypatch.setattr(SearchInfoTool, "max_output_chars", 120)

    reference, rendu = _comparer(
        outil, L._execute_search_info, {"queries": ["budget"]}, conversation
    )

    assert reference.truncated is True
    annonce = re.search(r"\[Tronqué : (\d+)→120 chars\]", reference.content)
    assert annonce is not None, (
        f"la coupure n'est pas annoncée : {reference.content[-80:]!r}"
    )
    assert int(annonce.group(1)) > 120, (
        "le nombre annoncé doit être celui d'AVANT la coupe"
    )
    # Et surtout : les identifiants SURVIVENT à la troncature. Ils voyagent hors du
    # JSON coupé, sans quoi la boucle ne pourrait plus rien citer d'une réponse tronquée.
    assert rendu["public_ids"] == reference.public_ids
    assert rendu["evidence_ids"] == reference.evidence_ids
    assert rendu["evidence_ids"], "une page tronquée doit rester citable"


# ---------- 2. PARITÉ : ask_files (la classe, désormais le chemin vivant) ----------


def test_parite_ask_files_fichier_lu(base_temporaire):
    """`ask_files` : réponse, preuve et identifiants, par les deux portes.

    `AskFilesTool.execute` est maintenant le chemin vivant : `_execute_ask_files`
    délègue à `AskFilesTool.call()` (voir le bloc de commentaire devant cet
    exécuteur). Ce test compare donc la référence (`call()`) à l'ADAPTATEUR GÉNÉRIQUE
    (`_resultat_pour_la_boucle`), qui est exactement ce que le chemin vivant
    retourne — les deux portes ne peuvent plus diverger, et ce test le prouve encore.
    Il vérifie au passage les deux distinctions que la version dégradée ne faisait
    pas.
    """
    conversation, versé, joint = _depot_et_discussion(base_temporaire)
    outil = AskFilesTool()
    trouve = _via_call(SearchInfoTool(), {"queries": ["budget"]}, conversation)
    doc_id = (trouve.public_ids or [])[0]

    reference = _via_call(
        outil,
        {"questions": ["Quel est le budget du trimestre ?"], "resource_ids": [doc_id]},
        conversation,
    )
    rendu = L._resultat_pour_la_boucle(
        outil,
        {
            "args": {
                "questions": ["Quel est le budget du trimestre ?"],
                "resource_ids": [doc_id],
            }
        },
        conversation,
        time.perf_counter(),
    )

    assert reference.ok and rendu["ok"] is True
    assert _ce_que_voit_la_boucle(reference) == (True, rendu["result"])
    assert rendu["public_ids"] == reference.public_ids == [doc_id]
    assert rendu["evidence_ids"] == reference.evidence_ids
    assert reference.evidence_ids == [f"{doc_id}:0"], (
        "la preuve doit pointer un fragment PERSISTÉ"
    )
    assert "12 000" in rendu["result"]


def test_parite_ask_files_id_inconnu(base_temporaire):
    """`doc_99` jamais vu : `ID_UNKNOWN`, pas un vague « fichier introuvable »."""
    conversation, _, _ = _depot_et_discussion(base_temporaire)
    outil = AskFilesTool()
    arguments = {"questions": ["Quel est le budget ?"], "resource_ids": ["doc_99"]}

    reference, rendu = _comparer(outil, _adaptateur_de(outil), arguments, conversation)

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "ID_UNKNOWN"
    assert _code(rendu["result"]) == "ID_UNKNOWN"


def test_parite_ask_files_hors_perimetre(base_temporaire):
    """Un `doc_N` d'une autre espace n'est pas connu de cette conversation : `ID_UNKNOWN`.

    Le label alloué ailleurs ne fait pas partie de CETTE conversation : le
    verdict distant n'est pas divulgué, et seul l'état accessible localement compte.
    La distinction avec `ID_OUT_OF_SCOPE` reste : un DocRef connu ici mais retiré
    ou devenu inaccessible donne `ID_OUT_OF_SCOPE`.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace_a = str(service.create_workspace(db, "Alpha").id)
        conv_a = _conversation(db, "conv_alpha", espace_a)
        (alpha,) = _verser(
            db, {"workspaceId": espace_a}, ["Alpha_rapt.pdf"], "parite-alpha"
        )
        _fiche(db, alpha, "Le rapport confidentiel de Alpha.")
        _fragment_lu(db, alpha, "Le rapport confidentiel de Alpha.")

        espace_b = str(service.create_workspace(db, "Beta").id)
        conv_b = _conversation(db, "conv_beta", espace_b)
        (beta,) = _verser(
            db, {"workspaceId": espace_b}, ["Beta_rapt.pdf"], "parite-beta"
        )
        _fiche(db, beta, "Le rapport de Beta.")
        _fragment_lu(db, beta, "Le rapport de Beta.")
    finally:
        db.close()

    # `find_files` de Beta nomme son fichier : c'est ce `doc_N` qu'Alpha va citer.
    nomme = _via_call(SearchInfoTool(), {"queries": ["rapport"]}, conv_b)
    doc_beta = (nomme.public_ids or [])[0]

    outil = AskFilesTool()
    reference, rendu = _comparer(
        outil,
        _adaptateur_de(outil),
        {"questions": ["Que dit le rapport ?"], "resource_ids": [doc_beta]},
        conv_a,
    )

    assert reference.ok is False and rendu["ok"] is False
    assert _code(reference.error or "") == "ID_UNKNOWN"
    assert _code(rendu["result"]) == "ID_UNKNOWN"


def test_parite_ask_files_fichier_inexistant(base_temporaire):
    """Un fichier jamais ingéré se DISTINGUE d'un fichier muet.

    Un artefact déposé et jamais extrait ne peut pas répondre ; si l'outil répond
    quand même « information absente du document », l'orchestrateur le confond avec
    un document vide et envoie l'utilisateur chercher une réponse jamais lue.
    L'absence porte donc le MOTIF, lu dans l'état d'ingestion.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace = str(service.create_workspace(db, "Vide").id)
        conversation = _conversation(db, "conv_vide", espace)
        (jamais_lu,) = _verser(
            db, {"workspaceId": espace}, ["Jamais_lu.pdf"], "parite-vide"
        )
        _fiche(db, jamais_lu, "Une fiche existe, mais aucun texte n'a été extrait.")
    finally:
        db.close()

    trouve = _via_call(SearchInfoTool(), {"queries": ["fiche"]}, conversation)
    doc_id = (trouve.public_ids or [])[0]
    outil = AskFilesTool()
    arguments = {"questions": ["Quel est le montant ?"], "resource_ids": [doc_id]}

    reference, rendu = _comparer(outil, _adaptateur_de(outil), arguments, conversation)

    assert reference.ok is True and rendu["ok"] is True
    assert _ce_que_voit_la_boucle(reference) == (True, rendu["result"])
    assert reference.evidence_ids == [], (
        "une réponse sans fragment ne fabrique pas de preuve"
    )
    assert "12 000" not in rendu["result"]


# ---------- 3. LA RÉGRESSION CENTRALE : le dépôt redevient visible ----------


def test_le_depot_de_l_espace_est_visible_par_le_chemin_migre(base_temporaire):
    """§2.5 l. 143 : le périmètre ne se réduit pas à la seule discussion.

    Deux fichiers, un mot commun, une seule différence : l'un est versé au DÉPÔT de
    l'espace, l'autre est JOINT à la discussion. Le chemin migré doit nommer les
    deux ; le mécanisme d'avant — une seule portée `conversation` — n'en voyait
    qu'un, et l'orchestrateur concluait « ce document n'existe pas ».

    Le test ne se fie pas au souvenir : il appelle encore la primitive que
    l'ancien chemin empruntait (`fts.search` sur la portée `conversation`) et vérifie
    qu'elle ne rend toujours RIEN pour le fichier du dépôt. La régression est donc
    prouvée par les deux bouts — même base, même index, même requête.
    """
    sessions = base_temporaire
    conversation, versé, joint = _depot_et_discussion(sessions)

    # ── Avant : une seule porte. Le dépôt n'est pas une portée de conversation.
    db = sessions()
    try:
        allowed = Scope.resolve_allowed_artifacts(conversation)
        assert {versé, joint} <= allowed, "le périmètre du DROIT contient bien les deux"
        porte_unique = Fts.search(
            db,
            query="budget",
            scope_kind="conversation",
            scope_id=conversation,
            allowed_ids=allowed,
        )
        vus_avant = {r["artifact_id"] for r in (porte_unique.get("results") or [])}
    finally:
        db.close()
    assert versé not in vus_avant, (
        "fait brut : le fichier du dépôt DOIT rester invisible d'ici"
    )
    assert joint in vus_avant, "le fichier joint, lui, passe"

    # ── Après : le chemin migré, lui, voit les deux.
    rendu = L._execute_search_info(
        {"args": {"queries": ["budget du trimestre"]}}, conv_id=conversation
    )

    assert rendu["ok"] is True
    assert "Note_depot.pdf" in rendu["result"], "le fichier du DÉPÔT doit être nommé"
    assert "Piece_jointe.pdf" in rendu["result"], "le fichier JOINT doit être nommé"
    assert "dépôt de l'espace" in rendu["result"], (
        "l'ORIGINE est dite, pas seulement codée"
    )
    # Les deux `doc_N` sont nommés ET citables : c'est la moitié « ressources
    # accessibles à cette discussion » du contrat.
    assert len(rendu["public_ids"]) == 2, f"un seul doc_N nommé : {rendu['public_ids']}"
    assert rendu["evidence_ids"], "chaque extrait doit porter sa preuve"


def test_le_chemin_migre_n_ouvre_pas_la_porte_a_un_autre_espace(base_temporaire):
    """Le dépôt visible est le SIEN, et celui-là seulement.

    Une portée élargie par erreur — « interrogeons tous les workspaces » — ne se voit
    dans aucun log : elle ne se voit qu'ici. Le filtre de DROIT reste
    `resolve_allowed_artifacts` ; `search_scopes` n'ajoute qu'une porte de RECHERCHE.
    """
    sessions = base_temporaire
    db = sessions()
    try:
        espace_a = str(service.create_workspace(db, "Alpha").id)
        conv_a = _conversation(db, "conv_alpha", espace_a)
        (alpha,) = _verser(
            db, {"workspaceId": espace_a}, ["Rapt_alpha.pdf"], "parite-a1"
        )
        _fiche(db, alpha, "Le rapport rapt de Alpha.")

        espace_b = str(service.create_workspace(db, "Beta").id)
        _conversation(db, "conv_beta", espace_b)
        (beta,) = _verser(db, {"workspaceId": espace_b}, ["Rapt_beta.pdf"], "parite-b1")
        _fiche(db, beta, "Le rapport rapt de Beta.")
    finally:
        db.close()

    rendu = L._execute_search_info({"args": {"queries": ["rapt"]}}, conv_id=conv_a)

    assert rendu["ok"] is True
    assert "Rapt_alpha.pdf" in rendu["result"]
    assert "Rapt_beta.pdf" not in rendu["result"], (
        "fuite inter-espace : le dépôt d'un autre espace est visible"
    )
    assert beta not in rendu["public_ids"]


# ---------- 5. TRANSPORT : une réussite partielle conserve ses sous-succès ----------


def test_result_partiel_conserve_sous_succes_et_preuves(monkeypatch):
    """Contrat commun §3.3 : « éléments utiles obtenus, mais une partie de
    l'opération n'a pas abouti » — §5.1 : « Réussite partielle conservée avec les
    sous-échecs ». Le pont de la boucle doit donc rendre le code d'erreur EN
    PLUS du display_summary (les sous-succès), et recopier les preuves — sans
    jamais transformer `partial` en `ok=True`.

    Mesuré sur `_resultat_pour_la_boucle` elle-même, avec un outil bouchon : ce
    sont les champs du `ToolResult` qui décident du rendu, pas une recherche.
    """
    from pydantic import BaseModel, ConfigDict

    class _ArgsVides(BaseModel):
        model_config = ConfigDict(extra="forbid")
        note: str = "partiel"

    class _OutilPartiel(BaseTool[_ArgsVides]):
        name = "search_info_bouchon"
        description = "bouchon partiel"
        input_model = _ArgsVides

        async def execute(self, args, ctx):  # noqa: ANN001
            return ToolResult(
                status=ToolStatus.PARTIAL,
                content='{"tool": "search_info", "ok": false, "by_query": []}',
                display_summary="search_info: 2 requête(s) exécutée(s), 1 en panne",
                error="[SERVICE_UNAVAILABLE] La recherche n'a pas pu être exécutée pour 1 requête(s) sur 3.",
                evidence_ids=["doc_5:12"],
                public_ids=["doc_5"],
            )

    rendu = L._resultat_pour_la_boucle(
        _OutilPartiel(), {"args": {"note": "partiel"}}, "conv_stub", time.perf_counter()
    )

    assert rendu["ok"] is False, "partial reste ok=False : l'arbitrage du tour tient"
    assert rendu["result"].startswith("[SERVICE_UNAVAILABLE]"), (
        "le code d'erreur garde la tête de la ligne, c'est lui que la boucle relit"
    )
    assert "2 requête(s) exécutée(s), 1 en panne" in rendu["result"], (
        "les sous-succès (display_summary) sont rendus AVEC le code d'erreur"
    )
    assert rendu["evidence_ids"] == ["doc_5:12"], (
        "les preuves du partiel sont transportées"
    )
    assert rendu["public_ids"] == ["doc_5"], "les doc_N du partiel sont transportés"

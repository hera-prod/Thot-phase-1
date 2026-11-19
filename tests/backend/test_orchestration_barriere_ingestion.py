"""Barrière d'ingestion — orchestration.md §2.1 et discussion.md §4.4 (tâche 5.1).

D'OÙ VIENT CE FICHIER : « le message n'est pas traité par l'orchestrateur tant que
l'ingestion requise de ces fichiers n'est pas achevée et qu'un état cohérent n'est
pas publié » (orchestration.md §2.1), et « elle ne suspend pas les autres
discussions de l'application » (même §). En l'état, `run_agent_loop` ne lisait
JAMAIS `artifacts.ingest_state` : un fichier déposé une seconde avant l'envoi
partait en tour sur un registre à moitié vide, sans que l'attente ni le retard
soient nommés.

CE QUE LES TESTS VERROUILLENT :

  1. le tour attend : un fichier en ingestion retient SA conversation ; une fois
     l'état cohérent publié, le tour le lit — le brief est produit après
     l'attente, pas avant.
  2. l'autre conversation n'attend rien : pendant le tour bloqué, un tour d'une
     autre discussion se termine normalement.
  3. ingestion jamais soldée au délai → état explicite nommé (code + message qui
     porte le nom du fichier), et AUCUN brief formulé sur le fichier.

Les états sont écrits par le vrai chemin (`attachment_ingest._ingérer_un` →
`etat.marquer`) : forcer des états à la main ne prouverait rien sur la barrière.
Les fichiers sont `text` — extraction locale, aucun modèle lourd — et le fil
d'ingestion est retardé d'une fraction de seconde pour donner au tour la course
qu'il devrait perdre (attendre).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base

CONV_A = "conv_barriere_a"
CONV_B = "conv_barriere_b"

TEXTE = b"# Note\n\nLe contenu qu'un tour devra lire une fois l'ingestion passee.\n"

# Décalage du fil d'ingestion : la barrière lit un état non terminal, puis
# attend la publication. Assez long pour ne pas dépendre d'un ordinateur rapide,
# assez court pour ne pas ralentir la suite.
DECALAGE_INGESTION_S = 0.3

# Délai injecté pour ces tests : la valeur de production est une constante
# PROVISOIRE (`DELAI_BARRIERE_INGESTION` dans `backend.config.defaults`, à
# arbitrer après mesure — §3.6 « les seuils sont configurables et à mesurer »),
# injectée ici en fraîcheur pour la durée de la suite.
ATTENTE_TEST_S = 0.5


class _ScriptedRuntime:
    """LFM2.5 simulé : un round de découverte, puis un brief.

    Aucune attente n'est scripted : si le tour attend, c'est le code testé qui
    le fait attendre. La barrière se joue avant la première génération.
    """

    runtime_id = "llama_cpp"
    runtime_fingerprint = "llama_cpp:LFM2.5-8B-simule"

    def __init__(self) -> None:
        self.briefs = 0

    def complete_chat(self, request):
        from types import SimpleNamespace as _NS

        choice = request.tool_choice
        named = choice["function"]["name"] if isinstance(choice, dict) else None
        if named == "submit_brief":
            self.briefs += 1
            message = {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_brief_{self.briefs}",
                        "function": {
                            "name": "submit_brief",
                            "arguments": json.dumps(
                                {
                                    "brief": "Le tunnel de lecture et le document sont prêts."
                                }
                            ),
                        },
                    }
                ],
            }
        else:
            message = {"content": "Je regarde la demande."}
        return _NS(raw={"choices": [{"message": message}]})


@pytest.fixture()
def etablissement(tmp_path, monkeypatch):
    """DB temporaire, émission capturée, chronomètre réel (attente gratuite)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'barriere.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", session)

    from backend.ipc import attachments as _att
    from backend.ipc import paths_bridge as _pb
    from backend.config import paths as _paths

    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    # Le chemin de dépôt traverse TROIS modules d'imports (`attachments` →
    # `paths_bridge` → `config.paths`) : tous doivent voir la racine du test,
    # sinon l'extraction cherche le blob dans le dossier réel de l'utilisateur
    # et le fichier est « introuvable » hors du périmètre de test.
    monkeypatch.setattr(_att, "get_data_dir", lambda: racine, raising=False)
    monkeypatch.setattr(_att, "get_temp_dir", lambda: racine / "temp", raising=False)
    monkeypatch.setattr(_att, "blobs_root", lambda: racine / "blobs", raising=False)
    monkeypatch.setattr(_pb, "get_data_dir", lambda: racine, raising=False)
    monkeypatch.setattr(_pb, "get_temp_dir", lambda: racine / "temp", raising=False)
    monkeypatch.setattr(
        _pb, "data_dirs", lambda: (racine, racine / "temp"), raising=False
    )
    monkeypatch.setattr(_paths, "get_data_dir", lambda: racine, raising=False)
    monkeypatch.setattr(_paths, "get_temp_dir", lambda: racine / "temp", raising=False)
    monkeypatch.setattr("backend.ipc.attachments.SessionLocal", session, raising=False)
    monkeypatch.setattr(
        "backend.search.observations.SessionLocal", session, raising=False
    )
    monkeypatch.setattr("backend.search.chunks.SessionLocal", session, raising=False)
    # En production, la promotion lance l'ingestion tout de suite. Ici, chaque
    # test décide de la chronologie : la promotion livre les ids, le test retarde.
    declenchements: list[str] = []
    monkeypatch.setattr(
        _att,
        "_declencher_ingestion",
        lambda ids: declenchements.extend(ids),
        raising=False,
    )

    from backend.inference import loop as L

    evenements: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        L, "emit", lambda event, payload, **kwargs: evenements.append((event, payload))
    )
    return SimpleNamespace(
        fabrication=session,
        evenements=evenements,
        promouvoir=_att.attachments_prepare,
        stagier=_att.stage_picker_bytes,
        declenchements=declenchements,
    )


def _creer_conv(etablissement, conv_id: str) -> None:
    """La conversation attendue par la promotion, écrite dans la base de test."""
    from backend.db.models import Conversation

    avec = etablissement.fabrication()
    try:
        avec.add(Conversation(id=conv_id, title="essai", mode="chat"))
        avec.commit()
    finally:
        avec.close()


def _deposer(etablissement, conv_id: str, nom: str = "note.md") -> str:
    """Un dépôt réel via le promotionneur — il écrit `ingest_state='depose'`.

    L'ingestion n'est pas lancée : le test retient l'artefact et décide quand
    (ou si) la faire passer. C'est la chronologie qui crée la course.
    """
    avec = etablissement.fabrication()
    try:
        item = etablissement.stagier(
            avec, {"requestId": uuid.uuid4().hex, "filename": nom, "bytes": TEXTE}
        )["items"][0]
        artefact = etablissement.promouvoir(
            avec, {"conversationId": conv_id, "stageItemIds": [item["stage_item_id"]]}
        )["artifact_ids"][0]
    finally:
        avec.close()
    assert artefact in etablissement.declenchements, (
        "la promotion doit déclencher l'ingestion"
    )
    return artefact


def _ingestion_retardee(artefact_id: str) -> threading.Thread:
    """L'ingestion réelle en fil, avec un décalage initial contrôlé.

    Le décalage laisse le tour démarrer PENDANT l'ingestion : la barrière lit
    un état non terminal, puis attend la publication.
    """
    from backend.ingestion import attachment_ingest as _ing

    def _fil():
        time.sleep(DECALAGE_INGESTION_S)
        _ing._ingérer_un(artefact_id)

    fil = threading.Thread(target=_fil, name=f"ingest-{artefact_id[:8]}")
    fil.start()
    return fil


def _demarrer_tour(
    etablissement, conv_id: str, artefact: str | None
) -> tuple[
    threading.Thread,
    dict,
]:
    """Le tour de `conv_id` dans un fil, plus la boîte où il dépose le résultat."""
    from backend.inference import loop as L

    runtime = _ScriptedRuntime()
    boite: dict = {}

    def _cible():
        try:
            _, trace = L.run_agent_loop(
                None,
                conv_id=conv_id,
                request_id=f"req_{uuid.uuid4().hex[:10]}",
                message="De quoi parle le fichier que je viens de déposer ?",
                image_paths=None,
                history_shared=[],
                runtime=runtime,
                attachment_ids=[artefact] if artefact else [],
                delai_attente_s=ATTENTE_TEST_S,
            )
            boite["trace"] = trace
        except Exception as cause:  # le refus nominal du test 3 passe ICI
            boite["exception"] = cause
        boite["runtime"] = runtime

    fil = threading.Thread(target=_cible, name=f"tour-{conv_id}")
    fil.start()
    return fil, boite


def _lire_etat(etablissement, artefact_id: str) -> str:
    """L'état d'ingestion, lu directement dans la base du test."""
    from backend.db.models import Artifact

    avec = etablissement.fabrication()
    try:
        ligne = avec.query(Artifact).filter(Artifact.id == artefact_id).first()
        assert ligne is not None, "l'artefact déposé a disparu de la base de test"
        return ligne.ingest_state
    finally:
        avec.close()


# ---------- 1. le tour attend un état cohérent, puis il lit ----------


def test_le_tour_attend_la_publication_d_un_etat_cohérent_puis_le_lit(etablissement):
    """Le tour démarre pendant l'ingestion, attend, puis produit son brief.

    Chronologie : le dépôt écrit `depose` ; le fil d'ingestion part avec un
    décalage ; le tour démarre avant l'état cohérent. La propriété d'orchestration.md §2.1 :
    tant que l'état cohérent n'est pas publié, le tour ne lit pas — puis il lit.
    """
    from backend.ingestion.etat import ETATS_TERMINAUX

    _creer_conv(etablissement, CONV_A)
    artefact = _deposer(etablissement, CONV_A)
    assert _lire_etat(etablissement, artefact) == "depose"

    ingestion = _ingestion_retardee(artefact)
    tour, boite = _demarrer_tour(etablissement, CONV_A, artefact)
    tour.join(timeout=DECALAGE_INGESTION_S / 3)
    vivant_durant_ingestion = tour.is_alive()

    tour.join(timeout=5.0)
    ingestion.join(timeout=2.0)
    assert ingestion.is_alive() is False, "l'ingestion retardée n'a jamais fini"
    assert tour.is_alive() is False, "le tour est resté bloqué après l'état cohérent"
    assert vivant_durant_ingestion, (
        "le tour s'est déjà terminé pendant l'ingestion : il n'a pas attendu"
        " l'état cohérent (orchestration.md §2.1)"
    )
    assert _lire_etat(etablissement, artefact) in ETATS_TERMINAUX
    assert "exception" not in boite, f"le tour a levé : {boite.get('exception')}"
    assert boite["runtime"].briefs >= 1, (
        "le tour est sorti sans brief : la barrière a refusé"
    )


# ---------- 2. l'autre conversation n'est pas retardée ----------


def test_une_autre_conversation_n_est_pas_retardee_par_la_barriere(etablissement):
    """Pendant le tour de A attend, B termine normalement (suspension locale).

    L'artefact de A reste `depose` — personne ne le solde dans ce test, le tour
    de A avance donc vers son délai. Le tour de B (sans pièce jointe) démarre et
    se termine DANS la fenêtre d'attente de A : aucune suspension globale.
    """
    _creer_conv(etablissement, CONV_A)
    _creer_conv(etablissement, CONV_B)
    artefact_a = _deposer(etablissement, CONV_A)

    tour_a, _ = _demarrer_tour(etablissement, CONV_A, artefact_a)
    tour_a.join(timeout=DECALAGE_INGESTION_S / 3)
    assert tour_a.is_alive(), "le tour A n'attend pas : la barrière ne bloque pas"

    debut = time.perf_counter()
    tour_b, boite_b = _demarrer_tour(etablissement, CONV_B, None)
    tour_b.join(timeout=5.0)
    duree_b = time.perf_counter() - debut

    assert tour_b.is_alive() is False, (
        "le tour B s'est arrêté : la barrière de A l'a suspendu"
    )
    assert boite_b["runtime"].briefs >= 1, "le tour de B est sorti sans brief"
    assert duree_b < ATTENTE_TEST_S, (
        f"B a duré {duree_b:.2f}s : attendre A suspend les autres discussions"
        " (orchestration.md §2.1)"
    )
    tour_a.join(timeout=ATTENTE_TEST_S + 2.0)  # A finit au délai ; sans déborder


# ---------- 3. ingestion jamais soldée → état explicite nommé ----------


def test_ingestion_non_soldee_au_delai_produit_un_etat_nomme_sans_synthese(
    etablissement,
):
    """L'ingestion reste à `depose` : le délai expire, le refus est nommé.

    orchestration.md §2.1 : « Un incident, une capacité manquante ou une ingestion
    interrompue produit un état explicite d'échec ou de blocage » — ici le
    blocage a un code, la raison porte le nom du fichier, et le runtime n'a
    formulé AUCUN brief : pas une synthèse fondée sur les premières pages.
    """
    _creer_conv(etablissement, CONV_A)
    artefact = _deposer(etablissement, CONV_A)
    # Aucun fil d'ingestion : l'état reste `depose`, personne ne le solde.

    tour, boite = _demarrer_tour(etablissement, CONV_A, artefact)
    tour.join(timeout=ATTENTE_TEST_S + 2.0)
    assert tour.is_alive() is False, "le tour refuse au délai"
    assert "exception" in boite, (
        "un tour bloqué au délai doit refuser avec un état nommé"
    )

    erreurs = [payload for nom, payload in etablissement.evenements if nom == "error"]
    blocages = [
        payload for payload in erreurs if payload.get("code") == "ingestion_bloquee"
    ]
    assert blocages, f"aucun état de blocage n'a été émis ; reçus : {erreurs}"
    message = blocages[-1].get("message") or ""
    # Le nom reconnaissable (discussion.md §4.4 : le dépôt écrit le nom d'origine)
    from backend.db.models import Artifact

    avec = etablissement.fabrication()
    try:
        ligne = avec.query(Artifact).filter(Artifact.id == artefact).first()
        assert ligne is not None, "l'artefact déposé a disparu de la base de test"
        nom_reconnu = ligne.display_name or ligne.original_filename or artefact
    finally:
        avec.close()
    assert nom_reconnu in message, (
        f"le message ne porte pas le nom du fichier : {message!r}"
    )
    assert boite["runtime"].briefs == 0, (
        "des premières pages ont servi de base à une synthèse"
    )


# ---------- 4. ingestion en échec -> état nommé, ne traite pas ----------


def _echouer_un_facture(
    etablissement, conv_id: str, nom: str = "facture_echec.csv"
) -> str:
    """Un artefact déposé, puis mis à l'état `echec` par le vrai chemin de l'état.

    L'état est posé par `etat.marquer` — le vocabulaire et l'unique écrivain que
    la barrière lit (`artifacts.ingest_state` → `etat.marquer`) : pas de fil
    d'ingestion à retarder, l'échec étant terminal, le tour doit le voir dès sa
    première lecture.
    """
    from backend.ingestion import etat as etat_module

    artefact_id = _deposer(etablissement, conv_id, nom=nom)
    avec = etablissement.fabrication()
    try:
        etat_module.marquer(avec, artefact_id, etat_module.ETAT_ECHEC)
        avec.commit()
    finally:
        avec.close()
    return artefact_id


def test_ingestion_en_echec_nomme_et_ne_traite_pas(etablissement):
    """L'ingestion échouée dès le premier tour refuse — état nommé, aucun brief.

    orchestration.md §2.1 : « Un incident, une capacité manquante ou une ingestion
    interrompue produit un état explicite d'échec ou de blocage ». Le tour ne
    franchit jamais la barrière : elle lève `INGESTION_EN_ECHEC` (code de refus
    `ingestion_en_echec`) avant toute génération. La raison porte le nom connu du
    fichier, et le runtime n'a formulé AUCUN brief — aucune synthèse ne peut se
    fonder sur un contenu inexistant ni partiel.
    """
    _creer_conv(etablissement, CONV_A)
    artefact_id = _echouer_un_facture(etablissement, CONV_A)

    assert _lire_etat(etablissement, artefact_id) == "echec"

    tour, boite = _demarrer_tour(etablissement, CONV_A, artefact_id)
    tour.join(timeout=ATTENTE_TEST_S + 2.0)
    assert tour.is_alive() is False, "le tour refuse d'entrée en ingestion échouée"
    assert "exception" in boite, "l'échec nommé ne franchit jamais la barrière"
    assert type(boite["exception"]).__name__ == "RuntimeError", (
        "un échec nommé doit lever un RuntimeError nommé"
    )
    assert "INGESTION_EN_ECHEC" in str(boite["exception"])

    erreurs = [payload for nom, payload in etablissement.evenements if nom == "error"]
    echecs = [p for p in erreurs if p.get("code") == "ingestion_en_echec"]
    assert echecs, f"aucun échec nommé émis ; reçus : {erreurs}"
    message = echecs[-1].get("message") or ""
    # Le nom reconnu (discussion.md §4.4 : le dépôt écrit le nom d'origine)
    from backend.db.models import Artifact

    avec = etablissement.fabrication()
    try:
        ligne = avec.query(Artifact).filter(Artifact.id == artefact_id).first()
        assert ligne is not None, "l'artefact déposé a disparu de la base de test"
        nom_reconnu = ligne.display_name or ligne.original_filename or artefact_id
    finally:
        avec.close()
    assert nom_reconnu in message, (
        f"l'échec ne porte pas le nom du fichier : {message!r}"
    )
    assert boite["runtime"].briefs == 0, (
        "des premières pages ont servi de base à une synthèse"
    )

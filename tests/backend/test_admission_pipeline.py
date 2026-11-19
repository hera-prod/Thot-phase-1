"""Phase 3 — admission d'ingestion : pipeline réel, claims, heartbeat, file.

Ces tests exercent le VRAI pipeline (`lancer_ingestion`, `_worker_ingestion`,
`_ingérer_un_impl`, `etat_admission`) avec une base SQLite éphémère et une
extraction simulée. Ils verrouillent :

- `_en_cours` n'est jamais peuplé à l'enfilage ni avant un claim réussi ;
- claim actif ailleurs → sortie immédiate, `_en_cours` vide ;
- échec d'acquisition DB → `_en_cours` vide ;
- succès → `_en_cours` peuplé pendant l'extraction, vidé dans le `finally` ;
- single-flight : un artefact déjà en file n'est pas ré-enfilé ;
- bail perdu pendant l'extraction → aucune écriture ultérieure ;
- bail perdu entre deux régions / après la dernière page → aucune écriture ;
- 200 artefacts + 1 GPU + chat : single-flight (pas de doublons), un seul
  worker, priorité interactive prouvée sous contention réelle ;
- `_battement_periodique` ne masque plus `BailPerdu` ; un battement tokenisé
  après libération n'écrit plus.

Aucun OCR réel : `extraire_texte` est monkeypatché.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Artifact, Base, Blob
from backend.ingestion import attachment_ingest
from backend.ingestion import bail as _bail
from backend.ingestion import etat as ETAT
from backend.ingestion.erreurs import BailPerdu

# Référence au VRAI starter, capturée AVANT que le fixture ne le neutralise :
# le test 200 artefacts l'exerce réellement (le wrapper ne fait qu'observer le
# fil créé, il ne réimplémente pas la single-flight).
_STARTER_REEL = attachment_ingest._demarrer_worker_si_necessaire


# ---------------------------------------------------------------------------
# Montage : base éphémère, worker contrôlé, aucun modèle
# ---------------------------------------------------------------------------


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    """Base SQLite éphémère + état d'admission remis à zéro.

    Le worker n'est PAS démarré automatiquement : les tests qui en ont besoin
    l'instancient explicitement pour pouvoir le joindre. Cela évite qu'un
    worker d'un test précédent consomme la file du suivant.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))

    # Neutralise le démarrage automatique : le test décide quand le worker vit.
    monkeypatch.setattr(
        attachment_ingest, "_demarrer_worker_si_necessaire", lambda: None
    )

    with attachment_ingest._verrou:
        attachment_ingest._en_file.clear()
        attachment_ingest._en_cours.clear()
        attachment_ingest._worker_demarre = False
    _vider_file()
    attachment_ingest._arret_admission.clear()

    yield sessions, racine

    attachment_ingest._arret_admission.set()
    _vider_file()
    # Ne laisse pas un worker d'un test précédent consommer la file du suivant.
    for fil in threading.enumerate():
        if fil.name == "ingestion-worker":
            fil.join(timeout=2.0)
    with attachment_ingest._verrou:
        attachment_ingest._en_file.clear()
        attachment_ingest._en_cours.clear()
        attachment_ingest._worker_demarre = False
    attachment_ingest._arret_admission.clear()


def _vider_file() -> None:
    while True:
        try:
            attachment_ingest._file_admission.get_nowait()
        except Exception:
            break
        else:
            attachment_ingest._file_admission.task_done()


def _creer_artefact(
    sessions,
    racine: Path,
    *,
    kind: str = "pdf",
    contenu: bytes = b"%PDF-1.7",
    etat_initial: str = ETAT.ETAT_DEPOSE,
) -> str:
    """Un artefact avec un blob RÉEL : `_chemin_artifact` doit le trouver."""
    marque = uuid.uuid4().hex[:12]
    artifact_id = f"art_{marque}"
    blob_id = f"blob_{marque}"
    relatif = f"blobs/{blob_id}.bin"
    fichier = racine / relatif
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_bytes(contenu)
    with sessions() as db:
        db.add(
            Blob(
                id=blob_id,
                sha256=marque,
                relative_path=relatif,
                size_bytes=len(contenu),
                mime_detected="application/pdf",
                state="ready",
            )
        )
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=blob_id,
                kind=kind,
                original_filename="piece.pdf",
                display_name="piece.pdf",
                ingest_state=etat_initial,
            )
        )
        db.commit()
    return artifact_id


def _en_cours_contient(artifact_id: str) -> bool:
    with attachment_ingest._verrou:
        return artifact_id in attachment_ingest._en_cours


def _attendre_admission_vide(timeout_s: float = 10.0) -> bool:
    """Attente BORNÉE, jamais synchrone sur 200 OCR."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if attachment_ingest.etat_admission()["admis"] == 0:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 1. `_en_cours` : enfilage vs démarrage réel
# ---------------------------------------------------------------------------


def test_en_cours_n_est_pas_ecrit_a_l_enfilage(pipeline) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)

    attachment_ingest.lancer_ingestion([artefact])

    etat = attachment_ingest.etat_admission()
    assert etat["en_cours"] == 0, "l'enfilage ne doit pas écrire en_cours"
    assert etat["en_file"] == 1
    assert etat["admis"] == 1
    assert not _en_cours_contient(artefact)


def test_claim_actif_ailleurs_sortie_immediate_en_cours_vide(pipeline) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)

    with sessions() as db:
        token = _bail.acquérir_bail(db, artefact, "autre-worker")
    assert token is not None

    attachment_ingest._ingérer_un_impl(artefact, modeles_autorises=True)

    assert not _en_cours_contient(artefact), "aucun worker local ne travaille"
    with sessions() as db:
        _bail.libérer_bail(db, artefact, token)


def test_echec_acquisition_db_en_cours_vide(pipeline, monkeypatch) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)

    def _acquerir_ko(*args, **kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(_bail, "acquérir_bail", _acquerir_ko)

    attachment_ingest._ingérer_un_impl(artefact, modeles_autorises=True)

    assert not _en_cours_contient(artefact)
    with sessions() as db:
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.ingest_claim_token is None, "aucun claim n'a été posé"


def test_succes_en_cours_peuple_puis_vide_dans_finally(pipeline, monkeypatch) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)
    vus_pendant: list[bool] = []

    def _extraire_simule(*args, **kwargs):
        with attachment_ingest._verrou:
            vus_pendant.append(artefact in attachment_ingest._en_cours)
        return ("Contrat de test. " * 40, "native")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", _extraire_simule)

    attachment_ingest._ingérer_un_impl(artefact, modeles_autorises=True)

    assert vus_pendant == [True], "en_cours doit être peuplé PENDANT l'extraction"
    assert not _en_cours_contient(artefact), "en_cours doit être vidé dans le finally"
    with sessions() as db:
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.ingest_claim_token is None, "le claim doit être libéré"


def test_en_cours_vide_meme_si_extraction_leve(pipeline, monkeypatch) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)

    def _extraire_ko(*args, **kwargs):
        raise RuntimeError("extraction cassée")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", _extraire_ko)

    attachment_ingest._ingérer_un_impl(artefact, modeles_autorises=True)

    assert not _en_cours_contient(artefact), "le finally doit nettoyer sur exception"
    with sessions() as db:
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.ingest_claim_token is None


# ---------------------------------------------------------------------------
# 2. Single-flight
# ---------------------------------------------------------------------------


def test_single_flight_pas_de_reenfilage(pipeline) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)

    attachment_ingest.lancer_ingestion([artefact, artefact, artefact])
    etat = attachment_ingest.etat_admission()
    assert etat["en_file"] == 1
    assert etat["admis"] == 1

    attachment_ingest.lancer_ingestion([artefact])
    etat = attachment_ingest.etat_admission()
    assert etat["en_file"] == 1, "un artefact déjà en file n'est pas ré-enfilé"
    assert etat["admis"] == 1


# ---------------------------------------------------------------------------
# 3. Bail perdu : aucune écriture ultérieure
# ---------------------------------------------------------------------------


def test_bail_perdu_pendant_extraction_aucune_ecriture_ulterieure(
    pipeline, monkeypatch
) -> None:
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)
    marquages: list[str] = []
    original_marquer = attachment_ingest._marquer

    def _capture_marquer(artifact_id, etat, raison, claim_token=None):
        marquages.append(etat)
        return original_marquer(artifact_id, etat, raison, claim_token)

    monkeypatch.setattr(attachment_ingest, "_marquer", _capture_marquer)
    monkeypatch.setattr(
        attachment_ingest,
        "extraire_texte",
        lambda *a, **k: (_ for _ in ()).throw(BailPerdu(artefact, "token-perdu")),
    )

    attachment_ingest._ingérer_un_impl(artefact, modeles_autorises=True)

    # Seul l'état transitoire `en_cours` a été marqué AVANT l'extraction.
    # Le handler `BailPerdu` retourne sans écrire d'état terminal.
    assert marquages == [ETAT.ETAT_EN_COURS]
    assert not _en_cours_contient(artefact)
    with sessions() as db:
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.ingest_claim_token is None, "le claim doit être libéré"


# ---------------------------------------------------------------------------
# 4. Heartbeat : `BailPerdu` n'est plus avalé
# ---------------------------------------------------------------------------


def test_battement_periodique_signale_bail_perdu() -> None:
    def _heartbeat_qui_perd() -> None:
        raise BailPerdu("art-test", "token-test")

    with attachment_ingest._battement_periodique(
        _heartbeat_qui_perd, intervalle_s=0.01
    ) as bail_perdu:
        deadline = time.monotonic() + 2.0
        while not bail_perdu.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bail_perdu.is_set(), "BailPerdu doit être signalé, pas avalé"


def test_battre_apres_liberation_ne_reecrit_pas(pipeline) -> None:
    """Un battement tokenisé après libération du bail est un no-op + arrêt.

    C'est la garantie qui rend inoffensif un fil de battement encore vivant
    après le `join` de `_battement_periodique` : l'UPDATE filtré par
    `ingest_claim_token` ne trouve plus de ligne, n'écrit rien, et `_battre`
    signale `BailPerdu` au lieu de prolonger un bail qui ne nous appartient plus.
    """
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)

    with sessions() as db:
        token = _bail.acquérir_bail(db, artefact, "test-battre")
    assert token is not None
    with sessions() as db:
        avant = (
            db.query(Artifact).filter(Artifact.id == artefact).one().ingest_heartbeat_at
        )

    with sessions() as db:
        assert _bail.libérer_bail(db, artefact, token) is True

    with pytest.raises(BailPerdu):
        attachment_ingest._battre(artefact, token)

    with sessions() as db:
        apres = (
            db.query(Artifact).filter(Artifact.id == artefact).one().ingest_heartbeat_at
        )
    assert apres == avant, "aucune écriture de heartbeat après libération"


# ---------------------------------------------------------------------------
# 5. 200 artefacts : single-flight, un seul worker, pas 200 threads
# ---------------------------------------------------------------------------


def _attendre_condition(predicat, timeout_s: float = 10.0) -> bool:
    """Attente bornée d'une condition observée par polling court."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicat():
            return True
        time.sleep(0.005)
    return False


def test_200_artefacts_single_flight_un_seul_worker_et_chat_servi(
    pipeline, monkeypatch
) -> None:
    """200 artefacts + 1 GPU + chat : single-flight, worker unique, chat prioritaire.

    Le scénario exerce le VRAI worker d'admission, le VRAI starter et le VRAI
    coordinateur de ressources. Aucun OCR réel : `extraire_texte` prend un bail
    « ocr » de type ingestion, dort brièvement, puis le rend.

    CONTENTION RÉELLE ET DÉLAI DISCRIMINANT : un détenteur factice retient la
    ressource pendant que le worker et 20 waiters d'ingestion supplémentaires
    s'enregistrent, puis le chat arrive (donc dernier en FIFO). Chaque waiter
    d'ingestion retient ensuite 0,15 s : en FIFO, le chat attendrait ~3 s, au-delà
    de son seuil de 2 s et de sa priorité. Le test échoue donc si le
    coordinateur redevient FIFO ou si le chat n'est pas servi en premier.

    FILE : `queue.Queue()` n'est PAS bornée en dur ici. Le test prouve la
    single-flight (200 artefacts uniques, aucun doublon) et l'unicité du worker,
    pas une borne mémoire.

    Le test est asynchrone : le worker, les waiters et le chat sont des threads,
    et toutes les attentes sont bornées.
    """
    sessions, racine = pipeline
    artefacts = [_creer_artefact(sessions, racine) for _ in range(200)]

    from backend.inference import resource_coordinator as rc

    coordinator = rc.get_resource_coordinator()
    coordinator.reset()
    consommateurs: set[str] = set()
    erreurs_chat: list[BaseException] = []
    delais_chat: list[float] = []
    ordre_service: list[str] = []
    ordre_verrou = threading.Lock()

    def _noter_service(nom: str) -> None:
        with ordre_verrou:
            ordre_service.append(nom)

    def _waiters_inscrits() -> int:
        valeur = coordinator.snapshot()["waiters"]
        return int(valeur) if isinstance(valeur, int) else 0

    def _chat_inscrit() -> bool:
        details = coordinator.snapshot()["waiter_details"]
        return isinstance(details, list) and any(
            isinstance(detail, dict) and detail.get("owner") == "chat-test"
            for detail in details
        )

    # Le détenteur factice force TOUS les requérants à s'enregistrer avant le
    # moindre service : c'est ce qui rend la priorité observable.
    blocage = coordinator.acquire(
        "ocr", "test-blocage", timeout_s=5.0, kind="interactive"
    )

    def _extraire_avec_bail(*args, **kwargs):
        # Simule un travail GPU d'ingestion court sous le bail coordinateur.
        consommateurs.add(threading.current_thread().name)
        lease = coordinator.acquire(
            "ocr",
            f"ingest-{uuid.uuid4().hex}",
            timeout_s=10.0,
            kind="ingestion",
        )
        try:
            _noter_service("worker")
            time.sleep(0.002)
        finally:
            coordinator.release(lease)
        return ("Contenu de test indexable. " * 40, "native")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", _extraire_avec_bail)

    # Le VRAI starter est exercé : le wrapper observe seulement le fil créé,
    # il ne réimplémente pas la garde de single-flight.
    workers: list[threading.Thread] = []

    def _starter_observe() -> None:
        avant = {fil.ident for fil in threading.enumerate()}
        _STARTER_REEL()
        for fil in threading.enumerate():
            if fil.name == "ingestion-worker" and fil.ident not in avant:
                workers.append(fil)

    monkeypatch.setattr(
        attachment_ingest, "_demarrer_worker_si_necessaire", _starter_observe
    )

    # Enfilage délibérément doublé : la single-flight doit produire 200 puts
    # uniques, pas 400. C'est l'absence de doublons qui est prouvée ici.
    enfilages: list[str] = []
    put_original = attachment_ingest._file_admission.put

    def _put_observe(item, *args, **kwargs):
        enfilages.append(item[0])
        return put_original(item, *args, **kwargs)

    monkeypatch.setattr(attachment_ingest._file_admission, "put", _put_observe)

    chat_servi = threading.Event()

    def _chat() -> None:
        try:
            debut = time.monotonic()
            lease = coordinator.acquire(
                "ocr", "chat-test", timeout_s=2.0, kind="interactive"
            )
            try:
                _noter_service("chat-0")
                delais_chat.append(time.monotonic() - debut)
            finally:
                coordinator.release(lease)
            chat_servi.set()
        except Exception as exc:
            # Le chat ne doit pas échouer : une exception est un échec du test.
            erreurs_chat.append(exc)

    def _waiter_ingestion(idx: int) -> None:
        lease = coordinator.acquire(
            "ocr", f"contention-{idx}", timeout_s=10.0, kind="ingestion"
        )
        try:
            _noter_service(f"ingestion-{idx}")
            time.sleep(0.15)
        finally:
            coordinator.release(lease)

    waiters_extra: list[threading.Thread] = []
    chat: threading.Thread | None = None
    initial_threads = threading.active_count()

    try:
        attachment_ingest.lancer_ingestion(artefacts + artefacts)
        assert len(workers) == 1, f"{len(workers)} workers démarrés pour 200 artefacts"

        # 1. Le worker s'enregistre derrière le détenteur factice.
        assert _attendre_condition(lambda: _waiters_inscrits() >= 1), (
            "le worker n'a pas rejoint la file GPU"
        )

        # 2. Vingt waiters ingestion supplémentaires : contention réelle.
        waiters_extra = [
            threading.Thread(
                target=_waiter_ingestion,
                args=(i,),
                name=f"contention-{i}",
                daemon=True,
            )
            for i in range(20)
        ]
        for waiter in waiters_extra:
            waiter.start()
        assert _attendre_condition(lambda: _waiters_inscrits() >= 21), (
            "les waiters d'ingestion ne se sont pas tous enregistrés"
        )

        # 3. Le chat arrive APRÈS eux : dernier en FIFO.
        chat = threading.Thread(target=_chat, name="test-chat", daemon=True)
        chat.start()
        assert _attendre_condition(lambda: _chat_inscrit()), (
            "le chat n'a pas rejoint la file GPU"
        )

        # 4. Libération du détenteur : le chat doit passer avant 21 ingestions.
        coordinator.release(blocage)
        blocage = None

        assert _attendre_condition(lambda: chat_servi.is_set()), (
            f"le chat n'a pas été servi dans le délai (erreurs={erreurs_chat!r})"
        )
        assert _attendre_admission_vide(timeout_s=120.0), (
            "le worker n'a pas absorbé les 200 artefacts dans le délai"
        )
    finally:
        if blocage is not None:
            try:
                coordinator.release(blocage)
            except Exception:
                pass
        attachment_ingest._arret_admission.set()
        for worker in workers:
            worker.join(timeout=10.0)
        for waiter in waiters_extra:
            waiter.join(timeout=5.0)
        if chat is not None:
            chat.join(timeout=6.0)
        attachment_ingest._arret_admission.clear()
        coordinator.reset()

    # 1. Single-flight : 400 demandes → 200 puts uniques, aucun doublon.
    assert len(enfilages) == len(set(enfilages)) == len(artefacts), (
        f"{len(enfilages)} puts pour {len(set(enfilages))} artefacts uniques"
    )
    # 2. Un SEUL consommateur : un worker, pas 200 threads.
    assert consommateurs == {"ingestion-worker"}, (
        f"consommateurs inattendus : {consommateurs!r}"
    )
    # 3. Threads bornés : le worker + le chat, pas 200.
    final_threads = threading.active_count()
    assert final_threads <= initial_threads + 2, (
        f"threads {initial_threads} → {final_threads} : la file doit borner"
    )
    # 4. PRIORITÉ INTERACTIVE PROUVÉE : le chat est servi AVANT les 21 waiters
    #    d'ingestion déjà enregistrés, dans son seuil discriminant de 1 s.
    assert not erreurs_chat, f"le chat a échoué : {erreurs_chat!r}"
    assert ordre_service and ordre_service[0] == "chat-0", (
        f"ordre de service inattendu : {ordre_service[:5]!r}"
    )
    assert delais_chat and delais_chat[0] <= 2.0, (
        f"le chat a attendu {delais_chat!r} s malgré sa priorité"
    )

    with sessions() as db:
        extraits = (
            db.query(Artifact)
            .filter(Artifact.ingest_state == ETAT.ETAT_EXTRAIT)
            .count()
        )
    assert extraits == 200, f"{extraits}/200 artefacts ont abouti"


# ---------------------------------------------------------------------------
# 6. Bail perdu aux frontières de région / après la dernière page
# ---------------------------------------------------------------------------


def _installer_faux_ocr_pdf(
    monkeypatch,
    *,
    bail_perdu: threading.Event,
    pages: int,
    declencheur: str,
    rendus: list[str],
    parse_calls: list[int],
    pages_construites: list[int],
) -> None:
    """Installe des faux modules pour exercer `_ocr_pdf` sans OCR réel.

    `declencheur` :
    - "region" : perte posée pendant le TRAITEMENT d'une région
      (`regions_vers_gate`), pas dans `_detect` ; le contrôle de région doit
      arrêter AVANT tout `parse_png` ;
    - "parse" : perte posée PENDANT `parse_png` ; le contrôle de région après
      le parse doit arrêter avant la construction de la page ;
    - "fin_page" : perte posée à la construction de `PageExtraite`, après tous
      les contrôles internes de la page ; seul le contrôle APRÈS la boucle peut
      la voir.

    `parse_calls` et `pages_construites` enregistrent les appels : le test
    prouve qu'aucun parse/page ne suit la perte. `rendus` enregistre les appels
    à `représentation_document` : aucun rendu final ne doit avoir lieu.
    """
    import contextlib
    import sys
    import types

    from backend.ingestion import figures_pdf as FIG
    from backend.ingestion import gate as G
    from backend.ingestion import markdown_pdf as MD
    from backend.ingestion import paddle_client as _pc

    class _FauxDocument:
        def __init__(self) -> None:
            self._pages = [object() for _ in range(pages)]

        def __len__(self) -> int:
            return len(self._pages)

        def __getitem__(self, index: int):
            return self._pages[index]

        def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "pymupdf",
        types.SimpleNamespace(open=lambda chemin: _FauxDocument()),
    )

    monkeypatch.setattr(G, "render_page_png", lambda page, dpi: object())
    monkeypatch.setattr(_pc, "pil_to_png_bytes", lambda image: b"png")
    monkeypatch.setattr(G, "gate_from_regions", lambda regions: "parse")
    monkeypatch.setattr(FIG, "regions_figure", lambda regions: [])

    def _regions_vers_gate(regions):
        if declencheur == "region":
            bail_perdu.set()
        return regions

    monkeypatch.setattr(G, "regions_vers_gate", _regions_vers_gate)

    def _detect(png, *, kind, owner):
        return [{"box": [0, 0, 1, 1], "category": "text"}]

    monkeypatch.setattr(_pc, "detect_regions", _detect)
    monkeypatch.setattr(FIG, "cropper_figure", lambda image, box: object())

    def _parse(png, *, task, kind, owner):
        parse_calls.append(len(parse_calls) + 1)
        if declencheur == "parse":
            bail_perdu.set()
        return "texte de page"

    monkeypatch.setattr(_pc, "parse_png", _parse)

    page_originale = MD.PageExtraite

    def _page_observee(*args, **kwargs):
        pages_construites.append(1)
        if declencheur == "fin_page":
            # La dernière action de la page pose la perte : le contrôle APRÈS la
            # boucle de pages doit la voir, pas seulement celui de la page suivante.
            bail_perdu.set()
        return page_originale(*args, **kwargs)

    # On garde la vraie dataclass sous le wrapper pour que `_ocr_pdf` assemble
    # une page valide ; le test n'observe que la construction.
    monkeypatch.setattr(MD, "PageExtraite", _page_observee)

    monkeypatch.setattr(
        MD,
        "représentation_document",
        lambda *a, **k: rendus.append("rendu") or "markdown",
    )

    # Le battement est contrôlé : l'événement est celui du test, pas un fil réel.
    @contextlib.contextmanager
    def _faux_battement(heartbeat):
        yield bail_perdu

    monkeypatch.setattr(attachment_ingest, "_battement_periodique", _faux_battement)


def _executer_ingestion_avec_faux_ocr(
    pipeline, monkeypatch, *, pages: int, declencheur: str
) -> tuple[list[str], list[str], list[int], list[int], str]:
    """Lance `_ingérer_un_impl` sur un `_ocr_pdf` factice et capture les écritures."""
    sessions, racine = pipeline
    artefact = _creer_artefact(sessions, racine)
    marquages: list[str] = []
    original_marquer = attachment_ingest._marquer

    def _capture_marquer(artifact_id, etat, raison, claim_token=None):
        marquages.append(etat)
        return original_marquer(artifact_id, etat, raison, claim_token)

    monkeypatch.setattr(attachment_ingest, "_marquer", _capture_marquer)

    bail_perdu = threading.Event()
    rendus: list[str] = []
    parse_calls: list[int] = []
    pages_construites: list[int] = []
    _installer_faux_ocr_pdf(
        monkeypatch,
        bail_perdu=bail_perdu,
        pages=pages,
        declencheur=declencheur,
        rendus=rendus,
        parse_calls=parse_calls,
        pages_construites=pages_construites,
    )

    def _extraire_qui_appelle_ocr(*args, **kwargs):
        return (
            attachment_ingest._ocr_pdf(
                "faux.pdf",
                heartbeat=kwargs.get("heartbeat"),
                kind="ingestion",
                owner=kwargs.get("claim_token"),
                artifact_id=args[0],
            ),
            "ocr",
        )

    monkeypatch.setattr(attachment_ingest, "extraire_texte", _extraire_qui_appelle_ocr)

    attachment_ingest._ingérer_un_impl(artefact, modeles_autorises=True)

    assert not _en_cours_contient(artefact)
    with sessions() as db:
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.ingest_claim_token is None, "le claim doit être libéré"
    return marquages, rendus, parse_calls, pages_construites, artefact


def test_bail_perdu_pendant_region_aucun_parse_ni_ecriture(
    pipeline, monkeypatch
) -> None:
    """Perte posée pendant le TRAITEMENT d'une région, pas dans `_detect`.

    Le contrôle de région qui suit `regions_vers_gate` doit arrêter AVANT tout
    `parse_png`. Si ce contrôle disparaît, `parse_png` est appelé et la page est
    construite : ce test échoue.
    """
    marquages, rendus, parse_calls, pages_construites, _ = (
        _executer_ingestion_avec_faux_ocr(
            pipeline, monkeypatch, pages=2, declencheur="region"
        )
    )

    assert marquages == [ETAT.ETAT_EN_COURS]
    assert parse_calls == [], "aucun parse ne doit suivre la perte de région"
    assert pages_construites == [], "aucune page ne doit être construite"
    assert rendus == [], "aucun rendu final ne doit avoir lieu après la perte"


def test_bail_perdu_pendant_parse_aucune_page_ni_rendu(pipeline, monkeypatch) -> None:
    """Perte posée PENDANT `parse_png` : le contrôle de région après le parse.

    Sans ce contrôle, la page serait construite puis le contrôle final (ou
    celui de la page suivante) lèverait plus tard : `pages_construites` le
    prouve, alors que `rendus` seul ne suffirait pas.
    """
    marquages, rendus, parse_calls, pages_construites, _ = (
        _executer_ingestion_avec_faux_ocr(
            pipeline, monkeypatch, pages=2, declencheur="parse"
        )
    )

    assert marquages == [ETAT.ETAT_EN_COURS]
    assert parse_calls == [1], "le parse de la page 1 a eu lieu, puis arrêt"
    assert pages_construites == [], "le contrôle post-parse doit empêcher la page"
    assert rendus == [], "aucun rendu final ne doit avoir lieu après la perte"


def test_bail_perdu_apres_derniere_page_aucune_ecriture_ulterieure(
    pipeline, monkeypatch
) -> None:
    marquages, rendus, parse_calls, pages_construites, _ = (
        _executer_ingestion_avec_faux_ocr(
            pipeline, monkeypatch, pages=1, declencheur="fin_page"
        )
    )

    # Une seule page : la perte est posée à la construction de la page, après
    # tous les contrôles internes. Seul le contrôle APRÈS la boucle peut l'arrêter.
    assert marquages == [ETAT.ETAT_EN_COURS]
    assert parse_calls == [1]
    assert pages_construites == [1]
    assert rendus == [], "aucun rendu final ne doit avoir lieu après la perte"


def test_blocs_figures_page_arrete_apres_bail_perdu(monkeypatch) -> None:
    """Le callback `verifier_bail` arrête le traitement des régions figure.

    Une perte à la deuxième frontière doit laisser la première région traitée,
    puis remonter : aucune région suivante n'est recadrée ni réexaminée.
    """
    from backend.ingestion import figures_pdf as FIG

    regions = [
        {"category": "figure", "score": 0.9, "box": [0, 0, 2, 2]},
        {"category": "figure", "score": 0.9, "box": [2, 2, 4, 4]},
        {"category": "figure", "score": 0.9, "box": [4, 4, 6, 6]},
    ]
    crops: list[object] = []
    monkeypatch.setattr(
        FIG,
        "regions_figure",
        lambda r: [(index + 1, region) for index, region in enumerate(r)],
    )
    monkeypatch.setattr(
        FIG,
        "cropper_figure",
        lambda image, box: crops.append(box) or object(),
    )
    monkeypatch.setattr(FIG, "bloc_figure_en_attente", lambda page, index: "bloc")
    monkeypatch.setattr(FIG, "bloc_figure_limite", lambda page, index, cause: "limite")

    appels: list[int] = []

    def _verifier_qui_perd() -> None:
        appels.append(1)
        if len(appels) == 2:
            raise BailPerdu("art-figure", "token-figure")

    with pytest.raises(BailPerdu):
        attachment_ingest._blocs_regions_figures(
            1,
            object(),
            regions,
            kind="ingestion",
            owner=None,
            collecte=None,
            verifier_bail=_verifier_qui_perd,
        )

    # Le callback est appelé à chaque frontière ; le 2e lève et la boucle
    # s'arrête : une seule région figure a été recadrée.
    assert len(appels) == 2
    assert len(crops) == 1

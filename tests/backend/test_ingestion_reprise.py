"""Phase 2 — les dettes d'ingestion se reprennent, ne se perdent pas.

Ce fichier verrouille le contrat de reprise :

  - une indisponibilité du modèle OCR (spawn, chargement, timeout) est une
    DETTE (`depose`), jamais un échec terminal ;
  - un PDF dont aucune page n'est reconnue reste rejouable, un PDF partiel est
    `partiel` et son texte reste servi ;
  - le backoff persistant écarte une échéance future, se remet à zéro au
    succès, et plafonne à cinq essais par un état terminal motivé ;
  - le timer périodique n'appelle la reprise qu'avec les modèles autorisés,
    à cinq artefacts par minute, et ne lève jamais si un tour est actif ;
  - un `en_cours` dont le battement est périmé est libéré du garde mémoire.

Montage : base SQLite éphémère, racines jetables, aucun modèle, aucun GPU,
aucun réseau.
"""

from __future__ import annotations

import io
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
from backend.ingestion import etat as ETAT
from backend.ingestion import paddle_client as PC
from backend.ingestion import reprise
from backend.ingestion.paddle_client import ModeleIndisponible


@pytest.fixture()
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reprise.db'}")
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
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids, **kw: None)
    return sessions, racine


def _creer(
    sessions,
    racine: Path,
    *,
    kind: str = "pdf",
    etat_initial: str = ETAT.ETAT_DEPOSE,
    contenu: bytes = b"%PDF-1.7",
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


def _lire(sessions, artifact_id: str) -> tuple[Artifact, ETAT.Etat]:
    with sessions() as db:
        artefact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
        return artefact, ETAT.lire(artefact)


def _poser_echeance(sessions, artifact_id: str, *, minutes: int) -> None:
    from datetime import timedelta

    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artifact_id).update(
            {"ingest_next_attempt_at": ETAT.utc_naif() + timedelta(minutes=minutes)},
            synchronize_session=False,
        )
        db.commit()


# ---------------------------------------------------------------------------
# 2.1 — indisponibilité du modèle : dette, pas échec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cause",
    [
        "worker PaddleOCR-VL : python introuvable (/usr/bin/python) : introuvable",
        "worker PaddleOCR-VL : timeout de chargement (>300s)",
        "worker PaddleOCR-VL : timeout d'inférence",
    ],
)
def test_modele_indisponible_laisse_depose_et_non_echec(base, monkeypatch, cause):
    sessions, racine = base
    artefact = _creer(sessions, racine)

    def indisponible(*args, **kwargs):
        raise ModeleIndisponible(cause)

    monkeypatch.setattr(attachment_ingest, "extraire_texte", indisponible)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE, (
        "une panne de modèle n'est pas un échec du fichier"
    )
    assert "indisponible" in lu.raison
    assert lu.est_reprenable is True
    # La tentative est ANNULÉE : une indisponibilité d'infrastructure n'est pas
    # une exécution imputable au fichier. `attempts` ne compte donc que les
    # exécutions réelles, et revient à zéro ici.
    assert brut.ingest_attempts == 0
    assert brut.ingest_next_attempt_at is not None


def test_modele_indisponible_au_plafond_ne_terminalise_pas(base, monkeypatch):
    """N indisponibilités du modèle ne rendent jamais un fichier sain `echec`.

    Le compteur est posé à `PLAFOND_ESSAIS - 1` : une tentative de contenu
    atteindrait le plafond. Une indisponibilité d'infrastructure, elle, est
    annulée ; l'état doit rester `depose` et l'échéance rester planifiée.
    """
    sessions, racine = base
    artefact = _creer(sessions, racine)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {"ingest_attempts": ETAT.PLAFOND_ESSAIS - 1}, synchronize_session=False
        )
        db.commit()
    monkeypatch.setattr(
        attachment_ingest,
        "extraire_texte",
        lambda *a, **k: (_ for _ in ()).throw(ModeleIndisponible("spawn KO")),
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE, (
        "une panne d'infrastructure ne doit jamais terminaliser un fichier sain"
    )
    assert lu.est_terminal is False
    assert brut.ingest_attempts == ETAT.PLAFOND_ESSAIS - 1
    assert brut.ingest_next_attempt_at is not None


# ---------------------------------------------------------------------------
# 2.3 — PDF : aucune page exploitable / pages partielles
# ---------------------------------------------------------------------------


def _markdown_pdf(pages: list[str], *, pages_attendues: int) -> str:
    corps = "\n\n".join(
        f"## Page {index + 1}\n\n{texte}" for index, texte in enumerate(pages)
    )
    vides = sum(1 for texte in pages if not texte.strip())
    complement = f" pages_vides={vides}" if vides else ""
    couverture = (
        f"<!-- thot:couverture chaine=ocr pages_attendues={pages_attendues} "
        f"pages_traitees={len(pages)}{complement} -->"
    )
    return f"{couverture}\n\n{corps}"


def test_pdf_sans_page_reconnue_reste_depose_et_rejouable(base, monkeypatch):
    sessions, racine = base
    artefact = _creer(sessions, racine)
    markdown = _markdown_pdf(
        [
            "(page 1 non reconnue — ModeleIndisponible: timeout)",
            "(page 2 non reconnue — ModeleIndisponible: timeout)",
        ],
        pages_attendues=2,
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE
    assert "aucune page exploitable" in lu.raison
    assert brut.ingest_next_attempt_at is not None


def test_pdf_partiel_est_partiel_et_rejoue(base, monkeypatch):
    sessions, racine = base
    artefact = _creer(sessions, racine)
    texte_utile = (
        "Contrat de prestation, montant total 1200 euros, signataire Atelier "
        "Beziers, execution verifiee le 12 mars 2026. " * 4
    )
    markdown = _markdown_pdf(
        [texte_utile, "(page 2 non reconnue — OCR KO)"],
        pages_attendues=2,
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_PARTIEL
    assert lu.a_texte is True, "le texte partiel reste servi"
    assert brut.ingest_next_attempt_at is not None
    assert lu.etat not in ETAT.ETATS_TERMINAUX
    assert lu.etat not in ETAT.ETATS_SANS_TEXTE

    # L'observation réussie porte le texte : il n'est pas caché.
    db = sessions()
    try:
        texte, _ = ETAT.texte_ingeste(
            db, db.query(Artifact).filter(Artifact.id == artefact).one()
        )
    finally:
        db.close()
    assert texte and "Contrat" in texte

    # L'échéance est due : la passe suivante le reprend.
    _poser_echeance(sessions, artefact, minutes=-1)
    vus: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest,
        "lancer_ingestion",
        lambda ids, **kw: vus.append(list(ids)),
    )
    bilan = reprise.balayer_reprise()
    assert artefact in bilan["repris"]
    assert vus == [[artefact]]


# ---------------------------------------------------------------------------
# 2.2 — backoff, reset, plafond
# ---------------------------------------------------------------------------


def test_une_echeance_future_n_est_pas_reprise(base, monkeypatch):
    sessions, racine = base
    artefact = _creer(sessions, racine)
    _poser_echeance(sessions, artefact, minutes=60)
    appels: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest,
        "lancer_ingestion",
        lambda ids, **kw: appels.append(list(ids)),
    )

    bilan = reprise.balayer_reprise()

    assert bilan["eligibles"] == 0
    assert bilan["repris"] == []
    assert appels == []


def test_un_succes_remet_les_essais_a_zero(base, monkeypatch):
    sessions, racine = base
    artefact = _creer(sessions, racine)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {"ingest_attempts": 3}, synchronize_session=False
        )
        db.commit()
    _poser_echeance(sessions, artefact, minutes=30)
    texte = (
        "Compte rendu complet de la reunion, decisions prises et responsables "
        "nommes, avec un contenu suffisamment long pour etre indexe. " * 4
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (texte, "native")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_EXTRAIT
    assert brut.ingest_attempts == 0
    assert brut.ingest_next_attempt_at is None


def test_le_plafond_termine_la_dette_avec_un_motif(base, monkeypatch):
    sessions, racine = base
    artefact = _creer(sessions, racine)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {"ingest_attempts": ETAT.PLAFOND_ESSAIS - 1}, synchronize_session=False
        )
        db.commit()
    # Une PANNE DE CONTENU (toutes les pages non reconnues) reste imputable au
    # fichier : c'est elle qui doit atteindre le plafond et devenir `echec`.
    # `ModeleIndisponible`, lui, est annulé et ne doit jamais terminaliser.
    markdown = _markdown_pdf(["(page 1 non reconnue — OCR KO)"], pages_attendues=1)
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_ECHEC
    assert "plafond d'essais atteint" in lu.raison
    assert lu.est_terminal is True
    assert brut.ingest_next_attempt_at is None


def test_le_plafond_d_un_partiel_donne_tronque(base, monkeypatch):
    sessions, racine = base
    artefact = _creer(sessions, racine)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {"ingest_attempts": ETAT.PLAFOND_ESSAIS - 1}, synchronize_session=False
        )
        db.commit()
    markdown = _markdown_pdf(
        [
            "Texte utile " * 40,
            "(page 2 non reconnue — OCR KO)",
        ],
        pages_attendues=2,
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    _, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_TRONQUE
    assert "plafond d'essais atteint" in lu.raison
    assert lu.est_terminal is True


# ---------------------------------------------------------------------------
# 2.4 — reprise périodique
# ---------------------------------------------------------------------------


def _arreter_timer() -> None:
    reprise.arreter_reprise_periodique()


def test_le_timer_appelle_avec_modeles_autorises_et_debit_borne(monkeypatch):
    _arreter_timer()
    monkeypatch.delenv("THOT_DISABLE_INGEST_REPRISE", raising=False)
    monkeypatch.setattr(reprise, "INTERVALLE_REPRISE_PERIODIQUE_S", 0.01)
    appels: list[dict] = []

    def faux_balayage(**kwargs):
        appels.append(kwargs)
        return {"ok": True, "repris": []}

    monkeypatch.setattr(reprise, "balayer_reprise", faux_balayage)
    try:
        demarrage = reprise.demarrer_reprise_periodique(est_pret=lambda: True)
        assert demarrage["statut"] == "demarre"
        deadline = time.time() + 2
        while not appels and time.time() < deadline:
            time.sleep(0.01)
    finally:
        _arreter_timer()

    assert appels, "le timer doit appeler la reprise"
    assert all(a["modeles_autorises"] is True for a in appels)
    assert all(a["limite"] == reprise.LIMITE_REPRISE_PERIODIQUE for a in appels)
    assert reprise.LIMITE_REPRISE_PERIODIQUE == 5


def test_le_timer_ne_fait_rien_si_tour_actif_et_ne_leve_pas(monkeypatch):
    _arreter_timer()
    monkeypatch.delenv("THOT_DISABLE_INGEST_REPRISE", raising=False)
    monkeypatch.setattr(reprise, "INTERVALLE_REPRISE_PERIODIQUE_S", 0.01)
    from backend.services import activity

    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    appels: list[dict] = []
    monkeypatch.setattr(reprise, "balayer_reprise", lambda **kw: appels.append(kw))
    try:
        reprise.demarrer_reprise_periodique(est_pret=lambda: True)
        time.sleep(0.1)
    finally:
        _arreter_timer()

    assert appels == []


def test_le_timer_attend_un_manager_pret(monkeypatch):
    _arreter_timer()
    monkeypatch.delenv("THOT_DISABLE_INGEST_REPRISE", raising=False)
    monkeypatch.setattr(reprise, "INTERVALLE_REPRISE_PERIODIQUE_S", 0.01)
    appels: list[dict] = []
    monkeypatch.setattr(reprise, "balayer_reprise", lambda **kw: appels.append(kw))
    try:
        reprise.demarrer_reprise_periodique(est_pret=lambda: False)
        time.sleep(0.1)
    finally:
        _arreter_timer()

    assert appels == []


def test_le_timer_se_desactive_par_variable(monkeypatch):
    _arreter_timer()
    monkeypatch.setenv("THOT_DISABLE_INGEST_REPRISE", "1")
    bilan = reprise.demarrer_reprise_periodique(est_pret=lambda: True)
    assert bilan["statut"] == "desactive"


# ---------------------------------------------------------------------------
# 2.5 — heartbeat et zombie en_cours
# ---------------------------------------------------------------------------


def test_un_en_cours_au_battement_perime_est_libere_et_rejoue(base, monkeypatch):
    from datetime import timedelta

    sessions, racine = base
    artefact = _creer(sessions, racine, etat_initial=ETAT.ETAT_EN_COURS)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {
                "ingest_updated_at": ETAT.utc_naif() - timedelta(hours=2),
                "ingest_heartbeat_at": ETAT.utc_naif() - timedelta(minutes=20),
            },
            synchronize_session=False,
        )
        db.commit()
    with attachment_ingest._verrou:
        attachment_ingest._en_cours[artefact] = "proprietaire-test"

    vus: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest,
        "lancer_ingestion",
        lambda ids, **kw: vus.append(list(ids)),
    )
    bilan = reprise.balayer_reprise()

    assert artefact in bilan["repris"]
    assert vus == [[artefact]]
    with attachment_ingest._verrou:
        assert artefact not in attachment_ingest._en_cours


def test_un_battement_frais_prote_meme_si_l_etat_est_vieux(base):
    from datetime import timedelta

    sessions, racine = base
    artefact = _creer(sessions, racine, etat_initial=ETAT.ETAT_EN_COURS)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {
                "ingest_updated_at": ETAT.utc_naif() - timedelta(hours=2),
                "ingest_heartbeat_at": ETAT.utc_naif(),
            },
            synchronize_session=False,
        )
        db.commit()

    bilan = reprise.balayer_reprise()

    assert artefact not in bilan["repris"]
    assert [m["artifact_id"] for m in bilan["ignores"]] == [artefact]


def test_cinquante_fichiers_ne_sont_pas_cinquante_en_cours(base, monkeypatch):
    """Phase 2 partielle : seul le worker écrit `en_cours`, pas le listage.

    Le pool borné qui empêcherait 50 fils simultanés est la Phase 3 (tâche 3.1).
    Ici, on verrouille déjà que la PASSE de reprise ne déclare personne
    `en_cours` : elle propose, elle ne travaille pas.
    """
    sessions, racine = base
    file_ids = [_creer(sessions, racine) for _ in range(50)]
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids, **kw: None)

    bilan = reprise.balayer_reprise(limite=50)

    assert len(bilan["repris"]) == 50
    with sessions() as db:
        en_cours = (
            db.query(Artifact)
            .filter(Artifact.ingest_state == ETAT.ETAT_EN_COURS)
            .count()
        )
    assert en_cours == 0, "aucun fichier n'est déclaré en cours par le listage"


# ---------------------------------------------------------------------------
# Revue Phase 2 — refus consenti, couverture vide, pannes worker, heartbeat
# ---------------------------------------------------------------------------


def test_un_refus_de_modele_repete_ne_consomme_pas_le_plafond(base):
    """Six refus consentis : toujours `depose`, `attempts` inchangé.

    Le défaut : chaque scan de démarrage incrémentait le compteur avant de
    rencontrer `ModeleRequis`. Après cinq lancements, une image jamais tentée
    devenait `echec` terminal « plafond d'essais atteint ».
    """
    sessions, racine = base
    artefact = _creer(
        sessions,
        racine,
        kind="image",
        contenu=b"\x89PNG\r\n\x1a\n" + b"\0" * 64,
    )

    for _ in range(6):
        attachment_ingest._ingérer_un(artefact, modeles_autorises=False)
        brut, lu = _lire(sessions, artefact)
        assert lu.etat == ETAT.ETAT_DEPOSE
        assert lu.est_reprenable is True
        assert "modèle" in lu.raison
        assert brut.ingest_attempts == 0, (
            "un refus consenti n'est pas une exécution réelle"
        )


def test_un_pdf_de_zero_page_reste_depose(base, monkeypatch):
    """Un document sans page n'a rien à indexer : dette, pas succès `extrait`."""
    sessions, racine = base
    artefact = _creer(sessions, racine)
    markdown = _markdown_pdf([], pages_attendues=0)
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    _, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE
    assert "aucune page exploitable" in lu.raison


def test_un_pdf_dont_toutes_les_pages_sont_vides_reste_depose(base, monkeypatch):
    """Toutes les pages vides : aucune mention de panne, mais aucun contenu.

    L'ancienne condition exigeait une page « non reconnue ». Un PDF dont l'OCR
    rend des pages vides passait donc au chemin de succès et devenait `extrait`.
    """
    sessions, racine = base
    artefact = _creer(sessions, racine)
    markdown = _markdown_pdf(["", "   ", ""], pages_attendues=3)
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    _, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE
    assert "aucune page exploitable" in lu.raison


class _FauxWorker:
    """Worker minimal pour éprouver la classification des pannes d'échange."""

    def __init__(self, *, stdout: io.StringIO, stdin=None, poll=None) -> None:
        self.stdout = stdout
        self.stdin = stdin if stdin is not None else io.StringIO()
        self._poll = poll

    def poll(self):
        return self._poll

    def terminate(self) -> None:  # `_arreter` l'appelle si le process vit
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        pass


class _StdinEcritureKO:
    def write(self, _contenu: str) -> int:
        raise OSError("broken pipe")

    def flush(self) -> None:
        pass


@pytest.mark.parametrize("panne", ["ecriture", "crash", "stdout"])
def test_une_panne_du_worker_est_une_indisponibilite(monkeypatch, panne):
    """Écriture impossible, crash et stdout fermé ne sont pas des fautes du fichier."""
    import select as select_module

    if panne == "ecriture":
        worker = _FauxWorker(stdout=io.StringIO(""), stdin=_StdinEcritureKO())
        monkeypatch.setattr(select_module, "select", lambda *a, **k: ([], [], []))
    elif panne == "crash":
        worker = _FauxWorker(stdout=io.StringIO(""), poll=0)
        monkeypatch.setattr(select_module, "select", lambda *a, **k: ([], [], []))
    else:
        worker = _FauxWorker(stdout=io.StringIO(""), poll=None)
        monkeypatch.setattr(
            select_module,
            "select",
            lambda rlist, wlist, xlist, timeout: (rlist, [], []),
        )

    monkeypatch.setattr(PC, "_ensure", lambda: worker)
    monkeypatch.setattr(PC, "_proc", worker)

    with pytest.raises(ModeleIndisponible):
        PC._envoyer_locked({"op": "parse"}, timeout_s=1)


def test_liberer_respecte_un_battement_recent_et_libere_un_ancien(base):
    from datetime import timedelta

    sessions, racine = base
    recent = _creer(sessions, racine, etat_initial=ETAT.ETAT_EN_COURS)
    ancien = _creer(sessions, racine, etat_initial=ETAT.ETAT_EN_COURS)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == recent).update(
            {
                "ingest_updated_at": ETAT.utc_naif() - timedelta(hours=2),
                "ingest_heartbeat_at": ETAT.utc_naif(),
            },
            synchronize_session=False,
        )
        db.query(Artifact).filter(Artifact.id == ancien).update(
            {
                "ingest_updated_at": ETAT.utc_naif() - timedelta(hours=2),
                "ingest_heartbeat_at": ETAT.utc_naif() - timedelta(minutes=20),
            },
            synchronize_session=False,
        )
        db.commit()
    with attachment_ingest._verrou:
        attachment_ingest._en_cours[recent] = "recent"
        attachment_ingest._en_cours[ancien] = "ancien"

    liberes = attachment_ingest.liberer_en_cours_expires([recent, ancien], seuil_s=900)

    assert liberes == [ancien]
    with attachment_ingest._verrou:
        assert recent in attachment_ingest._en_cours
        assert ancien not in attachment_ingest._en_cours


def test_le_plafond_efface_une_echeance_anterieure(base, monkeypatch):
    """Au plafond, `next_attempt_at` est NULL même si une date future existait."""
    sessions, racine = base
    artefact = _creer(sessions, racine)
    with sessions() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {
                "ingest_attempts": ETAT.PLAFOND_ESSAIS - 1,
                "ingest_next_attempt_at": ETAT.utc_naif(),
            },
            synchronize_session=False,
        )
        db.commit()
    markdown = _markdown_pdf(["(page 1 non reconnue — OCR KO)"], pages_attendues=1)
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (markdown, "ocr")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_ECHEC
    assert brut.ingest_next_attempt_at is None


def test_le_backoff_vaut_1_2_5_15_30_minutes(base):
    from datetime import timedelta

    sessions, racine = base
    artefact = _creer(sessions, racine)
    maintenant = ETAT.utc_naif()

    obtenus: list[int] = []
    for tentatives in (1, 2, 3, 4):
        with sessions() as db:
            prochaine = ETAT.planifier_tentative(
                db, artefact, tentatives, maintenant=maintenant
            )
        assert prochaine is not None
        obtenus.append(round((prochaine - maintenant) / timedelta(minutes=1)))

    assert obtenus == [1, 2, 5, 15]
    assert [ETAT.delai_backoff(index) for index in range(5)] == [1, 2, 5, 15, 30]
    with sessions() as db:
        assert (
            ETAT.planifier_tentative(
                db, artefact, ETAT.PLAFOND_ESSAIS, maintenant=maintenant
            )
            is None
        )


def test_une_panne_worker_laisse_la_dette_rejouable(base, monkeypatch):
    """Un crash worker n'est pas une faute de contenu : `depose`, pas `echec`."""
    sessions, racine = base
    artefact = _creer(sessions, racine)
    monkeypatch.setattr(
        attachment_ingest,
        "extraire_texte",
        lambda *a, **k: (_ for _ in ()).throw(ModeleIndisponible("stdout fermé")),
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    brut, lu = _lire(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE
    assert lu.est_reprenable is True
    # Le worker n'a pas exécuté de travail imputable au fichier : la panne est
    # côté infrastructure, donc la tentative est annulée et ne compte pas.
    assert brut.ingest_attempts == 0
    assert brut.ingest_next_attempt_at is not None


def test_deux_extractions_directes_simultanees_sont_bloquees(base, monkeypatch):
    """Le chemin synchrone ne contourne plus `_en_cours`."""
    sessions, racine = base
    artefact = _creer(sessions, racine)
    appels: list[int] = []
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: appels.append(1)
    )
    with attachment_ingest._verrou:
        attachment_ingest._en_cours[artefact] = "autre-proprietaire"

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert appels == [], "un second lancement direct a exécuté l'extraction"
    with attachment_ingest._verrou:
        assert attachment_ingest._en_cours.get(artefact) == "autre-proprietaire"

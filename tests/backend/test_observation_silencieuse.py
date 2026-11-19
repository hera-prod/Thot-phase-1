"""Un échec d'observation ne doit plus être avalé : il doit être NOMMÉ.

Le défaut que ces tests verrouillent est structurel, pas ponctuel.
`record_tool_observation` promettait « ne lève jamais » et rendait `None` sur
tout échec ; ses deux appelants principaux enchaînaient `except Exception: pass`.
Le résultat : un dépôt pouvait « réussir » — le fichier était visible, l'ID
existait — SANS qu'aucune observation n'ait été écrite, donc sans fiche, sans
fragments et sans ligne d'index. Rien ne distinguait ce dépôt d'un dépôt
correctement traité, ni pour l'utilisateur, ni pour l'opérateur, ni dans les
logs.

Il y avait même pire qu'un silence : `read_document` renvoyait un `evidence_id`
construit à partir de l'observation. Si l'écriture échouait, cet identifiant ne
désignait RIEN en base — et la compaction rejette ensuite les citations
d'observations inexistantes. Le `pass` ne perdait pas une trace : il
fabriquait une preuve.

CE QUE CES TESTS VERROUILLENT, et le contrat qu'ils établissent :

  - `record_tool_observation` LÈVE (`ObservationNonÉcrite`) quand la ligne n'a
    pas pu être écrite. Le contrat « ne lève jamais » est expressément retiré :
    il rendait l'échec INDISTINGUABLE d'une absence, donc inutilisable. Le tour
    ne meurt pas pour autant — `loop._record_observation` intercepte, journalise
    et rend `None`, et c'est ce test qui l'atteste ;
  - `None` ne signifie plus « échec » mais « rien à écrire » : ni identifiant
    d'artefact, ni artefact inconnu ;
  - un échec laisse la session RENDUE et la transaction abandonnée : on ne rend
    pas la main dans un état que personne n'a décrit ;
  - `read_document` ne rend plus un extrait « réussi » dont la preuve est
    absente ;
  - une extraction réussie dont l'observation ne s'écrit pas remonte, et n'est
    JAMAIS consignée comme un échec d'extraction — ce serait un second
    mensonge, plus faux encore que le premier ;
  - le chemin du tour journalise au lieu de taire.

Base neuve et jetable par test. L'observation est écrite par les MODÈLES, via
les vraies requêtes ; la panne est obtenue en rendant la seule écriture
d'observation impossible, et non en faisant lever une exception dans la
fonction testée.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    Observation,
)
from backend.ingestion import attachment_ingest
from backend.ingestion import paddle_client
from backend.ipc import attachments as A
from backend.ipc import paths_bridge as paths_bridge_module
from backend.search import observations as O
from backend.tools.base import ToolContext
from backend.tools.read_document import ReadDocumentTool

TEXTE = ("Rapport de maintenance. Client : Atelier Beziers.\n"
         "Montant : 4 250,00 euros. Reference : AX-7742-B.\n") * 3


class SessionSansÉcritureDObservation:
    """Une session qui LIT et écrit l'état, mais refuse d'écrire une OBSERVATION.

    C'est l'état d'une base qui accepte l'acquisition du claim et le relevé
    d'état, mais dont l'insertion d'observation échoue (disque plein,
    permissions, fichier verrouillé). On éprouve ainsi l'échec D'ÉCRITURE DE
    L'OBSERVATION sans faire lever une exception dans la fonction testée.

    POURQUOI PAS TOUS LES COMMITS : la Phase 3 acquiert le bail d'ingestion
    par un `commit` AVANT toute extraction (`bail.acquérir_bail`). Une session
    qui refusait tous les commits faisait échouer l'acquisition du claim, et
    `_ingérer_un` sortait avant d'atteindre l'observation : les tests
    d'ingestion mesuraient la panne du claim, pas le scénario visé. Le refus
    est donc ciblé sur la transaction qui porte une Observation en attente ;
    les commits du claim et de l'état restent réels, comme ils le sont dans
    une base où seule l'insertion d'observation est empêchée.

    Elle note aussi ce qu'on lui demande de faire, pour prouver qu'un échec ne
    laisse ni transaction ouverte ni session ouverte.
    """

    def __init__(self, reelle, journal: list[str]) -> None:
        self._reelle = reelle
        self._journal = journal

    def __getattr__(self, nom):
        return getattr(self._reelle, nom)

    def _refuse_l_observation(self) -> bool:
        """La transaction en cours porte-t-elle une Observation à insérer ?"""
        return any(isinstance(objet, Observation) for objet in self._reelle.new)

    def flush(self, *args, **kwargs):
        if self._refuse_l_observation():
            self._journal.append("flush")
            raise OperationalError("INSERT", {}, Exception("disque plein"))
        return self._reelle.flush(*args, **kwargs)

    def commit(self) -> None:
        self._journal.append("commit")
        if self._refuse_l_observation():
            raise OperationalError("COMMIT", {}, Exception("disque plein"))
        self._reelle.commit()

    def rollback(self) -> None:
        self._journal.append("rollback")
        self._reelle.rollback()

    def close(self) -> None:
        self._journal.append("close")
        self._reelle.close()


# ── Montage ───────────────────────────────────────────────────────────────────

@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Base neuve, index FTS, tables de fragments, racines de données jetables.

    Rend (sessions, journal) : le journal reçoit les opérations de session
    demandées pendant l'épreuve de l'échec.
    """
    moteur = create_engine(f"sqlite:///{tmp_path / 'observation.db'}")
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts
    from backend.search.chunks import ensure_chunk_tables

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "app"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False)
    monkeypatch.setattr(paths_bridge_module, "temp_root", lambda: racine / "temp", raising=False)
    monkeypatch.setattr(
        paths_bridge_module, "data_dirs", lambda: (racine, racine / "temp"), raising=False
    )
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None, raising=False)
    with _session(sessions) as db:
        ensure_chunk_tables(db)
        db.commit()
    journal: list[str] = []
    return sessions, journal


@pytest.fixture()
def ecriture_cassee(base, monkeypatch):
    """Rend l'écriture de L'OBSERVATION impossible, le reste intact.

    Le claim et le relevé d'état doivent continuer d'écrire : sans cela,
    `_ingérer_un` s'arrête à l'acquisition du bail et les tests d'ingestion ne
    voient jamais le chemin qu'ils prétendent éprouver.
    """
    sessions, journal = base
    monkeypatch.setattr(
        database_module, "SessionLocal",
        lambda: SessionSansÉcritureDObservation(sessions(), journal),
    )
    return journal


def _deposer_texte(sessions, contenu: str = TEXTE, nom: str = "rapport.txt") -> tuple[str, str]:
    """Un artefact `text` avec son blob SUR LE DISQUE.

    `text` plutôt qu'un autre type parce que c'est celui que `read_payload` sait
    lire sans OCR : le test porte sur l'écriture de l'observation, pas sur la
    capacité d'extraire. Le dépôt, lui, passe par le vrai chemin — pour les
    images de la section 3.
    """
    from backend.config.paths import get_data_dir

    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    artefact_id = f"art_{uuid.uuid4().hex[:12]}"
    blob_id = f"blob_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.add(Conversation(id=conversation, title="observation", mode="chat"))
        db.add(Artifact(id=artefact_id, blob_id=blob_id, kind="text",
                        original_filename=nom, display_name=nom))
        db.add(Blob(id=blob_id, sha256=blob_id * 4,
                    relative_path=f"blobs/{blob_id[:2]}/{blob_id}.bin",
                    size_bytes=len(contenu.encode("utf-8")), mime_detected="text/plain",
                    state="ready"))
        db.add(ArtifactLink(id=f"lnk_{blob_id}", artifact_id=artefact_id,
                            scope_kind="conversation", scope_id=conversation, role="attachment"))
        db.commit()
    fichier = get_data_dir() / f"blobs/{blob_id[:2]}/{blob_id}.bin"
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(contenu, encoding="utf-8")
    return conversation, artefact_id


def _deposer_image(sessions) -> str:
    """Une image déposée par le VRAI chemin de dépôt. Renvoie son id."""
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (60, 40), (240, 240, 240)).save(tampon, format="PNG")
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.add(Conversation(id=conversation, title="ingestion", mode="chat"))
        db.commit()
        item = A.stage_picker_bytes(db, {"requestId": "obs-silencieuse",
                                         "filename": "photo.png",
                                         "bytes": tampon.getvalue()})["items"][0]
        (artefact,) = A.attachments_prepare(
            db, {"conversationId": conversation, "stageItemIds": [item["stage_item_id"]],
                 "names": ["photo.png"]}
        )["artifact_ids"]
    return artefact


def _observations(sessions, artefact: str) -> list[Observation]:
    with _session(sessions) as db:
        return db.query(Observation).filter(Observation.artifact_id == artefact).all()


# ══════════════════════════════════════════════════════════════════════════════
# 1. LE CONTRAT DE `record_tool_observation`
# ══════════════════════════════════════════════════════════════════════════════

class TestContratObservation:

    def test_le_contrat_ne_plus_est_ne_leve_jamais(self, base, ecriture_cassee):
        """LE CONTRAT CHANGE, ET IL EST ÉCRIT ICI.

        Aucun test de la suite n'affirmait l'ancien « ne lève jamais » : ce
        contrat ne vivait que dans un docstring — et dans une mention de
        `backend/services/web/cache.py`, qui le décrit encore et demandera une
        mise à jour. Ce test le remplace, et dit pourquoi : un `None`
        INDISTINGUABLE d'une absence rendait l'appelant incapable de faire quoi
        que ce soit de l'échec, donc l'avalait.
        """
        sessions, _ = base
        _, artefact = _deposer_texte(sessions)
        with pytest.raises(O.ObservationNonÉcrite) as echec:
            O.record_tool_observation(artefact, "doc_ocr", "montant", TEXTE, True)
        message = str(echec.value)
        assert artefact in message, "l'artefact doit être nommé : on ne sait pas ce qui a été perdu"
        assert "doc_ocr" in message, "l'outil doit être nommé"
        assert not _observations(sessions, artefact), (
            "l'observation doit être absente, pas à moitié écrite"
        )

    def test_un_echec_ne_laisse_ni_transaction_ni_session(self, base, ecriture_cassee):
        """Rendre la main dans une transaction en suspens, c'est décider d'un
        état que personne n'a décrit. L'appelant doit trouver une session rendue
        et une transaction abandonnée."""
        sessions, journal = base
        _, artefact = _deposer_texte(sessions)
        with pytest.raises(O.ObservationNonÉcrite):
            O.record_tool_observation(artefact, "doc_ocr", "montant", TEXTE, True)
        assert "rollback" in journal, "la transaction doit être abandonnée explicitement"
        assert journal[-1] == "close", "la session doit être rendue, et en dernier"

    def test_une_base_qui_ne_s_ouvre_pas_est_un_echec_d_ecriture(self, base, monkeypatch):
        """Ouvrir la session fait partie de l'écriture. Une base verrouillée
        n'est donc pas une « absence d'observation » : c'est une observation
        PERDUE. Les deux doivent se traiter pareil, sinon l'appelant se voit
        demander de distinguer deux pannes qu'il ne peut pas traiter
        différemment."""
        sessions, _ = base

        def _factory():
            raise OperationalError("connect", {}, Exception("fichier verrouillé"))

        monkeypatch.setattr(database_module, "SessionLocal", _factory)
        with pytest.raises(O.ObservationNonÉcrite) as echec:
            O.record_tool_observation("art_x", "doc_ocr", "q", "texte", True)
        assert "art_x" in str(echec.value)

    def test_none_signifie_rien_a_ecrire_et_non_echec(self, base):
        """Les DEUX cas où l'on n'a rien à écrire. Ni l'un ni l'autre ne lève :
        ce ne sont pas des échecs, et les confondre fit perdre la seule chose
        qui distingue l'échec — sa trace."""
        sessions, _ = base
        assert O.record_tool_observation("", "doc_ocr", "q", "texte", True) is None
        assert O.record_tool_observation("art_inexistant", "doc_ocr", "q", "texte", True) is None

    def test_un_echec_d_enrichissement_ne_bouche_pas_l_observation(self, base, monkeypatch):
        """La frontière du nouveau contrat : la FICHE et les FRAGMENTS sont
        dérivés et rejouables, la LIGNE D'OBSERVATION est la preuve. Une
        réindexation cassée ne doit donc pas faire perdre l'extrait."""
        sessions, _ = base
        _, artefact = _deposer_texte(sessions)

        def _carte_cassee(*args, **kwargs):
            raise RuntimeError("index FTS indisponible")

        monkeypatch.setattr("backend.search.cards.revise_card_after_observation", _carte_cassee)
        observation = O.record_tool_observation(artefact, "doc_ocr", "montant", TEXTE, True)
        assert observation is not None and observation.status == "completed"
        assert len(_observations(sessions, artefact)) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 2. `read_document` — l'extrait sans preuve n'est pas une lecture réussie
# ══════════════════════════════════════════════════════════════════════════════

class TestReadDocument:

    def _appeler(self, conversation: str, artefact: str):
        outil = ReadDocumentTool()
        ctx = ToolContext(run_id="r1", request_id=conversation, conv_id=conversation)
        return asyncio.run(outil.call(
            {"artifact_id": artefact, "purpose": "lire le rapport", "max_chars": 2000}, ctx))

    def test_une_lecture_qui_reussit_trace_son_observation(self, base):
        """Le cas témoin : tout va bien, l'observation existe et l'outil rend
        l'extrait. Sans ce témoin, le test d'échec pourrait être une suite qui
        n'écrit jamais rien."""
        sessions, _ = base
        conversation, artefact = _deposer_texte(sessions)
        resultat = self._appeler(conversation, artefact)
        assert resultat.ok, resultat.error
        lignes = _observations(sessions, artefact)
        assert len(lignes) == 1 and lignes[0].tool_name == "read_document"

    def test_read_document_ne_ravale_plus_l_echec_d_observation(self, base, ecriture_cassee):
        """LE DÉFAUT. `except Exception: pass` rendait un extrait « lu » alors
        que rien n'était écrit : l'outil annonçait une lecture, la recherche ne
        trouvait rien, et l'`evidence_id` renvoyé ne désignait aucune ligne."""
        sessions, _ = base
        conversation, artefact = _deposer_texte(sessions)
        resultat = self._appeler(conversation, artefact)
        assert not resultat.ok, "un extrait sans observation ne doit pas être rendu réussi"
        assert "observation" in (resultat.error or "").lower(), (
            f"la cause doit être nommée : {resultat.error!r}"
        )
        assert not _observations(sessions, artefact)

    def test_un_none_n_est_pas_un_succes(self, base, monkeypatch):
        """`None` signifie « rien à écrire ». Ici l'extrait existe et son
        artefact vient d'être lu : c'est donc impossible, et l'impossible ne se
        rend pas comme une réussite."""
        sessions, _ = base
        conversation, artefact = _deposer_texte(sessions)
        monkeypatch.setattr(O, "record_tool_observation", lambda *a, **kw: None)
        resultat = self._appeler(conversation, artefact)
        assert not resultat.ok
        assert "observation" in (resultat.error or "").lower()


# ══════════════════════════════════════════════════════════════════════════════
# 3. L'ingestion — un texte extrait ne se perd pas en silence
# ══════════════════════════════════════════════════════════════════════════════

class TestIngestion:

    @pytest.fixture()
    def transcription(self, monkeypatch):
        """Un modèle de substitution : il rend un texte, sans GPU ni poids."""
        monkeypatch.setattr(paddle_client, "parse_png", lambda octets, **kw: TEXTE)
        return TEXTE

    def test_une_extraction_reussie_sans_observation_remonte(
        self, base, ecriture_cassee, transcription
    ):
        """L'extraction a marché, l'observation non. `_ingérer_un` laisse
        remonter : c'est à `lancer_ingestion` de journaliser, et à personne
        d'inventer un résultat."""
        sessions, _ = base
        artefact = _deposer_image(sessions)
        with pytest.raises(O.ObservationNonÉcrite):
            attachment_ingest._ingérer_un(artefact)

    def test_une_extraction_reussie_n_est_jamais_consignee_comme_echec(
        self, base, ecriture_cassee, transcription
    ):
        """LE PIÈGE DU `try/except` LARGE.

        Si l'écriture de l'observation échoue APRÈS une extraction réussie, un
        `except` trop large qui enregistre « extraction impossible » accuserait
        l'OCR d'une panne de base — et tracerait un `failed` qui n'est pas la
        vérité. Ici, la cause est celle qu'elle est, et rien n'est écrit.
        """
        sessions, _ = base
        artefact = _deposer_image(sessions)
        with pytest.raises(O.ObservationNonÉcrite):
            attachment_ingest._ingérer_un(artefact)
        assert not _observations(sessions, artefact), (
            "une extraction réussie ne doit pas être tracée comme un échec d'extraction"
        )

    def test_un_echec_dextraction_sans_traceur_remonte_egalement(self, base, ecriture_cassee):
        """Extraction impossible ET observation non inscriptible : la cause
        n'a pas d'observation où être consignée. Elle est alors remontée — une
        pile tracée vaut mieux qu'une absence totale, et surtout mieux qu'un
        dépôt « réussi »."""
        sessions, _ = base
        _, artefact = _deposer_texte(sessions)
        with _session(sessions) as db:
            db.execute(sql_text("UPDATE blobs SET relative_path = 'blobs/absent.bin'"))
            db.commit()
        with pytest.raises(O.ObservationNonÉcrite):
            attachment_ingest._ingérer_un(artefact)
        assert not _observations(sessions, artefact)


# ══════════════════════════════════════════════════════════════════════════════
# 4. LE TOUR — journaliser plutôt que taire
# ══════════════════════════════════════════════════════════════════════════════

class TestTour:

    def test_le_tour_signale_l_echec_d_observation(self, base, ecriture_cassee, capsys):
        """Le chemin du tour ne meurt pas — il ne se TAIT pas.

        `loop._record_observation` attrape l'exception et journalise ; c'est ce
        qui remplace le `None` silencieux. Le test verrouille les deux moitiés :
        aucune exception ne sort du tour, ET la cause y est écrite.
        """
        from backend.inference import loop as L

        sessions, _ = base
        _, artefact = _deposer_texte(sessions)
        obs_id = L._record_observation("doc_ocr", artefact, "montant",
                                       {"ok": True, "result": TEXTE},
                                       "run_1", "call_1")
        assert obs_id is None, "aucune observation : l'identifiant ne doit pas être inventé"
        journalise = capsys.readouterr().err
        assert "[observation]" in journalise and "record failed" in journalise, (
            f"le tour doit journaliser la cause : {journalise!r}"
        )

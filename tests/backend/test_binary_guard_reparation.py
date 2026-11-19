"""Réparer un index DÉJÀ pollué, sans le purger pour de faux.

Le garde-fau de `test_binary_guard_indexation.py` empêche le dommage d'exister.
Il ne répare pas celui qui est déjà écrit : sur le corpus de travail, un `.ods` de
122 Ko gardait 16 000 caractères de son propre conteneur ZIP en `payload_ref`, un
aperçu binaire en `artifact_search` et trois fragments binaires en
`artifact_chunks` — le tout sous le statut `completed`.

Ce que ces tests verrouillent :

  - le DÉTECTEUR trouve les trois écritures du même dommage, et les distingue de
    l'incomplet (un PDF tronqué à la borne est partiel, pas faux) ;
  - il ne confond pas un artefact SAIN avec un artefact pollué, et respecte le
    fait que seule la dernière extraction fait foi ;
  - la RÉPARATION ne fait rien par défaut : inspecter un index ne doit pas purger
    une ligne. Appliquer, c'est decisión explicite ;
  - une fois appliquée, elle purge ce qui est faux, marque la fiche `stale` AVEC
    son motif, et laisse les artefacts sains intacts.

Base neuve et jetable par test. `lancer_ingestion` est remplacé par un piège qui
échoue bruyamment : ces tests n'ont pas à déclencher d'extraction, et le proving
que la fonction ne le fait pas de elle-même fait partie du contrat.
"""

from __future__ import annotations

import zipfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Blob,
    Observation,
)
from backend.ingestion import attachment_ingest
from backend.search import fts as Fts
from backend.search.observations import PAYLOAD_CHARS

# ── Fabriques ─────────────────────────────────────────────────────────────────

def extrait_binaire() -> str:
    """Le contenu réellement indexé par l'extraction défectueuse : un en-tête de
    feuille suivi des octets du conteneur ZIP, lus en latin-1."""
    import io

    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr("content.xml", "<office:document-content/>")
    return ("# Feuille 1\n" + tampon.getvalue().decode("latin-1")) * 3


def texte_propre() -> str:
    lignes = ["# Feuille 1", "Date\tClient\tMontant"]
    lignes += [f"2026-03-{i:02d}\tDupont SARL\t{i * 1000}" for i in range(1, 40)]
    return "\n".join(lignes)


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reparation.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))

    def _interdit(*_args, **_kwargs):
        raise AssertionError("lancer_ingestion ne doit pas être déclenché par la réparation")

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", _interdit)
    return sessions


def _artefact(db, artifact_id: str, nom: str, kind: str = "sheet") -> Artifact:
    db.add(Blob(id=f"blob_{artifact_id}", sha256=artifact_id * 8,
                relative_path=f"blobs/{artifact_id}.bin", size_bytes=1024,
                mime_detected="application/octet-stream", state="ready"))
    db.add(Artifact(id=artifact_id, blob_id=f"blob_{artifact_id}", kind=kind,
                    original_filename=nom, display_name=nom))
    db.add(ArtifactLink(id=f"lnk_{artifact_id}", artifact_id=artifact_id,
                        scope_kind="conversation", scope_id="conv_1", role="attachment"))
    db.add(ArtifactCard(id=f"card_{artifact_id}", artifact_id=artifact_id, revision=1, is_current=1,
                        title=nom, summary="Fiche d'import.", keywords_json="[]", entities_json="[]",
                        status="indexed", analysis_status="indexed"))
    db.flush()
    return db.query(Artifact).filter(Artifact.id == artifact_id).one()


def _observation_ingestion(db, artifact: Artifact, contenu: str, suffixe: str = "") -> Observation:
    """Une observation d'ingestion écrite comme le faisait le défaut : statut
    `completed`, contenu de référence plein. Rien ici ne dit que c'est faux —
    c'est justement le problème."""
    obs = Observation(id=f"obs_{artifact.id}{suffixe}", artifact_id=artifact.id,
                      tool_name=Fts.OUTIL_INGESTION, tool_version="phase1",
                      input_fingerprint=f"fp_{artifact.id}{suffixe}",
                      status="completed", result_json='{"truncated": false}',
                      payload_ref=contenu)
    db.add(obs)
    db.flush()
    return obs


def _fragments(db, artifact: Artifact, morceaux: list[str]) -> None:
    for rang, texte in enumerate(morceaux):
        db.add(ArtifactChunk(id=f"chk_{artifact.id}_{rang}", artifact_id=artifact.id,
                             card_revision=1, ordinal=rang, text=texte))
    db.flush()


def _indexer(db, artifact: Artifact, extrait: str) -> None:
    """Indexe par le chemin VRAI — donc en passant par le garde-fau."""
    carte = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artifact.id).one()
    Fts.index_card_row(db, artifact, carte, "conversation", "conv_1", extrait)
    db.flush()


def _rejouer_index_pollue(db, artifact: Artifact, extrait: str) -> None:
    """Réécrit une ligne d'index BINAIRE — ce que l'ancien code faisait encore.

    Passage obligé par du SQL, et c'est VOLONTAIRE : `index_card_row` refuse
    aujourd'hui ce contenu, donc on ne pourrait pas rejouer le défaut par lui.
    C'est bien l'ancien comportement qu'il faut conserver réparable, parce que c'est
    lui qui doit rester réparable.
    """
    carte = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artifact.id).one()
    db.execute(
        sql_text("""INSERT INTO artifact_search
                    (artifact_id, scope_kind, scope_id, kind, card_revision,
                     filename, title, summary, keywords, entities, extracted_preview)
                    VALUES (:a, 'conversation', 'conv_1', :k, :rev, :fn, :t, :s, '', '', :ex)"""),
        {"a": artifact.id, "k": artifact.kind, "rev": carte.revision,
         "fn": artifact.original_filename, "t": carte.title, "s": carte.summary,
         "ex": extrait[:Fts.PREVIEW_CHARS]},
    )
    db.flush()


def _apercu_indexe(db, artifact_id: str) -> str:
    return db.execute(
        sql_text("SELECT extracted_preview FROM artifact_search WHERE artifact_id = :a"),
        {"a": artifact_id},
    ).scalar() or ""


# ══════════════════════════════════════════════════════════════════════════════
# 1. LE DÉTECTEUR
# ══════════════════════════════════════════════════════════════════════════════

class TestDetecteur:
    def test_trouve_les_trois_ecritures_du_meme_dommage(self, base):
        with _session(base) as db:
            art = _artefact(db, "art_pollu", "Avision 21.ods")
            _observation_ingestion(db, art, extrait_binaire())
            _fragments(db, art, [extrait_binaire()[:900], extrait_binaire()[900:1800]])
            _rejouer_index_pollue(db, art, extrait_binaire())

            trouves = Fts.detecter_ingestions_binaire_ou_tronquees(db)
            assert [t["artifact_id"] for t in trouves] == ["art_pollu"]
            entree = trouves[0]
            assert entree["filename"] == "Avision 21.ods"
            assert entree["kind"] == "sheet"
            assert set(entree["sources"]) == {"observation", "chunks", "index"}
            assert all(raison for raison in entree["raisons"])

    def test_le_motif_est_lisible_et_non_un_booleen(self, base):
        """Un rapport de réparation n'est lu que par quelqu'un qui doit AGIR. Un
        `binaire: true` ne lui dit ni quel format, ni ce qu'il peut faire."""
        with _session(base) as db:
            art = _artefact(db, "art_motif", "table.ods")
            _observation_ingestion(db, art, extrait_binaire())
            motif = Fts.detecter_ingestions_binaire_ou_tronquees(db)[0]["raisons"][0]
            assert "binaire" in motif and len(motif) > 20

    def test_artefact_sain_absent_du_rapport(self, base):
        with _session(base) as db:
            sain = _artefact(db, "art_sain", "ventes.csv")
            _observation_ingestion(db, sain, texte_propre())
            _fragments(db, sain, ["Date\tClient\tMontant", "2026-03-15\tDubois\t45000"])
            _indexer(db, sain, texte_propre())
            pollu = _artefact(db, "art_pollu", "table.ods")
            _observation_ingestion(db, pollu, extrait_binaire())

            trouves = Fts.detecter_ingestions_binaire_ou_tronquees(db)
            assert [t["artifact_id"] for t in trouves] == ["art_pollu"]

    def test_extraction_tronquee_signalee_comme_incomplete_pas_fausse(self, base):
        """Un PDF plus long que la borne est PARTIEL, pas faux : le dire
        «tronqué » et non «binaire» évite de le traiter comme un fichier à
        supprimer, et évite surtout de le croire intact."""
        with _session(base) as db:
            art = _artefact(db, "art_long", "rapport.pdf", kind="pdf")
            _observation_ingestion(db, art, ("Paragraphe de rapport. " * 2000)[:PAYLOAD_CHARS])
            trouves = Fts.detecter_ingestions_binaire_ou_tronquees(db)
            assert [t["artifact_id"] for t in trouves] == ["art_long"]
            raison = trouves[0]["raisons"][0]
            assert "tronquée" in raison and str(PAYLOAD_CHARS) in raison

    def test_seule_la_derniere_extraction_fait_foi(self, base):
        """Un artefact dont la PREMIÈRE extraction était binaire et la seconde
        propre est réparé. Le remonter ferait refaire un travail déjà fait —
        et surtout ferait croire qu'il est encore cassé."""
        with _session(base) as db:
            art = _artefact(db, "art_repare", "table.ods")
            _observation_ingestion(db, art, extrait_binaire(), suffixe="_v1")
            db.execute(
                sql_text("UPDATE observations SET created_at = '2026-01-01 00:00:00'"
                         " WHERE id = :i"), {"i": f"obs_{art.id}_v1"})
            _observation_ingestion(db, art, texte_propre(), suffixe="_v2")
            assert Fts.detecter_ingestions_binaire_ou_tronquees(db) == []


# ══════════════════════════════════════════════════════════════════════════════
# 2. LA RÉPARATION — rien par défaut, tout sur décision
# ══════════════════════════════════════════════════════════════════════════════

class TestReparation:
    def test_sans_appliquer_ne_touche_a_rien(self, base):
        """Le défaut doit être l'INSPECTION. Une réparation qui purge sur un doute
        est pire que le problème qu'elle corrige : elle ferait disparaître des
        fichiers parfaitement lisibles."""
        with _session(base) as db:
            art = _artefact(db, "art_pollu", "Avision 21.ods")
            _observation_ingestion(db, art, extrait_binaire())
            _fragments(db, art, [extrait_binaire()[:900]])
            _rejouer_index_pollue(db, art, extrait_binaire())

            rapport = Fts.reparer_ingestions_binaire_ou_tronquees(db)
            assert rapport["applique"] is False
            assert rapport["remis_a_reingerer"] == ["art_pollu"]
            assert _apercu_indexe(db, art.id) != ""
            assert db.query(ArtifactChunk).filter(ArtifactChunk.artifact_id == art.id).count() == 1
            carte = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == art.id).one()
            assert carte.analysis_status == "indexed"

    def test_appliquer_purge_ce_qui_est_faux(self, base):
        with _session(base) as db:
            art = _artefact(db, "art_pollu", "Avision 21.ods")
            _observation_ingestion(db, art, extrait_binaire())
            _fragments(db, art, [extrait_binaire()[:900], extrait_binaire()[900:]])
            _rejouer_index_pollue(db, art, extrait_binaire())

            rapport = Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            assert rapport["applique"] is True
            assert rapport["relance"] is False, "relancer doit rester une décision de l'appelant"
            assert _apercu_indexe(db, art.id) == ""
            assert db.query(ArtifactChunk).filter(ArtifactChunk.artifact_id == art.id).count() == 0

    def test_appliquer_passe_la_fiche_stale_avec_son_motif(self, base):
        """`stale` et non `failed` : un artefact binaire n'est pas une faute de
        l'utilisateur, c'est un état de traitement à refaire — et `stale` dit
        cela dans le vocabulaire du modèle. Le motif va dans la fiche, parce que
        c'est elle que l'utilisateur du fichier lit."""
        with _session(base) as db:
            art = _artefact(db, "art_pollu", "Avision 21.ods")
            _observation_ingestion(db, art, extrait_binaire())
            _rejouer_index_pollue(db, art, extrait_binaire())

            Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            carte = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == art.id).one()
            assert carte.analysis_status == "stale"
            assert carte.status == "stale"
            assert "à refaire" in (carte.summary or "")

    def test_artefact_sain_reste_intact(self, base):
        with _session(base) as db:
            sain = _artefact(db, "art_sain", "ventes.csv")
            _observation_ingestion(db, sain, texte_propre())
            _fragments(db, sain, ["Date\tClient\tMontant"])
            _indexer(db, sain, texte_propre())
            pollu = _artefact(db, "art_pollu", "table.ods")
            _observation_ingestion(db, pollu, extrait_binaire())

            Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            assert _apercu_indexe(db, sain.id) == texte_propre()
            assert db.query(ArtifactChunk).filter(ArtifactChunk.artifact_id == sain.id).count() == 1
            carte = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == sain.id).one()
            assert carte.analysis_status == "indexed"

    def test_une_liste_donnee_restreint_la_reparation(self, base):
        """Réingérer un PDF tronqué, c'est de l'OCR. La liste permet de ne
        remettre que les artefacts faux, et de laisser les partiels à une
        décision humaine."""
        with _session(base) as db:
            faux = _artefact(db, "art_faux", "table.ods")
            _observation_ingestion(db, faux, extrait_binaire())
            long_ = _artefact(db, "art_long", "rapport.pdf", kind="pdf")
            long_texte = ("Paragraphe de rapport. " * 2000)[:PAYLOAD_CHARS]
            _observation_ingestion(db, long_, long_texte)
            _indexer(db, long_, long_texte)

            rapport = Fts.reparer_ingestions_binaire_ou_tronquees(db, artefact_ids=["art_faux"],
                                                                  appliquer=True)
            assert rapport["remis_a_reingerer"] == ["art_faux"]
            assert _apercu_indexe(db, long_.id) != ""

    def test_artefact_sans_fiche_signale_et_non_crash(self, base):
        """Pas de fiche, pas de statut à remettre — mais la ligne d'index doit
        partir QUAND MÊME. Le rattacher à la fiche ferait laisser un moignon que
        la recherche continuerait d'interroger, et il serait signalé comme
        ignoré : le pire des deux mondes."""
        with _session(base) as db:
            art = _artefact(db, "art_pollu", "table.ods")
            _observation_ingestion(db, art, extrait_binaire())
            _rejouer_index_pollue(db, art, extrait_binaire())
            db.query(ArtifactCard).filter(ArtifactCard.artifact_id == art.id).delete()
            db.flush()

            rapport = Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            assert rapport["ignores"] == [
                {"artifact_id": "art_pollu", "raison": "aucune fiche à remettre à l'état"}]
            assert _apercu_indexe(db, art.id) == ""

    def test_reparer_deux_fois_ne_reparte_pas_de_zero(self, base):
        """L'observation est IMMUABLE : elle portera les octets de l'extraction
        fautive jusqu'à ce qu'une extraction propre la remplace. Un rapport qui la
        remonterait à chaque inspection ferait croire que la réparation n'a pas
        marché — on sépare donc « ce qui est faux » de « ce que cet appel remet ».
        """
        with _session(base) as db:
            art = _artefact(db, "art_pollu", "table.ods")
            _observation_ingestion(db, art, extrait_binaire())
            _rejouer_index_pollue(db, art, extrait_binaire())

            premier = Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            assert premier["remis_a_reingerer"] == ["art_pollu"]
            assert premier["deja_remis"] == []

            second = Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            assert second["remis_a_reingerer"] == [], "déjà remis : ne pas remettre deux fois"
            assert second["deja_remis"] == ["art_pollu"]
            assert [d["artifact_id"] for d in second["detectes"]] == ["art_pollu"], (
                "l'observation immuable reste fautive : elle est signalée, pas effacée")

    def test_rien_a_reparer_ne_renvoie_rien(self, base):
        with _session(base) as db:
            sain = _artefact(db, "art_sain", "ventes.csv")
            _observation_ingestion(db, sain, texte_propre())
            rapport = Fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
            assert rapport["remis_a_reingerer"] == [] and rapport["detectes"] == []
            assert Fts.detecter_ingestions_binaire_ou_tronquees(db) == []
            assert sain is not None

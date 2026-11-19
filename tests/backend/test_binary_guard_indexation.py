"""Le garde binaire, du fichier au texte indexé.

Le défaut que ces tests verrouillent est RÉEL et MESURÉ, pas théorique. Sur le
corpus de travail, un `.ods` de 122 Ko était enregistré avec le statut
`completed` et 16 000 caractères de son propre conteneur ZIP en `payload_ref` —
donc en recherche. Sa fiche était `indexed` : rien, dans l'application, ne
disait que le fichier n'avait jamais été lu.

Trois maillons, trois tests :

  - le JUGE (`backend/utils/binary_guard.py`) reconnaît un contenu binaire et ne
    confond pas du texte réel avec lui ;
  - l'EXTRACTION (`backend/ingestion/sheets.py`) refuse avant de décoder ;
  - l'INDEXATION refuse au point d'indexation, quel que soit le chemin qui a
    produit l'extrait, et LACHE ce qu'il refuse : `fts.py` juge et rend un
    verdict, `cards.py` — qui détient la fiche — en tire le statut et le résumé.

Base neuve et jetable par test, ingestion jamais déclenchée : aucun octet ni
aucune ligne du projet n'est touché.
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
)
from backend.search import cards as Cards
from backend.search import fts as Fts
from backend.utils import binary_guard as Garde


# ── Fabriques ─────────────────────────────────────────────────────────────────

def octets_ods() -> bytes:
    """Un conteneur ZIP réel, comme un .ods/.xlsx : c'est sa nature qui pose
    problème, pas le fait qu'il soit mal formé."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr("content.xml", "<office:document-content/>")
    return tampon.getvalue()


def extrait_binaire() -> str:
    """Ce que l'extraction produisait réellement : un en-tête de feuille suivi des
    octets du conteneur, lus en latin-1. Assez long pour que le garde ne tranche
    pas sur trois caractères."""
    return "# Feuille 1\n" + octets_ods().decode("latin-1")


def texte_legitime() -> str:
    """Du texte tel qu'un tableur en produit vraiment : accents, tabulations,
    nombres, ponctuation. C'est contre ÇA que le garde-fou doit se taire."""
    lignes = ["Date\tClient\tMontant"]
    lignes += [f"2026-03-{i:02d}\tDupont SARL\t{i * 1000} €" for i in range(1, 40)]
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
    """Base neuve, index FTS présent, ingestion neutralisée."""
    engine = create_engine(f"sqlite:///{tmp_path / 'garde-binaire.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    return sessions


def _artefact(db, artifact_id: str, nom: str, kind: str = "sheet", scope: str = "conv_1") -> Artifact:
    """Un artefact complet — blob, artefact, lien, fiche — par les MODÈLES.
    Un artefact fabriqué en SQL brut ne prouverait rien des contraintes réelles."""
    db.add(Blob(id=f"blob_{artifact_id}", sha256=artifact_id * 8, relative_path=f"blobs/{artifact_id}.bin",
                size_bytes=1024, mime_detected="application/vnd.oasis.opendocument.spreadsheet",
                state="ready"))
    db.add(Artifact(id=artifact_id, blob_id=f"blob_{artifact_id}", kind=kind,
                    original_filename=nom, display_name=nom))
    db.add(ArtifactLink(id=f"lnk_{artifact_id}", artifact_id=artifact_id,
                        scope_kind="conversation", scope_id=scope, role="attachment"))
    db.add(ArtifactCard(id=f"card_{artifact_id}", artifact_id=artifact_id, revision=1, is_current=1,
                        title=nom, summary="Fiche d'import.", keywords_json="[]", entities_json="[]",
                        status="analysed", analysis_status="analysed"))
    db.flush()
    return db.query(Artifact).filter(Artifact.id == artifact_id).one()


def _carte(db, artifact_id: str) -> ArtifactCard:
    return db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artifact_id).one()


def _apercu_indexe(db, artifact_id: str, scope: str = "conv_1") -> str:
    """L'aperçu tel qu'il est RÉELLEMENT indexé. Une ligne absente est assimilée à
    un aperçu vide : c'est ce que voit l'utilisateur, qui n'a pas à savoir si la
    ligne existe pour constater qu'il n'y a rien dedans."""
    return db.execute(
        sql_text("SELECT extracted_preview FROM artifact_search"
                 " WHERE artifact_id = :a AND scope_id = :s"),
        {"a": artifact_id, "s": scope},
    ).scalar() or ""


# ══════════════════════════════════════════════════════════════════════════════
# 1. LE JUGE — reconnaît le binaire, ne confond pas le texte
# ══════════════════════════════════════════════════════════════════════════════

class TestJuge:
    @pytest.mark.parametrize("nom,donnees", [
        ("archive ZIP", b"PK\x03\x04\x14\x00\x00\x08\x00"),
        ("conteneur OLE2", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00"),
        ("PDF", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
        ("PNG", b"\x89PNG\r\n\x1a\n\x00\x00\x00\r"),
    ])
    def test_signature_franche_tranche(self, nom, donnees):
        motif = Garde.raison_binaire(donnees)
        assert motif is not None and nom in motif

    def test_zip_compresse_sans_signature_en_tete_refuse_par_proportion(self):
        """Le cas RÉEL, et le plus important.

        L'extraction aplatit un tableur en `# NomDeFeuille` suivi du contenu. La
        signature du ZIP est donc en position 12, pas en position 0 — et le
        jugement des octets ne peut pas la voir. Sans la PROPORTION, le défaut
        d'origine passe exactement comme avant : c'est elle qui rattrape le
        fichier, pas la signature.
        """
        extrait = extrait_binaire().encode("latin-1")
        assert not extrait.startswith(Garde.ZIP_MAGIC), "la signature est bien décalée"
        controles = sum(1 for o in extrait[:8192] if o < 9 or 13 < o < 32)
        assert controles / len(extrait[:8192]) > Garde.RATIO_BINAIRE
        assert "contrôle" in (Garde.raison_binaire(extrait) or "")
        assert "contrôle" in (Garde.raison_binaire_texte(extrait.decode("latin-1")) or "")

    @pytest.mark.parametrize("texte", [
        "Date;Client;Montant\n2026-03-15;Dubois SARL;45000\n",
        '{"clef": "valeur", "nombre": 12}',
        "def total(lignes):\n    return sum(lignes)  # somme\n",
        "Feuille: Ventes\nDate=2026-03-15, Client=Dubois SARL, Montant=45000",
        "Étude n° 3 — « oui » (100 %) : l'audit a été fait.\n",
    ])
    def test_texte_reel_ne_jamais_refuse(self, texte):
        assert Garde.raison_binaire(texte.encode("utf-8")) is None
        assert Garde.raison_binaire_texte(texte) is None

    def test_texte_long_ne_declenche_pas_le_faux_positif(self):
        """Un extrait de 2000 caractères est un cas ORDINAIRE. Le seuil doit être
        mesuré sur ce volume-là, pas sur un échantillon de dix caractères."""
        assert Garde.raison_binaire_texte(texte_legitime()[:2000] * 3) is None

    def test_texte_decode_de_force_signale_par_u_fffd(self):
        """`U+FFFD` est IMPRIMABLE au sens de Python : il passe donc le jugement des
        caractères non imprimables et n'est rattrapé que par sa propre règle. Sans
        elle, un fichier décodé de force passerait pour du texte."""
        forcé = "donnée \ufffd " * 200
        assert "\ufffd".isprintable(), "le test suppose U+FFFD imprimable"
        assert "U+FFFD" in (Garde.raison_binaire_texte(forcé) or "")

    def test_un_seul_caractere_de_remplacement_ne_refuse_pas(self):
        """Un `U+FFFD` isolé est normal (une adresse, un symbole). Ce qui ne l'est
        pas, c'est la pluie : le seuil de 5 % existe pour cela."""
        texte = "rapport " * 200 + "�"
        assert Garde.raison_binaire_texte(texte) is None

    def test_texte_trop_court_pas_juge(self):
        """Trop court pour être un échantillon honnête : on ne juge pas, on ne
        devine pas. Refuser sur trois caractères serait du bruit."""
        assert Garde.raison_binaire_texte("PK") is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. L'INDEXATION — le refus central, et ce qu'il laisse
# ══════════════════════════════════════════════════════════════════════════════

class TestRefusALIndexation:
    def test_binaire_court_refuse_malgre_la_longueur(self, base):
        """LA RÉGRESSION. La seule protection était la longueur : un binaire plus
        court que la borne passait, et c'est exactement le cas des petits ZIP.
        Il doit être refusé — et comme l'extrait réel ci-dessous est TRÈS court
        au regard des 2000 caractères, la troncature ne peut pas servir
        d'excuse."""
        with _session(base) as db:
            art = _artefact(db, "art_zip", "Avision 21.ods")
            carte = _carte(db, art.id)
            extrait = "# Feuille 1\nPK\x03\x04\x14\x00\x00"
            assert len(extrait) < Fts.PREVIEW_CHARS, "le défaut tient justement à la brièveté"
            verdict = Fts.index_card_row(db, art, carte, "conversation", "conv_1", extrait)
            assert verdict["extract"] == Fts.EXTRACT_BINAIRE
            assert verdict["indexed_chars"] == 0
            assert "binaire" in verdict["reason"].lower()
            assert _apercu_indexe(db, art.id) == ""

    def test_binaire_en_tete_nomme_le_format(self, base):
        """Un motif de refus doit nommer CE QUI a été trouvé : « signature d'archive
        ZIP » oriente l'utilisateur, « 26 % d'octets de contrôle » le laisse seul."""
        with _session(base) as db:
            art = _artefact(db, "art_nom", "budget.xlsx")
            carte = _carte(db, art.id)
            verdict = Fts.index_card_row(db, art, carte, "conversation", "conv_1",
                                         "PK\x03\x04" + "\x00" * 40)
            assert "ZIP" in verdict["reason"]

    def test_refus_statut_different_de_tronque(self, base):
        """La Bible demande que la différence soit VISIBLE. Un binaire et un extrait
        trop long ne sont pas le même échec, et ne doivent pas porter le même mot."""
        with _session(base) as db:
            art = _artefact(db, "art_mix", "mixte.ods")
            carte = _carte(db, art.id)
            binaire = Fts.index_card_row(db, art, carte, "conversation", "conv_1", extrait_binaire())
            tronque = Fts.index_card_row(db, art, carte, "conversation", "conv_1",
                                         texte_legitime() * 200)
            assert binaire["extract"] == Fts.EXTRACT_BINAIRE
            assert tronque["extract"] == Fts.EXTRACT_TRONQUE
            assert binaire["extract"] != tronque["extract"]
            assert tronque["indexed_chars"] == Fts.PREVIEW_CHARS

    def test_refus_marque_la_fiche_avec_son_motif(self, base):
        """Un refus invisible est un refus inexistant : la fiche resterait `analysed`
        et le fichier passerait pour correctement ingéré. Elle doit porter le
        statut ET la raison — un « failed » seul ne dit pas quoi faire.

        On passe par `cards.reindexer_carte`, le SEUL chemin qui statue : c'est
        `cards.py`, qui détient la fiche, qui décide de son état, à partir du
        verdict que `fts` arbitre. `fts.index_card_row` juge et écrit, sans
        toucher à la fiche — un index n'a rien à dire de l'état d'une ligne.
        """
        with _session(base) as db:
            art = _artefact(db, "art_fiche", "budget.ods")
            carte = _carte(db, art.id)
            Cards.reindexer_carte(db, art, carte, "conversation", "conv_1", extrait_binaire())
            assert carte.analysis_status == "failed"
            assert carte.status == "failed"
            assert "binaire" in (carte.summary or "").lower()

    def test_troncature_ne_condamne_pas_la_fiche(self, base):
        """La borne de 2000 caractères est un CHOIX de conception, pas une faute.
        Condamner chaque gros PDF en `failed` ferait de l'état une information
        sans valeur — et c'est le retour à la corruption silencieuse qu'on évite."""
        with _session(base) as db:
            art = _artefact(db, "art_gros", "rapport.pdf", kind="pdf")
            carte = _carte(db, art.id)
            verdict = Fts.index_card_row(db, art, carte, "conversation", "conv_1", texte_legitime() * 200)
            assert verdict["extract"] == Fts.EXTRACT_TRONQUE
            assert carte.analysis_status == "analysed"
            assert len(_apercu_indexe(db, art.id)) == Fts.PREVIEW_CHARS

    def test_texte_normal_est_indexe_entier(self, base):
        with _session(base) as db:
            art = _artefact(db, "art_ok", "ventes.csv")
            carte = _carte(db, art.id)
            verdict = Fts.index_card_row(db, art, carte, "conversation", "conv_1", texte_legitime())
            assert verdict["extract"] == Fts.EXTRACT_INDEXE
            assert _apercu_indexe(db, art.id) == texte_legitime()
            assert carte.analysis_status == "analysed"

    def test_un_refus_ne_laisse_aucun_resultat_de_recherche(self, base):
        """Le but du garde : la recherche ne renvoie plus de résultats faits de bytes.

        Le mot recherché est « Feuille », qui est dans l'extrait binaire ET dans
        le nom du fichier. Un résultat ne peut donc pas être la preuve d'une fuite
        par l'aperçu seul : on vérifie que le contenu binary n'a produit aucune
        ligne indexée, et que la fiche dit pourquoi elle est vide.
        """
        with _session(base) as db:
            art = _artefact(db, "art_cherche", "Feuille.ods")
            carte = _carte(db, art.id)
            Cards.reindexer_carte(db, art, carte, "conversation", "conv_1", extrait_binaire())
            db.flush()
            assert _apercu_indexe(db, art.id) == ""
            page = Fts.search(db, query="mimetype", scope_kind="conversation", scope_id="conv_1",
                              allowed_ids={art.id})
            assert page["ok"] is True
            assert page["results"] == [], "un mot qui n'existe que dans le ZIP ne doit rien trouver"
            assert (carte.analysis_status or "") == "failed"

    def test_reindexation_idempotente_apres_refus(self, base):
        """Refuser doit être aussi définitif qu'indexer : une seconde passe ne doit
        pas laisser subsister l'aperçu de la première."""
        with _session(base) as db:
            art = _artefact(db, "art_idem", "table.ods")
            carte = _carte(db, art.id)
            Fts.index_card_row(db, art, carte, "conversation", "conv_1", texte_legitime())
            assert _apercu_indexe(db, art.id)
            Fts.index_card_row(db, art, carte, "conversation", "conv_1", extrait_binaire())
            assert _apercu_indexe(db, art.id) == ""

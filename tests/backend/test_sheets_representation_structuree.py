"""La représentation STRUCTURÉE des tableurs, branchée sur le chemin réel (tâche 4.5).

D'où vient ce fichier : l'ingestion aplatisait encore les tableurs en TSV
(`_aplatir`, `# Feuille\\n...\\t...`) — aucun en-tête contextualisé, aucune
provenance de cellule, aucun statut des formules. La tâche 4.5 remplace cet
aplatissement par l'enchaînement lecteur réel → `extract_schema` +
`build_chunks` (naturalisation) → `représenter_feuillet`, dont les trois
consumers du chemin (observation/read_document, index FTS, fiche find_files)
partagent UNE chaîne de caractères.

Ce que chaque test verrouille :

  - la DISTINCTION formule / résultat en cache (tableaux.md §3.3) porte sur
    un `.xlsx` réellement lu deux fois (double passage openpyxl) : une formule
    sans cache est dite « résultat en cache absent, jamais recalculé par Thot »
    — JAMAIS « résultat recalculé » (le moteur de calcul est hors périmètre,
    §3.4) — et son repère de ligne couvre la cellule de formule même sans cache
    (la cellule EXISTE dans le classeur, seule SA VALEUR manque) ;
  - le repère de ligne `[Feuille!A2:C2]` (fiche tableaux.md §2.5 : « Conserver
    la localisation : feuille/cellule/plage pour un tableur ») termine CHAQUE
    ligne naturalisée et est RETROUVABLE par la recherche : par contenu
    (« Dubois SARL ») ET par cellule (« A2 ») ;
  - un CSV porte UNE SÉMANTIQUE SANS FORMULE : aucune mention de « formule »
    ni de « résultat en cache » — le vocabulaire de la fiche §3.3 n'est pas
    pour lui ;
  - le rejet d'un non-tableur reste NOMMÉ par le chemin de dépôt, inchangé ;
  - un `.ods` réel (lecteur pandas) reçoit la sémantique « sans formules », et
    ses repères sont déclarés reconstruits — jamais présentés natifs (§2.5).

Les `.xlsx` sont écrits dans `tmp_path` par openpyxl (qui N'écrit PAS le
résultat en cache d'une formule) ; le cas « avec cache » est obtenu par une
chirurgie du XML interne (`<v>24</v>` dans la cellule de la formule) — c'est
exactement ce qu'un tableur réel écrit quand il a calculé la formule avant
d'enregistrer. Aucun réseau, aucun modèle, aucun octet du projet lu ou écrit.
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
)
from backend.ingestion.attachment_ingest import extraire_texte
from backend.search import fts as Fts

# ── Fabriques ─────────────────────────────────────────────────────────────────


def ecrire_xlsx_formule(tmp_path, *, cache=None) -> str:
    """Un `.xlsx` authentique, avec une formule en C2 (« =B2*C2 »).

    `cache=None` : le fichier tel qu'openpyxl l'écrit — aucun résultat en
    cache, ce qui est le cas de tout classeur jamais recalculé depuis sa
    génération programmatique. Sinon, chirurgie du XML interne pour écrire
    `<v>…</v>` DANS la cellule de formule : ce qu'un tableur réel y écrit quand
    il a calculé la formule avant d'enregistrer.
    """
    oxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / "factures.xlsx"
    classeur = oxl.Workbook()
    feuille = classeur.active
    feuille.title = "Factures"
    feuille.append(["Date", "Client", "Montant"])
    feuille.append(["2026-03-15", "Dubois SARL", "=B2*C2"])
    classeur.save(str(chemin))
    if cache is None:
        return str(chemin)
    destination = tmp_path / "factures_cache.xlsx"
    modifié, trouvé = False, False
    with zipfile.ZipFile(chemin) as zin, zipfile.ZipFile(destination, "w") as zout:
        for info in zin.infolist():
            contenu = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                xml = contenu.decode("utf-8")
                marqueur_cache = "<f>B2*C2</f><v></v>"
                if marqueur_cache in xml:
                    xml = xml.replace(marqueur_cache, f"<f>B2*C2</f><v>{cache}</v>")
                    trouvé, modifié = True, True
                    contenu = xml.encode()
            zout.writestr(info, contenu)
    assert trouvé, (
        "la chirurgie n'a pas trouvé la cellule de formule qu'elle devait instrumenter"
    )
    assert modifié
    return str(destination)


def ecrire_csv(tmp_path) -> str:
    chemin = tmp_path / "ventes.csv"
    chemin.write_bytes(b"Date;Client;Montant\n2026-03-15;Dubois SARL;45000\n")
    return str(chemin)


def octets_zip_brut() -> bytes:
    """Un conteneur ZIP au strict nécessaire — ni un `.xlsx`, ni un `.ods`
    lisible, mais indiscutablement binaire."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr("content.xml", "<office:document-content/>")
    return tampon.getvalue()


def ecrire_ods_facts(tmp_path) -> str:
    """Un `.ods` AUTHENTIQUE, écrit par odfpy — sans lui le format ne peut pas
    être fabriqué, et c'est précisément ce qui rend le test significatif."""
    pytest.importorskip("odf")
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    chemin = tmp_path / "ventes.ods"
    # Any explicite : les stubs d'odfpy ne connaissent pas l'attribut
    # `spreadsheet`, qui existe bien à l'exécution — même remarque que
    # `test_sheets_formats.octets_ods`.
    document: Any = OpenDocumentSpreadsheet()
    table = Table(name="Ventes")
    for ligne in (["Date", "Client", "Montant"], ["2026-03-15", "Dubois SARL", 45000]):
        rangée = TableRow()
        for valeur in ligne:
            cellule = (
                TableCell(valuetype="float", value=str(valeur))
                if isinstance(valeur, int)
                else TableCell(valuetype="string")
            )
            cellule.addElement(P(text=str(valeur)))
            rangée.addElement(cellule)
        table.addElement(rangée)
    document.spreadsheet.addElement(table)
    document.save(str(chemin))
    return str(chemin)


# ── Base d'indexation (le chemin réel FTS, comme chez les gardes binaires) ────


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
    engine = create_engine(f"sqlite:///{tmp_path / 'representation.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    return sessions


def _artefact(db, artifact_id: str, nom: str, kind: str = "sheet") -> Artifact:
    db.add(
        Blob(
            id=f"blob_{artifact_id}",
            sha256=artifact_id * 8,
            relative_path=f"blobs/{artifact_id}.bin",
            size_bytes=1024,
            mime_detected="application/octet-stream",
            state="ready",
        )
    )
    db.add(
        Artifact(
            id=artifact_id,
            blob_id=f"blob_{artifact_id}",
            kind=kind,
            original_filename=nom,
            display_name=nom,
        )
    )
    db.add(
        ArtifactLink(
            id=f"lnk_{artifact_id}",
            artifact_id=artifact_id,
            scope_kind="conversation",
            scope_id="conv_1",
            role="attachment",
        )
    )
    db.add(
        ArtifactCard(
            id=f"card_{artifact_id}",
            artifact_id=artifact_id,
            revision=1,
            is_current=1,
            title=nom,
            summary="Fiche d'import.",
            keywords_json="[]",
            entities_json="[]",
            status="indexed",
            analysis_status="indexed",
        )
    )
    db.flush()
    return db.query(Artifact).filter(Artifact.id == artifact_id).one()


def _carte(db, artifact: Artifact) -> ArtifactCard:
    return db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artifact.id).one()


# ══════════════════════════════════════════════════════════════════════════════
# 1. LA DISTINCTION FORMULE / RÉSULTAT EN CACHE (tableaux.md §3.3, §3.4)
# ══════════════════════════════════════════════════════════════════════════════


class TestDistinctionFormuleEtCache:
    def test_formule_sans_cache_est_dite_absente_et_jamais_recalculee(self, tmp_path):
        """LA DISTINCTION SUR LE CHEMIN RÉEL. Un `.xlsx` déposé, formule en C2,
        classeur jamais calculé : la représentation distingue la formule source
        (« =B2*C2 ») du résultat ABSENT du cache — et le dit « jamais recalculé
        par Thot », le moteur de calcul étant hors périmètre (§3.4) : Thot
        n'invente pas la valeur. L'ancienne lecture `data_only=True` seule
        rendait un `None` muet — une cellule vide de fait — quand la fiche §3.3
        exige que la différence entre « valeur native » et « formule sans
        résultat » soit VISIBLE."""
        rendu, mode = extraire_texte("doc_test", "sheet", ecrire_xlsx_formule(tmp_path))
        assert mode == "sheet"
        assert 'formule "=B2*C2"' in rendu
        assert "résultat en cache absent, jamais recalculé par Thot" in rendu
        assert "résultat en cache 24" not in rendu

    def test_le_statut_recalcule_est_jamais_produit(self, tmp_path):
        """« résultat recalculé » est un statut que seul un moteur de calcul
        identifié peut écrire (§3.4 « résultat recalculé » : produit par le
        moteur). Thot n'en a pas ici : le mot n'apparaît QUE dans la limitation
        (« jamais recalculé par Thot »), jamais comme un état de valeur."""
        rendu, _ = extraire_texte("doc_test", "sheet", ecrire_xlsx_formule(tmp_path))
        assert "résultat recalculé" not in rendu

    def test_formule_avec_cache_injecte_dit_le_cache(self, tmp_path):
        """Symétrique : quand le classeur PORTE un résultat en cache (chirurgie
        `<v>24</v>`, ce qu'Excel y écrit après calcul), la représentation cite
        la valeur AVEC son statut (« résultat en cache 24 ») — la moitié
        positive de §3.4 : « Indexer la valeur utile avec son statut si elle
        existe ; sinon conserver une limitation »."""
        rendu, _ = extraire_texte(
            "doc_test", "sheet", ecrire_xlsx_formule(tmp_path, cache=24)
        )
        assert 'formule "=B2*C2"' in rendu
        assert "résultat en cache 24" in rendu
        assert "résultat en cache absent" not in rendu

    def test_le_repere_couvre_la_cellule_de_formule_meme_sans_cache(self, tmp_path):
        """La cellule C2 EXISTE dans le classeur — le fichier la déclare — même
        quand aucun calcul n'a jamais déposé de résultat dans le cache (fiche
        §2.5 : conserver la localisation). Restreindre la plage à `A2:B2` pour
        ce seul motif ferait varier le repère d'un MÊME fichier selon que le
        tableur avait ou non calculé la formule avant d'enregistrer : le repère
        décrit la cellule, pas son cache."""
        for chemin in (
            ecrire_xlsx_formule(tmp_path),
            ecrire_xlsx_formule(tmp_path, cache=24),
        ):
            rendu, _ = extraire_texte("doc_test", "sheet", chemin)
            assert rendu.endswith("[Factures!A2:C2]"), (
                "la ligne naturalisée doit finir par son repère natif, qui "
                "couvre la cellule de formule : la ligne PORTE la cellule C2"
            )

    def test_les_chunks_portent_rang_repere_et_statut(self, tmp_path):
        """La représentation structurée (tâche 4.5) charge les chunks des
        données de provenance : rang natif (`ligne`), plage (`repere`),
        repère unitaire de la cellule formule (C2) dans le texte affiché. Ce
        sont des données PUBLIÈES avec les chunks, servies aux consumers qui
        veulent s'appuyer dessus (ask_files relit par plage)."""
        from backend.ingestion.sheets import (
            build_chunks,
            extract_schema,
            lire_classeur_feuillet,
        )

        chemin = ecrire_xlsx_formule(tmp_path)
        feuillet = lire_classeur_feuillet(chemin)
        assert feuillet.lecteur.startswith("openpyxl")
        chunks = build_chunks(
            feuillet.tables,
            extract_schema(feuillet.tables),
            cellules=feuillet.cellules,
        )
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk["ligne"] == 2, "rang NATIF du classeur, pas la position"
        assert chunk["repere"] == "Factures!A2:C2"
        assert chunk["texte_affiche"].startswith("Feuille: Factures — Factures!A2:C2")
        assert "Montant (C2) = formule" in chunk["texte_affiche"]
        assert chunk["texte_embedde"].endswith(" [Factures!A2:C2]")
        assert 'formule "=B2*C2"' in chunk["texte_embedde"]

    def test_avec_cache_le_chunk_cite_la_valeur_et_son_statut(self, tmp_path):
        from backend.ingestion.sheets import (
            build_chunks,
            extract_schema,
            lire_classeur_feuillet,
        )

        chemin = ecrire_xlsx_formule(tmp_path, cache=24)
        feuillet = lire_classeur_feuillet(chemin)
        chunks = build_chunks(
            feuillet.tables,
            extract_schema(feuillet.tables),
            cellules=feuillet.cellules,
        )
        chunk = chunks[0]
        assert (
            'Montant (C2) = formule "=B2*C2" (résultat en cache 24)'
            in chunk["texte_affiche"]
        )
        assert "résultat en cache 24" in chunk["texte_embedde"]
        assert "résultat en cache absent" not in chunk["texte_embedde"]


# ══════════════════════════════════════════════════════════════════════════════
# 2. LE DÉPÔT PAR LE CHEMIN RÉEL, RETROUVABLE PAR LA RECHERCHE
# ══════════════════════════════════════════════════════════════════════════════


class TestDepotEtRecherche:
    def test_le_depose_xlsx_est_indexe_et_retrouvable(self, base, tmp_path):
        """Bout en bout par le chemin VRAI : `extraire_texte` (ce que
        `_ingérer_un` appelle) → indexation FTS → recherche. La requête
        retrouve le fichier PAR CONTENU (« Dubois SARL ») et PAR CELLULE
        (« A2 ») — l'adresse est dans la chaîne que les trois consumers
        partagent, sinon elle serait invisible pour TOUS."""
        with _session(base) as db:
            art = _artefact(db, "art_repr", "factures.xlsx")
            carte = _carte(db, art)
            texte, mode = extraire_texte(art.id, "sheet", ecrire_xlsx_formule(tmp_path))
            assert mode == "sheet"
            verdict = Fts.index_card_row(
                db, art, carte, "conversation", "conv_1", texte
            )
            assert verdict["extract"] == Fts.EXTRACT_INDEXE
            db.flush()
            par_contenu = Fts.search(
                db,
                query="Dubois SARL",
                scope_kind="conversation",
                scope_id="conv_1",
                allowed_ids={art.id},
            )
            assert par_contenu["ok"] is True
            assert [r["artifact_id"] for r in par_contenu["results"]] == [art.id]
            par_cellule = Fts.search(
                db,
                query="A2",
                scope_kind="conversation",
                scope_id="conv_1",
                allowed_ids={art.id},
            )
            assert par_cellule["ok"] is True
            assert [r["artifact_id"] for r in par_cellule["results"]] == [art.id]

    def test_le_csv_est_indexe_et_retrouvable_par_contenu(self, base, tmp_path):
        """La garde binaire ne doit PAS confondre un tableur texte avec un
        conteneur : un CSV sain s'indexe et se retrouve PAR CONTENU, comme le
        xlsx. (Régression du défaut d'origine des lecteurs : un ZIP passait parce
        que seule la longueur protegeait.)"""
        with _session(base) as db:
            art = _artefact(db, "art_csv", "ventes.csv")
            carte = _carte(db, art)
            texte, mode = extraire_texte(art.id, "sheet", ecrire_csv(tmp_path))
            assert mode == "sheet"
            verdict = Fts.index_card_row(
                db, art, carte, "conversation", "conv_1", texte
            )
            assert verdict["extract"] == Fts.EXTRACT_INDEXE
            db.flush()
            page = Fts.search(
                db,
                query="Dubois SARL",
                scope_kind="conversation",
                scope_id="conv_1",
                allowed_ids={art.id},
            )
            assert page["ok"] is True
            assert [r["artifact_id"] for r in page["results"]] == [art.id]

    def test_un_csv_ne_parle_daucune_formule(self, tmp_path):
        """La sémantique formule/cache de la fiche §3.3 porte sur un CLASSEUR :
        un CSV n'a ni l'une ni l'autre, et lui prêter ce vocabulaire signalerait
        une distinction qui n'existe pas chez lui. La phrase naturalisée reste
        sémantiquement propre, et ses repères sont ANNONCÉS reconstruits (§2.5 :
        un CSV n'a pas d'adresse A1 native)."""
        rendu, mode = extraire_texte("doc_test", "sheet", ecrire_csv(tmp_path))
        assert mode == "sheet"
        assert "formule" not in rendu
        assert "résultat en cache" not in rendu
        assert "repères de cellule reconstruits" in rendu
        assert rendu.endswith("[Feuille 1!A2:C2]")

    def test_un_ods_reel_semantique_sans_formules_et_repere_annonces_construits(
        self, tmp_path
    ):
        """Lecteur pandas (odfpy requis) : les formules du classeur ne sont pas
        conservées — la représentation le dit DANS LE SCHÉMA (« le lecteur ne
        conserve pas de formule »), pas ligne à ligne où rien n'existerait à
        citer ; les repères sont déclarés reconstruits, jamais natifs (§2.5 :
        « La provenance distingue repère natif et coordonnées de
        reconstruction »). Une limitation documentée plutôt qu'un mensonge
        répété."""
        chemin = ecrire_ods_facts(tmp_path)
        rendu, mode = extraire_texte("doc_test", "sheet", chemin)
        assert mode == "sheet"
        assert "Dubois SARL" in rendu and "2026-03-15" in rendu
        assert "formules : aucune (le lecteur ne conserve pas de formule)" in rendu
        assert "repères de cellule reconstruits" in rendu
        assert "repères A1 natifs" not in rendu


# ══════════════════════════════════════════════════════════════════════════════
# 3. LE REJET D'UN NON-TABLEUR RESTE NOMMÉ, PAR LE CHEMIN DE DÉPÔT
# ══════════════════════════════════════════════════════════════════════════════


class TestRejetsNommes:
    def test_un_binaire_depose_comme_tableur_est_refuse_par_tous_les_lecteurs(
        self, tmp_path
    ):
        """Le chemin de dépôt (`extraire_texte`) doit conclure bruyamment : un
        ZIP qui n'est ni un classeur lisible ni un CSV texte est refusé par les
        DEUX lecteurs, et le message final le dit — chaque cause nommée, jamais
        un `{}`, jamais une extraction fantôme au format `PK\\x03\\x04…`."""
        chemin = tmp_path / "faux.xlsx"
        chemin.write_bytes(octets_zip_brut())
        with pytest.raises(RuntimeError) as echec:
            extraire_texte("doc_bin", "sheet", str(chemin))
        message = str(echec.value)
        assert "aucun lecteur de tableur n'a produit de contenu" in message
        assert "lire_classeur_feuillet" in message and "lire_csv_feuillet" in message
        assert "signature d'archive ZIP" in message  # la cause du refus CSV est nommée

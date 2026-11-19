"""Phase 7 — bornes et cohérence.

Ce que ces tests verrouillent, et pourquoi :

  - une archive tableur est inspectée AVANT `load_workbook` : une bombe de
    décompression ne doit pas être matérialisée pour être refusée ;
  - les lignes/cellules/feuilles réellement lues sont bornées, avec un message
    clair et sans repli qui retenterait la lecture refusée ;
  - `stage.picker_bytes` traverse le VRAI transport IPC en base64, et un
    fichier trop gros est refusé sans décodage ;
  - une ligne stdin géante est rejetée sans être accumulée en entier ;
  - l'aperçu est sérialisable en JSON (plus de `bytes` Python dans `emit`) ;
  - une page PDF à dimensions énormes est refusée avant `Image.frombytes` ;
  - le rejeu d'outbox et la reprise ne bloquent pas le démarrage.

Aucun modèle, aucun GPU. Les classeurs sont fabriqués dans `tmp_path`.
"""

from __future__ import annotations

import base64
import io
import json
import threading
import types
import zipfile

import pytest

from backend import main as backend_main
from backend.config.defaults import INPUT_LIMITS
from backend.inference import ipc
from backend.ingestion import gate as G
from backend.ingestion import sheets as S
from backend.ipc import attachments as A
from backend.utils import files as F


# ---------------------------------------------------------------------------
# 7.1 — Bombes de décompression et matérialisation bornée
# ---------------------------------------------------------------------------


def _ecrire_xlsx(tmp_path, nom: str = "normal.xlsx", lignes: int = 2) -> str:
    openpyxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / nom
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    for index in range(lignes):
        feuille.append([f"c{index}", index])
    classeur.save(str(chemin))
    return str(chemin)


def _ecrire_xlsx_formule_partagee(tmp_path) -> str:
    """`B1 = A1*2` maître partagé, `B2 = A2*2` dépendant SANS texte.

    C'est l'encodage exact d'Excel pour une formule recopiée. L'ancien second
    passage openpyxl rendait `B2 = =A2*2` ; la lecture XML doit rendre pareil.
    """
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "partagee_source.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["A1"] = 1
    feuille["A2"] = 2
    feuille["B1"] = "=A1*2"
    feuille["B2"] = "=A2*2"
    classeur.save(str(source))

    destination = tmp_path / "partagee.xlsx"
    remplacements = {
        '<c r="B1"><f>A1*2</f><v></v></c>': (
            '<c r="B1"><f t="shared" ref="B1:B2" si="0">A1*2</f><v></v></c>'
        ),
        '<c r="B2"><f>A2*2</f><v></v></c>': '<c r="B2"><f t="shared" si="0"/></c>',
    }
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(destination, "w") as zout:
        for info in zin.infolist():
            contenu = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                xml = contenu.decode("utf-8")
                for ancien, nouveau in remplacements.items():
                    assert ancien in xml, (
                        "la chirurgie n'a pas trouvé la cellule qu'elle devait "
                        "rendre partagée"
                    )
                    xml = xml.replace(ancien, nouveau)
                contenu = xml.encode()
            zout.writestr(info, contenu)
    return str(destination)


def _ecrire_xlsx_plage(
    tmp_path, nom: str, ligne_entete: int, ligne_donnees: int
) -> str:
    """Une feuille dont l'en-tête et les données commencent en colonne B.

    C'est la reproduction exacte du défaut de provenance : `iter_rows` complète
    la grille depuis A1, mais l'ancien code additionnait `min_row`/`min_col` de
    `calculate_dimension()` à des positions déjà 0-based.
    """
    openpyxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / nom
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "F"
    feuille[f"B{ligne_entete}"] = "Nom"
    feuille[f"C{ligne_entete}"] = "Valeur"
    feuille[f"B{ligne_donnees}"] = "a"
    feuille[f"C{ligne_donnees}"] = 1
    classeur.save(str(chemin))
    return str(chemin)


def _ecrire_xlsx_dependant_avant_maitre(tmp_path) -> str:
    """Un dépendant `shared` qui apparaît AVANT son maître dans le XML.

    La résolution ne doit pas dépendre de l'ordre de lecture : les dépendants
    sont collectés pendant le flux puis résolus après, quand le maître est connu.
    """
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "avant_source.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["A1"] = 1
    feuille["A2"] = 2
    feuille["B1"] = "=A1*2"
    feuille["B2"] = "=A2*2"
    classeur.save(str(source))

    destination = tmp_path / "avant.xlsx"
    remplacements = {
        '<c r="B1"><f>A1*2</f><v></v></c>': ('<c r="B1"><f t="shared" si="7"/></c>'),
        '<c r="B2"><f>A2*2</f><v></v></c>': (
            '<c r="B2"><f t="shared" ref="B1:B2" si="7">A2*2</f><v></v></c>'
        ),
    }
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(destination, "w") as zout:
        for info in zin.infolist():
            contenu = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                xml = contenu.decode("utf-8")
                for ancien, nouveau in remplacements.items():
                    assert ancien in xml
                    xml = xml.replace(ancien, nouveau)
                contenu = xml.encode()
            zout.writestr(info, contenu)
    return str(destination)


def _ecrire_xlsx_formule_partagee_noms_feuilles(tmp_path) -> str:
    """Un maître B1 et son dépendant C1, avec des noms de feuilles « adresse ».

    Le dépendant n'a pas de texte : il doit être reconstitué en décalant le
    maître d'une colonne, SANS toucher à `Q1` ni à `'Ma Feuille'`.
    """
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "noms_source.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["B1"] = "=Q1!A1+'Ma Feuille'!A1"
    feuille["C1"] = "=Q1!B1+'Ma Feuille'!B1"
    classeur.save(str(source))

    destination = tmp_path / "noms.xlsx"
    remplacements = {
        "<c r=\"B1\"><f>Q1!A1+'Ma Feuille'!A1</f><v></v></c>": (
            '<c r="B1"><f t="shared" ref="B1:C1" si="0">'
            "Q1!A1+'Ma Feuille'!A1</f><v></v></c>"
        ),
        "<c r=\"C1\"><f>Q1!B1+'Ma Feuille'!B1</f><v></v></c>": (
            '<c r="C1"><f t="shared" si="0"/></c>'
        ),
    }
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(destination, "w") as zout:
        for info in zin.infolist():
            contenu = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                xml = contenu.decode("utf-8")
                for ancien, nouveau in remplacements.items():
                    assert ancien in xml, "la chirurgie n'a pas trouvé la formule"
                    xml = xml.replace(ancien, nouveau)
                contenu = xml.encode()
            zout.writestr(info, contenu)
    return str(destination)


def _ecrire_xlsx_formule_tableau(tmp_path) -> str:
    """Une VRAIE formule de tableau (`t="array"`), écrite par openpyxl.

    Seule la cellule d'ancrage porte le texte ; B2 est une cellule de résultat
    avec cache et sans formule — c'est l'encodage réel d'un tableau Excel.
    """
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.worksheet.formula import ArrayFormula

    chemin = tmp_path / "tableau.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["A1"] = 1
    feuille["A2"] = 2
    feuille["B1"] = ArrayFormula("B1:B2", "=A1:A2*2")
    feuille["B2"] = 4
    classeur.save(str(chemin))
    return str(chemin)


def _ecrire_ods_content(tmp_path, nb_feuilles: int) -> str:
    """Un conteneur ODS minimal avec N `<table:table>` dans `content.xml`.

    Volontairement sans cellules : le test prouve que la borne se décide sur le
    NOMBRE de tables, avant que pandas ne matérialise le moindre DataFrame.
    """
    chemin = tmp_path / "trop.ods"
    tables = "".join(
        f'<table:table table:name="F{index}"><table:table-row>'
        "<table:table-cell><text:p>x</text:p></table:table-cell>"
        "</table:table-row></table:table>"
        for index in range(nb_feuilles)
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<office:document-content "
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        f"<office:body><office:spreadsheet>{tables}</office:spreadsheet>"
        "</office:body></office:document-content>"
    )
    with zipfile.ZipFile(chemin, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", content)
    return str(chemin)


def _ecrire_xlsx_formule_seule_sans_cache(tmp_path) -> str:
    """En-tête ligne 1, ligne 2 uniquement une formule sans cache, ligne 3 données.

    `B2="=1+1"` n'a pas de résultat en cache : `iter_rows(values_only=True)` rend
    `[None, None]` pour la ligne 2. La ligne existe pourtant dans le classeur —
    elle doit être rendue, pas sautée (tableaux.md §3.3).
    """
    openpyxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / "formule_seule.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["A1"] = "Nom"
    feuille["B1"] = "Valeur"
    feuille["B2"] = "=1+1"
    feuille["A3"] = "a"
    feuille["B3"] = 2
    classeur.save(str(chemin))
    return str(chemin)


def _ecrire_ods_avec_doctype(tmp_path) -> str:
    """Un ODS minimal dont `content.xml` déclare une entité interne."""
    chemin = tmp_path / "entite.ods"
    contenu = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE office:document-content [\n"
        '  <!ENTITY explosion "AAAA">\n'
        "]>\n"
        "<office:document-content "
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="F"><table:table-row><table:table-cell>'
        "<text:p>&explosion;</text:p></table:table-cell></table:table-row>"
        "</table:table></office:spreadsheet></office:body>"
        "</office:document-content>"
    )
    with zipfile.ZipFile(chemin, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", contenu)
    return str(chemin)


def _ecrire_xlsx_avec_doctype(tmp_path) -> str:
    """Un ZIP xlsx-like dont la feuille déclare une entité interne."""
    chemin = tmp_path / "entite.xlsx"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE worksheet [<!ENTITY boom "AAAA">]>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><v>&boom;</v></c></row></sheetData>'
        "</worksheet>"
    )
    with zipfile.ZipFile(chemin, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", xml)
    return str(chemin)


def _ecrire_ods_avec_doctype_apres_prefixe(tmp_path) -> str:
    """Un ODS dont le DOCTYPE tombe APRÈS les 64 Kio lus par le préfixe.

    Le remplissage est un commentaire XML valide (le prologue autorise les
    commentaires avant le DOCTYPE) : le contrôle borné ne voit donc pas la
    déclaration, et seul `defusedxml` peut la refuser pendant le flux.
    """
    chemin = tmp_path / "entite_tardive.ods"
    remplissage = "<!--" + "x" * (S.MAX_PREFIXE_XML + 1024) + "-->\n"
    contenu = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + remplissage
        + "<!DOCTYPE office:document-content [\n"
        '  <!ENTITY explosion "AAAA">\n'
        "]>\n"
        "<office:document-content "
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="F"><table:table-row><table:table-cell>'
        "<text:p>&explosion;</text:p></table:table-cell></table:table-row>"
        "</table:table></office:spreadsheet></office:body>"
        "</office:document-content>"
    )
    assert contenu.index("<!DOCTYPE") > S.MAX_PREFIXE_XML, (
        "le test doit placer la déclaration HORS du préfixe borné"
    )
    with zipfile.ZipFile(chemin, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", contenu)
    return str(chemin)


def _ecrire_xlsx_dependant_orphelin(tmp_path) -> str:
    """Un dépendant `shared` dont le `si` n'a AUCUN maître dans la feuille."""
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "orphelin_source.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["A1"] = 1
    feuille["B2"] = "=A2*2"
    classeur.save(str(source))

    destination = tmp_path / "orphelin.xlsx"
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(destination, "w") as zout:
        for info in zin.infolist():
            contenu = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                xml = contenu.decode("utf-8")
                ancien = '<c r="B2"><f>A2*2</f><v></v></c>'
                assert ancien in xml
                xml = xml.replace(
                    ancien, '<c r="B2"><f t="shared" si="42"/><v>5</v></c>'
                )
                contenu = xml.encode()
            zout.writestr(info, contenu)
    return str(destination)


def _interdire_load_workbook(monkeypatch) -> list[str]:
    """Installe un `load_workbook` qui échoue, et rend la trace des appels."""
    import openpyxl

    appels: list[str] = []

    def _load_workbook(*args, **kwargs):
        appels.append(str(args[0]) if args else "?")
        raise AssertionError("load_workbook ne doit pas être appelé avant la garde ZIP")

    monkeypatch.setattr(openpyxl, "load_workbook", _load_workbook)
    return appels


def test_xlsx_normal_passe(tmp_path):
    """La garde ne doit pas refuser un classeur sain."""
    chemin = _ecrire_xlsx(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.tables["Feuille"][0][0] == "c0"


def test_formule_partagee_dependante_est_traduite(tmp_path):
    """`B2` n'a pas de texte : sa formule se reconstitue depuis le maître `B1`."""
    chemin = _ecrire_xlsx_formule_partagee(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.cellules is not None
    grille = feuillet.cellules["Feuille"]

    assert grille[0][1].formule == "=A1*2"
    assert grille[1][1].formule == "=A2*2"
    # Le dépendant sans `<v>` n'a pas de cache : la formule est là, la valeur None.
    assert grille[1][1].valeur is None


def test_formule_partagee_respecte_les_references_absolues():
    """Un `$` fige sa ligne/colonne ; les chaînes littérales ne sont pas des adresses."""
    assert (
        S._traduire_formule_partagee('$A$1+A1+$A1+A$1+IF(B1>0,"A1",LOG10(B1))', 1, 1)
        == '$A$1+B2+$A2+B$1+IF(C2>0,"A1",LOG10(C2))'
    )


def test_dependant_partage_sans_maitre_garde_sa_cellule(tmp_path):
    """Un `si` orphelin ne s'invente pas — mais la cellule et son cache restent."""
    chemin = _ecrire_xlsx_dependant_orphelin(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.cellules is not None
    grille = feuillet.cellules["Feuille"]

    assert grille[1][1].formule is None
    assert grille[1][1].valeur == 5


def test_formule_plage_demarrant_en_b2_est_attachee_a_c2(tmp_path):
    """La grille d'`iter_rows` part de (1,1) : C2 ne doit pas glisser en B1."""
    openpyxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / "decalee.xlsx"
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Feuille"
    feuille["B2"] = 7
    feuille["C2"] = "=B2*2"
    classeur.save(str(chemin))

    feuillet = S.lire_classeur_feuillet(str(chemin))
    assert feuillet.cellules is not None
    grille = feuillet.cellules["Feuille"]

    assert grille[1][2].formule == "=B2*2"
    assert grille[1][1].formule is None
    assert grille[0][1].formule is None


def test_provenance_a1_d_une_plage_b2(tmp_path):
    """B2:C2 → la donnée B3:C3 est `ligne=3` et `F!B3:C3`, jamais ligne 4.

    L'ancien code additionnait `min_row`/`min_col` de `calculate_dimension()` à
    des positions de grille déjà 0-based depuis A1 : la provenance mentait d'une
    ligne et d'une colonne. On vérifie la représentation ENTIÈRE (rang, plage,
    en-têtes, valeurs), pas seulement la grille de cellules.
    """
    chemin = _ecrire_xlsx_plage(tmp_path, "b2.xlsx", 2, 3)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.minima["F"] == (2, 2), "dimension déclarée : B2:C3"

    schema = S.extract_schema(feuillet.tables)
    assert schema.headers["F"] == ["", "Nom", "Valeur"], (
        "la cellule A2 vide garde l'alignement de la grille ; elle ne doit "
        "jamais devenir la chaîne 'None'"
    )
    assert "None" not in schema.headers["F"]

    chunks = S.build_chunks(
        feuillet.tables,
        schema,
        cellules=feuillet.cellules,
    )
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["ligne"] == 3
    assert chunk["repere"] == "F!B3:C3"
    assert "Nom=a" in chunk["texte_affiche"]
    assert "Valeur=1" in chunk["texte_affiche"]
    assert chunk["texte_embedde"].endswith("[F!B3:C3]")

    rendu = S.représenter_feuillet(feuillet)
    assert "F!B3:C3" in rendu
    assert "None" not in rendu


def test_ligne_vide_en_tete_n_est_pas_l_entete(tmp_path):
    """Une feuille B3:C4 : les lignes 1-2 vides ne sont pas l'en-tête."""
    chemin = _ecrire_xlsx_plage(tmp_path, "vide_avant.xlsx", 3, 4)
    feuillet = S.lire_classeur_feuillet(chemin)

    schema = S.extract_schema(feuillet.tables)
    assert schema.headers["F"] == ["", "Nom", "Valeur"]
    assert schema.n_rows["F"] == 1

    chunk = S.build_chunks(
        feuillet.tables,
        schema,
        cellules=feuillet.cellules,
    )[0]
    assert chunk["ligne"] == 4
    assert chunk["repere"] == "F!B4:C4"
    assert "Nom=a" in chunk["texte_affiche"]


def test_formule_sans_cache_occupe_sa_ligne_et_reste_rendue(tmp_path):
    """`B2="=1+1"` sans cache : la ligne 2 existe dans le classeur.

    Ses valeurs sont toutes `None`, mais la formule occupe la ligne. Le schéma,
    les chunks et la représentation doivent la rendre — et compter EXACTEMENT
    les lignes rendues, pas une ligne fantôme ni une ligne perdue.
    """
    chemin = _ecrire_xlsx_formule_seule_sans_cache(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.cellules is not None

    schema = S.extract_schema(feuillet.tables, cellules=feuillet.cellules)
    assert schema.n_rows["Feuille"] == 2, "ligne 2 (formule) + ligne 3 (données)"
    assert "2 lignes" in schema.caption

    chunks = S.build_chunks(
        feuillet.tables,
        schema,
        cellules=feuillet.cellules,
    )
    assert [chunk["ligne"] for chunk in chunks] == [2, 3]
    assert "=1+1" in chunks[0]["texte_affiche"]
    assert "formule" in chunks[0]["texte_affiche"]
    assert "résultat en cache absent" in chunks[0]["texte_embedde"]
    assert "Nom=a" in chunks[1]["texte_affiche"]

    rendu = S.représenter_feuillet(feuillet)
    assert "formules : 1" in rendu
    assert "=1+1" in rendu
    assert "résultat en cache absent" in rendu
    assert "2 ligne(s) de données" in rendu


def test_formule_litteralement_vide_n_occupe_pas_sa_ligne():
    """`B2="="` est une formule vide : la ligne n'est pas occupée.

    `B2="=1+1"`, elle, porte une formule qui calcule quelque chose : la ligne
    reste occupée, avec ou sans résultat en cache. Le contrôle doit couvrir les
    deux chemins — la valeur de grille et l'objet `CelluleFeuillet`.
    """
    assert not S._cellule_non_vide("=")
    assert not S._cellule_non_vide("  =  ")
    assert S._cellule_non_vide("=1+1")

    assert not S._ligne_occupée([None, "="])
    assert S._ligne_occupée([None, "=1+1"])

    vide = S.CelluleFeuillet(valeur=None, formule="=")
    pleine = S.CelluleFeuillet(valeur=None, formule="=1+1")
    assert not S._ligne_occupée(
        [None, None],
        [S.CelluleFeuillet(valeur=None), vide],
    )
    assert S._ligne_occupée(
        [None, None],
        [S.CelluleFeuillet(valeur=None), pleine],
    )


def test_translation_ne_decale_pas_les_noms_de_feuilles():
    """`Q1!A1` est une feuille Q1, pas la cellule Q1 : elle reste Q1!B1.

    Mêmes pièges : `QTR1`, `Tab1`, `S1` — des noms qui ressemblent à des
    adresses — et les noms entre apostrophes (`'Ma Feuille'`, `'Q1'`).
    """
    assert S._traduire_formule_partagee("Q1!A1", 0, 1) == "Q1!B1"
    assert S._traduire_formule_partagee("'Ma Feuille'!A1", 0, 1) == "'Ma Feuille'!B1"
    assert S._traduire_formule_partagee("'Q1'!A1", 0, 1) == "'Q1'!B1"
    assert (
        S._traduire_formule_partagee("QTR1!A1+Tab1!B2+S1!C3", 1, 1)
        == "QTR1!B2+Tab1!C3+S1!D4"
    )


def test_formule_partagee_ne_decale_pas_les_noms_de_feuilles(tmp_path):
    """Bout en bout XML : le dépendant C1 reconstitue `Q1!B1`, pas `R1!B1`."""
    chemin = _ecrire_xlsx_formule_partagee_noms_feuilles(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.cellules is not None
    grille = feuillet.cellules["Feuille"]

    attendu = "=Q1!B1+'Ma Feuille'!B1"
    assert grille[0][2].formule == attendu
    assert grille[0][1].formule == "=Q1!A1+'Ma Feuille'!A1"
    assert "R1!B1" not in (grille[0][2].formule or "")


def test_translation_plages_absolues_et_ref_negatif():
    """`$`, plages `A1:B2` et sortie par le bord gauche/haut → `#REF!`."""
    assert (
        S._traduire_formule_partagee("SUM(Sheet1!$A$1:B2)", 1, 1)
        == "SUM(Sheet1!$A$1:C3)"
    )
    assert (
        S._traduire_formule_partagee("'Ma Feuille'!A1:B2", 1, 1) == "'Ma Feuille'!B2:C3"
    )
    assert S._traduire_formule_partagee("A1", 0, -1) == "#REF!"
    assert S._traduire_formule_partagee("A1", -1, 0) == "#REF!"


def test_translation_borne_la_grille_excel():
    """XFD1/A1048576 sont les dernières cellules ; au-delà, Excel écrit #REF!.

    L'oracle openpyxl produit `XFE1`/`A1048577` — des adresses hors grille qui
    n'existent pas. On suit le comportement Excel, le plus sûr.
    """
    assert S._traduire_formule_partagee("XFD1", 0, 1) == "#REF!"
    assert S._traduire_formule_partagee("A1048576", 1, 0) == "#REF!"
    assert S._traduire_formule_partagee("XFC1", 0, 1) == "XFD1"
    assert S._traduire_formule_partagee("A1048575", 1, 0) == "A1048576"


def test_formule_partagee_dependante_avant_maitre(tmp_path):
    """L'ordre XML ne compte pas : un dépendant peut précéder son maître."""
    chemin = _ecrire_xlsx_dependant_avant_maitre(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.cellules is not None
    grille = feuillet.cellules["Feuille"]

    assert grille[0][1].formule == "=A1*2", "dépendant B1 résolu depuis le maître B2"
    assert grille[1][1].formule == "=A2*2"


def test_formule_tableau_reelle_garde_son_texte(tmp_path):
    """Une vraie `t="array"` d'ancrage est une formule, pas une valeur vide."""
    chemin = _ecrire_xlsx_formule_tableau(tmp_path)
    feuillet = S.lire_classeur_feuillet(chemin)
    assert feuillet.cellules is not None
    grille = feuillet.cellules["Feuille"]

    assert grille[0][1].formule == "=A1:A2*2"
    assert grille[1][1].formule is None
    assert grille[1][1].valeur == 4


def test_feuilles_par_pandas_refuse_avant_materialisation_et_ferme(monkeypatch):
    """Trop de feuilles : refus AVANT `parse`, et le classeur est fermé."""
    pandas = pytest.importorskip("pandas")
    fermetures: list[bool] = []

    class _FauxClasseur:
        sheet_names = ("F0", "F1", "F2")

        def parse(self, *args, **kwargs):
            raise AssertionError("aucune feuille ne doit être matérialisée")

        def close(self):
            fermetures.append(True)

    monkeypatch.setattr(S, "MAX_FEUILLES", 2)
    monkeypatch.setattr(pandas, "ExcelFile", lambda chemin: _FauxClasseur())

    with pytest.raises(S.TableurRefuse, match="feuilles"):
        S._feuilles_par_pandas("faux.xlsx")
    assert fermetures == [True], "le `finally` doit fermer le classeur refusé"


def test_feuilles_par_pandas_ferme_meme_quand_la_lecture_echoue(monkeypatch):
    """Un moteur qui casse ne doit pas laisser le classeur ouvert."""
    pandas = pytest.importorskip("pandas")
    fermetures: list[bool] = []

    class _FauxClasseur:
        sheet_names = ("F",)

        def parse(self, *args, **kwargs):
            raise RuntimeError("moteur en panne")

        def close(self):
            fermetures.append(True)

    monkeypatch.setattr(pandas, "ExcelFile", lambda chemin: _FauxClasseur())

    with pytest.raises(RuntimeError, match="moteur en panne"):
        S._feuilles_par_pandas("faux.xlsx")
    assert fermetures == [True]


def test_ods_trop_de_feuilles_refuse_avant_pandas(tmp_path, monkeypatch):
    """La borne ODS se décide sur `content.xml`, sans matérialiser les feuilles."""
    pandas = pytest.importorskip("pandas")
    appels: list[str] = []

    def _excel_file(chemin):
        appels.append(str(chemin))
        raise AssertionError("pd.ExcelFile ne doit pas parser le DOM ODF")

    monkeypatch.setattr(pandas, "ExcelFile", _excel_file)
    monkeypatch.setattr(S, "_module_present", lambda nom: True)
    monkeypatch.setattr(S, "MAX_FEUILLES", 2)
    chemin = _ecrire_ods_content(tmp_path, 3)

    with pytest.raises(S.TableurRefuse, match="feuilles"):
        S.lire_classeur_feuillet(chemin)
    assert appels == []


def test_ods_entite_xml_refusee_avant_expansion(tmp_path, monkeypatch):
    """Un `<!DOCTYPE`/`<!ENTITY` ODS est refusé AVANT toute expansion mémoire."""
    monkeypatch.setattr(S, "_module_present", lambda nom: True)
    chemin = _ecrire_ods_avec_doctype(tmp_path)

    with pytest.raises(S.TableurRefuse, match="DOCTYPE|entit"):
        S._compter_feuilles_ods(chemin)
    with pytest.raises(S.TableurRefuse, match="DOCTYPE|entit"):
        S.lire_classeur_feuillet(chemin)


def test_xlsx_entite_xml_refusee_avant_expansion(tmp_path):
    """Le flux XML brut des formules xlsx refuse aussi les entités."""
    chemin = _ecrire_xlsx_avec_doctype(tmp_path)
    with pytest.raises(S.TableurRefuse, match="DOCTYPE|entit"):
        S._formules_xlsx(chemin, {"Feuille": "xl/worksheets/sheet1.xml"})


def test_ods_entite_apres_prefixe_borne_refusee(tmp_path, monkeypatch):
    """Un DOCTYPE placé APRÈS 64 Kio n'échappe pas au refus.

    Le préfixe borné est un refus PRÉCOCE, pas la barrière : `defusedxml`
    interdit DTD/entités pendant le flux, quelle que soit leur position.
    """
    monkeypatch.setattr(S, "_module_present", lambda nom: True)
    chemin = _ecrire_ods_avec_doctype_apres_prefixe(tmp_path)

    with pytest.raises(S.TableurRefuse, match="DOCTYPE|entit|DTD"):
        S._compter_feuilles_ods(chemin)
    with pytest.raises(S.TableurRefuse, match="DOCTYPE|entit|DTD"):
        S.lire_classeur_feuillet(chemin)


def test_sans_defusedxml_le_parsing_xml_refuse_explicitement(tmp_path, monkeypatch):
    """Sans `defusedxml`, aucun repli stdlib silencieux : refus explicite."""
    monkeypatch.setattr(S, "_ET_DURCI", None)

    with pytest.raises(S.TableurRefuse, match="defusedxml"):
        S._iterparse_xml(io.BytesIO(b"<root/>"), ("end",))

    monkeypatch.setattr(S, "_module_present", lambda nom: True)
    chemin = _ecrire_ods_content(tmp_path, 1)
    with pytest.raises(S.TableurRefuse, match="defusedxml"):
        S._compter_feuilles_ods(chemin)


def test_zip_bombe_refusee_avant_load_workbook(tmp_path, monkeypatch):
    """Petit compressé, énorme décompressé : refus AVANT openpyxl."""
    bombe = tmp_path / "bombe.xlsx"
    with zipfile.ZipFile(bombe, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # 20 Mio de zéros se compressent à quelques kilo-octets : ratio >> 100.
        archive.writestr("xl/worksheets/sheet1.xml", b"\x00" * (20 * 1024 * 1024))

    appels: list[str] = []

    def _load_workbook(*args, **kwargs):
        appels.append(str(args[0]) if args else "?")
        raise AssertionError("load_workbook ne doit pas être appelé sur une bombe")

    import openpyxl

    monkeypatch.setattr(openpyxl, "load_workbook", _load_workbook)

    with pytest.raises(S.TableurRefuse, match="ratio"):
        S.lire_classeur_feuillet(str(bombe))
    assert appels == []


def test_zip_trop_de_membres_refuse_avant_load_workbook(tmp_path, monkeypatch):
    """10 001 membres : l'annuaire seul suffit, openpyxl n'est jamais ouvert."""
    archive_path = tmp_path / "membres.xlsx"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(10_001):
            archive.writestr(f"m/{index}.xml", b"x")

    appels = _interdire_load_workbook(monkeypatch)
    with pytest.raises(S.TableurRefuse, match="membres"):
        S.lire_classeur_feuillet(str(archive_path))
    assert appels == []


def test_zip_profondeur_excessive_refusee_avant_load_workbook(tmp_path, monkeypatch):
    """17 niveaux de chemin interne : refus sur l'annuaire, avant openpyxl."""
    archive_path = tmp_path / "profonde.xlsx"
    nom = "/".join(f"d{index}" for index in range(16)) + "/f.xml"
    assert len(nom.split("/")) == 17

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(nom, b"x")

    appels = _interdire_load_workbook(monkeypatch)
    with pytest.raises(S.TableurRefuse, match="profond"):
        S.lire_classeur_feuillet(str(archive_path))
    assert appels == []


def test_zip_taille_decompressee_refusee_avant_load_workbook(tmp_path, monkeypatch):
    """200 Mio + 1 octet décompressés : refus sur la taille, avant openpyxl."""
    archive_path = tmp_path / "decompresse.xlsx"
    bloc = b"\x00" * (1024 * 1024)
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        with archive.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as flux:
            for _ in range(200):
                flux.write(bloc)
            flux.write(b"\x00")

    appels = _interdire_load_workbook(monkeypatch)
    with pytest.raises(S.TableurRefuse, match="décompressés"):
        S.lire_classeur_feuillet(str(archive_path))
    assert appels == []


def test_trop_de_lignes_refuse_sans_repli(tmp_path, monkeypatch):
    chemin = _ecrire_xlsx(tmp_path, lignes=6)
    monkeypatch.setattr(S, "MAX_LIGNES_FEUILLE", 3)
    with pytest.raises(S.TableurRefuse, match="lignes"):
        S.lire_classeur_feuillet(chemin)


def test_trop_de_cellules_refuse(tmp_path, monkeypatch):
    chemin = _ecrire_xlsx(tmp_path, lignes=4)
    monkeypatch.setattr(S, "MAX_CELLULES_TOTAL", 3)
    with pytest.raises(S.TableurRefuse, match="cellules"):
        S.lire_classeur_feuillet(chemin)


def test_trop_de_feuilles_refuse(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / "beaucoup.xlsx"
    classeur = openpyxl.Workbook()
    for index in range(3):
        feuille = classeur.create_sheet(f"F{index}")
        feuille.append(["x"])
    classeur.save(str(chemin))
    monkeypatch.setattr(S, "MAX_FEUILLES", 2)
    with pytest.raises(S.TableurRefuse, match="feuilles"):
        S.lire_classeur_feuillet(str(chemin))


def test_csv_trop_long_refuse(tmp_path, monkeypatch):
    chemin = tmp_path / "long.csv"
    chemin.write_text("a,b\n" + "1,2\n" * 10, encoding="utf-8")
    monkeypatch.setattr(S, "MAX_LIGNES_FEUILLE", 3)
    with pytest.raises(S.TableurRefuse, match="CSV"):
        S.read_csv_tables(str(chemin))


# ---------------------------------------------------------------------------
# 7.2 — Limites unifiées, base64 et lecture IPC bornée
# ---------------------------------------------------------------------------


def test_les_limites_viennent_du_point_unique():
    assert A.MAX_UPLOAD_BYTES == INPUT_LIMITS["upload_bytes"] == 20 * 1024 * 1024
    assert A.MAX_PREVIEW_BYTES == INPUT_LIMITS["preview_bytes"]
    assert F.MAX_PDF_SIZE == INPUT_LIMITS["upload_bytes"]
    assert F.MAX_IMAGE_SIZE == INPUT_LIMITS["image_bytes"]
    assert ipc.MAX_LINE_BYTES == INPUT_LIMITS["ipc_line_bytes"]
    assert G.MAX_PDF_PAGES == INPUT_LIMITS["pdf_max_pages"] == 20
    assert G.PDF_MAX_PIXELS == INPUT_LIMITS["pdf_max_pixels"]
    assert S.MAX_LIGNES_FEUILLE == INPUT_LIMITS["sheet_max_rows"]
    assert S.MAX_CELLULES_TOTAL == INPUT_LIMITS["sheet_max_cells"]


def test_pdf_max_pages_aligne_gate_et_ingestion():
    from backend.ingestion import attachment_ingest as AI

    assert AI.MAX_PAGES_PDF_INGESTION == G.MAX_PDF_PAGES == 20


def test_octets_base64_valides_et_invalides():
    assert A._octets_base64(base64.b64encode(b"abc").decode()) == b"abc"
    with pytest.raises(ValueError, match="base64"):
        A._octets_base64("!!!pas-du-base64!!!")


def test_fichier_21_mio_refuse_avant_decodage(monkeypatch):
    """Le plafond est vérifié sur la taille ESTIMÉE, avant `b64decode`."""
    import base64 as module_base64

    def _interdit(*args, **kwargs):
        raise AssertionError("b64decode ne doit pas être appelé au-delà du plafond")

    monkeypatch.setattr(module_base64, "b64decode", _interdit)

    trop_gros = b"%PDF" + b"x" * (21 * 1024 * 1024)
    encode = base64.b64encode(trop_gros).decode("ascii")
    with pytest.raises(ValueError, match="trop volumineux"):
        A.stage_picker_bytes(
            None,
            {"requestId": "phase7-gros", "filename": "gros.pdf", "bytes": encode},
        )


class _FluxGeant:
    """Flux qui ne matérialise jamais plus que la taille demandée par readline."""

    def __init__(self, taille: int) -> None:
        self.reste = taille
        self.demande_max = 0

    def readline(self, size: int = -1) -> str:
        self.demande_max = max(self.demande_max, size)
        if self.reste <= 0:
            return ""
        n = min(size if size > 0 else self.reste, self.reste)
        self.reste -= n
        return "x" * n


def test_ligne_stdin_geante_refusee_sans_accumulation(monkeypatch):
    flux = _FluxGeant(ipc.MAX_LINE_BYTES + 100_000)
    monkeypatch.setattr(ipc.sys, "stdin", flux)
    emis: list[tuple[str, dict, dict]] = []
    monkeypatch.setattr(
        ipc,
        "emit",
        lambda event, payload, **kwargs: emis.append((event, payload, kwargs)) or True,
    )

    thread = threading.Thread(
        target=ipc.stdin_loop, args=(lambda _r: None,), daemon=True
    )
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert flux.demande_max <= ipc.MAX_LINE_BYTES + 1
    assert [payload["code"] for _, payload, _ in emis if "code" in payload] == [
        "message_too_large"
    ]


def test_stdin_binaire_reel_traite_la_requete(monkeypatch):
    """`sys.stdin.buffer` réel : la branche OCTETS du lecteur, pas le repli texte."""
    requete = {"request_id": "binaire-1", "method": "health", "params": {}}
    flux = io.TextIOWrapper(
        io.BytesIO((json.dumps(requete) + "\n").encode("utf-8")), encoding="utf-8"
    )
    monkeypatch.setattr(ipc.sys, "stdin", flux)

    recus: list[dict] = []
    thread = threading.Thread(target=ipc.stdin_loop, args=(recus.append,), daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert [recu["request_id"] for recu in recus] == ["binaire-1"]


def test_lecture_stdin_binaire_bornee_sans_accumulation():
    """La borne de la branche binaire se compte en OCTETS, et vide la queue."""
    flux = io.TextIOWrapper(io.BytesIO(b"a" * 200 + b"\n"), encoding="utf-8")

    texte, trop_long = ipc._lire_ligne_bornee(flux, max_bytes=64)
    assert trop_long is True
    assert len(texte.encode("utf-8")) == 64

    # La queue a été consommée jusqu'au saut de ligne : la lecture suivante est EOF.
    suite, encore_trop_long = ipc._lire_ligne_bornee(flux, max_bytes=64)
    assert (suite, encore_trop_long) == ("", False)


def test_stage_picker_bytes_5_mio_via_le_transport_reel(tmp_path, monkeypatch):
    """Le vrai `stdin_loop` + le vrai handler : 5 Mio en base64 passent."""
    data = tmp_path / "data"
    (data / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(data))

    contenu = b"%PDF-1.4\n" + b"x" * (5 * 1024 * 1024)
    requete = {
        "request_id": "phase7-transport",
        "method": "stage.picker_bytes",
        "params": {
            "requestId": "phase7-transport",
            "filename": "gros.pdf",
            "bytes": base64.b64encode(contenu).decode("ascii"),
        },
    }
    monkeypatch.setattr(ipc.sys, "stdin", io.StringIO(json.dumps(requete) + "\n"))
    evenements: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        backend_main,
        "emit",
        lambda event, payload, **kwargs: evenements.append((event, payload)),
    )

    thread = threading.Thread(
        target=ipc.stdin_loop, args=(backend_main.handle,), daemon=True
    )
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert [event for event, _ in evenements] == ["stage_picker_bytes"]
    items = evenements[0][1]["items"]
    assert items[0]["size_bytes"] == len(contenu)
    assert items[0]["name"] == "gros.pdf"


def test_apercu_serialisable_en_json(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(data))

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    item = A.stage_picker_bytes(
        None,
        {"requestId": "phase7-apercu", "filename": "vase.png", "bytes": png},
    )["items"][0]
    resultat = A.get_staged_preview_bytes(None, {"path": item["stage_item_id"]})

    serialise = json.dumps({"event": "get_staged_preview_bytes", "payload": resultat})
    assert "base64" in serialise
    assert base64.b64decode(resultat["data"]).startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# 7.3 — Démarrage non bloqué
# ---------------------------------------------------------------------------


def test_le_rejeu_et_la_reprise_ne_bloquent_pas_le_demarrage(monkeypatch):
    demarre = threading.Event()
    libere = threading.Event()
    termine = threading.Event()

    def _replay_lent():
        demarre.set()
        libere.wait(timeout=5)
        termine.set()

    monkeypatch.setattr(backend_main, "_replay_pending_artifact_events", _replay_lent)
    from backend.ingestion import reprise as reprise_module

    monkeypatch.setattr(reprise_module, "demarrer_reprise", lambda **kwargs: {})

    fil = backend_main.demarrer_reprise_demarrage()

    assert demarre.wait(timeout=2), "le fil doit démarrer"
    assert not termine.is_set(), "le lanceur ne doit pas attendre le rejeu"
    assert fil.daemon is True

    libere.set()
    fil.join(timeout=2)
    assert termine.is_set()


# ---------------------------------------------------------------------------
# 7.4 — Plafonds PDF
# ---------------------------------------------------------------------------


def test_page_pdf_geante_refusee_avant_allocation(monkeypatch):
    class _PageGeante:
        rect = types.SimpleNamespace(width=10_000_000, height=10_000_000)

        def get_pixmap(self, dpi=None):
            raise AssertionError("get_pixmap ne doit pas être appelé")

    with pytest.raises(ValueError, match="trop grande"):
        G.render_page_png(_PageGeante())


def test_pixmap_geant_refuse_avant_frombytes(monkeypatch):
    class _PageSansRect:
        def get_pixmap(self, dpi=None):
            return types.SimpleNamespace(width=100_000, height=100_000, samples=b"")

    from PIL import Image

    monkeypatch.setattr(
        Image,
        "frombytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Image.frombytes ne doit pas allouer une page géante")
        ),
    )

    with pytest.raises(ValueError, match="trop grande"):
        G.render_page_png(_PageSansRect())

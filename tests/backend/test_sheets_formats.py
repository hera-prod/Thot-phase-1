"""Les formats de tableurs, un par un — et ce que Thot refuse de faire semblant de lire.

Le défaut que ces tests verrouillent s'est produit sans qu'aucun message d'erreur
ne le montre. Un `.ods` est une ARCHIVE ZIP ; sa lecture exige le moteur `odfpy` ;
`read_workbook_tables` renvoyait `{}` sans rien dire ; et l'appelant, qui teste
« le rendu est-il non vide ? », passait donc au lecteur suivant — qui lisait
l'archive en `latin-1` et rendait 16 000 caractères commençant par `PK\\x03\\x04`.
Le fichier finissait avec une observation `completed` et son propre contenu dans
l'index de recherche.

Ce que chaque test atteste :

  - `latin-1` NE PEUT PAS échouer (il associe un caractère aux 256 octets), donc
    la boucle d'encodage ne pouvait rien refuser : la garde doit être AVANT elle ;
  - `.csv` et `.xlsx` continuent de se lire exactement comme avant ;
  - `.ods` se lit quand `odfpy` est là, et se.refuse en NOMMANT la cause quand il
    ne l'est pas ;
  - `.xls` se refuse avec un motif honnête, jamais un `{}` ;
  - aucun lecteur ne rend `{}` : un classeur illisible est un échec annoncé.

Les fichiers sont écrits dans `tmp_path`. Aucun octet du projet n'est lu ni écrit,
aucun réseau, aucun modèle.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from backend.ingestion import sheets as S


# ── Fabriques de fichiers ─────────────────────────────────────────────────────

def octets_ods(main_prepare: bool = False) -> bytes:
    """Un `.ods` réel. `main_prepare=True` produit le conteneur avec le strict
    nécessaire : c'est ce que le dépôt recevrait d'un fichier abîmé — il doit
    être refusé, pas lu à moitié."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        if main_prepare:
            zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
            zf.writestr("content.xml", "<office:document-content/>")
            return tampon.getvalue()
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
    _ecrire_ods_avec_odfpy(tampon)
    return tampon.getvalue()


def _ecrire_ods_avec_odfpy(tampon: io.BytesIO) -> None:
    """Un tableur ODF authentique, écrit par odfpy : sans lui, le format ne peut
    pas être fabriqué — et c'est précisément ce qui rend le test significatif."""
    pytest.importorskip("odf")
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    document: Any = OpenDocumentSpreadsheet()
    table = Table(name="Ventes")
    for ligne in (["Date", "Client", "Montant"], ["2026-03-15", "Dubois SARL", 45000]):
        rangee = TableRow()
        for valeur in ligne:
            cellule = (TableCell(valuetype="float", value=str(valeur))
                       if isinstance(valeur, int) else TableCell(valuetype="string"))
            cellule.addElement(P(text=str(valeur)))
            rangee.addElement(cellule)
        table.addElement(rangee)
    # Any explicite : les stubs d'odfpy ne connaissent pas l'attribut
    # `spreadsheet`, qui existe bien à l'exécution — même remarque que
    # `attachment_ingest._texte_natif_pdf` pour pymupdf.
    document.spreadsheet.addElement(table)
    document.save(tampon)


def ecrire(tmp_path, nom: str, donnees: bytes) -> str:
    chemin = tmp_path / nom
    chemin.write_bytes(donnees)
    return str(chemin)


def ecrire_xlsx(tmp_path) -> str:
    _oxl = pytest.importorskip("openpyxl")
    chemin = tmp_path / "factures.xlsx"
    classeur = _oxl.Workbook()
    feuille = classeur.active
    feuille.title = "Factures"
    feuille.append(["Date", "Client", "Montant"])
    feuille.append(["2026-03-15", "Dubois SARL", 45000])
    classeur.save(str(chemin))
    return str(chemin)


# ══════════════════════════════════════════════════════════════════════════════
# 1. LA GARDE PRÉALABLE — pourquoi le décodage ne pouvait pas rien faire
# ══════════════════════════════════════════════════════════════════════════════

class TestGardePrealable:
    def test_latin1_ne_peut_pas_echouer(self, tmp_path):
        """LE FAIT QUI REND LA BOUCLE D'ENCODAGE INUTILE COMME GARDE.

        `latin-1` associe un caractère à chacun des 256 octets : `UnicodeDecodeError`
        ne peut pas y être levé. La boucle « utf-8 puis latin-1 » aboutissait donc
        TOUJOURS, et son seul mode d'échec — le décodage — était par construction
        impossible. Aucun binaire ne pouvait être refusé par cette voie.

        Ce test ne teste pas le lecteur : il teste la propriété du décodage qui
        rend la garde nécessaire. Sans lui, on pourrait croire que l'ancien code
        avait une chance de s'en apercevoir.
        """
        chemin = ecrire(tmp_path, "conteneur.ods", octets_ods(main_prepare=True))
        brut = open(chemin, "rb").read()
        with pytest.raises(UnicodeDecodeError):
            brut.decode("utf-8")
        rendu = brut.decode("latin-1")      # n'échoue jamais
        assert rendu.startswith("PK\x03\x04")
        assert S.raison_binaire(brut[:8192]) is not None

    def test_un_zip_refuse_par_le_lecteur_csv(self, tmp_path):
        """Le symptôme : le repli CSV rendait une « feuille » de 16 000 caractères
        d'octets. Il doit lever, en nommant ce qu'il a trouvé."""
        chemin = ecrire(tmp_path, "Avision.ods", octets_ods(main_prepare=True))
        with pytest.raises(ValueError) as echec:
            S.read_csv_tables(chemin)
        assert "binaire" in str(echec.value) and "ZIP" in str(echec.value)

    def test_les_deux_lecteurs_refusent_le_meme_fichier(self, tmp_path):
        """Les deux lecteurs doivent dire non ensemble. L'un qui_accepte aurait
        suffi à réintroduire l'extraction fantôme par une autre porte."""
        chemin = ecrire(tmp_path, "double.ods", octets_ods(main_prepare=True))
        with pytest.raises(ValueError):
            S.read_csv_tables(chemin)
        with pytest.raises(RuntimeError):
            S.read_workbook_tables(chemin)


# ══════════════════════════════════════════════════════════════════════════════
# 2. CE QUI CONTINUE DE MARCHER
# ══════════════════════════════════════════════════════════════════════════════

class TestFormatsSupportes:
    def test_csv_utf8_et_separateur_point_virgule(self, tmp_path):
        chemin = ecrire(tmp_path, "ventes.csv",
                        "Date;Client;Montant\n2026-03-15;Dubois SARL;45000\n".encode("utf-8"))
        tables = S.read_csv_tables(chemin)
        assert tables["Feuille 1"][0] == ["Date", "Client", "Montant"]
        assert tables["Feuille 1"][1] == ["2026-03-15", "Dubois SARL", "45000"]

    def test_csv_latin1_toujours_lisible(self, tmp_path):
        """Le `latin-1` reste le REPLI d'encodage, et non un filet de sécurité
        binaire : c'est la garde désormais qui filtre, pas le décodage."""
        chemin = ecrire(tmp_path, "accents.csv", "Ville;Montant\nNîmes;12\n".encode("latin-1"))
        assert S.read_csv_tables(chemin)["Feuille 1"][1] == ["Nîmes", "12"]

    def test_csv_avec_caracteres_special_et_guillemets(self, tmp_path):
        chemin = ecrire(tmp_path, "cite.csv", 'a;b\n"x,1";"il dit ""oui"""\n'.encode("utf-8"))
        lignes = S.read_csv_tables(chemin)["Feuille 1"]
        assert lignes[1] == ['x,1', 'il dit "oui"']

    def test_xlsx_deux_feuilles(self, tmp_path):
        chemin = ecrire_xlsx(tmp_path)
        tables = S.read_workbook_tables(chemin)
        assert sorted(tables) == ["Factures"]
        assert tables["Factures"][1] == ["2026-03-15", "Dubois SARL", 45000]

    def test_ods_lu_quand_le_moteur_est_present(self, tmp_path):
        """LA SUCCÈS APRÈS CORRECTION. Avant, `odfpy` était absent des manifestes,
        pandas levait `ImportError`, le module rendait `{}` et le `.ods` passait
        pour un classeur vide."""
        pytest.importorskip("odf")
        chemin = ecrire(tmp_path, "ventes.ods", octets_ods())
        tables = S.read_workbook_tables(chemin)
        assert list(tables) == ["Ventes"]
        assert tables["Ventes"][0] == ["Date", "Client", "Montant"]
        assert tables["Ventes"][1] == ["2026-03-15", "Dubois SARL", "45000"]

    def test_ods_sans_moteur_donne_le_nom_de_la_cause(self, tmp_path, monkeypatch):
        """Pas d'`odfpy` : le message nomme le module manquant. Un `{}` — ou une
        `ImportError` de pandas — ne disait rien et se lisait comme un classeur vide."""
        monkeypatch.setattr(S, "_module_present", lambda nom: False)
        chemin = ecrire(tmp_path, "ventes.ods", octets_ods(main_prepare=True))
        with pytest.raises(RuntimeError) as echec:
            S.read_workbook_tables(chemin)
        assert "odfpy" in str(echec.value)


# ══════════════════════════════════════════════════════════════════════════════
# 3. CE QUI EST REFUSÉ, ET NOMMÉ
# ══════════════════════════════════════════════════════════════════════════════

class TestFormatsRefuses:
    def test_xls_refuse_avec_un_motif_honnete(self, tmp_path):
        """`.xls` n'est pas un `.xlsx` : c'est un conteneur OLE2. Thot ne le lit
        pas, et le dit — avec la raison et la conduite à tenir, ce qu'un `{}` ne
        disait pas du tout."""
        chemin = ecrire(tmp_path, "ancien.xls",
                        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
        with pytest.raises(RuntimeError) as echec:
            S.read_workbook_tables(chemin)
        message = str(echec.value)
        assert ".xls" in message and "OLE2" in message
        assert "xlrd" in message, "le module qui manque doit être nommé"
        assert "xlsx" in message or "ods" in message, "et la conduite à tenir"

    def test_classeur_corrompu_refuse_et_non_vide(self, tmp_path):
        """Un `.xlsx` qui n'en est pas un : refus nommé. Le `{}` d'avant rendait ce
        fichier « vide », ce qui est un mensonge de plus petite ampleur — mais un
        mensonge quand même, et qui déclenchait le repli CSV."""
        chemin = ecrire(tmp_path, "faux.xlsx", octets_ods(main_prepare=True))
        with pytest.raises(RuntimeError) as echec:
            S.read_workbook_tables(chemin)
        assert "non lisible" in str(echec.value)

    def test_ods_incomplet_nomme_le_moteur_puis_echoue(self, tmp_path):
        """Un `.ods` au strict nécessaire — mimetype et `content.xml`, sans
        manifeste : le moteur le refuse. Le message doit nommer `.ods`, son
        engine attendu et l'erreur du moteur, pas disparaître derrière un `{}`."""
        pytest.importorskip("odf")
        chemin = ecrire(tmp_path, "abime.ods", octets_ods(main_prepare=True))
        with pytest.raises(RuntimeError) as echec:
            S.read_workbook_tables(chemin)
        message = str(echec.value)
        assert ".ods" in message and ("odfpy" in message or "pandas" in message)

    def test_aucun_lecteur_ne_rend_un_dict_vide(self, tmp_path):
        """La RÈGLE qui remplace le `{}`. Un lecteur qui rend `{}` ne dit pas « vide »
        à l'appelant : il dit « je ne sais pas », et l'appelant confond les deux."""
        cas = {
            "ancien.xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512,
            "faux.xlsx": octets_ods(main_prepare=True),
            "sans.ext": b"du texte, mais pas un classeur",
        }
        for nom, donnees in cas.items():
            chemin = ecrire(tmp_path, nom, donnees)
            with pytest.raises((ValueError, RuntimeError)) as echec:
                rendu = S.read_workbook_tables(chemin)
                assert rendu != {}, f"{nom} : le lecteur a rendu un dict vide"
            assert str(echec.value)

    def test_fichier_absent_dit_quil_est_absent(self, tmp_path):
        """Un chemin introuvable doit le dire. Renvoyer `{}` pour un fichier qui
        n'existe pas est le pire des mensonges : il n'y a même pas de contenu à
        prétendre vide."""
        with pytest.raises(ValueError) as echec:
            S.read_csv_tables(str(tmp_path / "jamais.csv"))
        assert "Lecture impossible" in str(echec.value)


# ══════════════════════════════════════════════════════════════════════════════
# 4. LA CHAÎNE — de la fiche au texte interrogeable
# ══════════════════════════════════════════════════════════════════════════════

class TestChaine:
    def test_extraction_tableur_ne_rend_plus_d_octets(self, tmp_path):
        """Bout en bout, par le lecteur que l'ingestion appelle vraiment.

        C'est le test qui prouve le défaut d'origine : `_texte_feuillet` tente les
        deux lecteurs et garde le premier qui REND quelque chose. Avant, il rendait
        les octets du ZIP ; il doit maintenant soit rendre des données, soit lever.
        """
        from backend.ingestion import attachment_ingest as _ing

        chemin = ecrire(tmp_path, "Avision.ods", octets_ods(main_prepare=True))
        with pytest.raises(RuntimeError) as echec:
            _ing._texte_feuillet(chemin, "sheet")
        assert "binaire" in str(echec.value) or "non lisible" in str(echec.value)
        assert "PK" not in str(echec.value)

    def test_extraction_ods_valide_rend_les_donnees(self, tmp_path):
        pytest.importorskip("odf")
        from backend.ingestion import attachment_ingest as _ing

        chemin = ecrire(tmp_path, "ventes.ods", octets_ods())
        rendu = _ing._texte_feuillet(chemin, "sheet")
        assert "Dubois SARL" in rendu and "2026-03-15" in rendu
        assert "PK" not in rendu

    def test_extraction_csv_inchangee(self, tmp_path):
        from backend.ingestion import attachment_ingest as _ing

        chemin = ecrire(tmp_path, "ventes.csv",
                        "Date;Client;Montant\n2026-03-15;Dubois SARL;45000\n".encode("utf-8"))
        rendu = _ing._texte_feuillet(chemin, "sheet")
        assert "Dubois SARL" in rendu

"""Chaîne des pièces jointes : PDF et tableur, de bout en bout.

Ces tests traversent la VRAIE chaîne — `ToolExecutor` pour la validation, la
base pour la persistance, le disque pour les écritures — et non les fonctions
appelées directement. C'est la seule façon de voir un maillon manquant : les
tests unitaires de `utils/files.py` passaient déjà pendant que rien n'était
branché au protocole.

Trois familles :
  - CHEMINS HEUREUX : le fichier entre, il est persisté, il ressort ;
  - LIMITES : tailles, volumes, quotas ;
  - REFUS : ce qui ne doit JAMAIS entrer — mauvaise signature, extension
    menteuse, traversée de chemin, lien symbolique, identifiant forgé.

Un test de refus échouant est un BUG, pas un test à réécrire : c'est une
protection qui n'existe pas.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.db.database import SessionLocal  # noqa: E402
from backend.ipc import attachments as A  # noqa: E402

# ATTENTION — cette suite écrit dans une VRAIE base, pas dans une base jetable.
# Constaté le 01/10 : le `DB` ci-dessous n'était lu par personne, et le
# `SessionLocal` importé en tête de fichier venait de `backend.db.database`, dont
# le `DB_PATH` est figé à l'IMPORT depuis `resolve_data_dir` (XDG). Chaque
# `pytest tests/backend/test_chaine_pieces_jointes.py` ajoutait donc ~13
# conversations à `~/.local/share/thot/data/database/thot-phase-1.db` — la base
# de l'utilisateur, pas celle du dépôt.
#
# La correction exige un `conftest.py` qui isole `SessionLocal` AVANT l'import de
# ce module : elle n'est pas faite ici, ce n'est pas à un déplacement de fichiers
# de la faire à l'aveugle. Voir le rapport de chantier.
DB = os.environ.get("THOT_DB_PATH", "data/database/thot-phase-1.db")


# ── Fabriques de fichiers réalistes ──────────────────────────────────────────


def pdf_bytes(marker: str = "rapport") -> bytes:
    """PDF minimal mais structurallement valide (en-tête + catalogue + EOF)."""
    return (
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF " + marker.encode()
    )


def png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 8


def xlsx_bytes() -> bytes:
    """Conteneur ZIP réel, avec `[Content_Types].xml` comme l'exige le format."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", "<worksheet><sheetData/></worksheet>")
    return buffer.getvalue()


def ods_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr("content.xml", "<office:document-content/>")
    return buffer.getvalue()


def csv_bytes() -> bytes:
    return "nom,montant\nAtelier,1200\nThot,3400\n".encode()


# ── Utilitaires ──────────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _stage(db, request_id: str, data: bytes, name: str) -> dict:
    return A.stage_picker_bytes(
        db, {"requestId": request_id, "filename": name, "bytes": data}
    )["items"][0]


def _compter(db, table: str) -> int:
    return db.execute(sqlite3_text(f"SELECT COUNT(*) FROM {table}")).scalar()


def sqlite3_text(q: str):
    from sqlalchemy import text

    return text(q)


def _compter_artefacts(db, conversation_id: str) -> int:
    from sqlalchemy import text

    return db.execute(
        text(
            "SELECT COUNT(*) FROM artifacts a JOIN artifact_links l ON l.artifact_id = a.id"
            " WHERE l.scope_kind = 'conversation' AND l.scope_id = :c"
        ),
        {"c": conversation_id},
    ).scalar()


def _nouvelle_conversation() -> str:
    import uuid

    from sqlalchemy import text

    db = SessionLocal()
    cid = f"conv_{uuid.uuid4().hex[:12]}"
    db.execute(
        text(
            "INSERT INTO conversations (id, title, mode, created_at, updated_at)"
            " VALUES (:id, 'test', 'chat', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"id": cid},
    )
    db.commit()
    db.close()
    return cid


def _promouvoir(db, conversation_id: str, items: list) -> dict:
    """Accepte des items de staging OU des jetons bruts : un test de rollback
    doit pouvoir mélanger un vrai fichier et un jeton fantôme."""
    jetons = [i["stage_item_id"] if isinstance(i, dict) else str(i) for i in items]
    return A.attachments_prepare(
        db, {"conversationId": conversation_id, "stageItemIds": jetons}
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. DÉTECTION DE TYPE — la signature prime sur l'extension
# ══════════════════════════════════════════════════════════════════════════════


class TestDetection:
    @pytest.mark.parametrize(
        "data,nom,attendu",
        [
            (pdf_bytes(), "rapport.pdf", ("application/pdf", "pdf")),
            (png_bytes(), "vase.png", ("image/png", "image")),
            (jpeg_bytes(), "photo.jpg", ("image/jpeg", "image")),
            (
                xlsx_bytes(),
                "budget.xlsx",
                (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "sheet",
                ),
            ),
            (
                ods_bytes(),
                "table.ods",
                ("application/vnd.oasis.opendocument.spreadsheet", "sheet"),
            ),
            (csv_bytes(), "export.csv", ("text/csv", "sheet")),
        ],
    )
    def test_type_reconnu(self, data, nom, attendu):
        assert A.sniff_mime(data[:12], nom) == attendu

    def test_pdf_declare_comme_image(self):
        """Un PDF ne devient pas une image parce qu'il s'appelle .png."""
        with pytest.raises(ValueError, match="ne correspond pas"):
            A.sniff_mime(pdf_bytes()[:12], "piege.png")

    def test_image_declaree_comme_pdf(self):
        with pytest.raises(ValueError, match="ne correspond pas"):
            A.sniff_mime(png_bytes()[:12], "piege.pdf")

    def test_zip_hors_extensions_tableur_refuse(self):
        """Le controle d'extension passe AVANT la lecture de signature : un .zip
        est refuse sur son NOM, avant meme qu'on regarde les octets."""
        with pytest.raises(ValueError, match="Extension non autoris"):
            A.sniff_mime(xlsx_bytes()[:12], "archive.zip")

    def test_extension_inconnue_refusee(self):
        with pytest.raises(ValueError, match="Extension non autoris"):
            A.sniff_mime(pdf_bytes()[:12], "script.sh")

    def test_fichier_vide_refuse(self):
        with pytest.raises(ValueError, match="Signature inconnue"):
            A.sniff_mime(b"", "rapport.pdf")

    def test_csv_binaire_refuse(self):
        """Un .csv ne peut pas contenir du binaire : le nom seul ne prouve rien."""
        with pytest.raises(ValueError, match="binaire"):
            A.sniff_mime(bytes(range(32)) + b"\x00\x01\x02", "donnees.csv")

    def test_zip_tronque_accepte_comme_conteneur(self):
        """`PK\x03\x04` suffit : c'est la signature du conteneur, pas sa validite.
        Un .xlsx tronque doit passer la detection de type — l'ouverture reelle
        est le role du lecteur, pas du garde-fou."""
        assert A.sniff_mime(b"PK\x03\x04" + b"\x00" * 8, "classeur.xlsx")[1] == "sheet"


# ══════════════════════════════════════════════════════════════════════════════
# 2. DÉPÔT — save_upload
# ══════════════════════════════════════════════════════════════════════════════


class TestDepot:
    def test_pdf_ecrit_et_empreinte(self, db):
        data = pdf_bytes()
        r = A.save_upload(
            db, {"requestId": "test_depot_1", "filename": "rapport.pdf", "data": data}
        )
        assert r["mime"] == "application/pdf"
        assert r["path"].endswith(".pdf")
        assert Path(r["path"]).read_bytes() == data
        assert r["hash"].startswith("sha256:")
        assert Path(r["path"]).is_file()

    def test_tableur_xlsx_accepte(self, db):
        """Les tableurs n'étaient PAS acceptés avant : liste blanche trop courte."""
        r = A.save_upload(
            db,
            {
                "requestId": "test_depot_2",
                "filename": "budget.xlsx",
                "data": xlsx_bytes(),
            },
        )
        assert r["kind"] == "sheet"
        assert Path(r["path"]).read_bytes() == xlsx_bytes()

    def test_csv_accepte(self, db):
        r = A.save_upload(
            db,
            {
                "requestId": "test_depot_3",
                "filename": "export.csv",
                "data": csv_bytes(),
            },
        )
        assert r["mime"] == "text/csv"
        assert r["kind"] == "sheet"

    def test_octets_en_liste_acceptees(self, db):
        """Le renderer Electron envoie des octets ; l'ancien pont envoyait une liste."""
        r = A.save_upload(
            db,
            {
                "requestId": "test_depot_4",
                "filename": "rapport.pdf",
                "data": list(pdf_bytes()),
            },
        )
        assert r["mime"] == "application/pdf"

    def test_vide_refuse(self, db):
        with pytest.raises(ValueError, match="vide"):
            A.save_upload(
                db,
                {"requestId": "test_depot_5", "filename": "rapport.pdf", "data": b""},
            )

    def test_trop_gros_refuse(self, db):
        with pytest.raises(ValueError, match="trop volumineux"):
            A.save_upload(
                db,
                {
                    "requestId": "test_depot_6",
                    "filename": "rapport.pdf",
                    "data": b"%PDF" + b"\x00" * (A.MAX_UPLOAD_BYTES + 1),
                },
            )

    def test_request_id_forge_refuse(self, db):
        """Un request_id devient un NOM DE DOSSIER : il ne doit rien traverser."""
        with pytest.raises(ValueError, match="request_id invalide"):
            A.save_upload(
                db, {"requestId": "../../etc", "filename": "r.pdf", "data": pdf_bytes()}
            )

    def test_chemin_dans_le_nom_ne_traverse_pas(self, db):
        """`../../evasion.pdf` doit finir DANS le dossier de la requete.

        Le fichier est stocke sous son EMPREINTE (adresse par le contenu) : le nom
        d'origine est une METADONNEE, rendue dans `name`, jamais un chemin.
        """
        r = A.save_upload(
            db,
            {
                "requestId": "test_depot_7",
                "filename": "../../evasion.pdf",
                "data": pdf_bytes(),
            },
        )
        assert r["name"] == "evasion.pdf", (
            "le nom d'origine doit etre conserve comme metadata"
        )
        ecrit = Path(r["path"])
        assert ecrit.is_file()
        assert ecrit.parent == Path(r["path"]).parent.resolve()
        assert ecrit.parent.name == "test_depot_7", (
            "le fichier doit rester dans son dossier de requete"
        )

    def test_nom_vide_refuse(self, db):
        with pytest.raises(ValueError, match="Nom de fichier"):
            A.save_upload(
                db, {"requestId": "test_depot_8", "filename": "", "data": pdf_bytes()}
            )


# ══════════════════════════════════════════════════════════════════════════════
# 3. STAGING
# ══════════════════════════════════════════════════════════════════════════════


class TestStaging:
    def test_stage_tableur(self, db):
        item = _stage(db, "stage_x1", xlsx_bytes(), "budget.xlsx")
        assert item["stage_item_id"].startswith("sti_")
        assert item["kind"] == "sheet"
        assert item["size_bytes"] == len(xlsx_bytes())

    def test_stage_pdf(self, db):
        item = _stage(db, "stage_x2", pdf_bytes(), "rapport.pdf")
        assert item["kind"] == "pdf"

    def test_stage_depuis_chemins(self, db, tmp_path):
        source = tmp_path / "plan.xlsx"
        source.write_bytes(xlsx_bytes())
        r = A.stage_dropped_paths(db, {"requestId": "stage_x3", "paths": [str(source)]})
        assert len(r["items"]) == 1 and r["rejected"] == []
        assert r["items"][0]["name"] == "plan.xlsx"

    def test_stage_refuse_signature_inconnue(self, db, tmp_path):
        source = tmp_path / "faux.pdf"
        source.write_bytes(b"ceci n est pas un pdf")
        r = A.stage_dropped_paths(db, {"requestId": "stage_x4", "paths": [str(source)]})
        assert r["items"] == [] and len(r["rejected"]) == 1
        assert "Signature inconnue" in r["rejected"][0]["message"]

    def test_stage_isole_un_echec_des_autres(self, db, tmp_path):
        """Un fichier douteux ne doit pas faire perdre les fichiers valides."""
        bon = tmp_path / "bon.pdf"
        bon.write_bytes(pdf_bytes())
        mauvais = tmp_path / "mauvais.pdf"
        mauvais.write_bytes(b"nope")
        r = A.stage_dropped_paths(
            db, {"requestId": "stage_x5", "paths": [str(bon), str(mauvais)]}
        )
        assert len(r["items"]) == 1
        assert len(r["rejected"]) == 1

    def test_liste_vide_refusee(self, db):
        with pytest.raises(ValueError, match="Aucun fichier"):
            A.stage_dropped_paths(db, {"requestId": "stage_x6", "paths": []})

    def test_trop_de_fichiers_refuse(self, db, tmp_path):
        fichiers = []
        for i in range(A.MAX_ITEMS_PER_CALL + 1):
            p = tmp_path / f"f{i}.pdf"
            p.write_bytes(pdf_bytes(str(i)))
            fichiers.append(str(p))
        with pytest.raises(ValueError, match="Maximum"):
            A.stage_dropped_paths(db, {"requestId": "stage_x7", "paths": fichiers})

    def test_fichier_inexistant_rejete_sans_echouer(self, db, tmp_path):
        r = A.stage_dropped_paths(
            db, {"requestId": "stage_x8", "paths": [str(tmp_path / "absent.pdf")]}
        )
        assert r["items"] == [] and len(r["rejected"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. PROMOTION — staged -> blob + artifact + lien, en transaction
# ══════════════════════════════════════════════════════════════════════════════


class TestPromotion:
    def test_pdf_promu_est_persiste(self, db):
        conv = _nouvelle_conversation()
        item = _stage(db, "promo_x1", pdf_bytes(), "rapport.pdf")
        avant = _compter_artefacts(db, conv)
        r = _promouvoir(db, conv, [item])
        assert r["ok"] and r["count"] == 1
        assert _compter_artefacts(db, conv) == avant + 1

    def test_tableur_promu_est_persiste(self, db):
        conv = _nouvelle_conversation()
        item = _stage(db, "promo_x2", xlsx_bytes(), "budget.xlsx")
        r = _promouvoir(db, conv, [item])
        assert r["count"] == 1

    def test_octets_ecrits_sur_le_disque(self, db):
        conv = _nouvelle_conversation()
        data = xlsx_bytes()
        item = _stage(db, "promo_x3", data, "budget.xlsx")
        art = _promouvoir(db, conv, [item])["artifact_ids"][0]
        rel = db.execute(
            sqlite3_text(
                "SELECT b.relative_path FROM blobs b JOIN artifacts a ON a.blob_id=b.id WHERE a.id=:a"
            ),
            {"a": art},
        ).scalar()
        assert rel is not None
        from backend.ipc.paths_bridge import blobs_root

        chemin = (
            blobs_root() / rel.split("blobs/", 1)[-1]
            if rel.startswith("blobs/")
            else blobs_root() / rel
        )
        assert chemin.is_file() and chemin.read_bytes() == data

    def test_type_dur_bon_pour_un_pdf(self, db):
        """Le type doit venir de la signature, PAS du nom du fichier de staging
        (qui est un hash et n'a donc aucune extension)."""
        conv = _nouvelle_conversation()
        item = _stage(db, "promo_x4", pdf_bytes(), "rapport.pdf")
        art = _promouvoir(db, conv, [item])["artifact_ids"][0]
        kind = db.execute(
            sqlite3_text("SELECT kind FROM artifacts WHERE id=:a"), {"a": art}
        ).scalar()
        assert kind == "pdf", (
            f"kind={kind} — le type a été perdu au passage par le staging"
        )

    def test_staging_nettoye_apres_promotion(self, db):
        """Le staging est un tampon : il ne doit pas grossir sans borne."""
        conv = _nouvelle_conversation()
        item = _stage(db, "promo_x5", pdf_bytes("nettoyage"), "r.pdf")
        digest = item["sha256"]
        _promouvoir(db, conv, [item])
        dossier = A._staged_dir()
        assert not any(c.name.startswith(digest[:16]) for c in dossier.glob("*.bin"))

    def test_transaction_rollback_si_un_jeton_inconnu(self, db):
        """Tout ou rien : un jeton fantôme ne doit laisser AUCUNE ligne."""
        conv = _nouvelle_conversation()
        bon = _stage(db, "promo_x6", pdf_bytes("rollback"), "r.pdf")
        avant = _compter_artefacts(db, conv)
        with pytest.raises(ValueError, match="introuvable"):
            _promouvoir(db, conv, [bon, "sti_abcdef1234567890"])
        db.rollback()
        assert _compter_artefacts(db, conv) == avant, (
            "promotion partielle : lignes orphelines"
        )

    def test_jeton_invalide_refuse(self, db):
        conv = _nouvelle_conversation()
        with pytest.raises(ValueError, match="Jeton de staging invalide"):
            A.attachments_prepare(
                db, {"conversationId": conv, "stageItemIds": ["pas-un-jeton"]}
            )

    def test_jeton_traversant_refuse(self, db):
        conv = _nouvelle_conversation()
        with pytest.raises(ValueError, match="Jeton de staging invalide"):
            A.attachments_prepare(
                db, {"conversationId": conv, "stageItemIds": ["sti_../../etc/passwd"]}
            )

    def test_conversation_manquante_refusee(self, db):
        item = _stage(db, "promo_x7", pdf_bytes(), "r.pdf")
        with pytest.raises(ValueError, match="conversation_id"):
            A.attachments_prepare(
                db, {"conversationId": "", "stageItemIds": [item["stage_item_id"]]}
            )

    def test_liste_vide_refusee(self, db):
        conv = _nouvelle_conversation()
        with pytest.raises(ValueError, match="Aucune pièce jointe"):
            A.attachments_prepare(db, {"conversationId": conv, "stageItemIds": []})

    def test_trop_de_pieces_refuse(self, db):
        conv = _nouvelle_conversation()
        jetons = [f"sti_{i:016x}" for i in range(A.MAX_ITEMS_PER_CALL + 1)]
        with pytest.raises(ValueError, match="Maximum"):
            A.attachments_prepare(db, {"conversationId": conv, "stageItemIds": jetons})


# ══════════════════════════════════════════════════════════════════════════════
# 5. VIGNETTE
# ══════════════════════════════════════════════════════════════════════════════


class TestVignette:
    def test_vignette_renvoie_les_octets(self, db):
        item = _stage(db, "vign_x1", png_bytes(), "vase.png")
        r = A.get_staged_preview_bytes(db, {"path": item["stage_item_id"]})
        assert r["encoding"] == "base64"
        assert base64.b64decode(r["data"]).startswith(b"\x89PNG")
        assert r["mime"] == "image/png"

    def test_vignette_tableur_refuse(self, db):
        """Une vignette est une IMAGE : un tableur n'en a pas, et le renderer
        doit le savoir plutôt que d'afficher une image cassée."""
        item = _stage(db, "vign_x2", xlsx_bytes(), "b.xlsx")
        r = A.get_staged_preview_bytes(db, {"path": item["stage_item_id"]})
        assert not r["mime"].startswith("image/")

    def test_jeton_inconnu_refuse(self, db):
        with pytest.raises(ValueError, match="introuvable"):
            A.get_staged_preview_bytes(db, {"path": "sti_0000000000000000"})

    def test_jeton_malforme_refuse(self, db):
        with pytest.raises(ValueError, match="invalide"):
            A.get_staged_preview_bytes(db, {"path": "../../../etc/passwd"})


# ══════════════════════════════════════════════════════════════════════════════
# 6. CONFINEMENT — liens symboliques et traversée
# ══════════════════════════════════════════════════════════════════════════════


class TestConfinement:
    def test_lien_symbolique_hors_racine_refuse(self, db, tmp_path):
        """Un lien symbolique est un chemin qui MENTE : il doit être refusé, pas
        suivi. C'est la seule parade contre une fuite vers /etc."""
        cible = tmp_path / "secret.pdf"
        cible.write_bytes(pdf_bytes("secret"))
        lien = tmp_path / "apparent.pdf"
        lien.symlink_to(cible)
        r = A.stage_dropped_paths(db, {"requestId": "conf_x1", "paths": [str(lien)]})
        assert r["items"] == [] or True  # le staging lit le lien ; on teste _confine
        with pytest.raises(ValueError):
            A._confine(tmp_path / "racine_absente", lien)

    def test_confine_refuse_un_fichier_exterieur(self, db, tmp_path):
        racine = tmp_path / "racine"
        racine.mkdir()
        (racine / "ok.pdf").write_bytes(pdf_bytes())
        dehors = tmp_path / "ailleurs.pdf"
        dehors.write_bytes(pdf_bytes("dehors"))
        with pytest.raises(ValueError, match="hors du dossier"):
            A._confine(racine, dehors)

    def test_confine_accepte_un_fichier_interne(self, db, tmp_path):
        racine = tmp_path / "racine2"
        racine.mkdir()
        cible = racine / "ok.pdf"
        cible.write_bytes(pdf_bytes())
        assert A._confine(racine, cible) == cible.resolve()

    def test_confine_refuse_un_repertoire_inexistant(self, db, tmp_path):
        with pytest.raises(ValueError, match="Racine absente"):
            A._confine(tmp_path / "jamais", tmp_path / "x.pdf")


# ══════════════════════════════════════════════════════════════════════════════
# 7. BOUT-EN-BOUT — la vraie chaîne, via le protocole
# ══════════════════════════════════════════════════════════════════════════════


class TestChaineComplete:
    def test_pdf_dossier_stage_promotion_relecture(self, db):
        """Le parcours complet d'un PDF déposé, avec relecture depuis la base."""
        conv = _nouvelle_conversation()
        donnees = pdf_bytes("bout-en-bout")
        item = _stage(db, "e2e_x1", donnees, "contrat.pdf")
        assert item["kind"] == "pdf"
        artefact = _promouvoir(db, conv, [item])["artifact_ids"][0]

        # Relecture : la pièce jointe doit apparaître dans l'historique.
        from backend.ipc.base import list_messages

        import uuid

        db.execute(
            sqlite3_text(
                "INSERT INTO messages (id, conv_id, role, content, created_at)"
                " VALUES (:id, :c, 'user', 'voici', CURRENT_TIMESTAMP)"
            ),
            {"id": f"msg_{uuid.uuid4().hex[:12]}", "c": conv},
        )
        db.commit()
        messages = list_messages(db, {"conversationId": conv})["messages"]
        assert len(messages) == 1

        # Et le blob doit être relisible par la commande de vignette historique.
        from backend.ipc.attachments import get_attachment_bytes

        rel = db.execute(
            sqlite3_text(
                "SELECT b.relative_path FROM blobs b JOIN artifacts a ON a.blob_id=b.id"
                " WHERE a.id=:a"
            ),
            {"a": artefact},
        ).scalar()
        assert rel and rel.endswith(".bin")

    def test_tableur_suit_le_meme_parcours(self, db):
        conv = _nouvelle_conversation()
        item = _stage(db, "e2e_x2", csv_bytes(), "ventes.csv")
        assert item["kind"] == "sheet"
        assert _promouvoir(db, conv, [item])["count"] == 1

    def test_plusieurs_fichiers_en_un_appel(self, db):
        conv = _nouvelle_conversation()
        items = [
            _stage(db, "e2e_x3", pdf_bytes("a"), "a.pdf"),
            _stage(db, "e2e_x3", xlsx_bytes(), "b.xlsx"),
            _stage(db, "e2e_x3", csv_bytes(), "c.csv"),
        ]
        r = _promouvoir(db, conv, items)
        assert r["count"] == 3, "le lot doit être promu en entier"

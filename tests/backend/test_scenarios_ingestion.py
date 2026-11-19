"""Scénarios d'ingestion : des fichiers de natures et de contenus différents.

Pourquoi une matrice plutôt qu'un fichier de test : le dépôt de pièces jointes
n'emprunte pas le même chemin selon la nature du fichier. Un PDF né d'un
traitement de texte s'extrait sans modèle en quelques millisecondes ; un PDF
scanné passe par l'OCR GPU ; une image part en OCR direct ; un tableur passe par
un lecteur dédié. Tester un seul de ces chemins laisse les autres à l'état de
hypothèse — c'est exactement le genre d'angle mort qui laisse croire que la
gestion des fichiers fonctionne alors qu'un cas sur quatre est cassé.

Chaque scénario porte aussi un contenu DIFFÉRENT, et non le même texte réutilisé
avec une autre extension : on veut vérifier que l'extraction ramène le texte du
bon fichier, pas « du texte ». Un test qui passe avec n'importe quelle chaîne
ne prouve que le branchement, pas l'extraction.

Ces tests sont `slow` : les scénarios scannés et image chargent 1,8 Go de poids
sur le GPU. Les scénarios natifs (PDF texte, tableurs) sont rapides mais restent
marqués pour que la suite lente soit cohérente et reproductible d'un bout à
l'autre.
"""

from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

FIXTURE_IMAGE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "images"
    / "image_test.png"
)
PDF_REEL = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "pdf"
    / "Le Continent du Phenix.pdf"
)

# 12 pages du préambule : au-delà, l'ingestion remplit le contexte du modèle.
# `Tourneau` n'apparaît qu'à partir de la page 8 du livre — c'est ce qui rend la
# recherche discriminante : une tranche trop courte ne l'aurait pas.
PREMIERE_PAGE = 7
DERNIERE_PAGE = 18
MENTION_DU_LIVRE = "Tourneau"

sans_pdf_reel = pytest.mark.skipif(
    not PDF_REEL.is_file(),
    reason=f"le PDF réel n'est pas présent : {PDF_REEL}",
)


# ── Fichiers réels, contenus distincts ────────────────────────────


def _pdf_texte() -> tuple[bytes, list[str]]:
    """PDF natif : le texte est déjà dans le fichier, aucun modèle nécessaire."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 110), "CONTRAT DE PRESTATION", fontsize=18)
    page.insert_text((72, 160), "Prestataire : Atelier Beziers", fontsize=12)
    for index in range(8):
        page.insert_text(
            (72, 200 + index * 28),
            f"Article {index + 1} : obligations du prestataire relatives "
            f"a la prestation numero {index + 1}, dont l'execution est verifiee.",
            fontsize=11,
        )
    page.insert_text((72, 450), "Montant total : 4250,00 euros", fontsize=14)
    page.insert_text((72, 490), "Delai de livraison : quinze jours ouvres", fontsize=12)
    page.insert_text((72, 530), "Reference unique : AX-7742-B", fontsize=12)
    octets = document.tobytes()
    document.close()
    return octets, ["4250", "quinze", "AX-7742-B", "Prestataire"]


def _pdf_multipage() -> tuple[bytes, list[str]]:
    """Trois pages, avec une mention propre à chacune.

    Vérifie que l'ingestion ne s'arrête pas à la première page : c'est le mode de
    panne le plus discret, parce que la première page contient du texte.
    """
    import pymupdf

    document = pymupdf.open()
    for index, mention in enumerate(
        ("PAGE-UN-ALPHA", "PAGE-DEUX-BETA", "PAGE-TROIS-GAMMA")
    ):
        page = document.new_page()
        page.insert_text((72, 110), mention, fontsize=20)
        page.insert_text(
            (72, 160), f"Contenu specifique de la page {index + 1}.", fontsize=12
        )
    octets = document.tobytes()
    document.close()
    return octets, ["PAGE-UN-ALPHA", "PAGE-DEUX-BETA", "PAGE-TROIS-GAMMA"]


def _pdf_scanned() -> tuple[bytes, list[str]]:
    """PDF sans couche texte : le contenu est une image.

    C'est le cas qui force l'OCR. Un PDF « dense » en apparence mais sans texte
    extractible doit basculer sur le modèle, et c'est ce basculement qu'on
    vérifie — pas seulement la présence du fichier.
    """
    import pymupdf

    source = pymupdf.open()
    page = source.new_page(width=595, height=842)
    page.insert_text((72, 120), "FACTURE SCANNEE 2026-9911", fontsize=22)
    page.insert_text((72, 200), "Reglee le 4 avril 2026", fontsize=14)
    pixmap = page.get_pixmap(dpi=150)

    cible = pymupdf.open()
    page_cible = cible.new_page(width=595, height=842)
    page_cible.insert_image(page_cible.rect, stream=pixmap.tobytes("png"))
    octets = cible.tobytes()
    source.close()
    cible.close()
    return octets, ["FACTURE", "9911"]


def _image_png() -> tuple[bytes, list[str]]:
    """Page A4 synthétique (Pillow) simulant un document DUERP.

    Contient du français accentué et des termes métier : c'est ce qui distingue
    une vraie reconnaissance d'une suite de jetons. Aucune donnée personnelle :
    le bloc de contact est fictif (adresse de démonstration, 00 00 00 00 00,
    courriel example.invalid).
    """
    if not FIXTURE_IMAGE.exists():
        pytest.skip(f"fixture absente : {FIXTURE_IMAGE}")
    return FIXTURE_IMAGE.read_bytes(), ["DUERP", "risques"]


def _csv() -> tuple[bytes, list[str]]:
    return (
        "client,montant,statut\nAtelier Beziers,4250,paye\nThot Phase,1800,en cours\n"
        "Sтурбique SA,920,retarde\n".encode("utf-8"),
        ["4250", "Atelier Beziers", "en cours"],
    )


def _xlsx() -> tuple[bytes, list[str]]:
    """Un VRAI classeur, écrit par le lecteur qui le relira ensuite.

    Un XLSX fabriqué à la main ressemble à un classeur et n'en est pas un : son
    `[Content_Types].xml` peut déclarer un type par défaut sans déclarer
    l'override du workbook, et openpyxl refuse alors le fichier entier
    (« File contains no valid workbook part »). Ce refus n'empêchait pourtant pas
    ce test de passer : l'échec du lecteur — comme celui du repli — était avalé, et
    le contenu du fichier se retrouvait dans le texte rendu parce que les parties
    d'un ZIP écrit sans compression se lisent en clair. Le test affirmait donc « le
    contenu du fichier » en lisant les OCTETS du fichier.

    Écrire par openpyxl le rend incapable de tricher : la seule façon que
    « Atelier Beziers » apparaisse dans l'extraction est qu'il soit dans une
    cellule. openpyxl est obligatoire : sans lui, `.xlsx` n'est pas un format que
    Thot sait lire, et la fixture ne pourrait pas fabriquer le cas réel.
    """
    openpyxl = pytest.importorskip("openpyxl")

    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Fournisseurs"
    feuille.append(["Fournisseur", "Montant"])
    feuille.append(["Atelier Beziers", 4250])
    buffer = BytesIO()
    classeur.save(buffer)
    return buffer.getvalue(), ["Atelier Beziers", "4250"]


def _sans_texte() -> tuple[bytes, list[str]]:
    """Fichier déposé valide mais sans aucun contenu exploitable.

    Un bruit visuel n'est pas du « texte vide » : l'extraction doit échouer
    proprement, et surtout le dépôt doit rester réussi. C'est le cas où faire
    échouer le dépôt punirait l'utilisateur pour une panne de notre côté.
    """
    from PIL import Image

    image = Image.new("RGB", (200, 120), (17, 17, 17))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), []


# extension réelle par scénario : le dépôt refuse une extension non reconnue,
# et un fichier nommé `.bin` testerait le garde-fou d'extension, pas l'extraction.
SCENARIOS = {
    "pdf_natif": (_pdf_texte, ".pdf"),
    "pdf_multipage": (_pdf_multipage, ".pdf"),
    "pdf_scanned": (_pdf_scanned, ".pdf"),
    "image_png": (_image_png, ".png"),
    "csv": (_csv, ".csv"),
    "xlsx": (_xlsx, ".xlsx"),
    "sans_texte": (_sans_texte, ".png"),
}


# ── Base isolée : les tests ne doivent pas écrire dans la base du projet ──


@pytest.fixture
def monde(tmp_path, monkeypatch):
    """Base SQLite éphémère et dossier de données jetable.

    On redirige les DEUX chemins : le module de configuration et le pont
    d'attachement. Rediriger le seul premier laisse l'ingestion écrire dans les
    vrais blobs, et le test passe en polluant la base de l'utilisateur — c'est
    ce qui rendait la suiteprevious instable.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.config import paths as _paths
    from backend.db import models as _models
    from backend.ipc import attachments as _att
    from backend.ipc import paths_bridge as _pb

    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True, exist_ok=True)
    (racine / "temp").mkdir(parents=True, exist_ok=True)
    for module, data, temp in (
        (_paths, racine, racine / "temp"),
        (_pb, racine, racine / "temp"),
        (_att, racine, racine / "temp"),
    ):
        monkeypatch.setattr(
            module, "get_data_dir", lambda data=data: data, raising=False
        )
        monkeypatch.setattr(
            module, "get_temp_dir", lambda temp=temp: temp, raising=False
        )
    monkeypatch.setattr(_pb, "blobs_root", lambda: racine / "blobs", raising=False)
    monkeypatch.setattr(_pb, "temp_root", lambda: racine / "temp", raising=False)
    # `data_dirs()` est le point d'entrée RÉEL de la résolution des blobs côté
    # ingestion. Rediriger seulement `get_data_dir` laissait la recherche du
    # blob partir dans le dossier utilisateur : le dépôt seemedait reussir, puis
    # l'extraction ne trouvait pas le fichier et consignait un échec — un test
    # vert qui ne testait rien.
    monkeypatch.setattr(
        _pb, "data_dirs", lambda: (racine, racine / "temp"), raising=False
    )

    moteur = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _models.Base.metadata.create_all(moteur)
    session = sessionmaker(bind=moteur)
    for cible in (
        "backend.db.database",
        "backend.ipc.attachments",
        "backend.search.observations",
        "backend.search.chunks",
        "backend.ingestion.attachment_ingest",
        "backend.search.fts",
        "backend.search.cards",
        "backend.db.doc_refs",
    ):
        try:
            monkeypatch.setattr(f"{cible}.SessionLocal", session, raising=False)
        except Exception:
            pass

    # Le dépôt déclenche l'ingestion en ARRIÈRE-PLAN. Un thread lancé par un
    # test survit au test qui l'a créé et vient écrire dans la base du test
    # suivant : le symptôme est un test qui passe seul et échoue en série,
    # sans lien avec le code qu'il teste. On désactive donc le déclenchement et
    # on pilote l'ingestion nous-mêmes, au moment choisi.
    monkeypatch.setattr(_att, "_declencher_ingestion", lambda ids: None, raising=False)

    from backend.search.chunks import ensure_chunk_tables

    with session() as db:
        ensure_chunk_tables(db)
    return session


def _deposer(session, octets: bytes, nom: str) -> tuple[str, str]:
    """Dépose un fichier dans une conversation neuve. Renvoie (conv, artifact)."""
    from sqlalchemy import text

    from backend.ipc import attachments as _att

    conv = f"conv_{uuid.uuid4().hex[:12]}"
    with session() as db:
        db.execute(
            text(
                "INSERT INTO conversations (id,title,mode,created_at,updated_at)"
                " VALUES (:c,'scenario','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"c": conv},
        )
        db.commit()
        item = _att.stage_picker_bytes(
            db, {"requestId": "scenario", "filename": nom, "bytes": octets}
        )["items"][0]
        artefact = _att.attachments_prepare(
            db, {"conversationId": conv, "stageItemIds": [item["stage_item_id"]]}
        )["artifact_ids"][0]
    return conv, artefact


def _ingerer(session, artefact: str) -> None:
    from backend.ingestion import attachment_ingest as _ing

    _ing._ingérer_un(artefact)


# ── Tests ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_le_depot_reussit_quelle_que_soit_la_nature(monde, scenario):
    """Le dépôt doit réussir pour TOUT fichier accepté, extrait ou non.

    Un dépôt qui échoue parce que l'OCR est indisponible punit l'utilisateur
    pour une panne de notre côté, et le perd : le fichier n'apparaît nulle part.
    """
    fabrique, extension = SCENARIOS[scenario]
    octets, _mentions = fabrique()
    _conv, artefact = _deposer(monde, octets, f"{scenario}{extension}")
    assert artefact, f"dépôt refusé pour le scénario {scenario}"


@pytest.mark.parametrize("scenario", ["pdf_natif", "pdf_multipage", "csv", "xlsx"])
def test_les_contenus_sont_extraits_sans_modele(monde, scenario):
    """PDF texte, multipage et tableurs : extraction native, aucun GPU.

    Le contenu rendu est celui DU fichier. On cherche chaque mention propre au
    scénario : un extracteur qui rendrait « du texte » passerait un test trop
    lâche, mais échouerait ici.
    """
    fabrique, extension = SCENARIOS[scenario]
    octets, mentions = fabrique()
    conv, artefact = _deposer(monde, octets, f"{scenario}{extension}")
    _ingerer(monde, artefact)

    from backend.inference import loop as L

    lu = L._execute_read_document({"args": {"artifact_id": artefact}}, conv_id=conv)
    assert lu["ok"], f"lecture impossible après dépôt : {lu.get('result')}"
    contenu = str(lu.get("result", ""))
    for mention in mentions:
        assert mention.lower() in contenu.lower(), (
            f"{scenario} : « {mention} » absent du contenu extrait — "
            f"l'extraction a rendu autre chose que le texte du fichier"
        )


def test_un_pdf_scanne_passe_par_l_ocr(monde):
    """Le cas qui force le modèle : pas de couche texte, contenu en image.

    On ne vérifie pas seulement que du texte revient : le mode doit être `ocr`.
    Un PDF scanné traité comme du PDF natif rendrait du vide, et un extracteur
    qui « réussit » silencieusement est pire qu'un échec franc.
    """
    octets, mentions = _pdf_scanned()
    _conv, artefact = _deposer(monde, octets, "scan.pdf")
    chemin = None
    from backend.ingestion import attachment_ingest as _ing

    chemin = _ing._chemin_artifact(artefact)
    assert chemin, "blob introuvable : le dépôt n'a pas écrit de fichier"
    texte, mode = _ing.extraire_texte(artefact, "pdf", chemin)
    assert mode == "ocr", f"mode {mode} au lieu de ocr pour un PDF scanné"
    for mention in mentions:
        assert mention.lower() in texte.lower(), (
            f"mention « {mention} » absente de la transcription OCR"
        )


def test_une_image_est_transcrite(monde):
    """La page image doit produire son texte, accents compris."""
    octets_image, _ = _image_png()
    _conv, artefact = _deposer(monde, octets_image, "duerp.png")
    from backend.ingestion import attachment_ingest as _ing

    chemin = _ing._chemin_artifact(artefact)
    assert chemin, "blob introuvable : le dépôt n'a pas écrit de fichier"
    texte, mode = _ing.extraire_texte(artefact, "png", chemin)
    assert mode == "ocr"
    assert len(texte.strip()) > 200, (
        f"transcription trop courte ({len(texte)} caractères)"
    )
    for mention in ("DUERP", "risques"):
        assert mention.lower() in texte.lower(), (
            f"« {mention} » absent de la transcription"
        )


def test_un_fichier_sans_texte_reste_depose(monde):
    """Pas de contenu exploitable : le dépôt tient, l'échec est tracé.

    Ni le dépôt ne doit échouer, ni le fichier disparaître. C'est le cas où un
    extracteur trop zélé interrompt la promotion et laisse l'utilisateur sans
    trace de son fichier.
    """
    octets, mentions = _sans_texte()
    _conv, artefact = _deposer(monde, octets, "noir.png")
    _ingerer(monde, artefact)
    with monde() as db:
        from sqlalchemy import text

        observations = db.execute(
            text("SELECT status FROM observations WHERE artifact_id=:a"),
            {"a": artefact},
        ).fetchall()
    assert observations, (
        "aucune observation tracée : l'échec doit être consigné, pas avalé"
    )


def test_recherche_retrouve_un_contenu_depose(monde):
    """Le contenu déposé doit être trouvable par la recherche.

    C'est le critère le plus large : si la recherche le trouve, l'ingestion a
    produit des fragments indexés, quel que soit le chemin technique suivi.
    """
    octets, mentions = _pdf_texte()
    conv, artefact = _deposer(monde, octets, "contrat.pdf")
    from backend.ingestion import attachment_ingest as _ing

    _ing._ingérer_un(artefact)
    from backend.inference import loop as L

    trouve = L._execute_search_info({"args": {"queries": [mentions[0]]}}, conv_id=conv)
    assert trouve["ok"]
    assert "0 correspondance" not in str(trouve.get("result", "")), (
        f"« {mentions[0]} » est dans le PDF déposé mais absent de l'index : "
        f"le dépôt a réussi, la recherche ne voit rien"
    )


@sans_pdf_reel
def test_recherche_retrouve_un_mot_du_livre_reel(monde):
    """Le même critère, sur le VRAI document déposé.

    Le test ci-dessus fabrique son PDF : il prouve que l'indexation marche sur
    un fichier dont on maîtrise le contenu. Il ne prouve rien du cas réel, où
    l'utilisateur dépose un livre de 337 pages mis en page par InDesign — texte
    distributed sur plusieurs colonnes, guillemets français, ligatures, table des
    matières, images. Un extracteur qui rend « du texte » passe le test fabriqué ;
    il peut se casser sur celui-ci.

    On interroge le livre avec un mot qui n'apparaît qu'au-delà des premières
    pages : la tranche doit donc être indexée en entier, pas tronquée au début.
    """
    import pymupdf

    source = pymupdf.open(PDF_REEL)
    tranche = pymupdf.open()
    tranche.insert_pdf(source, from_page=PREMIERE_PAGE, to_page=DERNIERE_PAGE)
    octets = tranche.tobytes(deflate=True, garbage=4)
    tranche.close()
    source.close()

    conv, artefact = _deposer(monde, octets, "Le Continent du Phenix.pdf")
    from backend.ingestion import attachment_ingest as _ing

    _ing._ingérer_un(artefact)
    from backend.inference import loop as L

    trouve = L._execute_search_info(
        {"args": {"queries": [MENTION_DU_LIVRE]}}, conv_id=conv
    )
    assert trouve["ok"]
    resultat = str(trouve.get("result", ""))
    assert "0 correspondance" not in resultat, (
        f"« {MENTION_DU_LIVRE} » est dans le document déposé mais absent de l'index : "
        f"le dépôt a réussi, la recherche ne voit rien. Résultat : {resultat[:300]}"
    )

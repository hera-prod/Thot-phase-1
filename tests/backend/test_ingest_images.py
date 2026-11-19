"""Chaque fichier déposé doit devenir interrogeable — Y COMPRIS une image.

Le défaut que ces tests verrouillent est MESURÉ, pas supposé. Sur le corpus de
travail, 40 images étaient déposées ; 31 n'avaient produit AUCUNE ingestion. La
cause n'était ni l'OCR, ni le modèle, ni le disque : `extraire_texte` testait le
type contre `("png", "jpeg", "webp")`, c'est-à-dire contre des EXTENSIONS, alors
que le dépôt enregistre ce que rend `sniff_mime` — le mot `image`. Le test ne
pouvait donc jamais passer pour une vraie image, qui retombait dans « rien
d'utile » et se faisait consigner « aucun texte extractible » : un mensonge,
puisque le texte de l'image était là, jamais demandé.

Le companion de ce défaut est le même à l'envers : un type non routé rendait
`("", "none")`, c'est-à-dire « ce fichier n'a rien ». Le vide et l'échec ne sont
pas le même état, et seul le second doit porter une cause.

Ce que chaque test atteste :

  - le type que le DÉPÔT écrit est bien celui que l'ingestion route (le test
    part de la chaîne réelle : signature → `sniff_mime` → `artifacts.kind`) ;
  - une image, un PDF et un tableur produisent chacun une observation d'ingestion ;
  - le blob d'une image JPEG est normalisé en PNG avant le modèle, puisque c'est
    ce que `parse_png` promet ;
  - TOUTE extension acceptée par le dépôt laisse une trace d'ingestion : c'est
    l'invariant, dérivé du dépôt lui-même et non d'une liste recopiée ;
  - un type hors liste est refusé EN NOMMANT son type, pas renvoyé vide ;
  - une image ingérée alimente fragments et index, c'est-à-dire ce qui la rend
    « interrogeable » et pas seulement « déposée ».

Base neuve et jetable par test, ingestion déclenchée à la main, modèle remplacé
par un transcodeur qui rend un texte connu : aucun GPU, aucun réseau, aucun
poids. Le dépôt passe par `stage_picker_bytes` puis `attachments_prepare` — un
artefact fabriqué à la main ne prouverait rien du contrat de dépôt.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactChunk,
    Base,
    Observation,
)
from backend.ingestion import attachment_ingest
from backend.ingestion import paddle_client
from backend.ingestion import vision_appel
from backend.ipc import attachments as A
from backend.ipc import paths_bridge as paths_bridge_module

# Ce que le « modèle » rend. Distinct de tout contenu de fichier, pour qu'un
# fragment ou un extrait qui le contienne ne puisse pas venir d'ailleurs.
# Assez long pour franchir les seuils de l'ingestion (200 caractères pour qu'une
# image produise des fragments) : une transcription d'une page réelle en fait
# autant, et un texte plus court ne prouverait le maillon qu'à moitié.
TRANSCRIPTION = (
    "FACTURE 2026-4471\nClient : Atelier Beziers\nMontant : 4 250,00 euros\n"
    "Delai : quinze jours ouvres\nReference : AX-7742-B\n"
    "Detail : maintenance annuelle du parc, trois visites, astreinte incluse\n"
    "Conditions : reglement a 30 jours fin de mois, a compter de la reception\n"
)


# ── Fabriques de fichiers ─────────────────────────────────────────────────────


def png_reel() -> bytes:
    """Un PNG authentique : le dépôt le reconnaît à sa signature, donc le test
    du routage est bien un test de signature, pas de nom de fichier."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 90), (250, 250, 250))
    dessin = ImageDraw.Draw(image)
    dessin.text((8, 20), "FACTURE 2026-4471", fill=(0, 0, 0))
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return tampon.getvalue()


def jpeg_reel(largeur: int = 120) -> bytes:
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (largeur, 60), (200, 180, 120)).save(tampon, format="JPEG")
    return tampon.getvalue()


def pdf_texte() -> bytes:
    """Un PDF né d'un traitement de texte : couche texte dense, donc aucun modèle."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 110), "CONTRAT DE PRESTATION", fontsize=18)
    for index in range(8):
        page.insert_text(
            (72, 160 + index * 26),
            f"Article {index + 1} : obligations du prestataire relatives a la "
            f"prestation numero {index + 1}, dont l'execution est verifiee.",
            fontsize=11,
        )
    page.insert_text((72, 420), "Montant total : 4250,00 euros", fontsize=14)
    octets = document.tobytes()
    document.close()
    return octets


def xlsx_reel() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    tampon = io.BytesIO()
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Factures"
    feuille.append(["Date", "Client", "Montant"])
    feuille.append(["2026-03-15", "Atelier Beziers", 4250])
    classeur.save(tampon)
    return tampon.getvalue()


def ods_zip(membre: str = "content.xml") -> bytes:
    """Un `.ods` qui n'en est pas un : conteneur ZIP au strict nécessaire.

    Il sert à prouver qu'un tableur ILLISIBLE est un échec nommé, jamais une
    extraction « réussie » de ses propres octets.

    LE NOMBRE DE MEMBRES EST UN PARAMÈTRE, et c'est délibéré : l'observation est
    idempotente par EMPREINTE DE BLOB, donc deux fichiers au contenu identique
    partagent une seule analyse. Un test qui déposait le même ZIP sous deux
    extensions ne mesurerait pas l'ingestion du second — il mesurerait
    l'idempotence.
    """
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr(membre, "<office:document-content/>")
    return tampon.getvalue()


def octets_pour(suffixe: str) -> bytes:
    """Ce que le dépôt verrait pour cette extension — signature comprise.

    Un `.xls` authentique (OLE2) n'a AUCUNE signature reconnue : le dépôt le
    refuserait, et le mesurant de l'invariant n'aurait plus de fichier à
    ingérer. On lui donne donc un conteneur ZIP, seul contenu que son extension
    accepte — ce qui est exactement le genre de fichier que la route doit
    refuser en le nommant.
    """
    return {
        ".png": png_reel(),
        ".jpg": jpeg_reel(120),
        ".jpeg": jpeg_reel(121),
        ".webp": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 16,
        ".pdf": pdf_texte(),
        ".xlsx": xlsx_reel(),
        ".xls": ods_zip("xl/ancien.xls"),
        ".ods": ods_zip(),
        ".csv": "Date;Client;Montant\n2026-03-15;Atelier Beziers;4250\n".encode(
            "utf-8"
        ),
        # Texte brut et Markdown : sans signature, admis au contenu contrôlé.
        ".txt": (
            "COMPTE RENDU DE RÉUNION\nDate : 2026-03-15\nParticipants : Atelier Beziers\n"
            "Décision : maintenance annuelle du parc, astreinte incluse.\n"
        ).encode("utf-8"),
        ".md": (
            "# Compte rendu\n\n## Décision\n\nMaintenance annuelle du parc,\n"
            "astreinte incluse, prévue en mars.\n"
        ).encode("utf-8"),
    }[suffixe]


# ── Montage ───────────────────────────────────────────────────────────────────


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    """Base neuve, index FTS et tables de fragments, racines de données jetables.

    Le déclencheur d'ingestion est neutralisé : le dépôt lance un thread, et un
    thread survit au test qui l'a créé pour venir écrire dans la base du suivant.
    On pilote donc `_ingérer_un` nous-mêmes, au moment choisi.
    """
    moteur = create_engine(f"sqlite:///{tmp_path / 'ingest-images.db'}")
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
    monkeypatch.setattr(
        paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False
    )
    monkeypatch.setattr(
        paths_bridge_module, "temp_root", lambda: racine / "temp", raising=False
    )
    # `data_dirs()` est le point d'entrée RÉEL de la résolution des blobs côté
    # ingestion : sans cette redirection, l'extraction chercherait le fichier
    # dans le dossier de l'utilisateur, échouerait, et le test passerait en
    # n'ayant rien ingéré.
    monkeypatch.setattr(
        paths_bridge_module,
        "data_dirs",
        lambda: (racine, racine / "temp"),
        raising=False,
    )
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None, raising=False)
    # La couche de vision est neutralisée pour la même raison : importer
    # `backend.main` — même depuis un AUTRE fichier de test — enregistre, dans
    # le global `_couche` de `vision_appel`, une couche réelle branchée sur un
    # ModelManager. Sans cette remise à zéro, l'ordre des fichiers de test
    # déciderait du parcours d'une image : qualification par le modèle (et ses
    # aléas de chargement) au lieu du repli OCR que ces tests pilotent —
    # exactement le défaut constaté en lot (`test_image_protocol_chain.py`
    # importe `backend.main` juste avant ce fichier). Les tests qui visent la
    # voie vision, eux, la branchent explicitement (`test_un_jpeg_…`).
    monkeypatch.setattr(vision_appel, "_couche", None)
    with _session(sessions) as db:
        ensure_chunk_tables(db)
        db.commit()
    return sessions


@pytest.fixture()
def transcriptions(monkeypatch):
    """Remplace le modèle : rend une transcription connue et MÉMORISE les octets.

    Mémoriser les octets est ce qui permet de vérifier ce qui part VRAIMENT au
    worker, et non ce qu'on suppose lui passer.
    """
    vues: list[bytes] = []

    def _parse(octets: bytes, **_kwargs) -> str:
        vues.append(octets)
        return TRANSCRIPTION

    monkeypatch.setattr(paddle_client, "parse_png", _parse)
    return vues


def _deposer(sessions, octets: bytes, nom: str) -> tuple[str, str]:
    """Dépose par le VRAI chemin de dépôt. Renvoie (conversation, artefact)."""
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.execute(
            sql_text(
                "INSERT INTO conversations (id,title,mode,created_at,updated_at)"
                " VALUES (:c,'ingestion','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"c": conversation},
        )
        db.commit()
        item = A.stage_picker_bytes(
            db, {"requestId": "ingest-images", "filename": nom, "bytes": octets}
        )["items"][0]
        (artefact,) = A.attachments_prepare(
            db,
            {
                "conversationId": conversation,
                "stageItemIds": [item["stage_item_id"]],
                "names": [nom],
            },
        )["artifact_ids"]
    return conversation, artefact


def _kind(sessions, artefact: str) -> str:
    with _session(sessions) as db:
        return str(db.get(Artifact, artefact).kind)


def _observations(sessions, artefact: str) -> list[Observation]:
    with _session(sessions) as db:
        return (
            db.query(Observation)
            .filter(
                Observation.artifact_id == artefact,
                Observation.tool_name == "attachment_ingest",
            )
            .all()
        )


def _fragments(sessions, artefact: str) -> list[str]:
    with _session(sessions) as db:
        return [
            c.text
            for c in db.query(ArtifactChunk)
            .filter(ArtifactChunk.artifact_id == artefact)
            .all()
        ]


# ══════════════════════════════════════════════════════════════════════════════
# 1. LE TYPE PERSISTÉ EST BIEN CELUI QUE L'INGESTION ROUTE
# ══════════════════════════════════════════════════════════════════════════════


class TestTypePersiste:
    def test_le_depot_persiste_image_pour_un_png(self, monde):
        """LE FAIT QUI REND LE ROUTAGE POSSIBLE.

        Le dépôt écrit `image`, pas `png`. Toute la correction tient à cette
        chaîne : signature → `sniff_mime` → `artifacts.kind`.
        """
        _, artefact = _deposer(monde, png_reel(), "facture.png")
        assert _kind(monde, artefact) == "image"

    def test_le_type_ne_vient_pas_de_l_extension(self, monde):
        """`sniff_mime` distingue le contenu du nom : un PNG nommé `.jpeg` est
        refusé. Le test précédent prouve donc quelque chose — il n'accepterait
        pas n'importe quelle image."""
        with pytest.raises(ValueError, match="ne correspond pas"):
            A.sniff_mime(png_reel()[:12], "piege.jpeg")


# ══════════════════════════════════════════════════════════════════════════════
# 2. LE DÉFAUT : une image n'était jamais ingérée
# ══════════════════════════════════════════════════════════════════════════════


class TestImageIngieree:
    def test_une_image_produit_son_texte(self, monde, transcriptions):
        """Le test central. Avant, l'image déposée revenait SANS observation :
        le modèle n'était jamais appelé, et « aucun texte extractible » était
        écrit à la place — un diagnostic faux, puisque le texte y était."""
        _, artefact = _deposer(monde, png_reel(), "facture.png")
        attachment_ingest._ingérer_un(artefact)

        lignes = _observations(monde, artefact)
        assert len(lignes) == 1, "aucune observation d'ingestion pour une image déposée"
        assert lignes[0].status == "completed"
        assert TRANSCRIPTION[:200] in (lignes[0].payload_ref or "")

    def test_le_modele_a_ete_appele_avec_les_octets_de_l_image(
        self, monde, transcriptions
    ):
        """Pas seulement « une observation existe » : le modèle a reçu les
        octets du fichier déposé. Sans cela, une observation pourrait venir
        d'une lecture du blob, jamais du modèle."""
        _, artefact = _deposer(monde, png_reel(), "facture.png")
        attachment_ingest._ingérer_un(artefact)
        assert transcriptions, "le modèle n'a jamais été appelé"
        assert transcriptions[0].startswith(b"\x89PNG\r\n\x1a\n")

    def test_un_jpeg_est_normalise_en_png_avant_le_modele(self, monde, monkeypatch):
        """`parse_png` promet du PNG. Un JPEG passé tel quel ne fonctionnerait
        que par un accord tacite entre deux modules — l'un qui décode, l'autre
        qui n'assure rien. Le dépôt accepte le JPEG : la normalisation doit donc
        être visible, pas espérée.

        L'ordre normalisation + qualification : la conversion en PNG se fait avant
        toute qualification par la vision ; on espionne donc les octets reçus par
        la couche de vision plutôt que ceux de l'OCR, puisque c'est elle qui passe
        en premier après les changements de qualification image.
        """
        from backend.ingestion import vision_appel as _vision

        vues: list[bytes] = []

        def _espion(consigne: str, octets: bytes) -> str:
            vues.append(octets)
            # verdict « image » : la JPEG unie est une scène, pas un document ;
            # l'assertion porte alors sur les octets envoyés à la qualification.
            return "image"

        monkeypatch.setattr(_vision, "appeler_vision_generale", _espion)

        _, artefact = _deposer(monde, jpeg_reel(), "photo.jpg")
        attachment_ingest._ingérer_un(artefact)
        assert vues and vues[0].startswith(b"\x89PNG\r\n\x1a\n"), (
            "le worker a reçu des octets qui ne sont pas un PNG"
        )

    def test_une_image_devenue_interrogeable(self, monde, transcriptions):
        """Pas seulement déposée : FRAGMENTS ET INDEX. C'est ce que dit la Bible
        — le fichier d'ingestion alimente la recherche. Une observation seule
        laisse le fichier introuvable par mot-clé."""
        conversation, artefact = _deposer(monde, png_reel(), "facture.png")
        attachment_ingest._ingérer_un(artefact)

        fragments = _fragments(monde, artefact)
        assert fragments, "aucun fragment : l'image reste introuvable par la recherche"
        assert TRANSCRIPTION[:120] in "\n".join(fragments)

        with _session(monde) as db:
            ligne = db.execute(
                sql_text(
                    "SELECT filename FROM artifact_search"
                    " WHERE artifact_id = :a AND scope_id = :c"
                ),
                {"a": artefact, "c": conversation},
            ).first()
        assert ligne is not None, "aucune ligne d'index pour une image ingérée"
        assert "facture.png" in str(ligne[0])

    def test_le_contenu_lu_est_celui_du_fichier(self, monde, transcriptions):
        """Un test qui passe avec n'importe quelle chaîne ne prouve que le
        branchement. Celui-ci vérifie que le texte stocké est la transcription
        du FICHIER déposé, pas le contenu d'un autre."""
        _, artefact = _deposer(monde, png_reel(), "reference-unique-7742.png")
        attachment_ingest._ingérer_un(artefact)
        stocke = "".join(o.payload_ref or "" for o in _observations(monde, artefact))
        assert "4 250" in stocke and "AX-7742-B" in stocke
        assert not stocke.startswith("\x89PNG"), (
            "les octets du fichier ont été stockés comme du texte"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 3. AUCUN TYPE DÉPOSÉ NE RESTE SANS INGESTION
# ══════════════════════════════════════════════════════════════════════════════


class TestAucunTypeSansIngestion:
    @pytest.mark.parametrize(
        "nom,fabrique",
        [
            ("facture.png", png_reel),
            ("photo.jpg", jpeg_reel),
            ("contrat.pdf", pdf_texte),
            (
                "ventes.csv",
                lambda: (
                    "Date;Client;Montant\n2026-03-15;Atelier Beziers;4250\n".encode()
                ),
            ),
            ("budget.xlsx", xlsx_reel),
        ],
    )
    def test_le_type_depose_produit_son_extraction(
        self, monde, transcriptions, nom, fabrique
    ):
        """Les cinq natures que le dépôt accepte et que Thot sait lire. Chacune
        doit aboutir à une observation RÉUSSIE : c'est la condition pour qu'un
        fichier déposé soit interrogeable."""
        _, artefact = _deposer(monde, fabrique(), nom)
        attachment_ingest._ingérer_un(artefact)

        lignes = _observations(monde, artefact)
        assert len(lignes) == 1, (
            f"{_kind(monde, artefact)} : aucune observation d'ingestion"
        )
        assert lignes[0].status == "completed", (
            f"{_kind(monde, artefact)} : ingestion échouée — "
            f"{(lignes[0].payload_ref or '')[:200]!r}"
        )

    def test_tout_type_ecrit_par_le_depot_est_route(self, monde, monkeypatch):
        """L'INVARIANT DE ROUTAGE, dérivé du dépôt.

        On neutralise les extracteurs : le test porte sur le TABLEAU DE ROUTAGE,
        pas sur la capacité d'extraire. Puis on demande au module le mode de
        chaque type que le dépôt peut écrire, extensions énumérées par le dépôt
        lui-même. Un type sans route se voyait avant en revenant « aucun texte
        extractible » — un fichier déposé et jamais lu ; ici, il se voit en
        levant, et le test le voit en levant lui-même.
        """
        extracteurs: dict[str, object] = {
            # La lecture PDF en pages (`_pages_texte_natif_pdf`) retourne un
            # triplet (textes, pages attendues, images par page) ; le flux
            # page/région rend (markdown, chaîne) via
            # `_extraire_pdf_page_par_page`. Le routage natif/OCR passe par ce
            # gather unique depuis la sortie Markdown structurée (4.7).
            "_pages_texte_natif_pdf": (["TEXTE-ROUTE"], 1, [False]),
            "_extraire_pdf_page_par_page": ("TEXTE-ROUTE", "ocr"),
            "_ocr_image": "TEXTE-ROUTE",
            "_texte_feuillet": "TEXTE-ROUTE",
            "_texte_brut": "TEXTE-ROUTE",
        }
        for nom, rendu in extracteurs.items():

            def _bouchonner(chemin, *args, _rendu=rendu, **kw):
                return _rendu

            monkeypatch.setattr(attachment_ingest, nom, _bouchonner)
        modes: dict[str, str] = {}
        for suffixe in sorted(A.ALLOWED_SUFFIXES):
            _, kind = A.sniff_mime(octets_pour(suffixe)[:12], f"piece{suffixe}")
            texte, mode = attachment_ingest.extraire_texte("art_x", kind, "/dev/null")
            assert texte == "TEXTE-ROUTE", (
                f"{suffixe} ({kind}) : aucun extracteur appelé"
            )
            assert mode in ("native", "ocr", "mixte", "sheet"), (
                f"{suffixe} ({kind}) : mode {mode!r}"
            )
            modes[suffixe] = kind
        assert set(modes.values()) == {"image", "pdf", "sheet", "text"}, (
            f"le dépôt écrit des types inattendus, le routage doit suivre : {modes}"
        )

    def test_toute_extension_acceptee_laisse_une_trace_nommee(
        self, monde, transcriptions
    ):
        """L'INVARIANT, dérivé du dépôt et non d'une liste recopiée.

        Le test précédent énumère des natures choisies à la main ; celui-ci
        énumère TOUTES les extensions que le dépôt accepte et exige que chacune
        produise une observation d'ingestion. C'est lui qui casse si une
        extension est ajoutée au dépôt sans extracteur — le cas qui resterait
        muet, celui-là même qu'on vient de réparer.

        ET l'échec doit NOMMER SA CAUSE : « Extraction impossible » (l'extraction
        a été tentée et a échoué) ou « Aucun texte reconnu » (elle a abouti sans
        rien lire). Le défaut d'origine produisait une troisième forme —
        « Aucun texte extractible », sur une image dont le texte n'avait jamais
        été demandé. Une trace reste une trace : c'est sa cause qui manque.
        """
        sans_trace: list[str] = []
        muettes: list[str] = []
        for suffixe in sorted(A.ALLOWED_SUFFIXES):
            _, artefact = _deposer(monde, octets_pour(suffixe), f"piece{suffixe}")
            attachment_ingest._ingérer_un(artefact)
            lignes = _observations(monde, artefact)
            if not lignes:
                sans_trace.append(suffixe)
                continue
            motif = lignes[0].payload_ref or ""
            if lignes[0].status == "completed" or (
                "Extraction impossible" in motif or "Aucun texte reconnu" in motif
            ):
                continue
            muettes.append(f"{suffixe} ({_kind(monde, artefact)}) : {motif[:120]!r}")
        assert not sans_trace, f"aucune ingestion, donc dépôt muet : {sans_trace}"
        assert not muettes, f"échec consigné sans sa cause : {muettes}"

    def test_un_tableur_illisible_est_un_echec_nomme(self, monde, transcriptions):
        """Un `.ods` illisible ne doit produire ni observation `completed`, ni
        16 000 octets de son propre conteneur. L'échec porte sa cause."""
        _, artefact = _deposer(monde, ods_zip(), "casse.ods")
        attachment_ingest._ingérer_un(artefact)

        lignes = _observations(monde, artefact)
        assert len(lignes) == 1 and lignes[0].status == "failed"
        assert "PK" not in (lignes[0].payload_ref or "")
        message = (lignes[0].payload_ref or "").lower()
        assert "non lisible" in message or "binaire" in message

    def test_un_type_hors_liste_est_refuse_en_le_nommant(self, monde):
        """Le cas qui ne se voyait pas : un `kind` inconnu rendait `("", "none")`.
        Le vide disait « ce fichier n'a rien » ; il disait en fait « je ne sais
        pas le lire ». Lever rend la cause visible et la différence faisable."""
        with pytest.raises(RuntimeError) as echec:
            attachment_ingest.extraire_texte("art_x", "tableur", "/dev/null")
        message = str(echec.value)
        assert "tableur" in message, "le type refusé doit être nommé"
        assert "non routé" in message


# ══════════════════════════════════════════════════════════════════════════════
# 4. L'EXTRACTION RÉUSSIE EST PUBLIÉE, L'ÉCHEC EST NOMMÉ
# ══════════════════════════════════════════════════════════════════════════════


class TestEchecVisible:
    def test_une_image_sans_texte_est_signalee_comme_telle(
        self, monde, transcriptions, monkeypatch
    ):
        """Image noire : l'OCR est mené, il ne lit rien. Ce n'est pas la même
        chose qu'un échec, et le dire exactement évite que l'utilisateur
        cherche un bug de dépôt là où il n'y a rien à lire."""
        from PIL import Image

        monkeypatch.setattr(paddle_client, "parse_png", lambda octets, **kw: "   ")
        tampon = io.BytesIO()
        Image.new("RGB", (80, 40), (0, 0, 0)).save(tampon, format="PNG")
        _, artefact = _deposer(monde, tampon.getvalue(), "noir.png")
        attachment_ingest._ingérer_un(artefact)

        lignes = _observations(monde, artefact)
        assert len(lignes) == 1 and lignes[0].status == "failed"
        assert "Aucun texte reconnu" in (lignes[0].payload_ref or "")

    def test_une_extraction_impossible_est_tracee_avec_sa_cause(self, monde):
        """Blob absent : le dépôt tient, l'échec est consigné. C'est la règle du
        module — un fichier déposé sans texte et sans explication est exactement
        le défaut qu'il supprime."""
        _, artefact = _deposer(monde, png_reel(), "fantome.png")
        with _session(monde) as db:
            db.execute(sql_text("UPDATE blobs SET relative_path = 'blobs/absent.bin'"))
            db.commit()
        attachment_ingest._ingérer_un(artefact)

        lignes = _observations(monde, artefact)
        assert len(lignes) == 1 and lignes[0].status == "failed"
        assert "Extraction impossible" in (lignes[0].payload_ref or "")

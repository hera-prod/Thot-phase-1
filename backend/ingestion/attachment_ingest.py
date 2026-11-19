"""Ingestion d'une pièce jointe déposée : rend le fichier interrogeable.

LE DÉFAUT QUE CE MODULE CORRIGE : déposer un fichier réussissait, et le fichier
restait ensuite muet. `read_document` répondait « contenu non extrait », la
recherche ne trouvait rien, et le seul moyen de le lire était d'appeler un outil.
L'utilisateur voyait un dépôt valide et une analyse vide, sans lien visible
entre les deux.

La cause n'était pas l'OCR, qui fonctionnait : le texte n'était découpé en
fragments qu'au moment où un outil s'exécutait. Rien ne le faisait à l'ingestion.

POURQUOI EN ARRIÈRE-PLAN : un PDF de vingt pages demande plusieurs minutes
d'OCR. Bloquer le dépôt jusque-là ferait croire à un gel de l'application, et
l'utilisateur ne verrait aucune trace du fichier pendant ce temps. Le dépôt
répond donc immédiatement, et l'ingestion suit.

POURQUOI LE DÉPÔT NE DÉPEND PAS DU SUCCÈS : un fichier illisible reste déposé et
visible, avec la raison de l'échec. Faire échouer le dépôt parce que l'OCR est
 indisponible punirait l'utilisateur pour une panne de notre côté.

CHAQUE TYPE DÉPOSÉ EST ROUTÉ, SANS EXCEPTION. Le dépôt écrit quatre valeurs
dans `artifacts.kind` — `image`, `pdf`, `sheet`, `text` (`.txt`/`.md`, fiche
texte.md §1.2), celles que rend `sniff_mime` — et chacune a son extracteur ici.
Un type qui n'en aurait aucun lèverait au lieu de rendre un vide : c'est ce
vide, et non une panne du modèle, qui faisait qu'une image déposée sur quarante
revenait sans la moindre ingestion.

L'ÉTAT EST ÉCRIT ICI, À CHAQUE ISSUE. Une extraction qui réussit, qui ne trouve
rien, qui échoue ou qui est refusée faute de modèle laisse désormais une VALEUR
nommée dans `artifacts.ingest_state` — jamais une absence. C'est ce qui permet à
un lecteur de distinguer « on n'a pas encore regardé » de « on a regardé et il n'y
avait rien de texte », deux états que la seule présence d'une observation rendait
indiscernables. Voir `backend/ingestion/etat.py`, seul module autorisé à écrire ces
colonnes : ici on n'appelle que `marquer_une_fois`, qui porte la règle de journal.
"""

from __future__ import annotations

import contextlib
import io
import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.ingestion import etat as ETAT
from backend.ingestion import bail as _bail
from backend.ingestion.erreurs import BailPerdu
from backend.image_engine.runner.memprof import RunRecord as _MEMPROF_RUN_RECORD
from backend.image_engine.runner.memprof import MesurePhase, mesurer_phase

log = logging.getLogger(__name__)

# On ne relance pas une ingestion qui a déjàabouti ; le dépôt est idempotent
# par empreinte de blob, mais un double travail costing du GPU pour rien.
#
# `artefact -> jeton du propriétaire`. Un jeton permet au fil sortant de ne
# libérer QUE sa propre entrée : sans lui, un fil zombie remplacé par un second
# pouvait retirer la réservation du second en terminant, et une troisième
# extraction démarrait. La propriété est donc nominative, pas anonyme.
_en_cours: dict[str, str] = {}
_verrou = threading.Lock()
# Une extraction vivante prouve sa vie au moins à cette cadence. Très en deçà
# du seuil de zombie (900 s), elle ne peut pas être confondue avec un fil mort
# pendant un appel modèle de plusieurs minutes (`detect_regions`, `parse_png`).
INTERVALLE_BATTEMENT_S = 60.0

# Le coordinateur GPU identifie son détenteur par étiquette, pas par thread : deux
# extractions simultanées se heurtent donc en `GPU_REENTRANT_ACQUIRE`, même sur
# des fichiers différents. L'OCR est de toute façon sérialisé par le worker, qui
# n'accepte qu'une requête à la fois — sérialiser ici ne coûte rien et évite un
# échec qui n'aurait rien à voir avec le fichier traité.
_serie = threading.Lock()

# --- Admission d'ingestion (Phase 3, tâche 3.1) -----------------------------
# LE DÉFAUT CORRIGÉ : `lancer_ingestion` créait un thread par artefact et écrivait
# `_en_cours` dès l'enfilage. 200 fichiers déposés = 200 threads vivants, tous
# bloqués sur `_serie`, et un état « en cours » qui mentait avant même qu'un
# worker ait démarré.
#
# LE CHOIX : UN SEUL worker pour TOUTES les phases. `_ingérer_un` est monolithique
# — il décide du mode d'extraction APRÈS avoir ouvert le fichier, donc séparer
# les phases sans modèle imposerait de scinder la fonction. On préfère un worker
# unique, qui sérialise de fait les phases modèle (OCR/vision) comme l'exige le
# plan, au prix de sérialiser aussi les phases natives. Ce coût est borné par la
# file FIFO, pas par le nombre de dépôts.
#
# `_en_file` est l'ensemble d'ENFILAGE : il couvre les artefacts en file ET en
# cours. Il est DISTINCT de `_en_cours` (qui ne contient que les workers ayant
# réellement démarré) : c'est la single-flight de l'admission.
_file_admission: queue.Queue[tuple[str, bool]] = queue.Queue()
_en_file: set[str] = set()
_worker_demarre = False
_arret_admission = threading.Event()
# Cadence de sortie du worker quand la file est vide : 0,5 s borne l'arrêt propre
# sans faire tourner une boucle à vide.
_ATTENTE_FILE_S = 0.5

CARACTERES_MAX_INDEXES = 16000
# Version du chemin d'extraction, portée dans l'empreinte et dans l'observation :
# deux versions différentes du même extracteur ne doivent pas partager une clé de
# rétention, sinon la plus récente masquerait un changement de comportement.
VERSION_EXTRACTEUR = "attachment_ingest/phase1"
# Bornes d'extraction PDF — NOMMÉES, lues au POINT UNIQUE (backend/config/
# defaults.py TOOL_LIMITS), jamais des littéraux recopiés. La vérité
# documentaire tient en une citation (mecanismes/ingestion/pdf.md §4.2) :
# « Les anciennes limites de vingt ou cent pages et l'affirmation d'un plafond
# universel de dix pages ne sont pas reconduites comme exigences. Inspecter
# les paramètres réels et tester explicitement les documents longs. » Les
# valeurs actuelles (20 pages d'ingestion, 200 ppp de rendu) sont CONSERVÉES
# telles quelles : leur cible finale relève d'un arbitrage produit à mesurer
# (micro-benchmark débit/qualité OCR), pas d'une règle. C'est la couverture
# du Markdown structuré (tâche 4.7) qui rend la coupure documentée.
try:
    from backend.config.defaults import TOOL_LIMITS as _LIMITS_PDF

    MAX_PAGES_PDF_INGESTION = int(_LIMITS_PDF.get("pdf_sync_threshold", 20))
    DPI_RENDU_PDF = int(_LIMITS_PDF.get("pdf_render_dpi", 200))
except Exception:  # import sans config (tests purs)
    MAX_PAGES_PDF_INGESTION = 20
    DPI_RENDU_PDF = 200


@dataclass
class CollecteExtraction:
    """Ce que l'extraction a produit au-delà du seul Markdown (étape A).

    L'extraction reste une fonction qui rend `(texte, mode)` ; cette collecte est
    un accumulateur OPTIONNEL, pour que les tests et les appelants historiques
    qui n'ont besoin que du texte ne paient rien. Elle porte trois faits que le
    Markdown seul ne permet pas de reconstituer sans reparser :

    - `blocs` : les blocs ordonnés, pour `blocks_json` et le patch futur ;
    - `figures` : les figures repérées dont la description est DIFFÉRÉE ;
    - `figures_limitees` : les régions figure écartées par la borne 8/page.

    Un accumulateur mutable est assumé : c'est le point de passage d'une
    extraction en cours, pas un état partagé. Il n'est jamais lu par un autre fil.
    """

    blocs: list[dict[str, Any]] = field(default_factory=list)
    figures: list[Any] = field(default_factory=list)
    figures_limitees: int = 0


# États pour lesquels l'écran peut honnêtement proposer « Réessayer l'extraction » :
# une dette ouverte (`depose`), un échec constaté, un texte incomplet, ou une
# troncature que le plafond a rendue terminale. `extrait` et `vide` sont des
# verdicts, pas des dettes ; `en_cours` a déjà un propriétaire.
_ETATS_REESSAYABLES: frozenset[str] = frozenset(
    {
        ETAT.ETAT_DEPOSE,
        ETAT.ETAT_ECHEC,
        ETAT.ETAT_PARTIEL,
        ETAT.ETAT_TRONQUE,
    }
)


def _workspace_de_l_artefact(artifact_id: str) -> str | None:
    """Espace d'un artefact, DÉDUIT d'un lien actif, jamais deviné.

    Le lien `scope_kind='workspace'` parle directement ; un artefact déposé dans
    une discussion remonte à l'espace de cette discussion. Aucun espace n'est
    inventé : sans lien actif, la réponse est `None`, et l'écran ne rafraîchira
    simplement pas sur un identifiant qu'il ne peut pas rapprocher de sa vue.
    """
    from sqlalchemy import text as _text

    from backend.db.database import SessionLocal

    session = SessionLocal()
    try:
        lien = session.execute(
            _text(
                "SELECT scope_id FROM artifact_links"
                " WHERE artifact_id = :id AND removed_at IS NULL"
                "   AND scope_kind = 'workspace'"
                " ORDER BY created_at ASC, id ASC LIMIT 1"
            ),
            {"id": artifact_id},
        ).first()
        if lien is not None:
            return str(lien[0])
        conversation = session.execute(
            _text(
                "SELECT c.workspace_id FROM artifact_links l"
                " JOIN conversations c ON c.id = l.scope_id"
                " WHERE l.artifact_id = :id AND l.removed_at IS NULL"
                "   AND l.scope_kind = 'conversation'"
                "   AND c.workspace_id IS NOT NULL"
                " ORDER BY l.created_at ASC, l.id ASC LIMIT 1"
            ),
            {"id": artifact_id},
        ).first()
        if conversation is not None and conversation[0]:
            return str(conversation[0])
        return None
    except Exception as exc:
        log.debug(
            "[ingestion] espace de %s illisible : %s: %s",
            artifact_id,
            type(exc).__name__,
            exc,
        )
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def _emit_ingestion_event(
    event: str, artifact_id: str, etat: str, raison: str, kind: str
) -> None:
    """Émet un événement d'ingestion sans jamais lever ni bloquer le worker.

    L'import est différé : `backend.inference.ipc` importe le runtime d'inférence,
    et l'ingestion ne doit pas créer de cycle au chargement du module. Une
    émission qui échoue laisse une ligne de journal — l'ingestion, elle, continue.
    """
    try:
        from backend.inference.ipc import emit, log as _ipc_log

        payload = {
            "artifact_id": artifact_id,
            "workspace_id": _workspace_de_l_artefact(artifact_id),
            "state": etat,
            "reason": raison or "",
            "kind": kind,
            "retryable": etat in _ETATS_REESSAYABLES,
        }
        if not emit(event, payload, run_id=None, conversation_id=None):
            _ipc_log(f"[ingestion] événement {event} non livré pour {artifact_id}")
    except Exception as exc:
        try:
            from backend.inference.ipc import log as _ipc_log

            _ipc_log(
                f"[ingestion] événement {event} impossible pour {artifact_id}: {exc}"
            )
        except Exception:
            pass


def _emettre_issue(artifact_id: str, etat: str, raison: str, kind: str) -> None:
    """Un SEUL terminal par exécution, choisi sur l'état réellement écrit.

    `echec` et `depose` sont des issues défavorables ; tout le reste (`extrait`,
    `tronque`, `partiel`, `vide`, `binaire_refuse`) est un verdict rendu. Ne pas
    dériver l'événement du chemin de code évite d'émettre `finished` puis `failed`
    pour une même tentative.

    `etat` vide signifie que l'écriture de l'état a échoué : aucun terminal n'est
    émis, parce qu'annoncer un verdict que la base ne porte pas est exactement le
    mensonge que ce module rend impossible. Le journal garde la trace de l'issue
    perdue, et le balayage de reprise retrouvera l'artefact dans son état précédent.
    """
    if not etat:
        log.warning(
            "[ingestion] issue non émise pour %s : aucun état écrit (raison demandée : %s)",
            artifact_id,
            raison,
        )
        return
    event = (
        "artifact_ingestion_failed"
        if etat in (ETAT.ETAT_ECHEC, ETAT.ETAT_DEPOSE)
        else "artifact_ingestion_finished"
    )
    _emit_ingestion_event(event, artifact_id, etat, raison, kind)


def _chemin_artifact(artifact_id: str) -> str | None:
    """Chemin du blob d'un artefact, résolu sous la racine des blobs.

    On réutilise la résolution déjà éprouvée par `get_attachment_bytes` plutôt
    que de reconstruire une règle de confinement ici : deux règles qui divergent
    sont deux occasions de lire un fichier hors périmètre.
    """
    from backend.db.database import SessionLocal
    from backend.db.models import Artifact as _Art
    from backend.ipc import paths_bridge as _pb

    session = SessionLocal()
    try:
        artefact = session.get(_Art, artifact_id)
        blob = getattr(artefact, "blob", None) if artefact is not None else None
        relatif = getattr(blob, "relative_path", None)
    except Exception:
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass
    if not relatif:
        return None
    # Les blobs sont stockés sous `<data>/blobs/...` ; on résout sous la racine
    # déclarée plutôt que de supposer la disposition du disque.
    for base in _pb.data_dirs():
        candidat = base / relatif
        if candidat.exists():
            return str(candidat)
    return None


def _texte_feuillet(chemin: str, kind: str) -> str:
    """La représentation STRUCTURÉE du tableur (tâche 4.5), lue sur le fichier RÉEL.

    L'ancien corps APLATISSAIT : un TSV courant d'onglet en onglet, sans en-tête
    contextualisé, sans provenance de cellule, sans distinction formule / résultat
    en cache — « l'ingestion aplatisse encore les tableurs en TSV » (tâche 4.5).
    Il vit désormais dans `sheets.représentation_feuillet`, point de convergence :
    lecteur réel → `extract_schema` + `build_chunks` → `représenter_feuillet`,
    servis aux trois consumers du chemin (observation/read_document, index de
    recherche, fiche find_files), avec les repères de cellule de
    mecanismes/ingestion/tableaux.md §2.5 et les statuts de valeur de §3.3-§3.4.

    `kind` vaut « sheet » pour TOUS les tableurs : c'est ce que le dépôt
    enregistre, quelle que soit l'extension ; la définition de l'ordre des
    lecteurs (csv vs classeur) reste la clé de lecture locale.
    """
    from backend.ingestion import sheets as _sh
    from backend.ingestion import markdown_pdf as _MD

    rendu = _sh.représentation_feuillet(chemin, kind)
    if not rendu.strip():
        return rendu
    return _MD.représentation_document(
        None,
        [_MD.PageExtraite(numero=1, texte=rendu)],
        chaine="sheet",
        pages_attendues=1,
        limite_pages=None,
        mention_page_sans_texte=_MD.MENTION_PAGE_SANS_TEXTE_NATIF,
    )


def _pages_texte_natif_pdf(
    chemin: str, max_pages: int
) -> tuple[list[str], int, list[bool]]:
    """Texte natif PAR PAGE, nombre réel de pages, et images par page.

    Retourne `(textes de page, pages attendues, a_images)`. Une page sans
    texte garde sa place ("" à son rang) pour que la numérotation physique
    reste alignée — pdf.md §2.2 : « utiliser une numérotation physique de
    référence à partir de un, en conservant séparément les labels imprimés ».
    `a_images[i]` dit si la page i porte au moins une image : une page avec
    image n'est jamais « purement native », elle demande la vision pour ses
    figures. Le document est lu UNE fois : les trois mesures viennent de la
    même passe.
    """
    import pymupdf

    textes: list[str] = []
    a_images: list[bool] = []
    document: Any = pymupdf.open(chemin)
    try:
        attendues = len(document)
        for index, page in enumerate(document):
            if index >= max_pages:
                break
            textes.append(page.get_text().strip())
            try:
                a_images.append(bool(page.get_images(full=True)))
            except Exception:
                a_images.append(False)
    finally:
        try:
            document.close()
        except Exception:
            pass
    return textes, attendues, a_images


def _titre_pdf(chemin: str) -> str | None:
    """Titre réel des métadonnées du PDF, ou None s'il est vide. Pur (hors IO)."""
    brut = (_metadata_pdf(chemin) or {}).get("title") or ""
    return str(brut).strip() or None


def _extraction_pdf_native(
    chemin: str, *, collecte: CollecteExtraction | None = None
) -> str | None:
    """Rendu Markdown structuré du chemin NATIF, ou None si une page demande un modèle.

    Le seuil est PAR PAGE (`gate.NATIVE_MIN_CHARS`), plus un seuil global :
    un PDF mixte ne bascule plus tout le document à cause d'une page pauvre.
    Une page avec image demande le routage par page même si son texte est dense
    — ses figures doivent être repérées (le chemin natif ne les ignore plus).
    La mesure porte sur le TEXTE PUR, jamais sur le Markdown. Le document est lu
    UNE fois : ce qui a servi au routage sert au rendu.
    """
    from backend.ingestion import gate as _gate

    textes_pages, pages_attendues, a_images = _pages_texte_natif_pdf(
        chemin, max_pages=MAX_PAGES_PDF_INGESTION
    )
    if any(a_images) or any(len(t) < _gate.NATIVE_MIN_CHARS for t in textes_pages):
        return None
    return _document_pdf_native(
        chemin,
        textes_pages,
        pages_attendues,
        MAX_PAGES_PDF_INGESTION,
        collecte=collecte,
    )


def _document_pdf_native(
    chemin: str,
    textes_pages: list[str],
    pages_attendues: int,
    max_pages: int,
    *,
    collecte: CollecteExtraction | None = None,
) -> str:
    """Représentation Markdown structurée du chemin NATIF (tâche 4.7).

    En-tête document : le titre extrait RÉELLEMENT (métadonnée `title` du PDF)
    ou rien — jamais une synthèse ; puis la couverture, puis une section par
    page dans l'ordre physique, avec sa mention explicite si la page n'a
    rien à offrir. La chaîne native ne fait tourner AUCUN modèle : pas de
    détection de régions, donc pas de figures re-examinées ici — c'est la
    chaîne documentaire OCR (les pages qui vont au modèle) qui les porte
    (pdf.md §1.1 : « La présence d'images ne justifie pas de refaire tout le
    document par OCR » ; pp-doclayoutv3.md §4.1 « PDF natif » : « l'extraction
    native reste privilégiée lorsqu'elle répond au besoin »).
    """
    from backend.ingestion import markdown_pdf as MD

    titre: str | None = None
    # La métadonnée passe par une ouverture courte, dédiée : la lecture des
    # textes a déjà refermé la sienne, et une métadonnée vaut une relecture.
    # (La relecture est milliseconde ; la porte est franchie UNE fois, ici,
    # avec la cause au même endroit.)
    meta = _metadata_pdf(chemin)
    brut = (meta or {}).get("title") or ""
    if str(brut).strip():
        titre = str(brut).strip()
    pages = [
        MD.PageExtraite(numero=index + 1, texte=texte)
        for index, texte in enumerate(textes_pages)
    ]
    if collecte is not None:
        collecte.blocs = MD.blocs_du_document(pages)
    return MD.représentation_document(
        titre,
        pages,
        chaine="native",
        pages_attendues=pages_attendues,
        limite_pages=max_pages,
        mention_page_sans_texte=MD.MENTION_PAGE_SANS_TEXTE_NATIF,
    )


def _metadata_pdf(chemin: str) -> dict:
    """Métadonnées brutes du PDF, pour le titre du document.

    Une lecture milliseconde, dédiée ; un PDF sans métadonnées donne un
    répertoire vide, jamais un titre fabriqué. Pur (hors IO).
    """
    import pymupdf

    document: Any = pymupdf.open(chemin)
    try:
        return dict(document.metadata or {})
    except Exception:
        return {}
    finally:
        try:
            document.close()
        except Exception:
            pass


def _ocr_image(
    chemin: str, *, kind: str = "ingestion", owner: str | None = None
) -> str:
    """Transcrit une image par le modèle OCR.

    Les octets sont normalisés en PNG avant l'envoi. Le worker décode par
    `PIL.Image.open`, qui accepte le JPEG et le WEBP — mais `parse_png` promet
    du PNG, et rien ne vérifie ce qui passe pourtant par elle. Un blob JPEG
    envoyé tel quel fonctionnerait par un accord tacite entre deux modules ;
    normaliser rend l'accord explicite, et donne au fichier illisible une erreur
    ici, nommée, au lieu d'une erreur dans le worker.

    CONTEXTE (Phase 3, 3.4) : `kind` et `owner` sont transmis au coordinateur
    GPU. L'ingestion passe `kind="ingestion"` et son claim token comme owner.
    """
    from backend.ingestion import paddle_client as _pc

    octets = Path(chemin).read_bytes()
    if not octets.startswith(b"\x89PNG\r\n\x1a\n"):
        from PIL import Image

        with Image.open(io.BytesIO(octets)) as image:
            octets = _pc.pil_to_png_bytes(image.convert("RGB"))
    return _pc.parse_png(octets, kind=kind, owner=owner)


def _texte_brut(chemin: str) -> str:
    """Le contenu littéral d'un fichier texte.

    Un fichier texte n'a rien à extraire : il EST son extraction. Le lire reste
    nécessaire parce que la fiche, les fragments et la ligne d'index se
    construisent À PARTIR d'une observation — laquelle porte ce texte. Sans
    elle, un `.txt` déposé resterait muet exactement comme une image le restait.

    Le texte est lu jusqu'à la BORNE LOCALE `MAX_CARACTERES_TEXTE` (dérivée de
    `MAX_CHUNKS × CHUNK_SIZE × 4 × (100 - CHUNK_OVERLAP) / 100 + marge`), jamais
    en entier : au-delà, aucun fragment supplémentaire ne serait conservé, et
    charger un fichier de plusieurs
    centaines de Mo pour n'en indexer que la tête ferait un pic mémoire inutile.
    La troncature est JOURNALISÉE avec les deux nombres. Seule l'observation
    d'audit reste bornée à `observations.PAYLOAD_CHARS`, appliquée par
    l'appelant.
    """
    from backend.search import chunks as _chunks

    borne = int(_chunks.MAX_CARACTERES_TEXTE)
    with open(chemin, encoding="utf-8", errors="replace") as fichier:
        # On lit UN caractère de plus que la borne : c'est la seule façon de
        # savoir que le fichier continue sans charger le reste en mémoire.
        texte = fichier.read(borne + 1)
    if len(texte) > borne:
        log.warning(
            "[ingestion] %s : fichier texte tronqué à %d caractères "
            "(MAX_CARACTERES_TEXTE = MAX_CHUNKS × CHUNK_SIZE × 4 × "
            "(100 - CHUNK_OVERLAP) / 100 + marge) — la suite n'est ni lue ni indexée",
            chemin,
            borne,
        )
        texte = texte[:borne]
    texte = texte.strip()
    if not texte:
        return texte
    from backend.ingestion import markdown_pdf as _MD

    return _MD.représentation_document(
        None,
        [_MD.PageExtraite(numero=1, texte=texte)],
        chaine="native",
        pages_attendues=1,
        limite_pages=None,
        mention_page_sans_texte=_MD.MENTION_PAGE_SANS_TEXTE_NATIF,
    )


# Les valeurs que le DÉPÔT écrit dans `artifacts.kind`, et rien d'autre : ce sont
# celles que rend `sniff_mime` (`backend/ipc/attachments.py`) — `image`, `pdf`,
# `sheet`, `text`. Les comparer à des EXTENSIONS (`png`, `jpeg`, `xlsx`) ne pouvait
# donc jamais aboutir pour une vraie image : elle retombait dans « rien d'utile », et
# l'ingestion consignait « aucun texte extractible », ce qui était FAUX — le
# texte est là, il n'avait jamais été demandé au modèle.
#
# Les extensions restent acceptées parce que d'autres lecteurs les connaissent
# encore (`reader.py` les propose comme indice d'outil) : les refuser ici
# casserait un test d'ingestion réel sans rien gagner, puisque le dépôt ne les
# produit plus.
KIND_IMAGE = "image"
KIND_PDF = "pdf"
KIND_TABLEUR = "sheet"
KIND_TEXTE = "text"
_KINDS_IMAGE = (KIND_IMAGE, "png", "jpeg", "jpg", "webp")
_KINDS_TABLEUR = (KIND_TABLEUR, "csv", "xlsx", "xls", "ods")


def extraire_texte(
    artifact_id: str,
    kind: str,
    chemin: str,
    *,
    heartbeat: Callable[[], None] | None = None,
    claim_token: str | None = None,
    collecte: CollecteExtraction | None = None,
) -> tuple[str, str]:
    """Retourne (texte, mode). Lève RuntimeError si l'extraction est impossible.

    Modes : `native` (texte du PDF ou du fichier texte), `ocr` (modèle),
    `mixte` (PDF dont les pages suivent des chaînes différentes : texte natif,
    OCR de régions et figures décrites), `sheet` (tableur).

    `heartbeat` est la preuve de vie de l'appelant : chaque étape significative
    l'appelle, sans quoi une extraction longue mais vivante passerait pour un
    zombie. Un callback absent (tests purs) ne change rien à l'extraction.

    CONTEXTE GPU (Phase 3, 3.4) : `kind` et `owner` sont dérivés de l'appel.
    L'ingestion est un travail de fond (`kind="ingestion"`) dont l'identité est
    le claim token — unique par exécution — ou, à défaut, l'artefact.

    UN TYPE NON ROUTÉ LÈVE au lieu de rendre `("", "none")`. Ce couple disait
    « ce fichier n'a rien » alors qu'il dit seulement « je ne sais pas le lire » —
    deux états que l'utilisateur ne peut pas distinguer, et dont le second est
    le nôtre. Lever rend l'échec traçable : `_ingérer_un` le consigne avec sa
    cause, donc un dépôt ne peut plus être « réussi mais muet ».
    """
    _owner = claim_token or artifact_id
    _coord_kind = "ingestion"
    if kind == KIND_PDF:
        rendu = _extraction_pdf_native(chemin, collecte=collecte)
        if rendu is not None:
            return rendu, "native"
        # PDF mixte ou scanné : chaque page est routée indépendamment (native,
        # OCR de régions, placeholders de figures). La chaîne rendue est le
        # verdict réel de cette passe — « native », « ocr » ou « mixte ».
        return _extraire_pdf_page_par_page(
            chemin,
            heartbeat=heartbeat,
            kind=_coord_kind,
            owner=_owner,
            artifact_id=artifact_id,
            claim_token=claim_token,
            collecte=collecte,
        )
    if kind in _KINDS_IMAGE:
        if heartbeat is not None:
            heartbeat()
        # Qualification par le modèle de vision AVANT tout OCR (images.md §3.1) :
        # c'est le VL qui tranche document/image, jamais un comptage de caractères.
        # Si la couche de vision n'est pas enregistrée (process de tests, sidecar
        # absent), on revient à l'OCR direct : la dette reste visible via l'état
        # d'ingestion, et on ne fait pas échouer un dépôt pour une indisponibilité
        # de modèle. Voir `vision_appel.VisionIndisponible` — docstring.
        try:
            from backend.ingestion import gate as _gate
            from backend.ingestion import qualification as _qualification
            from backend.ingestion import vision_appel as _vision_appel

            try:
                octets = Path(chemin).read_bytes()
                if not octets.startswith(b"\x89PNG\r\n\x1a\n"):
                    from PIL import Image as _Image

                    with _Image.open(io.BytesIO(octets)) as _image:
                        from backend.ingestion import paddle_client as _pc

                        octets = _pc.pil_to_png_bytes(_image.convert("RGB"))
            except Exception:  # fichier absent/illisible/neutralisé (tests de routage)
                return _ocr_image(chemin, kind=_coord_kind, owner=_owner), "ocr"

            def _qualifieur(png_bytes: bytes) -> str:
                return _qualification.qualifier_image(
                    png_bytes, _vision_appel.appeler_vision_generale
                )

            issue, texte = _gate.gate_image(
                octets, qualifier=_qualifieur, kind=_coord_kind, owner=_owner
            )
            if issue == "scene":
                # Image générale : description par la vision, distincte d'une
                # transcription. À défaut de vision (déjà qualifiée, elle doit
                # être là ; titre défensif si l'appel échoue), on laisse le
                # vide nommé plutôt qu'un OCR halluciné.
                try:
                    texte = _vision_appel.appeler_vision_generale(
                        "Décris cette image en détail.", octets
                    )
                except _vision_appel.VisionIndisponible:
                    texte = ""
            return (texte or ""), "ocr"
        except _vision_appel.VisionIndisponible:
            # Repli documenté : pas de modèle de vision → OCR direct. Le fichier
            # reste ingéré (texte ou vide nommé), jamais muet.
            return _ocr_image(chemin, kind=_coord_kind, owner=_owner), "ocr"
    if kind in _KINDS_TABLEUR:
        if heartbeat is not None:
            heartbeat()
        return _texte_feuillet(chemin, kind), "sheet"
    if kind == KIND_TEXTE:
        if heartbeat is not None:
            heartbeat()
        return _texte_brut(chemin), "native"
    raise RuntimeError(
        f"type de fichier non routé : {kind!r}. Aucun extracteur ne sait lire "
        "ce type ; il faut soit ajouter son extracteur ici, soit refuser ce "
        "type au dépôt — pas le laisser passer sans extraction."
    )


def _lever_si_bail_perdu(
    bail_perdu: threading.Event | None,
    *,
    artifact_id: str | None,
    chemin: str,
    etape: str,
    token: str | None = None,
) -> None:
    """Arrête l'extraction si le battement a signalé une perte de bail. Pur.

    Un appel OCR individuel de plusieurs minutes ne peut pas être interrompu au
    milieu ; ce contrôle est le point d'arrêt effectif le plus proche. Il LÈVE au
    lieu de rendre la main, pour qu'aucune écriture ne suive la perte.

    `token` est le jeton de claim (jamais `etape`, qui n'est qu'un repère de
    journal) : `BailPerdu` le porte pour que le fencing nomme le bail réellement
    perdu. L'appelant qui ne dispose pas du jeton passe `None`.
    """
    if bail_perdu is not None and bail_perdu.is_set():
        log.debug("[ingestion] bail perdu à l'étape %s", etape)
        raise BailPerdu(artifact_id or chemin, token)


def _ocr_pdf(
    chemin: str,
    *,
    heartbeat: Callable[[], None] | None = None,
    kind: str = "ingestion",
    owner: str | None = None,
    artifact_id: str | None = None,
    claim_token: str | None = None,
) -> str:
    """Représentation Markdown structurée d'un PDF scanné ou mixte.

    Enveloppe de compatibilité : rend le seul Markdown, comme avant. Le flux
    page/région vit dans `_extraire_pdf_page_par_page`, qui rend aussi la
    chaîne réellement suivie (« native », « ocr » ou « mixte »).

    `BailPerdu` et `ModeleIndisponible` traversent sans écriture : la perte de
    bail est au repreneur, l'indisponibilité du modèle remonte à l'appelant
    (voir `_extraire_pdf_page_par_page`).
    """
    markdown, _chaine = _extraire_pdf_page_par_page(
        chemin,
        heartbeat=heartbeat,
        kind=kind,
        owner=owner,
        artifact_id=artifact_id,
        claim_token=claim_token,
    )
    return markdown


def _boite_pixels(box: Any) -> tuple[float, float, float, float] | None:
    """Boîte de région normalisée en tuple de flottants, ou None. Pure."""
    if not box or len(box) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box[:4])
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _blocs_texte_natif(page: Any, numero: int) -> list[Any]:
    """Blocs de texte natif d'une page, avec leurs bboxes en pixels.

    `page.get_text("blocks")` donne la position de chaque bloc en points PDF ;
    le rendu et les régions de layout sont en pixels (DPI_RENDU_PDF). On
    convertit donc les bboxes une seule fois ici, pour que le tri
    positionnel de `markdown_pdf` compare des repères homogènes.
    """
    from backend.ingestion import gate as _gate
    from backend.ingestion import markdown_pdf as MD

    echelle = DPI_RENDU_PDF / 72.0
    blocs: list[Any] = []
    try:
        bruts = page.get_text("blocks")
    except Exception:
        texte = _gate.native_page_text(page)
        if texte:
            blocs.append(MD.BlocPage(type="texte", contenu=texte, page=numero, ordre=0))
        return blocs
    for ordre, bloc in enumerate(bruts):
        if len(bloc) < 7:
            continue
        x0, y0, x1, y1, texte, _no, type_bloc = bloc[:7]
        if int(type_bloc) != 0 or not str(texte).strip():
            continue
        blocs.append(
            MD.BlocPage(
                type="texte",
                contenu=str(texte).strip(),
                page=numero,
                bbox=(
                    float(x0) * echelle,
                    float(y0) * echelle,
                    float(x1) * echelle,
                    float(y1) * echelle,
                ),
                ordre=ordre,
            )
        )
    if not blocs:
        texte = _gate.native_page_text(page)
        if texte:
            blocs.append(MD.BlocPage(type="texte", contenu=texte, page=numero, ordre=0))
    return blocs


def _bloc_region_figure(
    numero_page: int,
    index_region: int,
    region: dict,
    image: Any,
    *,
    kind: str,
    owner: str | None,
    collecte: CollecteExtraction | None,
) -> tuple[list[Any], list[Any]]:
    """Arbitre une région figure SANS description LFM-VL (étape A).

    Rend `(blocs_figure, blocs_texte)`. Le libellé « image » reste ambigu : on
    mesure la densité OCR du crop (`region_est_photo_de_page`) et on bascule la
    région en texte quand elle est dense ; sinon, comme pour tout libellé de
    figure, on pose un PLACEHOLDER et une ligne `pending`. La description VLM
    est différée à l'étape B — ce module ne l'appelle plus.

    Un crop impossible ne supprime pas la figure : la boîte et la région
    suffisent à l'étape B pour recalculer le crop depuis le PDF. Le placeholder
    reste donc positionnel, et la ligne `pending` est créée dans tous les cas.
    """
    from backend.ingestion import figures_pdf as FIG
    from backend.ingestion import markdown_pdf as MD
    from backend.ingestion import paddle_client as _pc

    label = str(region.get("category", "")).strip().lower()
    box = region.get("box")
    bbox = _boite_pixels(box)
    crop = FIG.cropper_figure(image, box)
    if label in FIG.LABELS_REGION_PHOTO_PAGE and crop is not None:
        est_photo, texte = FIG.region_est_photo_de_page(
            crop,
            lambda c: _pc.parse_png(
                _pc.pil_to_png_bytes(c), task="parse", kind=kind, owner=owner
            ),
        )
        if est_photo and texte:
            return [], [
                MD.BlocPage(
                    type="texte",
                    contenu=MD.bloc_texte(numero_page, index_region, texte),
                    page=numero_page,
                    region=index_region,
                    bbox=bbox,
                )
            ]
    if collecte is not None:
        try:
            score = float(region.get("score", 0.0))
        except (TypeError, ValueError):
            score = None
        collecte.figures.append(
            FIG.FigurePlaceholder(
                page=numero_page,
                region=index_region,
                label=label or None,
                score=score,
                bbox=bbox,
            )
        )
    return [
        MD.BlocPage(
            type="figure",
            contenu=FIG.bloc_figure_en_attente(numero_page, index_region),
            page=numero_page,
            region=index_region,
            bbox=bbox,
        )
    ], []


def _blocs_regions_figures(
    numero_page: int,
    image: Any,
    regions: list[dict],
    *,
    kind: str,
    owner: str | None,
    collecte: CollecteExtraction | None,
    verifier_bail: Callable[[], None] | None = None,
) -> list[Any]:
    """Les blocs des FIGURES d'une page déjà rendue : repérage → placeholder.

    Utilisé dans les DEUX routes à figures : une page au texte natif suffisant
    (aucune OCR de texte) et une page scannée sans texte (l'OCR des régions
    texte/table est fait par l'appelant, puis cette fonction traite les
    figures). `FIG.regions_figure` garde les indices de lecture d'origine ; la
    borne `CROPS_FIGURE_MAX_PAR_PAGE` s'applique ici, une seule fois. Au-delà,
    les régions suivantes sont repérées, pas effacées : un bloc `limite` nomme
    le nombre de régions non traitées. Aucune description LFM-VL n'est posée
    ici : les figures retenues deviennent des placeholders et des lignes
    `pending` (étape A).
    """
    from backend.ingestion import figures_pdf as FIG
    from backend.ingestion import markdown_pdf as MD

    blocs: list[Any] = []
    candidates = FIG.regions_figure(regions)
    à_reviser = candidates[: FIG.CROPS_FIGURE_MAX_PAR_PAGE]
    for index_region, region in à_reviser:
        if verifier_bail is not None:
            verifier_bail()
        blocs_figure, blocs_texte = _bloc_region_figure(
            numero_page,
            index_region,
            region,
            image,
            kind=kind,
            owner=owner,
            collecte=collecte,
        )
        blocs.extend(blocs_figure)
        blocs.extend(blocs_texte)
    depassees = len(candidates) - len(à_reviser)
    if depassees:
        if collecte is not None:
            collecte.figures_limitees += depassees
        dernier = candidates[-1]
        blocs.append(
            MD.BlocPage(
                type="limite",
                contenu=FIG.bloc_figure_limite(
                    numero_page,
                    dernier[0],
                    f"borne de recadrage dépassée : {depassees} région(s) figure(s) "
                    "supplémentaire(s) non traitées (pp-doclayoutv3.md §4.5)",
                ),
                page=numero_page,
                region=dernier[0],
                bbox=_boite_pixels((dernier[1] or {}).get("box")),
            )
        )
    return blocs


def _extraire_page_pdf(
    page: Any,
    numero: int,
    *,
    heartbeat: Callable[[], None] | None,
    kind: str,
    owner: str | None,
    verifier_bail: Callable[[], None],
    collecte: CollecteExtraction | None,
) -> tuple[Any, str]:
    """Extrait une page en blocs ordonnés. Rend (page, chaîne de la page).

    Trois routes, décidées PAR PAGE :
    1. texte natif suffisant et aucune image → un bloc texte natif, zéro modèle ;
    2. texte natif suffisant mais images → blocs natifs + placeholders des
       figures retenues (aucune OCR du texte déjà natif) ;
    3. texte natif insuffisant → OCR des régions texte/table et placeholders
       des régions figure BORNÉES par `CROPS_FIGURE_MAX_PAR_PAGE`, sans
       décrire une région déjà couverte par l'OCR.

    Les chaînes rendues sont « native » (aucun modèle), « vision » (texte natif
    + figures repérées) et « ocr » (texte insuffisant, chaîne modèle) : l'agrégat
    du document en déduit « native », « ocr » ou « mixte ». « vision » garde son
    nom de routage, même si l'étape A ne fait que POSER les figures.
    """
    from backend.ingestion import figures_pdf as FIG
    from backend.ingestion import gate as G
    from backend.ingestion import markdown_pdf as MD
    from backend.ingestion import paddle_client as _pc

    native = G.native_page_text(page)
    a_images = G.page_a_des_images(page)
    seuil = G.NATIVE_MIN_CHARS

    # 1. Page native pure : zéro modèle.
    if len(native) >= seuil and not a_images:
        blocs = _blocs_texte_natif(page, numero)
        return MD.PageExtraite(
            numero=numero, texte=native, blocs=tuple(blocs)
        ), "native"

    # 2. Texte natif suffisant + images : garder le texte, poser les figures.
    if len(native) >= seuil and a_images:
        blocs = _blocs_texte_natif(page, numero)
        image = G.render_page_png(page, dpi=DPI_RENDU_PDF)
        png = _pc.pil_to_png_bytes(image)
        regions = G.regions_vers_gate(_pc.detect_regions(png, kind=kind, owner=owner))
        verifier_bail()
        blocs.extend(
            _blocs_regions_figures(
                numero,
                image,
                regions,
                kind=kind,
                owner=owner,
                collecte=collecte,
                verifier_bail=verifier_bail,
            )
        )
        return MD.PageExtraite(
            numero=numero, texte=native, blocs=tuple(blocs)
        ), "vision"

    # 3. Texte natif insuffisant : OCR par région, description des figures.
    image = G.render_page_png(page, dpi=DPI_RENDU_PDF)
    png = _pc.pil_to_png_bytes(image)
    regions = G.regions_vers_gate(_pc.detect_regions(png, kind=kind, owner=owner))
    verifier_bail()

    if not regions:
        # Repli borné : aucune région détectée → OCR de la page entière.
        texte = (_pc.parse_png(png, task="parse", kind=kind, owner=owner) or "").strip()
        blocs = (
            [
                MD.BlocPage(
                    type="texte",
                    contenu=MD.bloc_texte(numero, None, texte),
                    page=numero,
                    ordre=0,
                )
            ]
            if texte
            else []
        )
        return MD.PageExtraite(numero=numero, texte=texte, blocs=tuple(blocs)), "ocr"

    blocs: list[Any] = []
    for index_region, region in enumerate(regions, start=1):
        # Frontière de région : une perte de bail arrête l'extraction avant tout
        # nouvel appel OCR ou vision, jamais au milieu d'une page.
        verifier_bail()
        label = str(region.get("category", "")).strip().lower()
        box = region.get("box")
        if label in G.LABELS_TEXTE:
            crop = FIG.cropper_figure(image, box)
            if crop is None:
                continue
            texte = (
                _pc.parse_png(
                    _pc.pil_to_png_bytes(crop), task="parse", kind=kind, owner=owner
                )
                or ""
            ).strip()
            if texte:
                blocs.append(
                    MD.BlocPage(
                        type="texte",
                        contenu=MD.bloc_texte(numero, index_region, texte),
                        page=numero,
                        region=index_region,
                        bbox=_boite_pixels(box),
                    )
                )
        elif label in G.LABELS_TABLE:
            crop = FIG.cropper_figure(image, box)
            if crop is None:
                continue
            texte = (
                _pc.parse_png(
                    _pc.pil_to_png_bytes(crop), task="table", kind=kind, owner=owner
                )
                or ""
            ).strip()
            if texte:
                blocs.append(
                    MD.BlocPage(
                        type="table",
                        contenu=MD.bloc_table(numero, index_region, texte),
                        page=numero,
                        region=index_region,
                        bbox=_boite_pixels(box),
                    )
                )
        elif label in FIG.LABELS_REGION_FIGURE:
            # Traitées APRÈS la boucle, une seule fois et sous la borne de
            # `_blocs_regions_figures` : une boucle inline sur toutes les
            # figures ferait payer un appel vision par région, sans limite.
            continue
        else:
            # Libellé inconnu : OCR de la région comme texte, pour ne rien perdre.
            crop = FIG.cropper_figure(image, box)
            if crop is None:
                continue
            texte = (
                _pc.parse_png(
                    _pc.pil_to_png_bytes(crop), task="parse", kind=kind, owner=owner
                )
                or ""
            ).strip()
            if texte:
                blocs.append(
                    MD.BlocPage(
                        type="texte",
                        contenu=MD.bloc_texte(numero, index_region, texte),
                        page=numero,
                        region=index_region,
                        bbox=_boite_pixels(box),
                    )
                )
    # Dernière région terminée : une perte survenue pendant son parse doit
    # arrêter AVANT la construction de la page, même sans région figure.
    verifier_bail()
    # Les figures sont repérées UNE fois, bornées par
    # `CROPS_FIGURE_MAX_PAR_PAGE` ; les régions écartées laissent un bloc
    # `limite` au lieu de disparaître.
    blocs.extend(
        _blocs_regions_figures(
            numero,
            image,
            regions,
            kind=kind,
            owner=owner,
            collecte=collecte,
            verifier_bail=verifier_bail,
        )
    )
    texte_page = "\n\n".join(
        bloc.contenu for bloc in blocs if bloc.type in ("texte", "table")
    )
    return MD.PageExtraite(numero=numero, texte=texte_page, blocs=tuple(blocs)), "ocr"


def _extraire_pdf_page_par_page(
    chemin: str,
    *,
    heartbeat: Callable[[], None] | None = None,
    kind: str = "ingestion",
    owner: str | None = None,
    artifact_id: str | None = None,
    claim_token: str | None = None,
    collecte: CollecteExtraction | None = None,
) -> tuple[str, str]:
    """Représentation Markdown structurée d'un PDF, page par page.

    Chaque page est routée indépendamment : une page native ne paie aucun
    modèle ; une page avec figures garde son texte natif et pose des
    placeholders ; une page sans texte passe par l'OCR de ses régions. Les blocs
    sont assemblés par `markdown_pdf` dans l'ordre de lecture (y0, x0), donc un
    placeholder de figure s'intercale à sa position.

    La chaîne rendue est « native » (aucun modèle), « ocr » (toutes les pages
    ont demandé un modèle) ou « mixte » (pages natives et/ou figures repérées
    dans le même document).

    `BailPerdu` remonte sans écriture : le repreneur est responsable. Une
    `ModeleIndisponible` remonte aussi : ce n'est pas « une page a échoué »,
    c'est « le modèle n'est pas là ». Toute autre exception de page est
    consignée comme page non reconnue, sans tuer le document (pdf.md §5.1).
    """
    import pymupdf

    from backend.inference.resource_coordinator import ResourceBusy
    from backend.ingestion import markdown_pdf as MD
    from backend.ingestion import paddle_client as _pc

    pages: list[Any] = []
    modes_pages: list[str] = []
    attendues = 0
    document: Any = pymupdf.open(chemin)
    try:
        attendues = len(document)
        with _battement_periodique(heartbeat) as bail_perdu:

            def _verifier_bail(etape: str) -> None:
                _lever_si_bail_perdu(
                    bail_perdu,
                    artifact_id=artifact_id,
                    chemin=chemin,
                    etape=etape,
                    token=claim_token,
                )

            for index in range(min(attendues, MAX_PAGES_PDF_INGESTION)):
                _verifier_bail("pdf:page")
                if heartbeat is not None:
                    heartbeat()
                page = document[index]
                numero = index + 1
                try:
                    page_extraite, mode_page = _extraire_page_pdf(
                        page,
                        numero,
                        heartbeat=heartbeat,
                        kind=kind,
                        owner=owner,
                        verifier_bail=lambda: _verifier_bail("pdf:région"),
                        collecte=collecte,
                    )
                    pages.append(page_extraite)
                    modes_pages.append(mode_page)
                except BailPerdu:
                    # La perte de bail n'est JAMAIS transformée en page non
                    # reconnue : elle doit arrêter l'extraction sans écriture.
                    raise
                except _pc.ModeleIndisponible:
                    # Le worker n'est pas joignable : ce n'est pas « cette page
                    # a échoué », c'est « le modèle n'est pas là ».
                    raise
                except ResourceBusy:
                    # La garde de priorité a refusé l'OCR d'ingestion parce
                    # qu'un tour interactif est actif. Ce n'est PAS une page
                    # illisible : la convertir en « page non reconnue » ferait
                    # aboutir l'extraction avec un document amputé et empêcherait
                    # `_ingérer_un` de déposer la dette pour replanification.
                    raise
                except Exception as cause:  # une page en panne est nommée
                    mention = MD.mention_page_non_reconnue(
                        numero, f"{type(cause).__name__}: {cause}"
                    )
                    pages.append(
                        MD.PageExtraite(
                            numero=numero,
                            texte=mention,
                            blocs=(
                                MD.BlocPage(
                                    type="texte",
                                    contenu=mention,
                                    page=numero,
                                    ordre=0,
                                ),
                            ),
                        )
                    )
                    modes_pages.append("ocr")
            _verifier_bail("pdf:fin")
    finally:
        try:
            document.close()
        except Exception:
            pass

    if all(mode == "native" for mode in modes_pages):
        chaine = "native"
    elif all(mode == "ocr" for mode in modes_pages):
        chaine = "ocr"
    else:
        chaine = "mixte"
    if collecte is not None:
        collecte.blocs = MD.blocs_du_document(pages)
    titre = None if chaine == "ocr" else _titre_pdf(chemin)
    return (
        MD.représentation_document(
            titre,
            pages,
            chaine=chaine,
            pages_attendues=attendues,
            limite_pages=MAX_PAGES_PDF_INGESTION,
            mention_page_sans_texte=MD.MENTION_PAGE_SANS_TEXTE_OCR,
        ),
        chaine,
    )


# Le seuil natif/OCR est PAR PAGE, porté par `gate.NATIVE_MIN_CHARS` (point
# unique). L'ancien seuil GLOBAL de 200 caractères faisait basculer un document
# entier à cause d'une seule page pauvre : il est supprimé, pas déplacé. Une
# page avec image demande aussi la vision (figures), même si son texte est
# dense — le chemin natif ne les ignore plus.


class ModeleRequis(RuntimeError):
    """L'extraction de ce fichier passe par un modèle, et les modèles sont refusés.

    Un nom, et non un booléen, parce que ce refus doit être JOURNALISÉ et porté dans
    l'état d'ingestion : un fichier dont l'OCR a été repoussé faute de machine doit
    rester une dette visible, pas redevenir un « jamais traité » muet.
    """


def _extraire_sans_modele(
    kind: str, chemin: str, *, collecte: CollecteExtraction | None = None
) -> tuple[str, str]:
    """Comme `extraire_texte`, mais LÈVE `ModeleRequis` au lieu de démarrer un modèle.

    Le refus est posé AU POINT EXACT où le modèle serait nécessaire, pas avant : un PDF
    né d'un traitement de texte s'extrait en millisecondes, et le refuser d'emblée
    parce qu'« il pourrait » demander de l'OCR laisserait le fichier sans texte alors
    que son texte est dans le fichier. On fait donc le travail gratuit, et on s'arrête
    au moment où il ne l'est plus.

    C'est ce qui rend le balayage de reprise HONNÊTE : il ne dit pas « ces fichiers
    passent par un modèle », il dit « ces fichiers-ci passent par un modèle ».
    """
    if kind == KIND_PDF:
        rendu = _extraction_pdf_native(chemin, collecte=collecte)
        if rendu is not None:
            return rendu, "native"
        raise ModeleRequis(
            "PDF nécessitant un modèle : pages sans couche texte exploitable ou "
            "pages contenant des figures à décrire"
        )
    if kind in _KINDS_IMAGE:
        raise ModeleRequis("une image s'extrait par OCR")
    if kind in _KINDS_TABLEUR:
        return _texte_feuillet(chemin, kind), "sheet"
    if kind == KIND_TEXTE:
        return _texte_brut(chemin), "native"
    # Un type non routé lève pareillement : c'est un défaut de couverture, pas une
    # raison d'aller charger un modèle.
    raise RuntimeError(
        f"type de fichier non routé : {kind!r}. Aucun extracteur ne sait lire "
        "ce type ; il faut soit ajouter son extracteur ici, soit refuser ce "
        "type au dépôt — pas le laisser passer sans extraction."
    )


def _marquer(
    artifact_id: str, etat: str, raison: str = "", claim_token: str | None = None
) -> str:
    """Écrit l'état d'ingestion dans une session COURTE, puis la referme.

    La session est ouverte et fermée autour de l'écriture seule : l'extraction dure
    parfois plusieurs minutes, et garder une connexion pendant ce temps viderait le
    pool — c'est la raison pour laquelle `_ingérer_un` procède déjà ainsi. Une seule
    écriture ne justifie pas un `SessionLocal` tenu ouvert.

    FENCING (Phase 3, tâche 3.3) : quand `claim_token` est fourni, l'écriture
    n'est valide que si le bail est encore détenu. `BailPerdu` remonte au worker
    qui doit s'arrêter immédiatement.

    Rend l'état RÉELLEMENT écrit, ou `""` si l'écriture a échoué. Il n'y a
    volontairement aucun repli sur l'état demandé : `marquer_une_fois` avale les
    pannes d'écriture (verrou SQLite, artefact disparu) et rend `""` ; prétendre
    alors que l'état demandé est en base ferait émettre un événement qui mentirait
    sur le contenu réel.

    Un premier échec est JOURNALISÉ (artefact, état visé, raison) puis retenté
    UNE seule fois dans une nouvelle session courte : l'écriture finale ne doit
    pas dépendre d'un verrou transitoire. Si le second essai échoue aussi, le
    journal nomme l'artefact resté dans son état précédent — et, s'il était
    `en_cours`, le balayage de reprise le reprendra à `SEUIL_EN_COURS_S` (900 s).
    """
    from backend.db.database import SessionLocal as _SL
    from backend.ingestion.etat import marquer_une_fois

    def _ecrire() -> str:
        """Une tentative dans une session COURTE — jamais une transaction tenue."""
        session = _SL()
        try:
            return marquer_une_fois(
                session, artifact_id, etat, raison, claim_token=claim_token
            )
        finally:
            try:
                session.close()
            except Exception:
                pass

    ecrit = _ecrire()
    if ecrit:
        return ecrit

    # Le travail d'extraction est déjà fait : perdre le relevé ne doit pas le
    # faire échouer (c'est `marquer_une_fois`), mais laisser l'artefact en
    # `en_cours` en silence serait le défaut initial. On nomme l'échec, on
    # retente UNE fois dans une nouvelle session courte, puis on nomme le
    # résultat réel — qui peut rester vide.
    log.error(
        "[ingestion] état %s non écrit pour %s (raison : %s) : réessai unique",
        etat,
        artifact_id,
        raison or "(aucune)",
    )
    ecrit = _ecrire()
    if not ecrit:
        log.error(
            "[ingestion] état %s toujours non écrit pour %s après réessai : "
            "l'artefact garde son état précédent ; s'il était `en_cours`, le "
            "balayage de reprise le reprendra au seuil SEUIL_EN_COURS_S (900 s)",
            etat,
            artifact_id,
        )
    return ecrit


def _purger_index_binaire(artifact_id: str, claim_token: str | None = None) -> None:
    """Purge FTS + fragments + vecteurs d'un artefact refusé comme binaire.

    Best-effort MAIS journalisé : une purge silencieusement ratée laisserait des
    octets refusés dans la recherche, exactement le défaut que `binaire_refuse`
    doit rendre impossible.

    FENCING (Phase 3, tâche 3.3) : avec `claim_token`, la purge n'est exécutée
    que si le bail est encore détenu. La vérification est faite DANS la même
    session que la purge, avant l'écriture : un worker expiré ne doit pas purger
    l'index d'un artefact repris par un autre. Zéro ligne de garde lève
    `BailPerdu` sans rien supprimer.
    """
    from backend.db.database import SessionLocal as _SL
    from backend.db.models import Artifact as _Art
    from backend.search import fts as _fts

    session = _SL()
    try:
        if claim_token:
            detenu = (
                session.query(_Art.id)
                .filter(
                    _Art.id == artifact_id,
                    _Art.ingest_claim_token == claim_token,
                )
                .first()
                is not None
            )
            if not detenu:
                session.rollback()
                raise BailPerdu(artifact_id, claim_token)
        _fts.purger_index_artifact(session, artifact_id)
        session.commit()
    except BailPerdu:
        raise
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        log.warning(
            "[ingestion] purge binaire_refuse impossible pour %s : %s: %s",
            artifact_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def _tenter(artifact_id: str, claim_token: str | None = None) -> int:
    """Compte la tentative en cours, session courte. Rend son numéro.

    FENCING (Phase 3, tâche 3.3) : avec `claim_token`, l'incrément n'est écrit
    que si le bail est encore détenu. Zéro ligne lève `BailPerdu`.
    """
    from backend.db.database import SessionLocal as _SL

    session = _SL()
    try:
        return ETAT.demarrer_tentative(session, artifact_id, claim_token=claim_token)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _planifier_reprise(
    artifact_id: str, tentatives: int, claim_token: str | None = None
) -> None:
    """Écrit la prochaine échéance de reprise. Best-effort journalisé.

    L'état est déjà écrit quand on passe ici : perdre l'échéance ne doit pas
    faire croire que l'état n'a pas été posé. La ligne de journal dit quoi
    réparer si la base refuse l'écriture.

    FENCING : `BailPerdu` n'est jamais avalée — une échéance écrite pour un
    artefact repris par un autre serait une dette fantôme.
    """
    from backend.db.database import SessionLocal as _SL

    session = _SL()
    try:
        ETAT.planifier_tentative(
            session, artifact_id, tentatives, claim_token=claim_token
        )
    except BailPerdu:
        raise
    except Exception as exc:
        log.warning(
            "[ingestion] prochaine tentative non écrite pour %s : %s: %s",
            artifact_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def _reinitialiser_essais(artifact_id: str, claim_token: str | None = None) -> None:
    """Remet le compteur à zéro après un succès complet. Best-effort journalisé.

    FENCING : `BailPerdu` n'est jamais avalée — un worker expiré ne doit pas
    remettre à zéro le compteur d'un artefact repris par un autre.
    """
    from backend.db.database import SessionLocal as _SL

    session = _SL()
    try:
        ETAT.reinitialiser_essais(session, artifact_id, claim_token=claim_token)
    except BailPerdu:
        raise
    except Exception as exc:
        log.warning(
            "[ingestion] compteur d'essais non remis à zéro pour %s : %s: %s",
            artifact_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def _battre(artifact_id: str, claim_token: str | None = None) -> None:
    """Preuve de vie de l'extraction. Best-effort : un battement raté se journalise.

    FENCING (Phase 3, tâche 3.3) : avec `claim_token`, le battement n'est écrit
    que si le bail est encore détenu. Un `False` (bail perdu) doit arrêter le worker.
    """
    from backend.db.database import SessionLocal as _SL

    session = _SL()
    try:
        ok = ETAT.battre_heartbeat(session, artifact_id, claim_token=claim_token)
        if not ok and claim_token:
            raise BailPerdu(artifact_id, claim_token)
    except BailPerdu:
        # La perte de bail n'est jamais un battement « raté » : elle doit
        # traverser les couches sans dépendre du nom de la classe.
        raise
    except Exception as exc:
        log.debug(
            "[ingestion] battement non écrit pour %s : %s: %s",
            artifact_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def _annuler_tentative(artifact_id: str, claim_token: str | None = None) -> int:
    """Défait une tentative qui n'a pas commencé de travail réel. Rend le reste.

    FENCING : avec `claim_token`, l'annulation n'est écrite que si le bail est
    encore détenu ; sinon `BailPerdu` remonte et le worker s'arrête.
    """
    from backend.db.database import SessionLocal as _SL

    session = _SL()
    try:
        return ETAT.annuler_tentative(session, artifact_id, claim_token=claim_token)
    finally:
        try:
            session.close()
        except Exception:
            pass


@contextlib.contextmanager
def _battement_periodique(
    heartbeat: Callable[[], None] | None,
    *,
    intervalle_s: float = INTERVALLE_BATTEMENT_S,
) -> Iterator[threading.Event]:
    """Émet un battement PENDANT les appels longs, pas seulement entre eux.

    `detect_regions` et `parse_png` peuvent durer 600 s chacun : un battement
    posé avant la page laisse alors un trou de vingt minutes, et une page
    vivante passerait pour un zombie. Le fil de battement est daemon et arrêté
    dans le `finally` — un battement orphelin écrirait dans la base après le
    départ de son extraction.

    L'ARRÊT EST COOPÉRATIF ET BORNÉ : l'événement est vérifié à chaque tour
    (avant chaque battement), et le `join` est borné. Un battement déjà engagé
    dans un appel base ne peut pas être interrompu au milieu ; si le fil est
    encore vivant après le délai, c'est JOURNALISÉ plutôt que supposé terminé.
    La sécurité n'en dépend pas : le heartbeat réel est lié au claim
    (`lambda: _battre(fichier, claim_token)`), donc après libération du bail
    l'écriture est rejetée par le filtre `ingest_claim_token` (no-op + `False`).

    Rend un `Event` qui signale une perte de bail (`BailPerdu`) survenue dans
    le fil de battement. La boucle d'extraction DOIT le vérifier à chaque
    frontière de page/région : c'est le point d'arrêt effectif le plus proche
    sans modifier les clients OCR. Un appel OCR individuel de plusieurs minutes
    ne peut pas être interrompu au milieu ; l'arrêt a donc lieu au prochain
    point fencé, et AUCUNE écriture n'est faite après la perte. Un `BailPerdu`
    n'est jamais avalé silencieusement : il est signalé et le fil cesse de battre.
    """
    if heartbeat is None:
        yield threading.Event()
        return

    arret = threading.Event()
    bail_perdu = threading.Event()

    def _boucle() -> None:
        while not arret.is_set():
            if arret.wait(max(0.001, intervalle_s)):
                return
            if arret.is_set():
                return
            try:
                heartbeat()
            except BailPerdu as exc:
                # La perte de bail est une CONDITION D'ARRÊT, pas un battement
                # raté. On la signale à la boucle d'extraction et on cesse de
                # battre : insister ne ferait qu'écrire dans le vide.
                log.warning("[ingestion] bail perdu pendant le battement : %s", exc)
                bail_perdu.set()
                return
            except Exception as exc:  # un battement raté ne tue pas l'extraction
                log.debug(
                    "[ingestion] battement périodique ignoré : %s: %s",
                    type(exc).__name__,
                    exc,
                )

    fil = threading.Thread(target=_boucle, name="ingest-heartbeat", daemon=True)
    fil.start()
    try:
        yield bail_perdu
    finally:
        arret.set()
        fil.join(timeout=5.0)
        if fil.is_alive():
            # Un battement peut être bloqué dans un appel base au moment de
            # l'arrêt. On ne prétend pas l'avoir joint : le journal dit qu'il
            # peut encore écrire, et le fencing par claim_token rend cette
            # écriture inoffensive (no-op après libération du bail).
            log.warning(
                "[ingestion] fil de battement encore vivant après %.1f s "
                "d'arrêt ; l'écriture restante est bornée par le claim_token",
                5.0,
            )


def _etat_avec_plafond(etat_vise: str, raison: str, tentatives: int) -> tuple[str, str]:
    """Traduit une dette rejouable en état terminal AU PLAFOND. Pur.

    `depose` devient `echec` (le fichier n'a jamais pu être lu) ; `partiel`
    devient `tronque` (une partie du texte existe et reste servie, mais le
    rejeu ne complétera plus le document). La raison porte le plafond, sinon
    l'état terminal accuserait sans dire pourquoi il s'est arrêté.
    """
    if etat_vise not in (ETAT.ETAT_DEPOSE, ETAT.ETAT_PARTIEL):
        return etat_vise, raison
    if tentatives < ETAT.PLAFOND_ESSAIS:
        return etat_vise, raison
    terminal = ETAT.ETAT_TRONQUE if etat_vise == ETAT.ETAT_PARTIEL else ETAT.ETAT_ECHEC
    motif = f"{raison} ; plafond d'essais atteint ({ETAT.PLAFOND_ESSAIS})"
    return terminal, motif


def _basculer_reprise(
    artifact_id: str,
    etat_vise: str,
    raison: str,
    tentatives: int,
    claim_token: str | None = None,
) -> tuple[str, str]:
    """Écrit une dette rejouable et son échéance, ou son état terminal au plafond.

    Rend `(état réellement écrit, motif)` pour que l'appelant émette l'événement
    terminal sur l'état que la base porte — et non sur celui qu'il a demandé.
    Un échec après un texte déjà extrait restaure ce texte ; l'événement doit le
    dire, sinon l'écran afficherait un échec que le dépôt ne connaît pas.

    `_planifier_reprise` est appelé DANS TOUS LES CAS : au plafond il écrit
    explicitement NULL, ce qui retire une échéance antérieure. S'en abstenir
    laisserait une date future sur un état terminal — le balayage ne la lit
    plus, mais la donnée mentirait sur une reprise à venir.

    FENCING : les deux écritures portent le `claim_token`. Une perte de bail
    remonte en `BailPerdu` et arrête le worker, sans laisser d'état ni de dette.
    """
    etat_final, motif = _etat_avec_plafond(etat_vise, raison, tentatives)
    etat_ecrit = _marquer(artifact_id, etat_final, motif, claim_token=claim_token)
    _planifier_reprise(artifact_id, tentatives, claim_token=claim_token)
    return etat_ecrit, motif


def _motif_troncature(markdown: str) -> str | None:
    """Motif de troncature si la couverture signale des pages restantes. Pur.

    La couverture est la SEULE source : `pages_restantes` n'y est écrit que
    quand la borne a effectivement coupé. Une clé absente ne fabrique pas une
    troncature — on ne devine pas une coupure que l'extracteur n'a pas dite.
    """
    from backend.ingestion import markdown_pdf as _MD

    restantes = _MD.pages_restantes(markdown)
    if restantes is None or restantes <= 0:
        return None
    return (
        f"document tronqué à {MAX_PAGES_PDF_INGESTION} pages ; "
        f"{restantes} page(s) restante(s) non traitée(s)"
    )


def _persister_extraction_complete(
    artifact_id: str,
    texte: str,
    collecte: CollecteExtraction | None,
    *,
    claim_token: str | None = None,
):
    """Écrit la représentation complète, ses figures et les compteurs d'état.

    Appelée seulement après l'arbitrage qui a conclu que le texte est
    exploitable (`extrait`, `tronque`, `partiel`) : un contenu binaire refusé ne
    doit pas laisser de représentation derrière lui.

    La transaction est UNIQUE : représentation, lignes `pending` et compteurs
    tombent ensemble. `creer_representation(commit=False)` périme l'ancienne
    courante et insère la nouvelle ; les figures reçoivent la nouvelle
    `representation_id`. Les figures de l'ancienne courante passent
    `superseded` dans cette même transaction.

    FENCING : le claim est vérifié par un UPDATE conditionnel sur `artifacts`
    (`WHERE id=:id AND ingest_claim_token=:token`) juste avant les insertions.
    Un SELECT préalable laissait une fenêtre entre la lecture et l'écriture ; ici
    la base arbitre au moment d'écrire. Zéro ligne touchée lève `BailPerdu`
    APRÈS rollback : aucune représentation, aucune figure, aucune transaction
    laissée ouverte.

    ÉCHEC DE PERSISTANCE : une panne d'écriture (verrou, disque) remonte après
    rollback. L'état de l'artefact reste `en_cours` et sa dette est reprise par
    le balayage quand le bail expire — le seuil de 900 s est inchangé.
    """
    if not texte.strip():
        return None
    from backend.db.database import SessionLocal
    from backend.db.models import Artifact as _Art
    from backend.ingestion import figures_store as FS
    from backend.ingestion import representations as R

    session = SessionLocal()
    try:
        if claim_token:
            # UPDATE conditionnel, volontairement sans changement de valeur :
            # c'est le rowcount qui prouve que le bail est encore le nôtre.
            lignes = (
                session.query(_Art)
                .filter(
                    _Art.id == artifact_id,
                    _Art.ingest_claim_token == claim_token,
                )
                .update(
                    {_Art.ingest_updated_at: _Art.ingest_updated_at},
                    synchronize_session=False,
                )
            )
            if not lignes:
                session.rollback()
                raise BailPerdu(artifact_id, claim_token)
        blocs = collecte.blocs if collecte is not None else []
        if not blocs:
            # Un fichier texte/image n'a pas de blocs de page : on en pose un
            # seul, honnête, plutôt que de laisser `blocks_json` NULL et de
            # rendre le patch futur incapable de retrouver le contenu.
            blocs = [
                {
                    "page": 1,
                    "region": None,
                    "type": "texte",
                    "bbox": None,
                    "ordre": 0,
                    "contenu": texte,
                }
            ]
        representation = R.creer_representation(
            session,
            artifact_id,
            texte,
            blocs,
            extractor_version=VERSION_EXTRACTEUR,
            commit=False,
        )
        figures = collecte.figures if collecte is not None else []
        FS.enregistrer_figures_en_attente(
            session,
            artifact_id,
            representation.id,
            figures,
            extractor_version=VERSION_EXTRACTEUR,
        )
        FS.mettre_a_jour_etat_figures(
            session,
            artifact_id,
            figures_limited=(collecte.figures_limitees if collecte is not None else 0),
        )
        session.commit()
        return representation
    except Exception:
        session.rollback()
        raise
    finally:
        try:
            session.close()
        except Exception:
            pass


def _reveiller_figures_apres_etape_a(collecte: CollecteExtraction | None) -> None:
    """Réveille le worker figures APRÈS le commit de l'étape A — non bloquant.

    Le réveil est un simple `Event.set` : il ne lance aucun modèle et ne peut
    pas allonger la réponse d'ingestion. Si l'import ou le réveil échoue, le
    balayage périodique du worker retrouve les lignes `pending` : aucune figure
    n'est perdue par un enqueue manqué.
    """
    if collecte is None or not getattr(collecte, "figures", None):
        return
    try:
        from backend.ingestion import figures_worker

        figures_worker.reveiller_worker_figures()
    except Exception as exc:  # noqa: BLE001 — le fond ne casse pas l'ingestion
        log.warning(
            "[ingestion] réveil figures impossible : %s: %s", type(exc).__name__, exc
        )


def _ingérer_un_impl(
    artifact_id: str,
    *,
    modeles_autorises: bool = True,
    _proprietaire: str | None = None,
) -> None:
    from backend.db.database import SessionLocal
    from backend.inference.resource_coordinator import ResourceBusy
    from backend.ingestion import sheets as _sheets
    from backend.ingestion.paddle_client import ModeleIndisponible
    from backend.search.observations import (
        ObservationNonÉcrite,
        record_extraction_observation,
    )

    def consigner_extraction(
        payload: str,
        *,
        ok: bool,
        claim_token: str | None = None,
        representation_id: str | None = None,
    ) -> None:
        """Écrit l'observation d'extraction ; un échec d'écriture est état + remontée.

        L'ordre imposé est observation → état. Si l'observation ne peut pas être
        écrite, l'état est tout de même consigné (`echec`, ou le texte antérieur
        restauré) AVANT de laisser remonter l'erreur : un fichier ne doit pas
        rester `en_cours` pour une panne d'écriture qui, elle, est connue.

        FENCING (Phase 3, tâche 3.3) : quand `claim_token` est fourni, l'écriture
        n'est valide que si le bail est encore détenu. `record_extraction_observation`
        lève `BailPerdu` si le claim a été volé ; le worker doit alors s'arrêter.
        """
        try:
            record_extraction_observation(
                artifact_id,
                "attachment_ingest",
                None,
                payload,
                ok=ok,
                tool_version=VERSION_EXTRACTEUR,
                claim_token=claim_token,
                representation_id=representation_id,
            )
        except ObservationNonÉcrite as exc:
            # L'état est consigné AVANT de laisser remonter l'erreur, et l'issue
            # est émise sur CET état : si l'écriture a échoué aussi, `_marquer`
            # rend `""` et `_emettre_issue` s'abstient. Aucun terminal ne peut
            # donc annoncer un état absent de la base.
            motif = (
                f"extraction impossible — observation d'extraction non écrite : {exc}"
            )
            etat_ecrit = _marquer(
                artifact_id,
                ETAT.ETAT_ECHEC,
                motif,
                claim_token=claim_token,
            )
            _emettre_issue(artifact_id, etat_ecrit, motif, kind)
            raise

    # On ne garde AUCUNE session ouverte pendant l'extraction. Un OCR de page
    # demande plusieurs minutes ; garder une connexion pendant ce temps vide le
    # pool (limite 5, 10 en attente) et fait expirer les requêtes des autres
    # fils — y compris celles de l'interface, qui paraîtraient alors figées.
    session = SessionLocal()
    try:
        from backend.db.models import Artifact as _Art

        artefact = session.get(_Art, artifact_id)
        kind = (artefact.kind or "").lower() if artefact is not None else ""
    finally:
        # La session est fermée ICI et la refait pas dans le `finally` plus bas :
        # c'est la seule fenêtre pendant laquelle elle sert, et une fermeture
        # supplémentaire ne ferait que-voiler quel code la possède.
        try:
            session.close()
        except Exception:
            pass

    # Pré-contrôle de `_en_cours` : si un worker local travaille déjà, on sort
    # sans même tenter le claim. C'est une optimisation, pas la garantie : le
    # claim en base reste l'arbitre. On n'écrit PAS `_en_cours` ici.
    with _verrou:
        if artifact_id in _en_cours:
            log.info(
                "[ingestion] %s déjà en cours : lancement direct ignoré",
                artifact_id,
            )
            return
        if _proprietaire is None:
            _proprietaire = uuid.uuid4().hex

    # 1. ACQUÉRIR LE CLAIM AVANT LE COORDINATEUR GPU (Phase 3, contrainte 1).
    # La session est courte et commitée : aucune transaction SQLite ne reste
    # ouverte pendant l'attente de 120 s sur le coordinateur.
    _session_bail = SessionLocal()
    try:
        _token = _bail.acquérir_bail(_session_bail, artifact_id, _proprietaire)
    except Exception as exc:
        log.error(
            "[ingestion] acquisition du claim impossible pour %s : %s",
            artifact_id,
            exc,
        )
        _token = None
    finally:
        _session_bail.close()

    if _token is None:
        # SINGLE-FLIGHT (Phase 3, contrainte 3) : un autre worker détient le
        # claim actif. On ne reste pas en file indéfiniment : on sort, et le
        # balayage de reprise repassera quand le claim aura expiré ou été libéré.
        # `_en_cours` n'est PAS peuplé : aucun worker local ne travaille.
        log.info(
            "[ingestion] %s déjà en cours (claim actif) : lancement direct ignoré",
            artifact_id,
        )
        return

    # 2. Exécution fencée : toutes les écritures portent le token.
    tentatives = 0
    collecte = CollecteExtraction()
    try:
        # Réservation locale APRÈS le claim : `_en_cours` reflète un travail
        # réellement autorisé, jamais un simple enfilage ou une tentative refusée.
        with _verrou:
            _en_cours[artifact_id] = _proprietaire
        try:
            chemin = _chemin_artifact(artifact_id)
            if not chemin:
                raise RuntimeError("blob introuvable ou hors périmètre")
            with _serie:
                tentatives = _tenter(artifact_id, _token)
                motif_demarrage = f"extraction demandée (type {kind})"
                etat_en_cours = _marquer(
                    artifact_id,
                    ETAT.ETAT_EN_COURS,
                    motif_demarrage,
                    _token,
                )
                _battre(artifact_id, _token)
                # L'événement de démarrage suit l'écriture de `en_cours`, jamais
                # l'enfilage : un artefact en file n'a pas de propriétaire, et
                # annoncer son extraction donnerait un travail qui n'existe pas.
                # Il ne suffit pas d'avoir DEMANDÉ `en_cours` : il faut que la
                # base le porte, sinon l'écran afficherait un démarrage fantôme.
                if etat_en_cours == ETAT.ETAT_EN_COURS:
                    _emit_ingestion_event(
                        "artifact_ingestion_started",
                        artifact_id,
                        ETAT.ETAT_EN_COURS,
                        motif_demarrage,
                        kind,
                    )
                else:
                    log.warning(
                        "[ingestion] démarrage non annoncé pour %s : état "
                        "`en_cours` non écrit (état rendu : %r)",
                        artifact_id,
                        etat_en_cours,
                    )
                if modeles_autorises:
                    texte, mode = extraire_texte(
                        artifact_id,
                        kind,
                        chemin,
                        heartbeat=lambda: _battre(artifact_id, _token),
                        claim_token=_token,
                        collecte=collecte,
                    )
                else:
                    texte, mode = _extraire_sans_modele(kind, chemin, collecte=collecte)
        except ModeleRequis as exc:
            # Refus CONSENTI, pas une panne : l'état revient à `depose`, qui
            # signifie « extraction jamais aboutie ». On ne peut pas écrire
            # « jamais demandée » sans mentir — elle l'a été — mais `depose` est le
            # seul état non terminal qui n'affirme AUCUN résultat, et le motif
            # consigné dit pourquoi la dette reste ouverte. Un `echec`, lui,
            # dirait « quelque chose est cassé » alors que rien ne l'est.
            #
            # AUCUN TRAVAIL RÉEL N'A EU LIEU : le compteur posé avant le travail
            # est défait. Sinon cinq balayages de démarrage sans modèle
            # transformeraient un PDF scanné JAMAIS TENTÉ en `echec` terminal.
            tentatives = _annuler_tentative(artifact_id, _token)
            etat_ecrit, motif = _basculer_reprise(
                artifact_id,
                ETAT.ETAT_DEPOSE,
                f"extraction en attente d'un modèle : {exc}",
                tentatives,
                claim_token=_token,
            )
            _emettre_issue(artifact_id, etat_ecrit, motif, kind)
            log.info(
                "[ingestion] %s (%s) : %s — laissé pour une reprise autorisée "
                "à charger un modèle",
                artifact_id,
                kind,
                exc,
            )
            return
        except ModeleIndisponible as exc:
            # Le worker OCR n'a pas pu être lancé, chargé ou n'a pas répondu :
            # c'est une indisponibilité de NOTRE côté, jamais une faute du
            # fichier. Un `echec` terminal ferait payer à l'utilisateur une
            # panne de modèle ; `depose` garde la dette rejouable.
            #
            # La tentative est ANNULÉE : une panne d'infrastructure n'est pas
            # une exécution imputable au fichier. Sans cette annulation, N
            # coupures du modèle feraient atteindre le plafond d'essais à un
            # fichier sain, et `_etat_avec_plafond` le rendrait `echec`
            # terminal — exactement ce que le présent commentaire interdit.
            # `attempts` continue donc de ne compter que les exécutions
            # réelles, imputables au contenu. L'échéance est replanifiée avec
            # le nombre restant, et `ETAT.delai_backoff` borne l'index : la
            # dette reste visible et rejouable, sans date NULL.
            tentatives = _annuler_tentative(artifact_id, _token)
            etat_ecrit, motif = _basculer_reprise(
                artifact_id,
                ETAT.ETAT_DEPOSE,
                f"extraction en attente : modèle OCR indisponible — {exc}",
                tentatives,
                claim_token=_token,
            )
            _emettre_issue(artifact_id, etat_ecrit, motif, kind)
            log.info(
                "[ingestion] %s (%s) : modèle OCR indisponible, extraction "
                "différée — %s",
                artifact_id,
                kind,
                exc,
            )
            return
        except _sheets.FeuilletVide as exc:
            # Un tableur LISIBLE et SANS ligne de données n'est pas une panne :
            # au moins un lecteur a réussi à le lire — c'est le fichier qui
            # n'a rien à indexer (texte.md §2.3 : « Un fichier vide peut être
            # une ressource valide sans passage indexable ; ce n'est pas
            # nécessairement une panne »). écrire `echec` ici accuserait une
            # panne qui n'a pas lieu : c'est exactement la distinction que
            # ETATS_SANS_TEXTE demande d'entretenir entre `vide` et `echec`.
            message = (
                f"Le fichier est lisible mais vide : lecture réussie, aucune "
                "ligne de données trouvée. Le fichier reste consultable mais "
                "ne sera pas interrogeable par recherche."
            )
            # L'observation d'audit est écrite AVANT l'état, et un refus
            # (`ok=False`) n'ouvre ni fiche, ni fragment, ni vecteur, ni FTS.
            consigner_extraction(message, ok=False, claim_token=_token)
            etat_ecrit = _marquer(
                artifact_id,
                ETAT.ETAT_VIDE,
                f"extraction menée à terme — {exc} : le fichier reste "
                "consultable mais ne sera pas interrogeable par recherche",
                _token,
            )
            _reinitialiser_essais(artifact_id, _token)
            _emettre_issue(artifact_id, etat_ecrit, message, kind)
            return
        except ResourceBusy as exc:
            # L'ingestion n'a pas pu démarrer parce qu'un autre bail tient la
            # ressource : ce n'est pas une panne du fichier, et un `echec` serait
            # terminal. On laisse la dette ouverte, rejouable, avec le motif.
            # Refus d'ADMISSION, sans travail réel : la tentative ne compte pas.
            tentatives = _annuler_tentative(artifact_id, _token)
            etat_ecrit, motif = _basculer_reprise(
                artifact_id,
                ETAT.ETAT_DEPOSE,
                f"extraction en attente : ressource occupée — {exc}",
                tentatives,
                claim_token=_token,
            )
            _emettre_issue(artifact_id, etat_ecrit, motif, kind)
            log.info(
                "[ingestion] %s (%s) : ressource occupée, extraction différée — %s",
                artifact_id,
                kind,
                exc,
            )
            return
        except BailPerdu as exc:
            # Perte de bail : le worker s'arrête IMMÉDIATEMENT.
            # Aucune écriture (observation, état, index, purge) n'est autorisée
            # après la perte du bail. Le nouveau propriétaire est seul responsable.
            # Aucune transaction SQLite n'est ouverte ici : la session est courte.
            log.warning(
                "[ingestion] bail perdu pour %s : %s — arrêt sans écriture",
                artifact_id,
                exc,
            )
            return
        except ObservationNonÉcrite:
            # `consigner_extraction` a déjà écrit l'état et émis l'issue sur cet
            # état. On laisse remonter l'erreur sans repasser par le terminal
            # générique : un second événement raconterait deux fois la même panne.
            raise
        except Exception as exc:
            motif = f"{type(exc).__name__}: {exc}"
            # L'échec d'EXTRACTION doit être tracé, pas avalé : un fichier déposé
            # sans texte et sans explication est exactement le défaut que ce
            # module supprime. L'observation est écrite AVANT l'état ; `marquer`
            # restaurera `extrait`/`tronque`/`partiel` si un texte antérieur
            # existait, au lieu de le détruire par un `echec`.
            consigner_extraction(
                f"Extraction impossible : {motif}", ok=False, claim_token=_token
            )
            etat_ecrit = _marquer(
                artifact_id,
                ETAT.ETAT_ECHEC,
                f"extraction impossible — {motif}",
                _token,
            )
            _emettre_issue(
                artifact_id, etat_ecrit, f"extraction impossible — {motif}", kind
            )
            return
        if not texte.strip():
            # Extraction menée à terme, RIEN lu. C'est un état distinct d'un
            # échec, et le dire exactement évite que l'utilisateur cherche un
            # bug de dépôt là où il y a une image sans texte — ou un PDF dont
            # l'OCR n'a rien su transcrire.
            message = (
                f"Aucun texte reconnu (mode {mode}) : le fichier reste "
                "consultable mais ne sera pas interrogeable par recherche."
            )
            consigner_extraction(message, ok=False, claim_token=_token)
            etat_ecrit = _marquer(
                artifact_id,
                ETAT.ETAT_VIDE,
                f"aucun texte reconnu en mode {mode} : le fichier reste "
                "consultable mais ne sera pas interrogeable par recherche",
                _token,
            )
            _reinitialiser_essais(artifact_id, _token)
            _emettre_issue(artifact_id, etat_ecrit, message, kind)
            return
        # L'état suit le VERDICT de l'indexation, pas la seule présence de texte : un
        # ZIP passé au décodeur « produit » du texte, et c'est exactement le cas que
        # la longueur seule ne rattrape pas. `verdict_vers_etat` est la seule
        # traduction du verdict vers le relevé, donc il ne peut pas y en avoir deux.
        from backend.ingestion import markdown_pdf as _MD
        from backend.search import fts as _fts

        verdict = _fts.arbitrer_extrait(texte[:CARACTERES_MAX_INDEXES])
        raison = verdict.get("reason", "")
        # Une page en panne est NOMMÉE dans la RAISON et dans le rendu Markdown
        # (pdf.md §5.1 : « Échec d'une page → Résultat partiel, autres pages
        # conservées »). Les numéros sont RELITS au vocabulaire de
        # `markdown_pdf`, jamais déduits d'un indice libre.
        pages_en_panne = _MD.pages_non_reconnues(texte)
        pages_traitees = _MD.pages_traitees(texte)
        pages_vides = _MD.pages_vides(texte)
        # La troncature de page est un FAIT relu dans la couverture, jamais
        # déduit du nombre de pages du fichier : une extraction native peut
        # s'arrêter à 20 pages sur 68, et l'état doit le dire (`tronque`).
        motif_troncature = _motif_troncature(texte)
        # Un document sans aucune page n'a rien à indexer : sans cette borne,
        # sa couverture (`pages_traitees=0`) passait le chemin de succès et le
        # fichier devenait `extrait` avec zéro contenu.
        document_sans_page = pages_traitees == 0
        # Toutes les pages vides : aucune panne n'est nommée, donc l'ancienne
        # condition ne les voyait pas. Le contenu vide ne s'indexe pas comme
        # un succès — c'est une dette rejouable, comme des pages en panne.
        toutes_pages_vides = (
            pages_traitees is not None
            and pages_traitees > 0
            and pages_vides is not None
            and pages_vides >= pages_traitees
        )
        toutes_pages_en_panne = (
            bool(pages_en_panne)
            and pages_traitees is not None
            and pages_traitees > 0
            and len(pages_en_panne) >= pages_traitees
        )
        # Couverture ÉQUIVALENTEMENT vide : chaque page est soit blanche, soit
        # en panne. Une page portant une table ou une figure n'est ni l'un ni
        # l'autre — elle reste donc un contenu exploitable.
        pages_sans_texte = (pages_vides or 0) + len(pages_en_panne)
        couverture_sans_texte = (
            pages_traitees is not None
            and pages_traitees > 0
            and pages_vides is not None
            and pages_sans_texte >= pages_traitees
        )
        aucune_page_exploitable = (
            document_sans_page or toutes_pages_en_panne or couverture_sans_texte
        )
        if aucune_page_exploitable:
            # Il n'y a pas de texte exploitable à indexer. C'est une dette
            # rejouable, pas un document vide ni un échec terminal.
            if document_sans_page:
                motif = "aucune page exploitable — le document ne contient aucune page"
            elif toutes_pages_en_panne:
                detail = ", ".join(f"page {n}" for n in pages_en_panne)
                motif = f"aucune page exploitable — pages non reconnues : {detail}"
            elif toutes_pages_vides:
                motif = "aucune page exploitable — toutes les pages sont vides"
            else:
                motif = "aucune page exploitable — pages vides ou non reconnues"
            etat_ecrit, motif_ecrit = _basculer_reprise(
                artifact_id,
                ETAT.ETAT_DEPOSE,
                motif,
                tentatives,
                claim_token=_token,
            )
            _emettre_issue(artifact_id, etat_ecrit, motif_ecrit, kind)
            log.info(
                "[ingestion] %s (%s) : aucune page exploitable (%s), reprise différée",
                artifact_id,
                kind,
                motif,
            )
            return

        if verdict.get("extract") == _fts.EXTRACT_BINAIRE:
            # Un refus binaire ne laisse AUCUNE trace d'index : on purge d'abord
            # ce qu'une extraction antérieure aurait pu écrire, puis on consigne
            # l'audit en `ok=False`. L'ordre observation → état est celui imposé.
            _purger_index_binaire(artifact_id, _token)
            consigner_extraction(
                texte[:CARACTERES_MAX_INDEXES], ok=False, claim_token=_token
            )
            etat_ecrit = _marquer(
                artifact_id, ETAT.verdict_vers_etat(verdict), raison, _token
            )
            _reinitialiser_essais(artifact_id, _token)
            _emettre_issue(artifact_id, etat_ecrit, raison, kind)
            return

        if pages_en_panne:
            # Des pages ont échoué mais d'autres ont livré du texte : `partiel`
            # dit exactement cela. Le texte partiel RESTE servi (il n'est ni
            # terminal, ni sans texte) ; l'état est rejouable pour compléter.
            partiel = "traitement partiel — " + ", ".join(
                f"page {numero} non reconnue" for numero in pages_en_panne
            )
            # Une extraction partielle A DU TEXTE : sa représentation complète
            # est la nouvelle source, même si l'état reste rejouable.
            representation = _persister_extraction_complete(
                artifact_id, texte, collecte, claim_token=_token
            )
            consigner_extraction(
                texte[:CARACTERES_MAX_INDEXES],
                ok=True,
                claim_token=_token,
                representation_id=getattr(representation, "id", None),
            )
            if motif_troncature:
                # La borne de pages a coupé : même avec des pages en panne,
                # l'état terminal le plus précis est `tronque` (texte servi,
                # document incomplet pour une raison de conception). Le motif
                # nomme les deux causes.
                motif_partiel = f"{partiel} ; {motif_troncature}" + (
                    f" ; {raison}" if raison else ""
                )
                etat_ecrit = _marquer(
                    artifact_id, ETAT.ETAT_TRONQUE, motif_partiel, _token
                )
                _reinitialiser_essais(artifact_id, _token)
                _emettre_issue(artifact_id, etat_ecrit, motif_partiel, kind)
                return
            motif_partiel = partiel + (f" ; {raison}" if raison else "")
            etat_ecrit, motif_ecrit = _basculer_reprise(
                artifact_id,
                ETAT.ETAT_PARTIEL,
                motif_partiel,
                tentatives,
                claim_token=_token,
            )
            _emettre_issue(artifact_id, etat_ecrit, motif_ecrit, kind)
            return

        # Extraction RÉUSSIE. L'observation n'est plus « au mieux » : c'est elle
        # qui porte le texte, donc la fiche, les fragments et l'index. Son échec
        # remonte à `lancer_ingestion`, qui le journalise — un texte extrait puis
        # perdu en silence serait le défaut qu'on combat, à un cran plus haut.
        #
        # La représentation complète est écrite AVANT l'observation bornée : le
        # texte intégral n'est plus perdu au-delà de 16 k, et l'observation
        # garde son rôle d'audit borné inchangé.
        representation = _persister_extraction_complete(
            artifact_id, texte, collecte, claim_token=_token
        )
        consigner_extraction(
            texte[:CARACTERES_MAX_INDEXES],
            ok=True,
            claim_token=_token,
            representation_id=getattr(representation, "id", None),
        )
        if motif_troncature:
            # Un verdict `indexe` sur un document coupé à 20 pages reste une
            # extraction partielle : l'état doit dire `tronque`, pas `extrait`.
            motif_tronque = motif_troncature + (f" ; {raison}" if raison else "")
            etat_ecrit = _marquer(artifact_id, ETAT.ETAT_TRONQUE, motif_tronque, _token)
            _reinitialiser_essais(artifact_id, _token)
            _emettre_issue(artifact_id, etat_ecrit, motif_tronque, kind)
            return
        etat_ecrit = _marquer(
            artifact_id, ETAT.verdict_vers_etat(verdict), raison, _token
        )
        _reinitialiser_essais(artifact_id, _token)
        _emettre_issue(artifact_id, etat_ecrit, raison, kind)
    except BailPerdu as exc:
        # Le bail peut être perdu À LA PERSISTANCE, après l'extraction et hors
        # du `try` intérieur : `_persister_extraction_complete` lève alors
        # `BailPerdu` sur son UPDATE conditionnel. On s'arrête sans aucune
        # écriture terminale — même contrat que la perte pendant l'extraction —
        # au lieu de laisser l'exception tuer le worker.
        log.warning(
            "[ingestion] bail perdu à la persistance pour %s : %s — "
            "arrêt sans écriture",
            artifact_id,
            exc,
        )
        return
    finally:
        # Seul le PROPRIÉTAIRE retire son entrée. Un fil zombie remplacé par
        # un second ne doit pas libérer la réservation du second en terminant.
        if _proprietaire is not None:
            with _verrou:
                if _en_cours.get(artifact_id) == _proprietaire:
                    _en_cours.pop(artifact_id, None)

        # 3. LIBÉRER LE CLAIM (Phase 3, contrainte 2).
        # La libération est CONDITIONNELLE au token : un worker expiré ne peut
        # jamais libérer le bail d'un nouveau propriétaire. Le token est vérifié
        # dans la requête UPDATE (WHERE claim_token = :token).
        if _token is not None:
            _session_bail = SessionLocal()
            try:
                _bail.libérer_bail(_session_bail, artifact_id, _token)
            except Exception as exc:
                log.warning(
                    "[ingestion] échec de libération du claim pour %s : %s",
                    artifact_id,
                    exc,
                )
            finally:
                _session_bail.close()

        # Les figures `pending` viennent d'être commitées (ou le seront par une
        # reprise). Le réveil est posé APRÈS la libération de `_en_cours` : le
        # worker figures ne se croit pas bloqué par l'ingestion texte qu'il
        # vient de terminer. Un `Event.set` ne bloque pas la réponse ; un réveil
        # manqué est rattrapé par le balayage périodique du worker.
        _reveiller_figures_apres_etape_a(collecte)


def liberer_en_cours_expires(artifact_ids: list[str], *, seuil_s: int) -> list[str]:
    """Retire de `_en_cours` les artefacts dont le battement est périmé. Rend les libérés.

    `_en_cours` protège d'un double lancement DANS le processus. Un fil mort
    sans `finally` (ou une autre instance) laisserait une entrée éternelle ;
    cette fonction relit la preuve de vie en base et rend la place. Elle ne
    touche pas à l'état : la reprise reste au balayage.

    Un battement RÉCENT protège l'entrée, même si `ingest_updated_at` est
    ancien : c'est le battement qui prouve la vie, pas le début du travail.
    Un âge INCALCULABLE (`None`) ne libère pas non plus : l'ignorance n'est
    pas une preuve de mort.
    """
    from backend.db.database import SessionLocal as _SL
    from backend.db.models import Artifact as _Art

    cibles = [str(a) for a in artifact_ids if a]
    if not cibles:
        return []
    session = _SL()
    try:
        lignes = (
            session.query(
                _Art.id,
                _Art.ingest_heartbeat_at,
                _Art.ingest_updated_at,
            )
            .filter(
                _Art.id.in_(cibles),
                _Art.ingest_state == ETAT.ETAT_EN_COURS,
            )
            .all()
        )
    except Exception as exc:
        # Une lecture impossible ne doit pas se confondre avec « rien à
        # libérer » : le journal nomme la panne, et aucune entrée n'est retirée.
        log.error(
            "[ingestion] libération des en-cours impossible : %s: %s",
            type(exc).__name__,
            exc,
        )
        return []
    finally:
        try:
            session.close()
        except Exception:
            pass
    liberes: list[str] = []
    for artifact_id, battement, maj in lignes:
        age = ETAT.age_secondes(battement or maj)
        if age is None or age < seuil_s:
            continue
        with _verrou:
            _en_cours.pop(str(artifact_id), None)
        liberes.append(str(artifact_id))
    return liberes


def _worker_ingestion() -> None:
    """Consommateur unique de la file d'admission (Phase 3, tâche 3.1).

    UN SEUL worker : il sérialise les phases modèle (OCR/vision) comme l'exige
    le plan. Les phases natives sont aussi sérialisées, faute de pouvoir scinder
    `_ingérer_un` sans en dupliquer la logique de routage — choix documenté dans
    l'en-tête de `_file_admission`.

    `_en_cours` n'est PAS écrit ici : c'est `_ingérer_un` qui le pose quand il
    démarre réellement le travail. Le worker gère seulement l'ensemble d'ENFILAGE
    `_en_file`, qui couvre la file ET l'exécution.
    """
    while not _arret_admission.is_set():
        try:
            artifact_id, modeles_autorises = _file_admission.get(
                timeout=_ATTENTE_FILE_S
            )
        except queue.Empty:
            continue
        try:
            _ingérer_un(artifact_id, modeles_autorises=modeles_autorises)
        except Exception:
            # Un worker qui meurt sans rien écrire laisserait le fichier dans
            # l'état que le balayage de reprise rattrape. `log.exception` porte
            # la pile ET le nom de l'artefact.
            log.exception("[ingestion] worker interrompu pour %s", artifact_id)
        finally:
            _file_admission.task_done()
            with _verrou:
                _en_file.discard(artifact_id)


def _demarrer_worker_si_necessaire() -> None:
    """Démarre le worker unique s'il ne tourne pas déjà. Idempotent."""
    global _worker_demarre
    with _verrou:
        if _worker_demarre:
            return
        _worker_demarre = True
        threading.Thread(
            target=_worker_ingestion, name="ingestion-worker", daemon=True
        ).start()


def etat_admission() -> dict[str, int]:
    """Compteurs d'admission pour l'observabilité (Phase 6).

    `en_file` ne compte que les artefacts EN ATTENTE dans la file (`qsize`),
    pas ceux en cours d'exécution. `admis` porte l'ensemble d'enfilage — file +
    exécution — et `en_cours` ne compte que les workers ayant réellement
    démarré. `en_file` et `en_cours` sont distincts : c'est la garantie
    qu'aucun état « en cours » n'est écrit à l'enfilage.
    """
    with _verrou:
        return {
            "en_file": _file_admission.qsize(),
            "en_cours": len(_en_cours),
            "admis": len(_en_file),
        }


def arreter_ingestion() -> None:
    """Arrêt propre de l'admission. Le worker termine sa tâche courante.

    Les artefacts encore EN FILE sont ABANDONNÉS : ils restent en base dans un
    état rejouable (`depose`/`en_cours`) et le balayage de reprise les reprendra.
    Ce choix évite d'attendre un OCR de plusieurs minutes à l'arrêt de
    l'application ; il est documenté plutôt que silencieux.
    """
    _arret_admission.set()


def execution_en_cours(artifact_id: str) -> bool:
    """Un travail couvre-t-il déjà cet artefact ? Lecture seule, jamais bloquante.

    Trois preuves, dans l'ordre du moins cher : l'ensemble d'enfilage local
    (`_en_file`, file + exécution), les workers locaux réellement démarrés
    (`_en_cours`), puis un claim FRAIS en base — un autre processus peut travailler
    sans que ce processus le sache. Un claim expiré ne compte pas : il est
    ré-acquérable.

    Une lecture de base impossible répond `True` : refuser de relancer est
    réversible, doubler un travail GPU ne l'est pas. Le journal distingue ce
    cas (« état de travail illisible ») d'un claim réellement frais ; le libellé
    de la réponse utilisateur, lui, appartient à l'appelant IPC et reste
    volontairement inchangé ici tant que ce fichier est hors périmètre.
    """
    with _verrou:
        if artifact_id in _en_file or artifact_id in _en_cours:
            return True
    try:
        from backend.db.database import SessionLocal
        from backend.ingestion import bail as _bail

        session = SessionLocal()
        try:
            return _bail.bail_actif(session, artifact_id)
        finally:
            try:
                session.close()
            except Exception:
                pass
    except Exception as exc:
        log.warning(
            "[ingestion] état de travail de %s illisible : %s: %s — relance refusée",
            artifact_id,
            type(exc).__name__,
            exc,
        )
        return True


def lancer_ingestion(
    artifact_ids: list[str], *, modeles_autorises: bool = True
) -> list[str]:
    """Admet des artefacts dans la file bornée d'ingestion. Ne lève jamais.

    Rend les artefacts RÉELLEMENT enfilés : un appelant qui a vérifié le
    single-flight juste avant peut ainsi distinguer « relancé » de « doublon
    ignoré » au lieu de l'affirmer à tort.

    Appelé après la promotion : le dépôt a déjà répondu à l'utilisateur.

    `modeles_autorises=False` fait REFUSER l'OCR et la vision, fichier par fichier et
    au point exact où ils seraient nécessaires. C'est le mode du balayage de reprise :
    charger un modèle au démarrage de l'application transformerait un rattrapage en
    attente de plusieurs minutes, et le ferait échouer sur une machine sans modèle.
    La dette, elle, reste ÉCRITE — donc visible, journalisée, et reprenable plus tard.

    SINGLE-FLIGHT (Phase 3, 3.1) : un artefact déjà en file ou en cours n'est pas
    ré-enfilé. `_en_file` est distinct de `_en_cours` : il couvre l'attente, pas
    seulement l'exécution. `_en_cours` n'est écrit qu'au démarrage du worker.
    """
    cibles = [a for a in artifact_ids if a]
    if not cibles:
        return []
    a_enfiler: list[str] = []
    with _verrou:
        for artifact_id in cibles:
            if artifact_id in _en_file or artifact_id in _en_cours:
                continue
            _en_file.add(artifact_id)
            a_enfiler.append(artifact_id)
    if not a_enfiler:
        return []
    for artifact_id in a_enfiler:
        _file_admission.put((artifact_id, modeles_autorises))
    _demarrer_worker_si_necessaire()
    return a_enfiler


_mesurer_ingestion_originale = _ingérer_un_impl


def _ingérer_un(
    artifact_id: str,
    *,
    modeles_autorises: bool = True,
    _proprietaire: str | None = None,
) -> None:
    with mesurer_phase(f"ingestion:{artifact_id}"):
        return _mesurer_ingestion_originale(
            artifact_id,
            modeles_autorises=modeles_autorises,
            _proprietaire=_proprietaire,
        )

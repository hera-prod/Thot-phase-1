"""Ingestion des sources web : le résultat conservé devient une ressource documentaire.

LE MANQUE QUE CE MODULE COMBLE : `web_research` persistait ses pages dans le
cache de conversation (`WebSnapshot`), mais aucun artefact n'était créé —
`grep artifact backend/services/web/` rendait zéro. Un résultat web cité au
tour N était donc introuvable au tour N+1 par `find_files` et `search_info`,
qui ne lisent que le périmètre documentaire (`scope.resolve_allowed_artifacts`).

LA FICHE QUI RÉPOND : `docs/intentions/mecanismes/ingestion/sources_web.md` et
`docs/intentions/outils/web_research.md`. Ce qu'elles fixent, et que ce module
applique littéralement :

* **QUAND le résultat devient ressource** — à la conservation, à l'exécution de
  l'outil, pas à la citation ni à la diffusion : web_research.md §3.1, étapes 6-7 —
  « 6. Enregistrer les ressources documentaires produites dans la discussion.
  7. Réaliser ou planifier leur ingestion. » ; et sources_web.md §4.1 — « Le
  résultat utile devient une ressource documentaire de la discussion ayant
  lancé la recherche. »
* **DE QUELLE PORTÉE** — conversation : web_research.md §3.3 — « Une ressource
  web conservée appartient à sa discussion. » ; elle alimente le corpus de
  l'espace de rattachement, « sans transfert automatique vers un autre espace
  ni promotion au dépôt ».
* **CE QUI EST INGÉRÉ** — le contenu retenu, en texte de travail, pas le corps
  réseau brut : sources_web.md §3.5 — « Distinguer le contenu effectivement
  reçu de sa représentation normalisée » ; et §3.6 — « Un Markdown lisible peut
  être associé à des métadonnées internes structurées. » L'archivage du corps
  reçu n'est PAS imposé (§3.5 : « Cette fiche n'impose pas l'archivage
  systématique de toutes les réponses réseau ») : ce module n'archive pas le
  corps HTTP, il conserve le texte extrait et sa provenance.
* **L'INGESTION PASSE PAR LE CHEMIN COMMUN** — sources_web.md §4.3 : « Les
  passages utiles sont préparés selon le pipeline Texte et encodés avec le
  profil ColBERT validé. La fiche documentaire aide `find_files`. » Ici, la
  seule chose écrite est Blob + Artifact (kind `text`) + lien de conversation
  + `doc_N` ; tout le reste — observation, fiche, fragments, index — vient de
  `attachment_ingest`, exactement le chemin d'un `.txt` déposé. Aucun pipeline
  parallèle.
  DEUX CHEMINS D'ADMISSION, UN CHOIX ASSUMÉ : quand `synchronise=True`, la
  recherche web doit rendre un artefact DÉJÀ ingéré au retour (le tour attend
  son corpus) ; `_ingérer_un` est alors appelé directement, hors file, dans le
  fil appelant. Ce n'est pas une fuite de l'admission : c'est le même chemin
  fencé (claim, heartbeat, états) exécuté de façon synchrone. Quand
  `synchronise=False`, l'ingestion passe par `lancer_ingestion`, donc par la
  file bornée et le worker unique. La branche synchrone est documentée ici
  parce qu'elle contourne VOLONTAIREMENT la file : le blocage est le contrat
  de `web_research`, pas un oubli de routage.
* **LA DÉDUPLICATION** — sources_web.md §4.5 : « Si un contenu identique est
  obtenu de nouveau, une nouvelle consultation peut être enregistrée sans
  dupliquer inutilement le corps conservé. » Le texte de travail est donc bâti
  sur ce qui identifie la VERSION (titre, URL, provenance, publication,
  contenu) et PAS sur la date de consultation — qui change à chaque
  revalidation. Même version dans la même conversation : la ressource déjà
  là est retournée, rien n'est réécrit ni réingéré. La date de consultation,
  elle, reste dans le cache (`WebSnapshot.fetched_at`), seul endroit qui peut
  la dater à chaque vérification réelle (§2.4).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid

log = logging.getLogger(__name__)

_TITRE_MAX = 120
# Ce qu'un nom affiché ne doit pas porter : même règle que le dépôt
# (`attachments._nom_depotable`) — un séparateur de chemin ferait passer le
# titre d'une page pour une adresse. Réécrit ici pour ne pas dépendre d'un
# privé d'un autre module ; une seule règle, appliquée deux fois, resterait
# une occasion de diverger.
_TITRE_IMPUBLABLE = re.compile(r"[/\\\n\r\0]")

# Le niveau de lecture se rend dans le texte de travail : sources_web.md §2.2
# refuse qu'un résumé se présente comme une page lue. Deux niveaux sont
# reconnaissables à la source elle-même : un statut HTTP 0 ou un extracteur
# `engine-resume-1` disent « résumé fourni par le moteur — page non lue ».
_RESUME_MOTEUR = "engine-resume-1"
NIVEAU_RESUME = "résumé fourni par le moteur — page non lue"
NIVEAU_LU = "contenu lu par extraction (peut être partiel)"


def _titre_depotable(page) -> str:
    """Le titre de la page, réduit à un nom de fichier sûr — jamais vide.

    Un titre peut contenir tout ce qu'un HTML autorise : séparateurs, contrôle,
    mille caractères. On ne le laisse pas nommer un affichage tel quel, et sans
    titre, l'URL tient lieu de nom — un nom absent ferait une fiche muette.
    """
    brut = " ".join((getattr(page, "title", "") or "").split())
    brut = _TITRE_IMPUBLABLE.sub(" ", brut)[:_TITRE_MAX].strip()
    if brut:
        return brut
    url = getattr(page, "final_url", "") or getattr(page, "url", "") or ""
    try:
        from urllib.parse import urlsplit

        hote = (urlsplit(url).hostname or "").lower()
        chemin = [part for part in (urlsplit(url).path or "").split("/") if part]
        dernier = chemin[-1] if chemin else ""
        brut = " ".join((dernier or hote or "page web").split())
        brut = _TITRE_IMPUBLABLE.sub(" ", brut)[:_TITRE_MAX].strip()
    except ValueError:
        brut = ""
    return brut or "page web"


def texte_de_travail(page, source_type: str) -> str:
    """La représentation normalisée du contenu retenu, avec sa provenance.

    L'ordre est celui de la forme logique de la fiche (sources_web.md §3.6) :
    origine et service, URL, dates typées, niveau de lecture, contenu. La date
    de consultation n'y figure PAS : elle change à chaque revalidation, et la
    faire entrer ferait de chaque relecture une nouvelle version — exactement
    la duplication que §4.5 interdit. Elle reste dans le cache, datée à chaque
    vérification réelle.
    """
    niveau = (
        NIVEAU_RESUME
        if (
            int(getattr(page, "status", 0) or 0) == 0
            or getattr(page, "extractor_version", "") == _RESUME_MOTEUR
        )
        else NIVEAU_LU
    )
    titre = _titre_depotable(page)
    url = getattr(page, "final_url", "") or getattr(page, "url", "") or ""
    service = getattr(page, "proposed_by", "") or "source directe"
    publication = getattr(page, "published_at", "") or "inconnue"
    lignes = [
        f"# {titre}",
        "",
        f"Source : {url}",
        f"Type de source : {source_type or getattr(page, 'source_type', '') or 'general'}",
        f"Service ayant fourni le contenu : {service}",
        f"Date de publication déclarée : {publication}",
        f"Niveau de lecture : {niveau}",
        "",
        "## Contenu retenu",
        "",
        (getattr(page, "text", "") or "").strip(),
        "",
    ]
    return "\n".join(lignes)


def enregistrer_ressource_web(
    conv_id: str,
    page,
    public_id: str,
    *,
    source_type: str,
    synchronise: bool = False,
    modeles_autorises: bool = True,
) -> str:
    """Persiste le résultat web comme ressource documentaire de la discussion.

    Retourne l'`artifact_id` créé (ou déjà présent), `""` si rien n'était à
    enregistrer. Ne JAMAIS masquer une panne d'écriture par `""` : la fonction
    lève, et c'est l'appelant — seul à savoir que la recherche a réussi — qui
    décide de ce qu'un échec de conservation fait au rendu (fiche web_research
    §4.1 : « Conservation échouée — ne pas promettre une réutilisation locale
    persistante »).

    Chaîne exacte, dans UNE transaction : Blob (dédupliqué par empreinte,
    comme le dépôt) → Artifact `kind="text"` (état `depose`, écrit ici car
    c'est le moment où la dette naît) → lien de conversation (`role
    web_research`, portée qui décide de la réutilisation) → `doc_N` par le
    registre. Puis ingestion par le chemin commun, hors transaction.
    """
    if not conv_id or not public_id or page is None:
        return ""
    contenu = texte_de_travail(page, source_type)
    if not contenu.split("## Contenu retenu", 1)[-1].strip():
        # sources_web.md §3.2 : « Une fiche de référence peut être enregistrée
        # sans texte intégral » — mais sans AUCUN contenu retenu, il n'y a rien
        # à ingérer et rien à chercher : on ne crée pas une coquille vide.
        return ""

    data = contenu.encode("utf-8")
    empreinte = hashlib.sha256(data).hexdigest()
    nom = f"{_titre_depotable(page)}.md"

    from backend.db.database import SessionLocal
    from backend.db.models import Artifact, ArtifactLink, Blob
    from backend.ingestion import etat as ETAT

    db = SessionLocal()
    try:
        # La déduplication regarde d'abord le CONTENU (une ressource identique
        # peut exister d'une autre conversation — le blob est global) puis
        # affine : la ressource n'est réutilisée que si CETTE conversation a
        # déjà un lien actif vers un artefact vivant de ce contenu. Un contenu
        # identique vu d'une autre conversation reste une ressource distincte
        # (sources_web.md §4.5 : « une déduplication physique ne partage pas
        # les ressources entre discussions »).
        blob_existant = db.query(Blob).filter(Blob.sha256 == empreinte).first()
        if blob_existant is not None:
            deja_la = (
                db.query(Artifact)
                .join(ArtifactLink, ArtifactLink.artifact_id == Artifact.id)
                .filter(
                    Artifact.blob_id == blob_existant.id,
                    Artifact.deleted_at.is_(None),
                    ArtifactLink.scope_kind == "conversation",
                    ArtifactLink.scope_id == conv_id,
                    ArtifactLink.removed_at.is_(None),
                )
                .first()
            )
            if deja_la is not None:
                return deja_la.id

        from backend.ipc import paths_bridge as _pb

        if blob_existant is not None:
            blob_id = blob_existant.id
        else:
            racine = _pb.blobs_root()
            dossier = racine / empreinte[:2]
            dossier.mkdir(parents=True, exist_ok=True)
            cible = dossier / f"{empreinte}.bin"
            if not cible.exists():
                cible.write_bytes(data)
            blob_id = f"blob_{uuid.uuid4().hex[:16]}"
            db.add(
                Blob(
                    id=blob_id,
                    sha256=empreinte,
                    relative_path=f"blobs/{empreinte[:2]}/{empreinte}.bin",
                    size_bytes=len(data),
                    mime_detected="text/markdown",
                    state="ready",
                )
            )
            db.flush()

        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=blob_id,
                kind="text",
                original_filename=nom,
                display_name=nom,
                ingest_state=ETAT.ETAT_DEPOSE,
                ingest_reason="ressource web conservée dans la discussion, ingestion en attente",
                ingest_updated_at=ETAT.utc_naif(),
            )
        )
        db.flush()
        # Le lien de portée, écrit ICI avec son rôle provenant — le lien de
        # conversation est ce que scope.py lit, et son rôle nomme l'origine web
        # sans changer le périmètre. `docrefs.allocate_doc_label` garantit
        # ensuite le lien ET le label public : les deux dans la même
        # transaction que l'artefact.
        db.add(
            ArtifactLink(
                id=f"lnk_{uuid.uuid4().hex[:16]}",
                artifact_id=artifact_id,
                scope_kind="conversation",
                scope_id=conv_id,
                role="web_research",
            )
        )
        # Flush AVANT `allocate_doc_label` : la session tourne sans
        # autoflush, et un lien non flushé n'existe pas encore aux yeux de
        # la requête de `docrefs` — qui en aurait créé un second, avec un
        # rôle différent : deux vérités sur la même provenance.
        db.flush()
        from backend.search import docrefs as _docrefs

        _docrefs.allocate_doc_label(db, conv_id, artifact_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass

    # Ingestion de la ressource dans UNE séquence observable : pas de fil
    # d'arrière-plan. Le dépôt a déjà répondu par l'écriture de l'artefact ;
    # l'ingestion complète ensuite l'état dans le même ordre que le reste du
    # chemin. C'est la décision de la fiche, rendue synchrone pour ne pas laisser
    # une page « en cours » d'ingestion au retour de `web_research`. Si l'ingestion
    # échoue, l'artefact reste présent et son état porte la cause (§3.1).
    if synchronise:
        try:
            from backend.ingestion import attachment_ingest as _ing

            _ing._ingérer_un(artifact_id, modeles_autorises=modeles_autorises)
        except Exception:  # noqa: BLE001 — la ressource existe ; l'état porte la cause
            log.exception(
                "[sources_web] ingestion synchronisée échouée pour %s", artifact_id
            )
    else:
        try:
            from backend.ingestion import attachment_ingest as _ing

            _ing.lancer_ingestion([artifact_id], modeles_autorises=modeles_autorises)
        except Exception:  # noqa: BLE001 — la ressource existe ; l'ingestion reste rejouable
            log.exception("[sources_web] ingestion non déclenchée pour %s", artifact_id)
    return artifact_id

# backend/services/depot_integrite.py — intégrité du dépôt d'un espace : vérifier, réparer
#
# D'OÙ VIENT CE FICHIER : d'une MESURE, pas d'une intuition — trois fichiers du dépôt
# sur quatre étaient invisibles à la recherche textuelle, et 186 liens actifs sur 227
# n'avaient aucune ligne dans `artifact_search`. Cause lue dans le code :
# `cards.ensure_card_on_import` indexe une ligne par lien actif AU MOMENT DE L'APPEL
# (cards.py:114-126) ; un dépôt versé APRÈS n'en reçoit jamais, et rien ne réindexe.
# OÙ VA QUOI : `verifier_depot()` (lecture seule) et `reparer_depot()` (idempotente),
# sur un espace ou sur tous (`workspace_id=None`).
# POURQUOI LA PORTÉE EST `scope_kind='workspace'` : le dépôt est ce que l'UTILISATEUR y a
# versé. Un fichier resté dans une discussion de l'espace n'en fait pas partie, et le
# vérifier ici mêlerait deux ensembles — voir `espaces.depot`.
# POURQUOI LA RÉPARATION EST SI BORNE : une donnée absente du disque ne se répare pas.
# Se tromper de chemin, ou dédupliquer deux artefacts, déplacerait ou ferait disparaître
# des données pour flatter un rapport. On signale, et l'utilisateur décide.
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Les états dégradés que ce module sait nommer, dans l'ordre de gravité pratique :
# ce que l'utilisateur ne voit plus d'abord, puis ce qui est incohérent, puis ce qui
# est stocké deux fois.
TYPES_ANOMALIES: tuple[str, ...] = (
    "index_manquant",  # invisible à la recherche textuelle
    "nom_vide",  # invisible à l'inventaire du dépôt et à la recherche
    "extraction_absente",  # traité, et il n'en reste rien d'exploitable
    "extraction_binaire",  # traité, mais le contenu n'est pas du texte
    "extraction_tronquee",  # traité, mais une partie du document reste hors index
    "lien_mort",  # un lien actif ne mène plus à rien
    "blob_manquant",  # un artefact sans sa ligne d'octets
    "fichier_absent",  # une ligne d'octets sans fichier sur le disque
    "blob_orphelin",  # un fichier d'octets sans ligne en base
    "doublon",  # deux artefacts du même dépôt sur le même blob
)

# Réparables par cette maintenance, et RIEN D'AUTRE : ces deux repairs réindexent ou
# renonnent à partir de données DÉJÀ présentes en base. Les trois anomalies
# d'extraction en sont EXCLUES, et c'est le cœur du choix : les réparer demanderait de
# relancer une extraction, donc de charger un modèle et d'écrire du contenu nouveau.
# Une maintenance qui fabrique du texte est une maintenance qui invente.
TYPES_REPARABLES: tuple[str, ...] = ("index_manquant", "nom_vide")

# Pour chaque anomalie non réparable, la raison du refus. Elle accompagne le rapport :
# « pas réparé » sans explication ressemble à un oubli.
RAISONS_DE_REFUS: dict[str, str] = {
    "fichier_absent": (
        "les octets sont absents du disque : retrouver leur chemin supposerait d'inventer "
        "une adresse, et écrire au mauvais endroit déplacerait le contenu"
    ),
    "blob_orphelin": (
        "un fichier sans ligne en base peut être une promotion en cours (écrit avant "
        "commit) : une vérification ne le supprime pas ; un nettoyage explicite doit "
        "d'abord s'assurer qu'aucune écriture concurrente ne le référence"
    ),
    "blob_manquant": (
        "la ligne blob porte l'empreinte et la taille des octets : la recréer sans les "
        "octets fabriquerait une preuve de contenu qui n'existe pas"
    ),
    "lien_mort": (
        "restaurer l'artefact ou retirer le lien sont deux décisions d'utilisateur ; "
        "aucune maintenance ne les prend à sa place"
    ),
    "doublon": (
        "fusionner deux artefacts ferait disparaître une ligne du dépôt et son doc_refs : "
        "c'est un choix, pas une correction"
    ),
    "extraction_absente": (
        "relancer l'extraction coûterait du temps machine et produirait du contenu "
        "nouveau : ce n'est plus une correction, c'est un traitement à la demande"
    ),
    "extraction_binaire": (
        "le contenu n'est pas du texte : aucun extracteur n'en tirera jamais de phrases, "
        "et le réindexer ne ferait que réécrire le même refus"
    ),
    "extraction_tronquee": (
        "le document dépasse ce que Thot retient par conception ; le rendre entier "
        "demanderait un autre extracteur, pas une maintenance"
    ),
}

# Nombre de lectures de `verifier_depot`, quel que soit le nombre de fichiers : le
# contrôle d'un dépôt doit coûter le même pour un fichier et pour mille.
LECTURES_PAR_VERIFICATION = 5

# Taille d'un `IN (...)` de la réparation : sous la borne de variables liées de SQLite
# (999 sur les moteurs anciens), pour qu'un dépôt dégradé ne tombe pas sur la requête.
TAILLE_DE_LOT = 900

# Récupération des `.bin` orphelins (Phase 4) : un fichier plus jeune que ce TTL
# peut être une promotion entre `os.replace` et `commit` ; on ne le touche pas.
# Passé le TTL, aucune transaction saine ne peut encore le revendiquer.
ORPHAN_BLOB_TTL_S = 24 * 60 * 60
MAX_ORPHELINS_PAR_PASSE = 500

# État du verrou de processus (voir `acquerir_verrou_gc_vie`).
_VERROU_GC_FD: int | None = None
_VERROU_GC_NIVEAU = 0


# ---------------------------------------------------------------------------
# Données lues : une fois, au passage de la base à la logique
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Lien:
    """Lien de dépôt ACTIF (`removed_at` vide) : le seul qui porte un fichier."""

    link_id: str
    artifact_id: str
    scope_kind: str
    scope_id: str  # sur un lien de dépôt, l'identifiant de l'espace


@dataclass(frozen=True)
class _Artefact:
    artifact_id: str
    blob_id: str
    kind: str
    display_name: str
    original_filename: str
    est_supprime: bool
    # L'état d'ingestion est LU, jamais deviné. Avant ces colonnes, ce module ne
    # pouvait rien dire d'une extraction : il ne cherchait pas à le dire, faute de
    # donnée. Elles arrivent dans la MÊME requête que le reste de l'artefact —
    # `LECTURES_PAR_VERIFICATION` vaut 5 et doit le rester : vérifier un dépôt doit
    # coûter le même qu'il porte un fichier ou mille, et un état d'ingestion ne vaut
    # pas une sixième lecture.
    ingest_state: str = ""
    ingest_reason: str = ""

    @property
    def est_nomme(self) -> bool:
        return bool(self.display_name.strip() or self.original_filename.strip())


@dataclass(frozen=True)
class _Blob:
    blob_id: str
    sha256: str
    relative_path: str
    size_bytes: int


@dataclass
class _Inventaire:
    """Ce que la base dit du dépôt.

    Une source lue vaut sa liste, ou `None` si elle était ILLISIBLE — jamais une liste
    vide confondue avec un dépôt vide : une source illisible ne doit pas se lire comme
    une absence d'anomalie, sinon la réparation s'y fierait et écrirait à tort.
    """

    workspace_id: str | None = None
    liens: list[_Lien] | None = None
    artefacts: dict[str, _Artefact] | None = None
    blobs: dict[str, _Blob] | None = None
    fiches: dict[str, int] | None = None
    paires_indexees: set[tuple[str, str, str]] | None = None
    diagnostics: list[str] = field(default_factory=list)


def _lignes(
    db, sql: str, params: dict, etiquette: str, diagnostics: list[str]
) -> list[tuple] | None:
    """Lecture gardée : une source illisible se signale, elle ne fait pas tomber le rapport.

    `verifier_depot` ne lève sur AUCUNE anomalie, pas même sur une base dégradée : c'est
    précisément dans une base dégradée qu'un rapport est utile, et une exception
    ramènerait l'appelant à ne rien savoir.
    """
    from sqlalchemy import text as _text

    try:
        return [tuple(row) for row in db.execute(_text(sql), params)]
    except Exception as exc:
        diagnostics.append(
            f"{etiquette} illisible ({type(exc).__name__}) : cette partie n'est pas vérifiée"
        )
        return None


def _inventaire(db, workspace_id: str | None) -> _Inventaire:
    """Cinq lectures, une seule par table, et une sous-requête par lien de dépôt.

    Le N+1 le plus tentant du module est ici — un blob, une fiche, une ligne d'index
    par artefact. Il est évité par des sous-requêtes corrélées au lien plutôt que par
    des `IN (...)` d'identifiants, qui exploseraient au-delà du millier.
    """
    inv = _Inventaire(workspace_id=str(workspace_id or "").strip() or None)
    params: dict[str, Any] = {}
    porte = (
        "SELECT artifact_id FROM artifact_links"
        " WHERE scope_kind = 'workspace' AND removed_at IS NULL"
    )
    filtre_espace = ""
    if inv.workspace_id:
        porte += " AND scope_id = :ws"
        filtre_espace = " AND scope_id = :ws"
        params["ws"] = inv.workspace_id

    lignes = _lignes(
        db,
        "SELECT id, artifact_id, scope_kind, scope_id FROM artifact_links"
        " WHERE scope_kind = 'workspace' AND removed_at IS NULL"
        f"{filtre_espace} ORDER BY scope_id, artifact_id, id",
        params,
        "artifact_links",
        inv.diagnostics,
    )
    if lignes is not None:
        inv.liens = [
            _Lien(str(lid), str(aid), str(kind), str(scope))
            for lid, aid, kind, scope in lignes
        ]

    lignes = _lignes(
        db,
        "SELECT DISTINCT artifact_id, scope_kind, scope_id FROM artifact_search"
        f" WHERE scope_kind = 'workspace'{filtre_espace}",
        params,
        "artifact_search",
        inv.diagnostics,
    )
    if lignes is not None:
        inv.paires_indexees = {(str(a), str(k), str(s)) for a, k, s in lignes}

    lignes = _lignes(
        db,
        f"SELECT id, blob_id, kind, display_name, original_filename,"
        f" deleted_at IS NOT NULL,"
        f" COALESCE(ingest_state, ''), COALESCE(ingest_reason, '')"
        f" FROM artifacts"
        f" WHERE id IN ({porte}) ORDER BY id",
        params,
        "artifacts",
        inv.diagnostics,
    )
    if lignes is not None:
        inv.artefacts = {
            str(aid): _Artefact(
                str(aid),
                str(blob),
                str(kind or ""),
                str(display or ""),
                str(original or ""),
                bool(corbeille),
                str(etat or ""),
                str(motif or ""),
            )
            for aid, blob, kind, display, original, corbeille, etat, motif in lignes
        }

    lignes = _lignes(
        db,
        "SELECT b.id, b.sha256, b.relative_path, b.size_bytes FROM blobs b",
        {},
        "blobs",
        inv.diagnostics,
    )
    if lignes is not None:
        inv.blobs = {
            str(bid): _Blob(str(bid), str(sha or ""), str(rel or ""), int(size or 0))
            for bid, sha, rel, size in lignes
        }

    lignes = _lignes(
        db,
        f"SELECT artifact_id, revision, is_current FROM artifact_cards"
        f" WHERE artifact_id IN ({porte})",
        params,
        "artifact_cards",
        inv.diagnostics,
    )
    if lignes is not None:
        fiches: dict[str, tuple[tuple[int, int], int]] = {}
        for aid, revision, is_current in lignes:
            cle = (1 if int(is_current or 0) == 1 else 0, int(revision or 0))
            meilleur = fiches.get(str(aid))
            if meilleur is None or cle > meilleur[0]:
                fiches[str(aid)] = (cle, cle[1])
        inv.fiches = {aid: revision for aid, (_, revision) in fiches.items()}

    return inv


def _vivants(inv: _Inventaire) -> list[tuple[_Lien, _Artefact]]:
    """Les couples (lien de dépôt, artefact VIVANT) : ce que le dépôt présente."""
    if inv.liens is None or inv.artefacts is None:
        return []
    return [
        (lien, inv.artefacts[lien.artifact_id])
        for lien in inv.liens
        if lien.artifact_id in inv.artefacts
        and not inv.artefacts[lien.artifact_id].est_supprime
    ]


def _espaces_de(inv: _Inventaire, artifact_id: str) -> list[str]:
    return sorted(
        {lien.scope_id for lien in (inv.liens or []) if lien.artifact_id == artifact_id}
    )


def _fiche(inv: _Inventaire, artifact_id: str) -> int | None:
    return (inv.fiches or {}).get(artifact_id)


def _chemin_de(relative_path: str) -> Path:
    from backend.config.paths import get_data_dir

    return get_data_dir() / relative_path


# ---------------------------------------------------------------------------
# Détection : fonctions pures sur l'inventaire
# ---------------------------------------------------------------------------


def _lien_mort(inv: _Inventaire) -> list[dict]:
    """Lien actif dont l'artefact n'existe plus, ou est supprimé.

    La corbeille d'un artefact laisse normalement son lien : le dépôt ne montre plus
    rien, mais la ligne reste. Elle est signalée, jamais retirée — retirer un lien est
    une décision d'utilisateur (`espaces.unshare_artifact`).
    """
    if inv.liens is None or inv.artefacts is None:
        return []
    trouves = []
    for lien in inv.liens:
        artefact = inv.artefacts.get(lien.artifact_id)
        if artefact is None:
            raison = "l'artefact n'existe plus en base"
        elif artefact.est_supprime:
            raison = "l'artefact est supprimé : il porte une date de suppression"
        else:
            continue
        trouves.append(
            {
                "type": "lien_mort",
                "artifact_id": lien.artifact_id,
                "workspace_id": lien.scope_id,
                "workspace_ids": [lien.scope_id],
                "scope_kind": lien.scope_kind,
                "scope_id": lien.scope_id,
                "link_id": lien.link_id,
                "raison": raison,
            }
        )
    return sorted(trouves, key=lambda a: (a["artifact_id"], a["scope_id"]))


def _index_manquant(inv: _Inventaire) -> list[dict]:
    """Lien actif VIVANT sans ligne d'index pour son couple (scope, artefact).

    C'est l'anomalie mesurée : un fichier parfaitement versé, invisible par mot-clé.
    Les liens morts en sont exclus — remettre en index un artefact supprimé nuirait
    sans rien réparer, et les signaler une fois suffit.
    """
    if inv.paires_indexees is None:
        return []
    trouves = []
    for lien, artefact in _vivants(inv):
        if (lien.artifact_id, lien.scope_kind, lien.scope_id) in inv.paires_indexees:
            continue
        revision = _fiche(inv, lien.artifact_id)
        trouves.append(
            {
                "type": "index_manquant",
                "artifact_id": lien.artifact_id,
                "workspace_id": lien.scope_id,
                "workspace_ids": [lien.scope_id],
                "scope_kind": lien.scope_kind,
                "scope_id": lien.scope_id,
                "display_name": artefact.display_name or artefact.original_filename,
                "kind": artefact.kind,
                "fiche_revision": revision,
                "detail": (
                    "le lien est postérieur à la dernière indexation : "
                    "ensure_card_on_import n'indexe que les liens actifs au moment de l'appel"
                    if revision is not None
                    else "aucune fiche pour cet artefact : rien n'a jamais été indexé"
                ),
            }
        )
    return sorted(trouves, key=lambda a: (a["artifact_id"], a["scope_id"]))


def _nom_vide(inv: _Inventaire) -> list[dict]:
    """Artefact du dépôt sans nom affichable : ni `display_name`, ni `original_filename`.

    `espaces.depot()` filtre ces fichiers-là : un dépôt dont le fichier n'a pas de nom
    est un dépôt où l'utilisateur voit un fichier en moins. Le nom est repris dans l'ordre
    qui n'invente rien — le nom d'origine, puis l'empreinte du blob, parce qu'un sha256
    est au moins une adresse exacte et stable.
    """
    if inv.blobs is None:
        return []
    trouves = []
    for lien, artefact in _vivants(inv):
        if artefact.est_nomme:
            continue
        blob = inv.blobs.get(artefact.blob_id)
        if artefact.original_filename.strip():
            source, nom = "original_filename", artefact.original_filename.strip()
        elif blob is not None and blob.sha256.strip():
            source, nom = "empreinte du blob", blob.sha256.strip()
        else:
            source, nom = "aucune", ""
        trouves.append(
            {
                "type": "nom_vide",
                "artifact_id": lien.artifact_id,
                "workspace_id": lien.scope_id,
                "workspace_ids": _espaces_de(inv, lien.artifact_id),
                "scope_kind": lien.scope_kind,
                "scope_id": lien.scope_id,
                "kind": artefact.kind,
                "blob_id": artefact.blob_id,
                "display_name": artefact.display_name,
                "original_filename": artefact.original_filename,
                "source_du_nom": source,
                "nom_retrouve": nom,
            }
        )
    return sorted(trouves, key=lambda a: (a["artifact_id"], a["scope_id"]))


def _blob_manquant(inv: _Inventaire) -> list[dict]:
    """Artefact sans sa ligne `blobs` : impossible sous `foreign_keys=ON`, réel dès
    qu'une connexion tierce écrit sans cette PRAGMA."""
    if inv.blobs is None:
        return []
    trouves = [
        {
            "type": "blob_manquant",
            "artifact_id": artefact.artifact_id,
            "workspace_id": lien.scope_id,
            "workspace_ids": _espaces_de(inv, artefact.artifact_id),
            "scope_kind": lien.scope_kind,
            "scope_id": lien.scope_id,
            "blob_id": artefact.blob_id,
            "kind": artefact.kind,
            "detail": f"aucune ligne blobs pour blob_id = {artefact.blob_id}",
        }
        for lien, artefact in _vivants(inv)
        if artefact.blob_id not in inv.blobs
    ]
    return sorted(trouves, key=lambda a: (a["artifact_id"], a["scope_id"]))


def _fichier_absent(inv: _Inventaire) -> list[dict]:
    """Ligne `blobs` dont le `relative_path` ne désigne aucun fichier sur le disque.

    La vérification est un `stat` par blob, pas une requête : elle doit rester
    honnête quand la base et les octets sont séparés — clé USB démontée, portable
    déplacé — ce qui est précisément le moment où il faut le dire.
    """
    if inv.blobs is None:
        return []
    trouves = []
    for lien, artefact in _vivants(inv):
        blob = inv.blobs.get(artefact.blob_id)
        if blob is None or not blob.relative_path.strip():
            continue
        attendu = _chemin_de(blob.relative_path)
        try:
            present = attendu.is_file()
        except OSError:
            present = False
        if present:
            continue
        trouves.append(
            {
                "type": "fichier_absent",
                "artifact_id": artefact.artifact_id,
                "workspace_id": lien.scope_id,
                "workspace_ids": _espaces_de(inv, artefact.artifact_id),
                "scope_kind": lien.scope_kind,
                "scope_id": lien.scope_id,
                "blob_id": blob.blob_id,
                "kind": artefact.kind,
                "relative_path": blob.relative_path,
                "chemin_attendu": str(attendu),
                "taille_declaree": blob.size_bytes,
            }
        )
    return sorted(trouves, key=lambda a: (a["artifact_id"], a["scope_id"]))


def _blob_orphelin(inv: _Inventaire) -> list[dict]:
    """Fichier `.bin` de la racine des blobs sans AUCUNE ligne `blobs`.

    Lecture seule. Un tel fichier peut être une promotion concurrente entre
    `os.replace` et son `commit` ; le supprimer depuis une vérification
    détruirait un dépôt en cours. On le nomme donc, avec son chemin et sa
    taille, et `reparer_depot` refuse de le supprimer pour la même raison.
    """
    if inv.blobs is None:
        return []
    from backend.config.paths import get_data_dir

    data_dir = get_data_dir()
    racine = data_dir / "blobs"
    try:
        if not racine.is_dir():
            return []
    except OSError:
        return []
    connus = {str(blob.relative_path) for blob in inv.blobs.values()}
    trouves = []
    for fichier in sorted(racine.rglob("*.bin")):
        try:
            if fichier.is_symlink() or not fichier.is_file():
                continue
            relatif = fichier.relative_to(data_dir).as_posix()
            taille = int(fichier.stat().st_size)
        except (OSError, ValueError):
            continue
        if relatif in connus:
            continue
        trouves.append(
            {
                "type": "blob_orphelin",
                "artifact_id": None,
                "workspace_id": None,
                "workspace_ids": [],
                "scope_kind": None,
                "scope_id": None,
                "display_name": fichier.name,
                "relative_path": relatif,
                "chemin": str(fichier),
                "taille": taille,
                "detail": "fichier .bin présent sur disque sans aucune ligne blobs",
            }
        )
    return sorted(trouves, key=lambda a: a["relative_path"])


def purger_orphelins_blobs_expires(
    db,
    ttl_s: int = ORPHAN_BLOB_TTL_S,
    *,
    maintenant: float | None = None,
    limite: int = MAX_ORPHELINS_PAR_PASSE,
) -> dict:
    """Supprime les `.bin` de `data/blobs` sans ligne `blobs`, plus vieux que TTL.

    Deux crashes laissent un `.bin` définitif sans ligne :
      - entre `os.replace` et le `commit` de la promotion ;
      - entre le commit du GC et l'`unlink` du fichier.
    Sans cette passe, `_blob_orphelin` les voit mais `reparer_depot` refuse — ils
    resteraient à vie.

    La sûreté repose sur trois gardes : le TTL (une promotion en cours est plus
    jeune), l'absence de suivi des liens symboliques, et une RE-LECTURE de la
    table `blobs` juste avant `unlink`, doublée d'une comparaison d'identité du
    fichier (inode/taille/mtime). Si une promotion a recréé ou référencé le
    fichier entre-temps, il est conservé.

    Idempotent : ne supprime que des fichiers déjà orphelins et expirés.
    """
    from backend.config.paths import get_data_dir

    data_dir = get_data_dir().resolve()
    racine = data_dir / "blobs"
    seuil = (time.time() if maintenant is None else float(maintenant)) - max(
        0, int(ttl_s)
    )
    resultat = {"purged": 0, "examined": 0, "failed": 0, "errors": []}
    try:
        if not racine.is_dir():
            return resultat
    except OSError as exc:
        resultat["failed"] += 1
        resultat["errors"].append(
            f"racine blobs illisible: {type(exc).__name__}: {exc}"
        )
        return resultat

    for dossier, sous_dossiers, fichiers in os.walk(str(racine), followlinks=False):
        # Ne jamais descendre dans un lien symbolique de répertoire.
        sous_dossiers[:] = [
            nom for nom in sous_dossiers if not (Path(dossier) / nom).is_symlink()
        ]
        for nom in fichiers:
            if not nom.endswith(".bin"):
                continue
            if resultat["purged"] >= max(0, int(limite)):
                return resultat
            chemin = Path(dossier) / nom
            try:
                if chemin.is_symlink() or not chemin.is_file():
                    continue
                identite = _identite_fichier(chemin)
                if identite is None:
                    continue
                if identite["mtime_ns"] / 1_000_000_000 >= seuil:
                    continue  # frais : promotion possible, on conserve
                resultat["examined"] += 1
                if not _orphelin_encore_supprimable(db, chemin, identite, data_dir):
                    continue
                chemin.unlink()
                resultat["purged"] += 1
            except (OSError, ValueError) as exc:
                resultat["failed"] += 1
                resultat["errors"].append(f"{chemin}: {type(exc).__name__}: {exc}")
    if resultat["purged"]:
        log.info("[blobs] %d .bin orphelin(s) expiré(s) purgé(s)", resultat["purged"])
    return resultat


def _identite_fichier(chemin: Path) -> dict[str, int] | None:
    """Identité d'un fichier, lue une fois : sert à détecter un remplacement."""
    try:
        stat = chemin.stat()
    except OSError:
        return None
    return {
        "ino": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _orphelin_encore_supprimable(
    db, chemin: Path, identite: dict[str, int], data_dir: Path
) -> bool:
    """Re-vérifie juste avant `unlink` : toujours orphelin, toujours le même fichier."""
    from sqlalchemy import text as _text

    if chemin.is_symlink() or not chemin.is_file():
        return False
    if _identite_fichier(chemin) != identite:
        return False  # recréé/réécrit entre le balayage et la suppression
    try:
        relatif = chemin.relative_to(data_dir).as_posix()
    except ValueError:
        return False
    try:
        ligne = db.execute(
            _text(
                "SELECT 1 FROM blobs WHERE relative_path = :rel OR sha256 = :sha LIMIT 1"
            ),
            {"rel": relatif, "sha": chemin.stem},
        ).first()
    except Exception as exc:  # noqa: BLE001 - fail-safe : ne pas supprimer si on ne sait pas
        log.warning("[blobs] vérification base impossible pour %s : %s", chemin, exc)
        return False
    return ligne is None


@contextmanager
def verrou_gc(data_dir: Path | None = None):
    """Verrou inter-processus du GC (`flock` non bloquant sur `data/.gc.lock`).

    Le GC du disque ne doit jamais courir en parallèle de la promotion d'un
    blob : une suppression entre `os.replace` et `commit` détruirait un dépôt
    en cours. Le sidecar garde ce verrou pendant TOUTE sa vie
    (`acquerir_verrou_gc_vie`) ; un CLI `--gc` dans un autre processus échoue
    donc tant que le sidecar tourne. Le contexte est ré-entrant : la passe de
    démarrage, déjà couverte par le verrou de vie, n'essaie pas de le reprendre.
    POSIX utilise `fcntl.flock` ; Windows utilise `msvcrt.locking` sur le même
    fichier. Si aucun des deux n'existe, on échoue FERMÉ plutôt que de laisser
    un balayage non protégé. Le sidecar, lui, démarre sans verrou (fail-open) :
    la protection concurrente est POSIX/Windows-native, pas une condition
    d'existence du produit.
    """
    global _VERROU_GC_FD, _VERROU_GC_NIVEAU

    if _VERROU_GC_FD is not None:
        _VERROU_GC_NIVEAU += 1
        try:
            yield
        finally:
            _VERROU_GC_NIVEAU -= 1
        return

    fd = _ouvrir_verrou_gc(data_dir)
    _VERROU_GC_FD = fd
    _VERROU_GC_NIVEAU = 1
    try:
        yield
    finally:
        _VERROU_GC_NIVEAU -= 1
        if _VERROU_GC_NIVEAU <= 0:
            _VERROU_GC_FD = None
            _liberer_verrou_gc(fd)


def acquerir_verrou_gc_vie(data_dir: Path | None = None) -> None:
    """Garde le verrou GC jusqu'à la fin du processus (sidecar).

    Tant qu'il vit, un CLI `--gc` lancé ailleurs ne peut pas acquérir le
    verrou : il ne supprimera jamais un blob qu'une promotion du sidecar est en
    train d'écrire. Le verrou est libéré par le noyau à la sortie du processus.
    """
    global _VERROU_GC_FD, _VERROU_GC_NIVEAU

    if _VERROU_GC_FD is not None:
        return
    _VERROU_GC_FD = _ouvrir_verrou_gc(data_dir)
    _VERROU_GC_NIVEAU = 1


def _ouvrir_verrou_gc(data_dir: Path | None) -> int:
    root = Path(data_dir) if data_dir is not None else _data_dir()
    root.mkdir(parents=True, exist_ok=True)
    chemin = root / ".gc.lock"
    fd = os.open(str(chemin), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _verrouiller_fd(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _verrouiller_fd(fd: int) -> None:
    """Verrou exclusif non bloquant, POSIX (`flock`) ou Windows (`msvcrt`)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - dépend de la plateforme
        _verrouiller_fd_windows(fd)
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeError(
            "Un GC est déjà en cours (sidecar actif ?) : réessayez plus tard."
        ) from exc


def _verrouiller_fd_windows(fd: int) -> None:
    """Fallback Windows : verrou d'un octet à l'offset 0 du `.gc.lock`."""
    try:
        import msvcrt
    except ImportError as exc:  # pragma: no cover - ni fcntl ni msvcrt
        raise RuntimeError(
            "Un verrou inter-processus est indisponible sur cette plateforme : "
            "arrêtez le sidecar avant de lancer un GC manuel."
        ) from exc
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise RuntimeError(
            "Un GC est déjà en cours (sidecar actif ?) : réessayez plus tard."
        ) from exc


def _deverrouiller_fd(fd: int) -> None:
    """Libère le verrou, quel que soit le mécanisme qui l'a posé."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - dépend de la plateforme
        try:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _liberer_verrou_gc(fd: int) -> None:
    _deverrouiller_fd(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _data_dir() -> Path:
    from backend.config.paths import get_data_dir

    return get_data_dir()


def _doublon(inv: _Inventaire) -> list[dict]:
    """Deux artefacts du MÊME dépôt qui partagent le même blob.

    Le dépôt déduplique les OCTETS, pas les lignes : verser deux fois le même fichier
    dans un espace crée deux artefacts et un seul fichier, donc le dépôt affiche le
    document en double. Le regroupement est par espace, jamais global — deux espaces
    peuvent légitimement détenir le même contenu, et les confondre serait un rapport faux.
    """
    groupes: dict[tuple[str, str], list[tuple[_Lien, _Artefact]]] = {}
    for lien, artefact in _vivants(inv):
        groupes.setdefault((lien.scope_id, artefact.blob_id), []).append(
            (lien, artefact)
        )
    trouves = []
    for (workspace_id, blob_id), membres in sorted(groupes.items()):
        if len(membres) < 2:
            continue
        ids = sorted(artefact.artifact_id for _, artefact in membres)
        for lien, artefact in membres:
            trouves.append(
                {
                    "type": "doublon",
                    "artifact_id": artefact.artifact_id,
                    "workspace_id": workspace_id,
                    "workspace_ids": [workspace_id],
                    "scope_kind": lien.scope_kind,
                    "scope_id": lien.scope_id,
                    "blob_id": blob_id,
                    "kind": artefact.kind,
                    "meme_blob_que": [i for i in ids if i != artefact.artifact_id],
                    "detail": f"{len(ids)} artefacts de cet espace sur le blob {blob_id}",
                }
            )
    return sorted(trouves, key=lambda a: (a["workspace_id"], a["artifact_id"]))


def _entree_extraction(
    inv: _Inventaire, lien: _Lien, artefact: _Artefact, type_: str, etat: str
) -> dict:
    """La forme commune des trois anomalies d'extraction.

    Elle est partagée parce que ces trois défauts se disent de la même façon : un
    artefact, un espace, un motif. Les factorsiser garantit qu'un jour où l'on
    oublierait un champ, ce serait pour les TROIS — donc visible — et non pour un seul,
    dont l'absence passerait inaperçue dans un rapport que personne ne relit ligne à
    ligne.

    Le motif vient de `ingestion.etat`, jamais d'une phrase écrite ici : l'écran, l'outil
    et le rapport doivent dire la MÊME chose sur le MÊME fichier. Trois formulations,
    c'est trois vérités, dont deux fausses.
    """
    from backend.ingestion import etat as _etat

    return {
        "type": type_,
        "artifact_id": artefact.artifact_id,
        "workspace_id": lien.scope_id,
        "workspace_ids": _espaces_de(inv, artefact.artifact_id),
        "scope_kind": lien.scope_kind,
        "scope_id": lien.scope_id,
        "kind": artefact.kind,
        "blob_id": artefact.blob_id,
        "ingest_state": etat,
        "detail": _etat.phrase_sans_texte(artefact),
    }


def _anomalie_d_extraction(
    inv: _Inventaire, etats: frozenset, type_: str
) -> list[dict]:
    """Pure : range `inv` sur les seuls artefacts dont l'état est dans `etats`."""
    from backend.ingestion import etat as _etat

    if inv.artefacts is None:
        return []  # source illisible : on n'accuse rien, cf. `_lignes`
    trouves = []
    for lien, artefact in _vivants(inv):
        etat = _etat.lire(artefact).etat
        if etat not in etats:
            continue
        trouves.append(_entree_extraction(inv, lien, artefact, type_, etat))
    return sorted(trouves, key=lambda a: (a["artifact_id"], a["scope_id"]))


def _extraction_absente(inv: _Inventaire) -> list[dict]:
    """Artefact dont l'extraction a été MENÉE et n'a laissé aucun texte exploitable.

    DEUX causes, et non trois : `vide` (le fichier n'a rien à dire) et `echec` (l'extraction
    a échoué, motif à l'appui). Le binaire est EXCLU parce qu'il a sa propre anomalie —
    un état qui en déclencherait deux ferait apparaître un dépôt plus abîmé qu'il ne
    l'est. Une fois LUES, la différence entre ces deux-là est ce qui permet à
    l'utilisateur d'agir : un fichier vide n'a rien à corriger, un échec demande de
    réessayer.

    `depose` et `en_cours` n'y sont PAS, et c'est délibéré. Un dépôt en attente
    d'extraction n'est pas un dépôt abîmé : le balayage de reprise (`ingestion.reprise`)
    le solde au démarrage, et l'accuser ici produirait un dépôt « dégradé » sur une
    base parfaitement saine — et un rapport qui crie au loup perd sa crédibilité.

    `inconnu` non plus : c'est l'absence d'information, pas un défaut constaté. Un
    détecteur qui accuse ce qu'il n'a pas vérifié est le premier à fabriquer le
    mensonge qu'il prétend poursuivre.
    """
    return _anomalie_d_extraction(inv, _etats_extraction_nulle(), "extraction_absente")


def _extraction_binaire(inv: _Inventaire) -> list[dict]:
    """Artefact traité, mais dont le contenu n'est pas du texte.

    L'état vient de `artifacts.ingest_state`, écrit par l'ingestion à partir du verdict
    de `fts.arbitrer_extrait`. On NE réinférerait pas ici depuis les octets — même avec
    `detecter_ingestions_binaire_ou_tronquees()`, qui existe et qui sait le faire. Deux
    raisons, et elles ne sont pas des préférences de goût :

      1. COÛT. Cette fonction est un détecteur de ce module ; le module promet cinq
         lectures quel que soit le nombre de fichiers, et cette promesse est TESTÉE.
         Ré-analyser les octets coûterait quatre lectures de plus par vérification —
         sur le chemin du DÉMARRAGE, puisque `init_db` signale le dépôt dégradé.
      2. SENS. Le juge des octets ne voit ni ce qui a été demandé à ce fichier, ni si une
         extraction propre a réussi depuis. Ré-inférer ici reviendrait à remplacer une
         décision déjà prise par un coup de sonde sur l'index — c'est-à-dire à perdre
         exactement ce que la tâche 1.6 vient d'obtenir.

    Ce que l'on perd, et qu'il faut dire : un artefact dont l'observation est polluée
    mais dont l'état n'a jamais été écrit n'est pas signalé ici. Il est en `inconnu`,
    et le rapport le dit — ce qui est exact, puisque c'est vrai.
    """
    return _anomalie_d_extraction(
        inv, frozenset({"binaire_refuse"}), "extraction_binaire"
    )


def _extraction_tronquee(inv: _Inventaire) -> list[dict]:
    """Artefact traité, mais dont une partie du document reste hors de l'index.

    DISTINCTE de `extraction_absente`, et c'est le sens de la distinction qui compte :
    ici le fichier EST interrogeable, sur une partie de lui. Condamner une
    recherche entière à cause d'un PDF de quatre-vingts pages dont l'aperçu s'arrête
    à 2000 caractères viderait l'état de toute information — la borne est une décision
    de conception, pas une faute. Le rapport le dit sans le réparer : l'utilisateur
    peut vouloir la borne relevée, et c'est sa décision.
    """
    return _anomalie_d_extraction(inv, frozenset({"tronque"}), "extraction_tronquee")


def _etats_extraction_nulle() -> frozenset:
    from backend.ingestion import etat as _etat

    return _etat.ETATS_EXTRACTION_NULLE


_DETECTEURS = {
    "index_manquant": _index_manquant,
    "nom_vide": _nom_vide,
    "extraction_absente": _extraction_absente,
    "extraction_binaire": _extraction_binaire,
    "extraction_tronquee": _extraction_tronquee,
    "lien_mort": _lien_mort,
    "blob_manquant": _blob_manquant,
    "fichier_absent": _fichier_absent,
    "blob_orphelin": _blob_orphelin,
    "doublon": _doublon,
}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def verifier_depot(db, workspace_id: str | None = None) -> dict:
    """Rapport d'intégrité du dépôt — STRICTEMENT lecture seule.

    Aucune écriture, aucun commit : cinq SELECT et rien d'autre. Le coût ne dépend pas
    du nombre de fichiers.

    Ne lève sur AUCUNE anomalie, ni sur une base dégradée. Ce qui n'a pas pu être lu
    part dans `diagnostics` et n'est PAS compté comme une absence d'anomalie : accuser à
    tort un dépôt sain serait pire qu'un rapport muet, puisque la réparation se fie à ce
    rapport et y produirait une écriture injustifiée.

    `workspace_id=None` vérifie le dépôt de TOUS les espaces.
    """
    inv = _inventaire(db, workspace_id)
    anomalies = {nom: _DETECTEURS[nom](inv) for nom in TYPES_ANOMALIES}
    return _rapport(inv, anomalies)


def _rapport(inv: _Inventaire, anomalies: dict[str, list[dict]]) -> dict:
    total = sum(len(v) for v in anomalies.values())
    return {
        "ok": True,
        "workspace_id": inv.workspace_id,
        "portee": (
            f"dépôt de l'espace {inv.workspace_id}"
            if inv.workspace_id
            else "dépôts de tous les espaces"
        ),
        "requetes": LECTURES_PAR_VERIFICATION,
        "mesure": {
            "liens_actifs": len(inv.liens or []),
            "artefacts_vivants": len(
                {artefact.artifact_id for _, artefact in _vivants(inv)}
            ),
            "fiches_actives": len(inv.fiches or {}),
            "paires_indexees": len(inv.paires_indexees or set()),
        },
        "anomalies": {nom: anomalies[nom] for nom in TYPES_ANOMALIES},
        "par_type": {nom: len(anomalies[nom]) for nom in TYPES_ANOMALIES},
        "total": total,
        "sain": total == 0 and not inv.diagnostics,
        "diagnostics": list(inv.diagnostics),
    }


def reparer_depot(db, workspace_id: str | None = None) -> dict:
    """Répare ce qui se répare sans risque, et signale le reste sans y toucher.

    Idempotent par construction : chaque réparation dérive d'un état manquant que la
    vérification relit AVANT d'agir, donc un second passage ne trouve plus rien à faire.
    Aucune révision de fiche supplémentaire non plus : `ensure_card_on_import` renvoie
    la fiche existante s'il y en a une.

    Deux repairs, dans cet ordre, et l'ordre n'est pas indifférent :
      1. `nom_vide` d'abord — la ligne d'index porte le NOM du fichier ; renommer après
         avoir indexé laisserait une ligne décrivant le nom précédent ;
      2. `index_manquant` ensuite — `ensure_card_on_import` garantit la fiche rev 1
         (pending) quand l'artefact n'en a aucune, ce qui le rend trouvable par son nom
         sans qu'aucun contenu soit inventé, puis la ligne manquante est réécrite depuis
         cette fiche EXISTANTE. Aucune fiche n'est réécrite, aucun contenu n'est fabriqué.

    RIEN n'est jamais effacé : ni ligne, ni fichier, ni lien. `blob_manquant`,
    `fichier_absent`, `lien_mort` et `doublon` sortent dans `signales` avec la raison du
    refus — ce sont des pertes d'octets ou des décisions d'utilisateur, pas des oublis.

    Effet de bord assumé et vérifié : quand l'artefact n'a aucune fiche,
    `ensure_card_on_import` indexe TOUS ses liens actifs, pas seulement celui du dépôt.
    La réparation ajoute donc parfois des lignes sous d'autres portées (une
    conversation, un second espace) — c'est le comportement de cette fonction, jamais
    une perte, et l'index s'en trouve plus complet qu'avant.
    """
    avant = verifier_depot(db, workspace_id)
    anomalies = avant["anomalies"]
    repares: dict[str, list[dict]] = {nom: [] for nom in TYPES_REPARABLES}
    echecs: list[dict] = []

    artefacts = _artefacts_orm(
        db,
        sorted({a["artifact_id"] for nom in TYPES_REPARABLES for a in anomalies[nom]}),
    )
    for entree in anomalies["nom_vide"]:
        _repare_nom(db, entree, artefacts, repares["nom_vide"], echecs)
    for entree in anomalies["index_manquant"]:
        _repare_index(db, entree, artefacts, repares["index_manquant"], echecs)

    signales: dict[str, list[dict]] = {}
    refuses: list[dict] = []
    for nom in TYPES_ANOMALIES:
        if nom in TYPES_REPARABLES:
            continue
        signales[nom] = [
            {**a, "raison_du_refus": RAISONS_DE_REFUS[nom]} for a in anomalies[nom]
        ]
        refuses.extend(signales[nom])

    return {
        "ok": True,
        "workspace_id": avant["workspace_id"],
        "avant": avant,
        "repares": repares,
        "repares_effectuees": {nom: len(repares[nom]) for nom in TYPES_REPARABLES},
        "signales": signales,
        "non_repares": sorted(
            refuses + echecs,
            key=lambda a: (
                a["type"],
                str(a.get("artifact_id") or ""),
                str(a.get("scope_id") or ""),
            ),
        ),
        "apres": verifier_depot(db, workspace_id),
    }


def _artefacts_orm(db, artifact_ids: list[str]) -> dict:
    """Une seule lecture par lot, jamais une par artefact.

    Les lots existent parce que SQLite plafonne le nombre de variables liées d'un `IN`
    (999 sur les moteurs anciens) : un dépôt de deux mille fichiers anonymisés ne doit
    pas échouer sur la forme de la requête.
    """
    if not artifact_ids:
        return {}
    from backend.db.models import Artifact as _Art

    par_lots: dict[str, Any] = {}
    for debut in range(0, len(artifact_ids), TAILLE_DE_LOT):
        tranche = artifact_ids[debut : debut + TAILLE_DE_LOT]
        par_lots.update(
            {str(a.id): a for a in db.query(_Art).filter(_Art.id.in_(tranche)).all()}
        )
    return par_lots


def _repare_nom(
    db, entree: dict, artefacts: dict, faites: list[dict], echecs: list[dict]
) -> None:
    """Rend un nom affichable à un artefact qui n'en a pas, sans jamais l'inventer.

    L'ordre est celui du rapport : `original_filename`, puis l'empreinte du blob. Un
    artefact sans nom ET sans blob lisible n'est pas renommé — il est signalé.
    """
    artefact = artefacts.get(entree["artifact_id"])
    nom = entree["nom_retrouve"]
    if artefact is None:
        echecs.append(
            {
                **entree,
                "echec": "l'artefact a disparu entre la vérification et la réparation",
            }
        )
        return
    if not nom:
        echecs.append(
            {**entree, "echec": "aucun nom à reprendre : ni nom d'origine ni empreinte"}
        )
        return
    artefact.display_name = nom
    _commit(db, faites, echecs, {**entree, "display_name": nom})


def _repare_index(
    db, entree: dict, artefacts: dict, faites: list[dict], echecs: list[dict]
) -> None:
    """Garantit la fiche, puis réécrit LA LIGNE D'INDEX MANQUANTE.

    `ensure_card_on_import` n'indexe que les liens actifs AU MOMENT où il CRÉE la fiche :
    sur un artefact qui en a déjà une, il la renvoie sans rien réécrire. L'anomalie
    mesurée est exactement ce cas — 186 liens sans ligne alors que la fiche existe — donc
    la ligne manquante est réécrite depuis cette fiche, avec l'aperçu texte que
    l'ingestion aurait produit. Même contenu, même révision : rien n'est fabriqué.

    La réécriture passe par `cards.reindexer_carte` et non par `fts.index_card_row` :
    le verdict d'indexation doit alors se lire dans la fiche. Une ligne d'index
    qu'on reconstruit sans jamais regarder le statut de sa fiche laisserait un
    fichier binaire annoncé « analysé » — l'anomalie qu'on répare, reproduite par
    le geste qui répare.
    """
    from backend.search import cards

    artefact = artefacts.get(entree["artifact_id"])
    if artefact is None:
        echecs.append(
            {
                **entree,
                "echec": "l'artefact a disparu entre la vérification et la réparation",
            }
        )
        return
    try:
        card = cards.ensure_card_on_import(db, artefact)
        cards.reindexer_carte(
            db,
            artefact,
            card,
            entree["scope_kind"],
            entree["scope_id"],
            extracted_preview=cards._text_preview(db, artefact),
        )
    except Exception as exc:
        db.rollback()
        echecs.append(
            {
                **entree,
                "echec": f"réindexation impossible ({type(exc).__name__}: {exc})",
            }
        )
        return
    _commit(
        db,
        faites,
        echecs,
        {
            **entree,
            "fiche_revision": int(card.revision or 0),
            "indexee": f"{entree['scope_kind']}:{entree['scope_id']}",
        },
    )


def _commit(db, faites: list[dict], echecs: list[dict], entree: dict) -> None:
    """Un commit par réparation : un dépôt de deux cents fichiers ne perd pas les cent
    quatre-vingt-dix-neuf premiers parce que le centième a échoué."""
    try:
        db.commit()
        faites.append(entree)
    except Exception as exc:
        db.rollback()
        echecs.append(
            {**entree, "echec": f"écriture refusée ({type(exc).__name__}: {exc})"}
        )


# ---------------------------------------------------------------------------
# Pour l'écran
# ---------------------------------------------------------------------------


def _noms_espaces(db, ids: set[str]) -> dict[str, str]:
    """Les noms d'espaces, en UNE lecture — ou pas de nom du tout.

    La table des espaces tient en quelques lignes : la lire entière coûte le même
    prix qu'un `IN (...)`, sans la limite de paramètres liés que SQLite impose et
    sans avoir à choisir une taille de lot. C'est ce qui rend le coût INDIFFÉRENT au
    nombre d'espaces : 2 ou 300, une lecture.

    Un id sans nom s'affiche tel quel : l'écran a quelque chose à montrer même
    quand la table des espaces ne répond pas.
    """
    if not ids:
        return {}
    from sqlalchemy import text as _text

    try:
        return {
            row[1]: row[0]
            for row in db.execute(_text("SELECT name, id FROM workspaces"))
            if row[1] in ids
        }
    except Exception:
        return {}


def resume_pour_ecran(db, rapport: dict) -> dict:
    """Traduit un rapport de console en quelque chose qu'un écran peut honnêtement montrer.

    Le rapport sait *ce qui ne va pas* mais ne sait pas *où* le dire : il compte. Cette
    fonction range les anomalies par espace et les sépare en TROIS catégories : ce que la
    réparation touche, ce qui se relance depuis le dépôt (`a_reessayer`), et ce qu'elle
    refuse. Un échec d'extraction n'est pas « réparable » par la maintenance — celle-ci ne
    charge aucun modèle — et le confondre avec un refus définitif priverait l'utilisateur
    d'une action réelle.

    Elle ne réévalue rien : elle ne lit que les noms, pour qu'une anomalie ne soit pas
    présentée comme un identifiant. Ajouter les noms coûte une lecture de plus que la
    vérification, et c'est pourquoi `lectures` est recompté ici plutôt que réutilisé.
    """
    anomalies: dict[str, list[dict]] = rapport.get("anomalies", {})
    par_espace: dict[str, dict[str, int]] = {}
    for nom, entrees in anomalies.items():
        for entree in entrees:
            for espace in entree.get("workspace_ids") or [entree.get("workspace_id")]:
                if espace:
                    par_espace.setdefault(espace, {}).setdefault(nom, 0)
                    par_espace[espace][nom] += 1

    noms = _noms_espaces(db, set(par_espace))
    espaces = []
    for espace_id, compteurs in sorted(par_espace.items()):
        total = sum(compteurs.values())
        reparties = [
            entree
            for entrees in anomalies.values()
            for entree in entrees
            if espace_id
            in (entree.get("workspace_ids") or [entree.get("workspace_id")])
        ]
        from backend.ingestion import etat as _etat

        _reessayables = sum(
            1
            for entree in reparties
            if entree.get("type") == "extraction_absente"
            and entree.get("ingest_state") == _etat.ETAT_ECHEC
        )
        refus_total = sum(
            compteurs.get(nom, 0)
            for nom in TYPES_ANOMALIES
            if nom not in TYPES_REPARABLES
        )
        refuses_par_type = {}
        for nom in TYPES_ANOMALIES:
            if nom in TYPES_REPARABLES:
                continue
            restants = compteurs.get(nom, 0) - (
                _reessayables if nom == "extraction_absente" else 0
            )
            if restants > 0:
                refuses_par_type[nom] = restants
        espaces.append(
            {
                "workspace_id": espace_id,
                "nom": noms.get(espace_id) or espace_id,
                "nom_connu": espace_id in noms,
                "par_type": {
                    nom: compteurs.get(nom, 0)
                    for nom in TYPES_ANOMALIES
                    if compteurs.get(nom)
                },
                # « À réessayer » est une TROISIÈME catégorie, et non un synonyme de
                # « réparable » : une extraction échouée se relance depuis le dépôt
                # de l'espace, avec un modèle, tandis que la maintenance ne touche
                # qu'à l'index et aux noms. Un `extraction_absente` en `vide` n'y
                # entre pas : le fichier n'a rien à dire, le relancer ne changerait
                # pas son verdict. Il reste donc parmi les refus.
                "a_reessayer": sum(
                    1
                    for entree in reparties
                    if entree.get("type") == "extraction_absente"
                    and entree.get("ingest_state") == _etat.ETAT_ECHEC
                ),
                # Le détail des refus, décomposé comme le détail des réparables : un écran
                # qui ne pourrait dire QUE « 2 refus » laisserait l'utilisateur devant une
                # catégorie opaque, et il ne pourrait pas juger s'il s'en occupe.
                "refuses_par_type": refuses_par_type,
                "total": total,
                "reparable": sum(compteurs.get(nom, 0) for nom in TYPES_REPARABLES),
                "refuse": refus_total - _reessayables,
                "exemples": [
                    {
                        "type": entree.get("type"),
                        "display_name": entree.get("display_name")
                        or entree.get("original_filename")
                        or entree.get("artifact_id"),
                        "raison": entree.get("raison") or entree.get("detail") or "",
                        "ingest_state": entree.get("ingest_state") or "",
                    }
                    for entree in reparties[:8]
                ],
            }
        )

    # Trié par NOM, pas par identifiant : les ids sont générés, donc leur ordre est
    # arbitraire et changerait d'une exécution à l'autre. L'écran doit offrir un ordre
    # STABLE et lisible — alphabétique ici, comme le reste de l'atelier.
    espaces.sort(key=lambda e: (e["nom"].casefold(), e["workspace_id"]))

    return {
        "ok": True,
        "sain": bool(rapport.get("sain")),
        "lecture_complete": not rapport.get("diagnostics"),
        "diagnostics": list(rapport.get("diagnostics", [])),
        "total": rapport.get("total", 0),
        "reparable": sum(
            sum(c.get(nom, 0) for nom in TYPES_REPARABLES) for c in par_espace.values()
        ),
        "a_reessayer": sum(espace["a_reessayer"] for espace in espaces),
        # `refuse` exclut les échecs relançables : les additionner présenterait
        # comme « irréparable » un document qu'un simple réessai peut sauver.
        "refuse": sum(
            sum(c.get(nom, 0) for nom in TYPES_ANOMALIES if nom not in TYPES_REPARABLES)
            for c in par_espace.values()
        )
        - sum(espace["a_reessayer"] for espace in espaces),
        "espaces": espaces,
        "mesure": rapport.get("mesure", {}),
        "lectures": rapport.get("requetes", LECTURES_PAR_VERIFICATION)
        + (1 if noms else 0),
    }


__all__ = [
    "LECTURES_PAR_VERIFICATION",
    "MAX_ORPHELINS_PAR_PASSE",
    "ORPHAN_BLOB_TTL_S",
    "RAISONS_DE_REFUS",
    "TYPES_ANOMALIES",
    "TYPES_REPARABLES",
    "acquerir_verrou_gc_vie",
    "purger_orphelins_blobs_expires",
    "reparer_depot",
    "resume_pour_ecran",
    "verifier_depot",
    "verrou_gc",
]

# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    """Vérifie, et répare sur demande, l'état d'un dépôt.

    Sans option de réparation, c'est une pure lecture : utile avant de décider.
    La réparation ne supprime rien — ce qu'elle refuse de faire est dit.
    """
    import argparse

    parseur = argparse.ArgumentParser(
        prog="python3 -m backend.services.depot_integrite",
        description="Vérifie l'état des dépôts d'espace, et répare ce qui se répare.",
    )
    parseur.add_argument(
        "--espace",
        default=None,
        help="ne porter que sur cet espace (par défaut : tous)",
    )
    parseur.add_argument(
        "--reparer",
        action="store_true",
        help="appliquer les réparations sûres (réindexation, nom manquant)",
    )
    parseur.add_argument(
        "--gc",
        action="store_true",
        help=(
            "purger les liens échus, les artefacts sans lien actif ou récent, et "
            "les .bin orphelins expirés (GC GLOBAL : tous les espaces ; "
            "`--espace` est ignoré). Échoue si le sidecar tient le verrou GC : "
            "arrêtez-le d'abord."
        ),
    )
    args = parseur.parse_args(argv)

    from backend.db.database import SessionLocal, init_db

    if args.gc:
        from backend.search import trash as _trash

        if args.espace:
            print(
                "--gc est global : il porte sur tous les espaces, "
                f"`--espace {args.espace}` est ignoré."
            )
        try:
            with verrou_gc():
                # Migrations sous le verrou : on ne touche pas au schéma d'une
                # base qu'un sidecar vient peut-être d'ouvrir.
                init_db()
                db_gc = SessionLocal()
                try:
                    rapport_gc = _trash.purge(db_gc)
                    db_gc.commit()
                    rapport_orphelins = purger_orphelins_blobs_expires(db_gc)
                except Exception:
                    db_gc.rollback()
                    raise
                finally:
                    db_gc.close()
        except Exception as exc:
            print(f"GC impossible : {type(exc).__name__} : {exc}")
            return 1
        import json

        print(json.dumps(rapport_gc, ensure_ascii=False, indent=2))
        if rapport_orphelins["purged"] or rapport_orphelins["errors"]:
            print(
                f"Blobs orphelins expirés retirés : {rapport_orphelins['purged']} ; "
                f"erreurs : {len(rapport_orphelins['errors'])}"
            )
        echecs = (
            int(rapport_gc.get("artifacts_failed", 0))
            + int(rapport_gc.get("blobs_failed", 0))
            + int(rapport_orphelins.get("failed", 0))
        )
        if echecs:
            print(f"GC terminé avec {echecs} échec(s).")
            return 1
        return 0

    init_db()
    db = SessionLocal()
    try:
        rapport = (
            reparer_depot(db, args.espace)
            if args.reparer
            else verifier_depot(db, args.espace)
        )
    finally:
        db.close()

    if args.reparer:
        avant, apres = rapport.get("avant", {}), rapport.get("apres", {})
        for type_, entrees in sorted(avant.get("anomalies", {}).items()):
            if not entrees:
                continue
            faites = rapport.get("repares_effectuees", {}).get(type_, 0)
            if faites:
                print(f"  réparé   {type_}  {faites} fichier(s)")
            else:
                refus = rapport.get("non_repares", [{}])[0].get("raison_du_refus", "")
                print(f"  signalé  {type_}  {len(entrees)} fichier(s) — {refus}")
        avant_total = sum(avant.get("par_type", {}).values())
        apres_total = sum(apres.get("par_type", {}).values())
        print(
            f"\n  {avant_total - apres_total} anomalie(s) résolue(s), "
            f"{apres_total} restante(s) non destructives."
        )
        return 0

    if rapport.get("sain"):
        print(
            f"  Dépôt sain — {rapport['mesure']['liens_actifs']} lien(s), "
            f"{rapport['requetes']} lectures."
        )
        return 0
    for type_, entrees in sorted(rapport.get("anomalies", {}).items()):
        for entree in entrees:
            details = entree.get("chemin_attendu") or entree.get("source_du_nom") or ""
            print(f"  {type_:16s} {entree.get('artifact_id')}  {details}")
    print(f"\n  {rapport['total']} anomalie(s), {rapport['requetes']} lectures.")
    print("  Réparable : npm run depot:reparer")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())

# backend/ipc/espaces.py — commandes utilisateur des espaces (MVP).
# Chaque handler prend la session ORM et l'argument reçu du frontend, puis RETOURNE
# le payload : aucun emit ici, l'appelant émet. Les clés de retour sont snake_case et
# les arguments entrants sont lus en camelCase puis snake_case (héritage de
# pont IPC Electron. Les `datetime` du modèle sont convertis en chaînes ISO : sans
# cela le payload n'est pas sérialisable en JSON.
#
# CE QUE CE FICHIER EST, ET CE QU'IL N'EST PAS : la DERNIER couche d'un module déjà
# écrit et testé. `services/espaces.py` détient tous les algorithmes — dépôt,
# compteurs, corbeille, déplacement — et ce fichier ne les réimplémente pas, il les
# nomme. Il ne détient aucune décision non plus : la portée d'un espace se calcule
# côté serveur, jamais à partir d'un `workspace_id` reçu d'un client ou d'un modèle.
#
# POURQUOI AUCUN OUTIL ICI (E07, `01_classe_espace.md:74-84`) : un espace est une
# commande UTILISATEUR, distincte du chat. Ce module n'importe donc aucun schéma
# d'outil, aucun registre et rien de `backend.inference` : il n'a aucun chemin vers
# le modèle, donc aucun moyen de lui transmettre un identifiant d'espace. Le test
# `test_les_quatre_interdits_du_modele_sont_structuraux` vérifie cette absence sur
# les outils ; cette frontière se vérifie ici, dans le sens inverse.
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from backend.ipc.images import _iso_text, _param
from backend.services import depot_integrite
from backend.services import espaces as service

# Un SEUL refus pour « aucun id », « id deviné » et « espace en corbeille ». Trois
# messages distincts en confirmeraient un : il suffirait de deviner un identifiant
# pour apprendre qu'il désigne quelque chose.
_WORKSPACE_UNKNOWN = "[WORKSPACE_UNKNOWN] Espace introuvable."

# Un SEUL refus pour « aucun id » et « document absent de cet espace », comme pour
# l'espace lui-même : deux messages en confirmeraient un, et il suffirait d'essayer
# un identifiant pour apprendre qu'il désigne quelque chose.
_LINK_UNKNOWN = "[LINK_NOT_FOUND] Ce document n'est pas partagé dans cet espace."

# Refus d'écrire sans intention exprimée. Volontairement SANS Workspace/Lien : ce
# refus ne porte sur aucune cible, et lui emprunter leur vocabulaire laisserait
# croire qu'un espace est en cause.
_CONFIRMATION_MANQUANTE = "[CONFIRMATION_REQUIRED] La réparation n'est pas confirmée."

# Colonnes de `conversations` pour l'enrichissement du dépôt de l'espace. Le tri est
# fait en SQL, dans la seule requête : `updated_at` décroissant, `id` en départage —
# et SQLite classe les `updated_at` NULS en dernier, ce qu'un tri en Python sur des
# chaînes ISO devrait reproduire à la main.
_CONVERSATIONS_SQL = text(
    """
    SELECT id,
           COALESCE(title, '') AS title,
           COALESCE(mode, '') AS mode,
           created_at AS created_at,
           updated_at AS updated_at
    FROM conversations
    WHERE id IN :conversation_ids
    ORDER BY updated_at DESC, id DESC
    """
).bindparams(bindparam("conversation_ids", expanding=True))


def _text(params: dict[str, Any], *names: str) -> str | None:
    """Texte entrant : une chaîne, ou rien. Un autre type n'est pas un nom."""
    value = _param(params, *names)
    return value if isinstance(value, str) else None


def _workspace_id(params: dict[str, Any]) -> str:
    """L'espace visé par la commande. Sans id, rien à désigner : refus immédiat."""
    workspace_id = (_text(params, "workspaceId", "workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError(_WORKSPACE_UNKNOWN)
    return workspace_id


def _workspace_connu(db: Session, workspace_id: str) -> Any:
    """Espace connu, ou refus. L'existence d'un espace ne se devine pas."""
    workspace = service.get_workspace(db, workspace_id)
    if workspace is None:
        raise ValueError(_WORKSPACE_UNKNOWN)
    return workspace


def _workspace_actif(db: Session, workspace_id: str) -> Any:
    """Espace ACTIF, ou refus. Un espace en corbeille n'accepte aucune écriture."""
    workspace = service.get_workspace(db, workspace_id, include_trashed=False)
    if workspace is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")
    return workspace


def _entier(valeur: Any, defaut: int = 0) -> int:
    """Entier entrant, ou défaut. Une valeur illisible ne casse pas la commande."""
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return defaut


def _workspace_payload(workspace: Any, stats: dict[str, Any]) -> dict[str, Any]:
    """Un espace et ses compteurs RECALCULÉS, jamais stockés (`01_classe_espace.md:66-68`)."""
    return {
        "id": str(workspace.id),
        "name": str(workspace.name),
        "description": workspace.description,
        "state": str(workspace.state),
        "created_at": _iso_text(workspace.created_at),
        "updated_at": _iso_text(workspace.updated_at),
        "stats": stats,
    }


def _conversation_payload(row: Any) -> dict[str, Any]:
    """Une ligne de `conversations` en objet affichable."""
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "mode": str(row["mode"]),
        "created_at": _iso_text(row["created_at"]),
        "updated_at": _iso_text(row["updated_at"]),
    }


def espace_list(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Espaces actifs, du plus rempli au plus récent.

    Les compteurs sont recalculés par `stats()` pour chaque espace : le MVP compte
    peu d'espaces, et surtout il ne réimplémente aucun compteur — un compteur
    recalculé ailleurs deviendrait un second mensonge en cas d'évolution du service.
    """
    del params  # MVP : aucun filtre, la liste est celle des espaces actifs.
    workspaces = service.list_workspaces(db, include_trashed=False)
    entries = [
        _workspace_payload(workspace, service.stats(db, str(workspace.id)))
        for workspace in workspaces
    ]
    # Discussions décroissantes, puis création décroissante. Les dates ISO se
    # comparent lexicographiquement ; une date absente est la plus ancienne.
    entries.sort(
        key=lambda entry: (entry["stats"]["discussions"], entry["created_at"] or ""),
        reverse=True,
    )
    return {"workspaces": entries, "count": len(entries)}


def espace_create(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Crée un espace et le renvoie déjà décompté.

    Le nom manquant n'est pas traité ici : `create_workspace` refuse avec
    `[NAME_REQUIRED]` et ce module n'a pas d'erreur propre à inventer.
    """
    name = _text(params, "name") or ""
    description = _text(params, "description")
    workspace = service.create_workspace(db, name, description)
    return _workspace_payload(workspace, service.stats(db, str(workspace.id)))


def espace_get(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Un espace et ses compteurs — les deux dans la même réponse.

    Le compteur est partagé entre les deux clés plutôt que recalculé : c'est le
    service qui le calcule, et deux appels pourraient donner deux mesures d'un
    dépôt vivant.
    """
    workspace_id = _workspace_id(params)
    workspace = _workspace_connu(db, workspace_id)
    stats = service.stats(db, workspace_id)
    return {"workspace": _workspace_payload(workspace, stats), "stats": stats}


def espace_stats(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Compteurs d'un espace, tels que `services/espaces.py` les définit."""
    workspace_id = _workspace_id(params)
    _workspace_connu(db, workspace_id)
    return service.stats(db, workspace_id)


def espace_conversations(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Conversations ACTIVES de l'espace, corbeille exclue.

    `space_conversation_ids` ne rend que des ids : la ligne est relue ici pour
    porter un titre et une date à l'écran. La corbeille des conversations
    existe désormais (`backend/services/corbeille.py`) : `space_conversation_ids`
    filtre `deleted_at IS NULL`, donc une discussion mise à la corbeille quitte
    cette liste et le corpus de l'espace DÈS sa suppression — §4.1 de
    `cycle_de_vie_des_informations.md`, qui invalide la formulation « y compris
    chats supprimés » de `01_architecture_registre_index.md:73-78` (voir le
    docstring du service pour l'harmonisation à faire).

    L'espace est sanctionné avant la lecture comme dans les autres commandes : un
    id inconnu répond `[WORKSPACE_UNKNOWN]`, jamais une liste vide — sinon
    « aucune conversation » et « aucun espace » seraient la même réponse, et un
    appelant ne pourrait pas distinguer une panne d'un espace réellement vide.
    """
    workspace_id = _workspace_id(params)
    _workspace_connu(db, workspace_id)
    conversation_ids = service.space_conversation_ids(db, workspace_id)
    if not conversation_ids:
        return {"conversations": []}
    rows = (
        db.execute(_CONVERSATIONS_SQL, {"conversation_ids": conversation_ids})
        .mappings()
        .all()
    )
    return {"conversations": [_conversation_payload(row) for row in rows]}


def _entretien_payload(db: Session, workspace: Any) -> dict[str, Any]:
    """L'état de l'entretien mémoire d'un espace, tel que l'écran le lit.

    Le verrou est la seule source de vérité pour « en cours » et « différé » :
    l'IPC ne recalcule pas une occupation à partir d'horodatages. Les seuils
    restent ceux du service, jamais des constantes recopiées ici.
    """
    del db  # l'état vient du verrou et des colonnes déjà chargées
    from backend.services.activity import etat_activite
    from backend.services.memoire_auto import (
        SEUIL_INACTIVITE_S,
        SEUIL_TOURS_MEMOIRE,
        _derniere_activite,
        _now,
    )

    tours = int(getattr(workspace, "auto_memory_turn_count", 0) or 0)
    derniere = _derniere_activite(workspace)
    inactivite_s = (
        None if derniere is None else max(0.0, (_now() - derniere).total_seconds())
    )
    activite = etat_activite()
    kind = activite["kind"]

    etat = "inconnu"
    raison = None
    if kind == "memoire" and activite["owner_id"] == f"memoire:{workspace.id}":
        etat, raison = "en_cours", "en_cours"
    elif kind in ("chat", "image", "memoire") or activite["tour_actif"]:
        etat = "differe"
        raison = "verrou_occupe" if kind == "memoire" else "tour_actif"
    elif tours < SEUIL_TOURS_MEMOIRE:
        etat, raison = "a_jour", "seuil_tours"
    elif derniere is not None:
        if inactivite_s is None or inactivite_s < SEUIL_INACTIVITE_S:
            etat, raison = "en_attente", "activite_recente"
        else:
            etat, raison = "en_attente", "seuil_atteint"

    return {
        "tours": tours,
        "seuil_tours": SEUIL_TOURS_MEMOIRE,
        "inactivite_s": inactivite_s,
        "seuil_inactivite_s": SEUIL_INACTIVITE_S,
        "etat": etat,
        "raison": raison,
        "derniere_activite": _iso_text(derniere),
        "dernier_traitement": _iso_text(
            getattr(workspace, "memory_last_processed_at", None)
        ),
    }


def _memoire_differee(db: Session, workspace_id: str, workspace: Any) -> dict[str, Any]:
    """Refus d'une écriture mémoire pendant qu'un entretien tient le verrou.

    Le verrou est global : une écriture hors entretien ne doit pas modifier les
    fiches pendant que l'entretien les lit et les réécrit. Le renderer relit le
    même snapshot que l'onglet, et rien n'a été muté.
    """
    from backend.services import memoire as _memoire
    from backend.services import memoire_auto as _memoire_auto

    return {
        "ok": False,
        "statut": "differe",
        "raison": "verrou_occupe",
        "elements": _memoire.lister_elements_courants(db, workspace_id),
        "synthese": _memoire_auto.synthese_pour_ui(db, workspace_id),
        "entretien": _entretien_payload(db, workspace),
    }


def espace_memoire(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Les éléments courants et la synthèse d'un espace, pour l'onglet."""
    workspace_id = _workspace_id(params)
    workspace = _workspace_connu(db, workspace_id)
    from backend.services import memoire as _memoire
    from backend.services import memoire_auto as _memoire_auto

    return {
        "elements": _memoire.lister_elements_courants(db, workspace_id),
        "synthese": _memoire_auto.synthese_pour_ui(db, workspace_id),
        "entretien": _entretien_payload(db, workspace),
    }


def espace_memoire_rafraichir(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Rafraîchissement automatique ou manuel de la mémoire d'un espace.

    `seuilMessages` (camelCase du renderer) et `seuil_messages` désignent le
    même seuil. Un `contenu` fourni emprunte le chemin explicite du service ;
    sans lui, `rafraichir_memoire_espace` délègue au forçage déterministe.
    """
    workspace_id = _workspace_id(params)
    workspace = _workspace_actif(db, workspace_id)
    from backend.services import memoire as _memoire
    from backend.services import memoire_auto as _memoire_auto

    force = bool(params.get("force"))
    seuil_messages = _entier(
        params.get("seuilMessages") or params.get("seuil_messages")
    )
    raw_contenu = params.get("contenu")
    contenu = (
        raw_contenu.strip()
        if isinstance(raw_contenu, str) and raw_contenu.strip()
        else None
    )
    # Sans contenu explicite, le service délègue à l'entretien, qui prend sa
    # propre réservation. Avec un contenu, l'écriture est hors entretien : elle
    # doit donc prendre la réservation mémoire avant d'écrire, ou être différée.
    if contenu is None:
        resultat = _memoire.rafraichir_memoire_espace(
            db,
            workspace_id,
            force=force,
            seuil_messages=seuil_messages,
            contenu=None,
        )
    else:
        from backend.services.activity import liberer, try_reserver

        reservation = try_reserver("memoire", f"memoire:{workspace_id}", timeout=0.0)
        if reservation is None:
            return _memoire_differee(db, workspace_id, workspace)
        try:
            resultat = _memoire.rafraichir_memoire_espace(
                db,
                workspace_id,
                force=force,
                seuil_messages=seuil_messages,
                contenu=contenu,
            )
        finally:
            liberer(reservation)
    return {
        "elements": _memoire.lister_elements_courants(db, workspace_id),
        "synthese": _memoire_auto.synthese_pour_ui(db, workspace_id),
        "rafraichissement": resultat,
        "entretien": _entretien_payload(db, workspace),
    }


def espace_memoire_forcer(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Force l'entretien déterministe de la mémoire d'un espace actif.

    Un entretien différé (verrou de chat, d'image ou de mémoire) n'est pas une
    erreur : le statut est rendu tel quel et l'écran affiche sa raison.
    """
    workspace_id = _workspace_id(params)
    workspace = _workspace_actif(db, workspace_id)
    from backend.services import memoire as _memoire
    from backend.services import memoire_auto as _memoire_auto

    resultat = _memoire_auto.forcer_memoire_espace(db, workspace_id)
    return {
        "elements": _memoire.lister_elements_courants(db, workspace_id),
        "synthese": _memoire_auto.synthese_pour_ui(db, workspace_id),
        "rafraichissement": resultat,
        "entretien": _entretien_payload(db, workspace),
    }


def espace_memoire_corriger(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Corrige une fiche de l'espace puis reconstruit la synthèse.

    `workspaceId` borne la correction : une fiche d'un autre espace est refusée
    par le service, jamais corrigée au passage.
    """
    workspace_id = _workspace_id(params)
    workspace = _workspace_actif(db, workspace_id)
    entry_id = (_text(params, "entryId", "entry_id") or "").strip()
    content = (_text(params, "content", "contenu") or "").strip()
    version = params.get("expectedVersion")
    if version is None:
        version = params.get("expected_version")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "[INVALID_VERSION] expectedVersion doit être un entier."
            ) from exc
    from backend.services import memoire as _memoire
    from backend.services import memoire_auto as _memoire_auto
    from backend.services.activity import liberer, try_reserver

    reservation = try_reserver("memoire", f"memoire:{workspace_id}", timeout=0.0)
    if reservation is None:
        return _memoire_differee(db, workspace_id, workspace)
    try:
        resultat = _memoire.correct(
            db,
            entry_id,
            content,
            expected_version=version,
            workspace_id=workspace_id,
        )
        # Un conflit de révision n'est PAS une correction : reconstruire la
        # synthèse ici écraserait l'état que l'utilisateur n'a pas pu modifier,
        # et l'écran annoncerait un succès sur une écriture refusée.
        if resultat.get("ok") is False or resultat.get("code") == "REVISION_CONFLICT":
            raise ValueError(
                "[REVISION_CONFLICT] La fiche a changé entre-temps ; "
                "relisez la version courante avant de corriger."
            )
        _memoire_auto.construire_synthese(db, workspace_id)
        return {
            "elements": _memoire.lister_elements_courants(db, workspace_id),
            "synthese": _memoire_auto.synthese_pour_ui(db, workspace_id),
            "correction": resultat,
        }
    finally:
        liberer(reservation)


def espace_memoire_supprimer(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Supprime une fiche et dépose une exclusion durable de son contenu.

    `purge_memory(exclure=True)` fait les deux : la fiche disparaît et ce
    qu'elle disait ne renaît pas d'une extraction ultérieure. La synthèse est
    ensuite reconstruite depuis les fiches restantes.
    """
    workspace_id = _workspace_id(params)
    workspace = _workspace_actif(db, workspace_id)
    entry_id = (_text(params, "entryId", "entry_id") or "").strip()
    from backend.services import memoire as _memoire
    from backend.services import memoire_auto as _memoire_auto
    from backend.services.activity import liberer, try_reserver

    reservation = try_reserver("memoire", f"memoire:{workspace_id}", timeout=0.0)
    if reservation is None:
        return _memoire_differee(db, workspace_id, workspace)
    try:
        resultat = _memoire.purge_memory(db, entry_id, workspace_id, exclure=True)
        _memoire_auto.construire_synthese(db, workspace_id)
        return {
            "elements": _memoire.lister_elements_courants(db, workspace_id),
            "synthese": _memoire_auto.synthese_pour_ui(db, workspace_id),
            "suppression": resultat,
        }
    finally:
        liberer(reservation)


def espace_depot(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Le dépôt de l'espace, tel que `services/espaces.py` l'inventorie.

    Un dépôt VIDE est une réponse légitime : personne n'a rien versé dans cet
    espace. Il ne doit donc pas se confondre avec l'absence d'espace, et
    l'espace est sanctionné avant la lecture comme dans les autres commandes.
    """
    workspace_id = _workspace_id(params)
    _workspace_connu(db, workspace_id)
    return service.depot(db, workspace_id)


def espace_depot_retirer(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Retire un document du dépôt. Le blob reste : le retrait est un `removed_at`.

    L'id du document est exigé ici et non laissé au service : un appelant qui
    l'oublie reçoit un refus lisible, pas une recherche de lien vide.
    """
    workspace_id = _workspace_id(params)
    _workspace_connu(db, workspace_id)
    artifact_id = (_text(params, "artifactId", "artifact_id") or "").strip()
    if not artifact_id:
        raise ValueError(_LINK_UNKNOWN)
    return service.unshare_artifact(db, workspace_id, artifact_id)


def espace_depot_renommer(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Renomme le nom AFFICHÉ d'un document du dépôt (`depot.md` §5.2).

    Les deux arguments sont nommés à l'entrée : un appel sans document ni
    nom ne renomme rien, et le serveur ne devine pas l'un pour l'autre.
    Le contenu et les informations extraites ne bougent pas — c'est le
    service qui garantit, et ce handler ne fait que dire QUI et QUOI.
    """
    workspace_id = _workspace_id(params)
    _workspace_connu(db, workspace_id)
    artifact_id = (_text(params, "artifactId", "artifact_id") or "").strip()
    nom = (_text(params, "nouveauNom", "nouveau_nom", "nom", "name") or "").strip()
    if not artifact_id:
        raise ValueError(_LINK_UNKNOWN)
    if not nom:
        raise ValueError("[NAME_REQUIRED] Un renommage exige le nom à afficher.")
    return service.renommer_depot(db, workspace_id, artifact_id, nom)


# ---------------------------------------------------------------------------
# Intégrité des dépôts — portée : TOUS les espaces
# ---------------------------------------------------------------------------
#
# Ces deux commandes ne demandent ni `workspaceId` ni vérification d'espace, et
# c'est délibéré : `_workspace_id` lève `ValueError(_WORKSPACE_UNKNOWN)` quand
# l'id manque (ipc/espaces.py:64-69), ce qui est juste pour « quel dépôt ? » et
# absurde pour « tous les dépôts ». Les copier aurait donné un bouton mort.
#
# La portée est donc écrite dans le nom du service, pas dans un paramètre.


def espace_depot_verifier(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """L'état des dépôts, groupé par espace. Strictement lecture seule.

    Le retour distingue trois choses que l'écran doit pouvoir dire séparément :
    rien à signaler, des anomalies, et une lecture incomplète. `diagnostics`
    non vide signifie que toutes les tables n'ont pas répondu — ce n'est pas
    « zéro anomalie », et l'appelant ne doit pas le lire comme tel.
    """
    return depot_integrite.resume_pour_ecran(db, depot_integrite.verifier_depot(db))


def espace_depot_reparer(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Répare ce qui se répare dans tous les dépôts, et signale le reste.

    Une écriture exige `confirme: true`. Sans ce paramètre, un appel égaré —
    une réémission de l'interface, un canal qui n'a pas eu de réponse et qui réessaie —
    modifierait la base sans que personne n'ait rien demandé. L'intention
    s'écrit donc dans l'appel.

    Ne supprime rien : les refus sont renvoyés pour être montrés, pas exécutés.
    """
    if not _vrai(params.get("confirme")):
        raise ValueError(_CONFIRMATION_MANQUANTE)
    rapport = depot_integrite.reparer_depot(db)
    return {
        "ok": True,
        "avant": depot_integrite.resume_pour_ecran(db, rapport.get("avant", {})),
        "apres": depot_integrite.resume_pour_ecran(db, rapport.get("apres", {})),
        "repares": rapport.get("repares_effectuees", {}),
        "non_repares": rapport.get("non_repares", []),
    }


def espace_conversation_deplacer(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Déplace une discussion vers un autre espace, à l'acte ou en évaluation.

    Sans `confirme`, le retour est le RAPPORT de déplacement (`assess_move`) :
    ce que le geste ferait, sans le faire. Avec `confirme`, le rattachement
    change et les conséquences listées s'exécutent (`request_move`). §7.3 de
    `docs/intentions/fonctionnalites/espaces.md` : « Déplacer une discussion de
    l'espace X vers l'espace Y déplace la discussion et les fichiers qui lui
    sont liés. »

    Les trois arguments sont nommés à l'entrée — rejet explicite, jamais un
    résultat fabriqué : une conversation sans cible ne déplace rien, et une
    évaluation sans « quelle conversation ? » n'évaluerait rien.
    """
    conversation_id = (_text(params, "conversationId", "conversation_id") or "").strip()
    if not conversation_id:
        raise ValueError(
            "[CONVERSATION_REQUIRED] Le déplacement exige la conversation visée : "
            "l'identifiant manque."
        )
    target_workspace_id = (
        _text(params, "targetWorkspaceId", "target_workspace_id") or ""
    ).strip()
    if not target_workspace_id:
        raise ValueError(
            "[WORKSPACE_REQUIRED] Le déplacement exige l'espace de destination : "
            "le rattachement ne se déduit pas."
        )
    return service.request_move(
        db, conversation_id, target_workspace_id, confirm=_vrai(params.get("confirme"))
    )


def espace_conversation_partager(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Verse un fichier de la discussion dans le dépôt de son espace.

    Le versement suit `share_artifact` (`services/espaces.py:208`) et « Le
    dépôt rassemble les documents volontairement mis à disposition de
    toutes les discussions de l'espace » (`espaces.md` §6) : le lien
    ArtifactLink portée workspace est créé, les octets ne sont PAS copiés. espaces.md:241 : « Procédure de promotion d'un fichier de
    discussion vers le dépôt » reste un point ouvert — la fiche délibère ;
    c'est la cible qui est déduite du rattachement de la discussion :
    l'utilisateur ne choisit pas un espace, il verse dans celui de la
    discussion sous ses yeux.

    Deux arguments nommés à l'entrée : sans discussion ni fichier, rien
    n'est versé et aucun résultat n'est fabriqué. Une discussion hors de
    tout espace est un refus clair — deviner une cible ferait sortir un
    fichier privé d'un espace vers un autre sans vérification.
    """
    conversation_id = (_text(params, "conversationId", "conversation_id") or "").strip()
    if not conversation_id:
        raise ValueError(
            "[CONVERSATION_REQUIRED] Le versement exige la discussion source : "
            "l'identifiant manque."
        )
    artifact_id = (_text(params, "artifactId", "artifact_id") or "").strip()
    if not artifact_id:
        raise ValueError(
            "[ARTIFACT_REQUIRED] Le versement exige le fichier visé : "
            "l'identifiant manque."
        )
    # La cible est l'espace DE LA DISCUSSION, déduit côté serveur : un
    # workspaceId reçu d'un client ne fait que confirmer (moyennant match).
    cible = (
        _text(
            params,
            "workspaceId",
            "workspace_id",
            "targetWorkspaceId",
            "target_workspace_id",
        )
        or ""
    ).strip() or service.conversation_workspace(db, conversation_id)
    if not cible:
        raise LookupError(
            "[CONVERSATION_SANS_ESPACE] Cette discussion n'est rattachée à "
            "aucun espace : il n'y a pas de dépôt à alimenter."
        )
    _workspace_connu(db, cible)
    return service.share_artifact(db, cible, artifact_id, from_conv_id=conversation_id)


def _vrai(valeur: Any) -> bool:
    """`confirme` doit être vrai, pas simplement présent.

    Un `confirme: "false"` ou `confirme: 0` est un refus : c'est le seul moyen
    qu'ait l'appelant de dire qu'il n'a pas confirmé, et le lire autrement
    reviendrait à écrire sur une intention non exprimée.
    """
    return valeur is True

# backend/search/space_info.py — recherche d'information AU NIVEAU ESPACE
#
# D'OÙ VIENT CE FICHIER : troisième ligne de la matrice de permissions
# (`01_architecture_registre_index.md:91-97`). C'est la notion la plus facile à
# rater en lisant les maquettes, parce qu'elle ne se voit pas :
#
#   | Contexte                            | Accès FICHIER        | Accés INFORMATION |
#   | Recherche niveau espace             | JAMAIS               | TOUS les fichiers de chats de l'espace… |
#
# L'information et le fichier sont DEUX DROITS DISTINCTS. Un espace peut donner
# accès au contenu d'un autre chat de l'espace, jamais à son fichier manipulable.
# C'est ce qui rend le corpus d'un espace utilisable sans jamais donner le
# contrôle des fichiers des conversations voisines — le vrai problème d'un dossier
# partagé.
#
# DEUX GARDIENS, ET ILS SONT INDÉPENDANTS :
# 1. Ici, on ne rend AUCUN `artifact_id` exploitable. On rend un extrait et un
#    nom de provenance : « un extrait d'information n'est pas une poignée sur le
#    fichier » (`:97-99`).
# 2. Même si un id fuitait, `ask_files` refuserait : le périmètre d'un chat ne
#    contient que SES pièces jointes plus les fichiers UTILISATEUR de l'espace,
#    jamais les fichiers des autres conversations. Un id soumis hors de ce
#    périmètre reçoit `ID_OUT_OF_SCOPE` (`scope.check_artifact_scope`).
#
# L'espace est résolu EN BASE depuis la conversation. Le client ne le transmet
# jamais — c'est la faille d'Unsloth, où le backend relaie le `project_id` du
# client sans le redériver (`04_classe_perimetre.md:123-137`).
from __future__ import annotations

import re
from typing import Any

from backend.search import cursor as _cur

# Bornes de restitution : une recherche d'information rend des EXTRAITS, jamais
# un document. Les mêmes bornes que les autres résultats de recherche du projet.
MAX_EXCERPT_CHARS = 800
DEFAULT_LIMIT = 5


# `fts.search` remplit `name` par `original_filename or display_name or art.id` :
# les deux premières colonnes sont NOT NULL mais `''` les satisfait, donc un
# artefact sans nom de fichier rendrait son ID TECHNIQUE. Or la promesse de ce
# module est l'inverse de « rendre un id exploitable ». On refuse donc tout nom
# qui ressemble à un identifiant, comme le fait `find_files._public_name`.
_TECHNICAL_ID = re.compile(r"(?:art_|sti_|sha_|mem_|chk_|obs_|card_|bl?ob)[A-Za-z0-9_]*\Z",
                           re.IGNORECASE)


def _provenance_of(result: dict[str, Any]) -> str:
    nom = str(result.get("name") or "").strip()
    if not nom or _TECHNICAL_ID.fullmatch(nom):
        return "document"
    return nom


def _excerpt_of(result: dict[str, Any]) -> str:
    """Extrait borné, sans l'artifact_id ni aucun identifiant technique.

    `fts.search` rend la fiche dans une clé NESTED `card` : chercher `summary` au
    premier niveau renvoyait une chaîne vide pour chaque résultat, et
    l'information d'espace se réduisait au nom du fichier. On lit donc la carte,
    puis l'aperçu extrait, et seulement ensuite les clés plates.
    """
    card = result.get("card")
    parts: list[str] = []
    if isinstance(card, dict):
        for key in ("summary", "extracted_preview", "title"):
            value = card.get(key)
            if value:
                parts.append(str(value))
    if isinstance(card, dict) and card.get("extracted_preview"):
        parts.append(str(card["extracted_preview"]))
    if not parts:
        for key in ("excerpt", "snippet", "summary", "text"):
            value = result.get(key)
            if value:
                parts.append(str(value))
    return " — ".join(parts)[:MAX_EXCERPT_CHARS]


def search_space_information(db: Any, workspace_id: str, query: str, *,
                             limit: int = DEFAULT_LIMIT,
                             cursor: str | None = None) -> dict[str, Any]:
    """Cherche dans le corpus INFORMATIONNEL d'un espace. Jamais de fichier.

    `fts.search` filtre sur un seul couple (scope_kind, scope_id), alors que le
    corpus d'un espace est l'union de plusieurs portées : chaque conversation de
    l'espace, plus la portée de l'espace elle-même. On interroge donc chaque
    portée séparément avec le MÊME `allowed_ids` — celui de l'espace — puis on
    fusionne. Le filtre d'autorisation est ainsi appliqué avant tout classement,
    dans les deux cas, et il est identique.
    """
    from backend.db.models import Conversation as _Conv
    from backend.search import fts as _fts, scope as _scope

    allowed = _scope.resolve_space_information_scope(workspace_id)
    scopes: list[tuple[str, str]] = [("workspace", workspace_id)]
    # `deleted_at IS NULL` comme `resolve_space_information_scope` : une
    # discussion en corbeille n'offre plus de portée de recherche (§4.1 du
    # cycle de vie — ses entrées d'index de portée conversation sont parties).
    for row in db.query(_Conv.id).filter(
            _Conv.workspace_id == workspace_id,
            _Conv.deleted_at.is_(None)).all():
        scopes.append(("conversation", str(row[0])))

    merged: dict[str, dict[str, Any]] = {}
    echecs: list[str] = []
    for scope_kind, scope_id in scopes:
        try:
            page = _fts.search(db, query=query, scope_kind=scope_kind, scope_id=scope_id,
                               allowed_ids=allowed, limit=max(int(limit) * 4, 20))
        except Exception as exc:
            # Ne JAMAIS confondre une panne et une absence : « SI rien
            # d'exploitable / Élargir la recherche locale, PAS conclure à
            # l'absence » (`backend_espaces.md:82-83`). Un `total: 0` fabriqué
            # par une erreur est pire qu'une erreur franche.
            echecs.append(f"{scope_kind}:{type(exc).__name__}")
            continue
        if not page.get("ok"):
            echecs.append(f"{scope_kind}:{page.get('code', 'UNKNOWN')}")
            continue
        for result in (page.get("results") or []):
            aid = str(result.get("artifact_id") or "").strip()
            if not aid or aid in merged:
                continue
            merged[aid] = {
                "provenance": _provenance_of(result),
                "kind": str(result.get("kind") or ""),
                "origin": scope_kind,
                "score": float(result.get("score") or 0.0),
                "excerpt": _excerpt_of(result),
            }

    if echecs and not merged:
        return {"ok": False, "code": "SPACE_SEARCH_FAILED",
                "workspace_id": workspace_id, "scopes_failed": echecs,
                "message": "Recherche d'espace indisponible : résultat inconnu, pas vide."}
    if echecs:
        # Partiellement exploitable : on rend ce qu'on a ET on signale les
        # portées qui n'ont pas répondu, pour que le appelant sache que la
        # page est incomplète.
        merged_result = _page(merged, workspace_id, query, limit, cursor, partial=echecs)
        merged_result["ok"] = True
        merged_result["partial"] = echecs
        return merged_result

    return _page(merged, workspace_id, query, limit, cursor, partial=None)


def _page(merged: dict[str, dict[str, Any]], workspace_id: str, query: str,
          limit: int, cursor: str | None, *, partial: list[str] | None) -> dict[str, Any]:
    """Tri déterministe puis fenêtrage. `partial` signale des portées muettes."""
    ranked = sorted(merged.values(), key=lambda r: (-r["score"], r["provenance"]))
    total = len(ranked)

    # Curseur lié à l'empreinte de l'ordre rendu, comme partout ailleurs
    # (`search/cursor.py`). Ici l'ordre ne dépend que du score FTS, mais le
    # corpus peut changer entre deux pages : on invalide donc explicitement
    # plutôt que de décaler en silence.
    fp = _cur.fingerprint(workspace_id, query.strip().lower(), total,
                          *[f"{r['provenance']}:{r['score']}" for r in ranked])
    start = 0
    if cursor:
        dec = _cur.decode(cursor)
        if not dec or dec.get("qh") != fp:
            return {"ok": False, "code": "SPACE_CURSOR_MISMATCH", "total": total,
                    "message": "Le corpus de l'espace a changé entre deux pages : "
                               "relance sans curseur."}
        try:
            start = max(0, int(dec.get("off", 0)))
        except Exception:
            start = 0

    page = ranked[start:start + max(1, int(limit))]
    out: dict[str, Any] = {
        "ok": True,
        "scope": "workspace_information",
        "workspace_id": workspace_id,
        "total": total,
        "returned_count": len(page),
        "cursor": start,
        "next_cursor": (_cur.encode(fp, start + len(page))
                        if start + len(page) < total else None),
        "has_more": start + len(page) < total,
        # Le fait que ce soit une recherche d'information, et pas une liste de
        # fichiers, se voit dans le résultat : aucun id n'y figure.
        "file_access": False,
        "results": [{
            "provenance": r["provenance"],
            "kind": r["kind"],
            "origin": r["origin"],
            "score": round(r["score"], 4),
            "excerpt": r["excerpt"],
        } for r in page],
    }
    if partial:
        out["partial"] = partial
    return out

# backend/ipc/images.py — commandes galerie d'images (pont IPC Electron).
# Chaque handler prend la session ORM et l'argument reçu du frontend, puis RETOURNE
# le payload : aucun emit ici, l'appelant émet. Les clés de retour sont snake_case
# (contrat `ImageListPage` de frontend/src/lib/ipcBridge.ts) et les arguments entrants
# sont lus en camelCase puis snake_case (contrat IPC Electron).
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

GALLERY_PAGE_LIMIT = 24
GALLERY_LIMIT_MAX = 48
GALLERY_ERROR_MAX_CHARS = 300

# Provenance lue dans artifact_cards.summary ; absente ⇒ None côté frontend.
PROVENANCE_KEYS = (
    "prompt",
    "checkpoint",
    "backend",
    "quality",
    "aspect_ratio",
    "width",
    "height",
    "size",
    "seed",
    "origin",
    "run_id",
    "mode",
    "source_artifact_id",
    "session_id",
    "revision",
    "work_sha256",
    "mask_sha256",
    "reference_sha256",
    "mask_kind",
)

_GALLERY_SQL = """
SELECT a.id AS artifact_id,
       COALESCE(a.display_name, a.original_filename, a.id) AS display_name,
       COALESCE(a.original_filename, '') AS original_filename,
       a.created_at AS created_at,
       COALESCE(b.size_bytes, 0) AS size_bytes,
       COALESCE(b.mime_detected, '') AS mime_detected,
       c.summary AS summary
FROM artifacts a
JOIN blobs b ON b.id = a.blob_id
LEFT JOIN artifact_cards c ON c.id = (
    SELECT c2.id FROM artifact_cards c2
    WHERE c2.artifact_id = a.id AND c2.is_current = 1
    ORDER BY c2.revision DESC, c2.id DESC LIMIT 1
)
WHERE a.kind = 'image'
  AND a.deleted_at IS NULL
  AND (
      EXISTS (
          SELECT 1 FROM artifact_links l
          WHERE l.artifact_id = a.id AND l.removed_at IS NULL
            AND l.scope_kind = 'library' AND l.role = 'generated'
      )
      OR (:include_imports = 1 AND EXISTS (
          SELECT 1 FROM artifact_links l
          WHERE l.artifact_id = a.id AND l.removed_at IS NULL
            AND l.scope_kind = 'conversation' AND l.role = 'attachment'
      ))
  )
  AND (:scope_id IS NULL OR EXISTS (
      SELECT 1 FROM artifact_links l
      WHERE l.artifact_id = a.id AND l.removed_at IS NULL AND l.scope_id = :scope_id
  ))
  AND (:space_id IS NULL OR EXISTS (
      SELECT 1 FROM artifact_links l
      JOIN workspaces w ON w.id = CASE
          WHEN l.scope_kind = 'workspace' THEN l.scope_id
          ELSE (SELECT c.workspace_id FROM conversations c
                WHERE c.id = l.scope_id AND c.deleted_at IS NULL)
      END
      WHERE l.artifact_id = a.id AND l.removed_at IS NULL
        AND l.scope_kind IN ('workspace', 'conversation')
        AND w.state = 'active' AND w.id = :space_id
  ))
  AND (:no_space = 0 OR NOT EXISTS (
      SELECT 1 FROM artifact_links l
      JOIN workspaces w ON w.id = CASE
          WHEN l.scope_kind = 'workspace' THEN l.scope_id
          ELSE (SELECT c.workspace_id FROM conversations c
                WHERE c.id = l.scope_id AND c.deleted_at IS NULL)
      END
      WHERE l.artifact_id = a.id AND l.removed_at IS NULL
        AND l.scope_kind IN ('workspace', 'conversation')
        AND w.state = 'active'
  ))
ORDER BY a.created_at DESC, a.id DESC
LIMIT :fetch_limit OFFSET :page_offset
"""

_LINKS_SQL = text(
    """
    SELECT artifact_id, scope_kind, scope_id, COALESCE(role, '') AS role
    FROM artifact_links
    WHERE artifact_id IN :artifact_ids AND removed_at IS NULL
    ORDER BY artifact_id ASC, scope_kind ASC, scope_id ASC, role ASC
    """
).bindparams(bindparam("artifact_ids", expanding=True))

# Espaces d'un artefact, groupés par artefact pour toute la page (une requête).
# Un artefact appartient à un espace par un lien direct `workspace` ou par un
# lien `conversation` vers une discussion non supprimée de cet espace ; l'espace
# doit lui-même être actif (pas en corbeille).
_SPACES_SQL = text(
    """
    SELECT DISTINCT l.artifact_id AS artifact_id,
           w.id AS space_id,
           w.name AS space_name
    FROM artifact_links l
    JOIN workspaces w ON w.id = CASE
        WHEN l.scope_kind = 'workspace' THEN l.scope_id
        ELSE (SELECT c.workspace_id FROM conversations c
              WHERE c.id = l.scope_id AND c.deleted_at IS NULL)
    END
    WHERE l.artifact_id IN :artifact_ids
      AND l.removed_at IS NULL
      AND l.scope_kind IN ('workspace', 'conversation')
      AND w.state = 'active'
    ORDER BY l.artifact_id ASC, w.name ASC, w.id ASC
    """
).bindparams(bindparam("artifact_ids", expanding=True))

_ARTIFACT_STATE_SQL = "SELECT kind, deleted_at FROM artifacts WHERE id = :artifact_id"
_SOFT_DELETE_ARTIFACT_SQL = (
    "UPDATE artifacts SET deleted_at = CURRENT_TIMESTAMP WHERE id = :artifact_id"
)
_SOFT_DELETE_LINKS_SQL = (
    "UPDATE artifact_links SET removed_at = CURRENT_TIMESTAMP "
    "WHERE artifact_id = :artifact_id AND removed_at IS NULL"
)

_TRUE_LITERALS = frozenset({"true", "1", "on"})
_FALSE_LITERALS = frozenset({"false", "0", "off"})


def _bounded(message: str, max_chars: int = GALLERY_ERROR_MAX_CHARS) -> str:
    """tronque un message d'erreur pour qu'il reste lisible côté UI."""
    return message[:max_chars]


def _root_message(exc: BaseException) -> str:
    """Message utile sans le SQL ni les valeurs liées que SQLAlchemy ajoute.

    `str(exc)` sur une erreur SQLAlchemy recopie la requête et ses paramètres :
    ce que la galerie ne doit jamais renvoyer au frontend.
    """
    origin = getattr(exc, "orig", None)
    return str(origin) if isinstance(origin, BaseException) else str(exc)


def _param(params: dict[str, Any], *names: str) -> Any:
    """Lit un argument en camelCase puis snake_case ; None si absent."""
    for name in names:
        value = params.get(name)
        if value is not None:
            return value
    return None


def _int_param(params: dict[str, Any], name: str, default: int) -> int:
    """Lit un entier à la frontière IPC : absent ⇒ défaut, sinon int strict."""
    raw = params.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} doit être un entier.")
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        text_value = raw.strip()
        if not text_value:
            return default
        try:
            return int(text_value)
        except ValueError as exc:
            raise ValueError(f"{name} doit être un entier.") from exc
    raise ValueError(f"{name} doit être un entier.")


def _bool_param(raw: Any, name: str) -> bool:
    """Lit un booléen à la frontière IPC (le frontend envoie un vrai booléen)."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        literal = raw.strip().lower()
        if literal in _TRUE_LITERALS:
            return True
        if literal in _FALSE_LITERALS:
            return False
    raise ValueError(f"{name} doit être un booléen.")


def _iso_text(value: Any) -> str | None:
    """created_at est une texte SQL pour le frontend ; datetime si relu brut."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _scalar(value: Any) -> Any:
    """Scalaire JSON uniquement : listes et objets ne sont pas de la provenance."""
    if value is None or isinstance(value, (list, dict)):
        return None
    return value


def _parse_card_summary(summary: Any) -> dict[str, Any]:
    """Provenance d'une fiche artefact ; un summary illisible vaut « aucune provenance »."""
    provenance: dict[str, Any] = dict.fromkeys(PROVENANCE_KEYS)
    if not isinstance(summary, str) or not summary.strip():
        return provenance
    try:
        document = json.loads(summary)
    except ValueError:
        return provenance
    if not isinstance(document, dict):
        return provenance
    for key in PROVENANCE_KEYS:
        provenance[key] = _scalar(document.get(key))
    return provenance


def _scopes_by_artifact(
    db: Session, artifact_ids: list[str]
) -> dict[str, list[dict[str, str]]]:
    """Rattachements vivants de la page, groupés par artefact (une requête, pas N)."""
    if not artifact_ids:
        return {}
    grouped: dict[str, list[dict[str, str]]] = {
        artifact_id: [] for artifact_id in artifact_ids
    }
    rows = db.execute(_LINKS_SQL, {"artifact_ids": artifact_ids}).mappings()
    for row in rows:
        grouped[str(row["artifact_id"])].append(
            {
                "scope_kind": str(row["scope_kind"]),
                "scope_id": str(row["scope_id"]),
                "role": str(row["role"]),
            }
        )
    return grouped


def _spaces_by_artifact(
    db: Session, artifact_ids: list[str]
) -> dict[str, list[dict[str, str]]]:
    """Espaces actifs de la page, groupés par artefact (une requête, pas N)."""
    if not artifact_ids:
        return {}
    grouped: dict[str, list[dict[str, str]]] = {
        artifact_id: [] for artifact_id in artifact_ids
    }
    seen: dict[str, set[str]] = {artifact_id: set() for artifact_id in artifact_ids}
    rows = db.execute(_SPACES_SQL, {"artifact_ids": artifact_ids}).mappings()
    for row in rows:
        artifact_id = str(row["artifact_id"])
        space_id = str(row["space_id"])
        if space_id in seen[artifact_id]:
            continue
        seen[artifact_id].add(space_id)
        grouped[artifact_id].append({"id": space_id, "name": str(row["space_name"])})
    return grouped


def _image_item(
    row: Any,
    scopes: list[dict[str, str]],
    spaces: list[dict[str, str]],
) -> dict[str, Any]:
    """Un item de galerie : colonnes de l'artefact + provenance + drapeaux dérivés."""
    provenance = _parse_card_summary(row["summary"])
    quality = provenance["quality"]
    normalized_quality = quality.strip().lower() if isinstance(quality, str) else ""
    roles = {(scope["scope_kind"], scope["role"]) for scope in scopes}
    has_library_generated = ("library", "generated") in roles
    has_conversation_attachment = ("conversation", "attachment") in roles
    deprecated = normalized_quality.startswith("unvalidated")
    return {
        "artifact_id": str(row["artifact_id"]),
        "display_name": str(row["display_name"]),
        "original_filename": str(row["original_filename"]),
        "created_at": _iso_text(row["created_at"]),
        "kind": "image",
        "size_bytes": max(0, int(row["size_bytes"] or 0)),
        "mime_detected": str(row["mime_detected"]),
        "scopes": scopes,
        "spaces": spaces,
        "is_import": has_conversation_attachment and not has_library_generated,
        "experimental": deprecated or "experimental" in normalized_quality,
        "deprecated": deprecated,
        **provenance,
    }


def image_list(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Page de la galerie : images générées (imports seulement si demandé)."""
    page_limit = max(
        1, min(_int_param(params, "limit", GALLERY_PAGE_LIMIT), GALLERY_LIMIT_MAX)
    )
    page_offset = _int_param(params, "offset", 0)
    if page_offset < 0:
        raise ValueError("offset doit être supérieur ou égal à zéro")
    raw_imports = _param(params, "includeImports", "include_imports")
    include_imports = (
        False if raw_imports is None else _bool_param(raw_imports, "include_imports")
    )
    raw_scope_id = _param(params, "scopeId", "scope_id")
    scope_id = raw_scope_id.strip() if isinstance(raw_scope_id, str) else None
    raw_space_id = _param(params, "spaceId", "space_id")
    space_id = (
        raw_space_id.strip()
        if isinstance(raw_space_id, str) and raw_space_id.strip()
        else None
    )
    raw_no_space = _param(params, "noSpace", "no_space")
    no_space = False if raw_no_space is None else _bool_param(raw_no_space, "no_space")
    if space_id is not None and no_space:
        raise ValueError("[BAD_ARGS] spaceId et noSpace sont exclusifs.")

    try:
        # Une ligne de plus que la page : c'est elle qui dit s'il reste des images.
        rows = (
            db.execute(
                text(_GALLERY_SQL),
                {
                    "include_imports": 1 if include_imports else 0,
                    "scope_id": scope_id or None,
                    "space_id": space_id,
                    "no_space": 1 if no_space else 0,
                    "fetch_limit": page_limit + 1,
                    "page_offset": page_offset,
                },
            )
            .mappings()
            .all()
        )
        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]
        page_ids = [str(row["artifact_id"]) for row in page_rows]
        scopes_by_artifact = _scopes_by_artifact(db, page_ids)
        spaces_by_artifact = _spaces_by_artifact(db, page_ids)
        items = [
            _image_item(
                row,
                scopes_by_artifact.get(str(row["artifact_id"]), []),
                spaces_by_artifact.get(str(row["artifact_id"]), []),
            )
            for row in page_rows
        ]
    except Exception as exc:
        raise RuntimeError(_bounded(f"Erreur galerie: {_root_message(exc)}")) from exc

    return {
        "items": items,
        "next_offset": page_offset + len(items),
        "has_more": has_more,
    }


def image_delete(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Suppression logique : l'artefact part, les octets du blob restent intacts.

    `foreign_keys=ON` est posé par l'écouteur `connect` de backend/db/database.py ;
    SQLite l'ignore dans une transaction ouverte, donc aucun PRAGMA ici.
    """
    raw_artifact_id = _param(params, "artifactId", "artifact_id")
    artifact_id = raw_artifact_id.strip() if isinstance(raw_artifact_id, str) else ""
    if not artifact_id:
        raise ValueError("Image introuvable.")
    try:
        row = (
            db.execute(text(_ARTIFACT_STATE_SQL), {"artifact_id": artifact_id})
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("Image introuvable.")
        if str(row["kind"]) != "image":
            raise ValueError(
                "Seules les images peuvent être supprimées depuis la galerie."
            )
        if row["deleted_at"] is None:
            db.execute(text(_SOFT_DELETE_ARTIFACT_SQL), {"artifact_id": artifact_id})
            db.execute(text(_SOFT_DELETE_LINKS_SQL), {"artifact_id": artifact_id})
        db.commit()
    except ValueError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise RuntimeError(
            _bounded(f"Suppression impossible: {_root_message(exc)}")
        ) from exc
    # INVALIDATION GLOBALE, APRÈS le commit : l'image supprimée peut être la
    # source d'une fiche dans PLUSIEURS espaces. La synchronisation applique la
    # règle « stale seulement si toutes les sources sont perdues », donc une
    # fiche qui garde une autre source reste simplement en provenance partielle.
    try:
        from backend.services import memoire as _memoire

        _memoire.invalider_artefact_global(db, artifact_id)
    except Exception:  # noqa: BLE001 - la suppression est déjà committée
        log.warning(
            "[images] invalidation mémoire impossible pour %s",
            artifact_id,
            exc_info=True,
        )
    return {"ok": True, "artifact_id": artifact_id}


def image_rename(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Renomme le nom affiché d'une image, sans toucher à son identité.

    Même écriture que `espaces.renommer_depot` : `display_name` change et une
    révision de fiche porte le nouveau titre ; `original_filename`, le blob et
    les liens restent intacts. Un nom identique est idempotent (aucune révision).
    """
    from backend.db.models import Artifact as _Art

    raw_artifact_id = _param(params, "artifactId", "artifact_id")
    artifact_id = raw_artifact_id.strip() if isinstance(raw_artifact_id, str) else ""
    raw_name = _param(params, "name", "display_name", "displayName")
    clean = raw_name.strip() if isinstance(raw_name, str) else ""
    if not clean:
        raise ValueError("[NAME_REQUIRED] Le nom de l’image ne peut pas être vide.")
    if len(clean) > 200:
        raise ValueError(
            "[NAME_TOO_LONG] Le nom de l’image est limité à 200 caractères."
        )

    try:
        art = db.query(_Art).filter(_Art.id == artifact_id).first()
        if art is None or art.deleted_at is not None:
            raise ValueError("Image introuvable.")
        if str(art.kind) != "image":
            raise ValueError(
                "Seules les images peuvent être renommées depuis la galerie."
            )
        if art.display_name == clean:
            return {
                "ok": True,
                "artifact_id": artifact_id,
                "changed": False,
                "display_name": art.display_name,
            }
        art.display_name = clean
        from backend.search import cards as _cards

        _cards.revise_card_on_rename(db, art, clean)
        db.commit()
    except ValueError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise RuntimeError(
            _bounded(f"Renommage impossible: {_root_message(exc)}")
        ) from exc
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "changed": True,
        "display_name": clean,
    }


def image_set_backend(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Bascule l'opt-in du backend image (fichier-drapeau lu par flux_spike).

    `db` est inutilisé : l'opt-in vit sur disque, jamais en base.
    """
    del db
    raw_enabled = params.get("enabled")
    if raw_enabled is None:
        raise ValueError("Paramètre 'enabled' manquant.")
    enabled = _bool_param(raw_enabled, "enabled")
    try:
        from backend.services import flux_spike

        flux_spike.set_backend_enabled(enabled)
    except OSError as exc:
        raise RuntimeError(
            _bounded(
                f"Impossible de {'activer' if enabled else 'désactiver'} "
                f"le backend image: {exc}"
            )
        ) from exc
    return {"enabled": enabled}


def image_generate_options(params: dict[str, Any]) -> dict[str, Any]:
    """Options optionnelles transmises à services.image_service.generate."""
    out: dict[str, Any] = {}
    origin = _param(params, "origin")
    if origin is not None:
        out["origin"] = origin
    source_transform = _param(params, "sourceTransform", "source_transform")
    if source_transform is not None:
        out["source_transform"] = source_transform
    source_size = _param(params, "sourceSize", "source_size")
    if source_size is not None:
        out["source_size"] = source_size
    return out

# tools/find_files.py — Résolution de fichier (spec 01/02)
# Schéma : query ≤150 (dure ColBERT) et/ou types/recency, au moins un critère. Pas d'evidence_id en sortie.
# Robustesse spec 02 : EMPTY_QUERY / TOO_LONG / ENUM_INVALID / UNKNOWN_ARG / EMPTY_CALL,
# ambiguïté <0.05 + captions, boost nom exact rank 1, tri date desc sans texte, near-miss, registre vide.
#
# TROIS FAÇONS DE RETROUVER UN FICHIER (Bible §2.5, l. 147) : par son NOM, par son
# CONTENU (la recherche sémantique ci-dessous), ou par une RÉFÉRENCE de
# l'utilisateur — « la dernière image que je t'ai envoyée » (l. 153). La troisième
# n'est pas une quatrième recherche : elle lit un ORDINAL dans la demande et le
# résout dans l'ordre d'ajout des pièces jointes de CETTE discussion.
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator, field_validator

from backend.config.defaults import TOOL_LIMITS
from backend.tools.base import BaseTool, ToolContext, ToolResult
from backend.tools.errors import ToolErrorCode

if TYPE_CHECKING:  # le vocabulaire des références vit dans `media_policy`, chargé à l'usage
    from backend.inference.media_policy import ReferenceMedia

_ALLOWED_TYPES = ("image", "pdf", "sheet", "text")
_ALLOWED_RECENCY = ("recent", "today")
_PUBLIC_ID_RE = re.compile(r"doc_[1-9]\d*\Z")
_TECHNICAL_ID_RE = re.compile(r"(?:art_|sti_)[A-Za-z0-9_]+\Z", re.IGNORECASE)


def _public_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _PUBLIC_ID_RE.fullmatch(candidate) else ""


def _public_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or _TECHNICAL_ID_RE.fullmatch(name):
        return "fichier"
    return name


def _public_text(value: object) -> str:
    return _TECHNICAL_ID_RE.sub("[identifiant interne retiré]", str(value or ""))


def _type_de(nom: object, kind: object) -> str:
    """Type du fichier pour CE schéma (image | pdf | sheet | text).

    WHY l'extension prime sur `kind` pour les tableurs : les fichiers hérités
    portent `kind="text"` alors que leur extension dit la vérité. Un seul endroit
    tranche, sinon « la dernière image » et le filtre `type=["sheet"]` ne
    regarderaient pas le même champ et divergeraient.
    """
    if str(nom or "").lower().endswith((".xlsx", ".ods", ".csv")):
        return "sheet"
    return str(kind or "text").lower()


def _publies_nommes(results: list[dict]) -> list[str]:
    """`doc_N` que cette page NOMME, dans l'ordre du rendu, sans doublon. Pur.

    WHY ces identifiants sortent sur le `ToolResult` et pas seulement dans `content` :
    `BaseTool.call()` tronque `content` à `max_output_chars` et couper un JSON en
    plein milieu le rend illisible. La boucle doit pouvoir citer une réponse
    tronquée, donc elle lit `public_ids` sur un champ à part — c'est ce que font
    déjà `SearchInfoTool` et `AskFilesTool`.
    """
    vus: list[str] = []
    for r in results or []:
        public_id = _public_id(r.get("public_id") or r.get("artifact_id") or r.get("doc_label"))
        if public_id and public_id not in vus:
            vus.append(public_id)
    return vus


def _public_mapping(db, conv_id: str, allowed: set[str]) -> dict[str, str]:
    """Construit la mapping interne vers le label doc_N local et l'enregistre."""
    from backend.search import docrefs as _docrefs

    try:
        _docrefs.backfill_from_links(db, conv_id)
    except Exception:
        pass
    try:
        return {
            str(row.get("artifact_id") or "").strip(): _public_id(row.get("public_id") or row.get("doc_label"))
            for row in _docrefs.list_for_conv(db, conv_id)
            if str(row.get("artifact_id") or "").strip() in allowed
            and _public_id(row.get("public_id") or row.get("doc_label"))
        }
    except Exception:
        return {}


# `origin` : la portée qui autorise CETTE discussion à nommer le fichier.
# Le dépôt de l'espace n'est pas « joint à ce message » : sans cette distinction,
# l'orchestrateur répondrait « voici le fichier que tu m'as envoyé » sur un fichier
# que l'utilisateur n'a jamais joint ici.
_ORIGINE_FR = {"conversation": "cette discussion", "workspace": "dépôt de l'espace"}


def _origines(db, conv_id: str, artifact_ids: list[str]) -> dict[str, str]:
    """Portée de chaque fichier, lue dans les liens actifs — jamais devinée.

    Un fichier joint à cette discussion reste « cette discussion » même s'il est
    aussi versé au dépôt : c'est l'origine que `docrefs` retient pour son `doc_N`,
    et les deux doivent dire la même chose.
    """
    ids = [str(a) for a in artifact_ids if str(a or "").strip()]
    if not ids:
        return {}
    from backend.db.models import ArtifactLink as _Link
    try:
        rows = db.query(_Link.artifact_id, _Link.scope_kind, _Link.scope_id).filter(
            _Link.artifact_id.in_(ids), _Link.removed_at.is_(None),
        ).all()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for artifact_id, scope_kind, scope_id in rows:
        if str(scope_kind or "") == "conversation" and str(scope_id or "") == str(conv_id or ""):
            out[str(artifact_id)] = "conversation"
    for artifact_id, scope_kind, _scope_id in rows:
        if str(scope_kind or "") == "workspace":
            out.setdefault(str(artifact_id), "workspace")
    return out


def _nom_public(db, conv_id: str, artifact_id: str, cache: dict[str, str]) -> str:
    """``doc_N`` connu, ou alloué MAINTENANT parce que cette ligne va être rendue.

    C'est le contrat d'allocation paresseuse : un dépôt de cinquante fichiers ne
    coûte un numéro que pour ceux que la discussion montre. Un fichier sans label
    ne se perd donc pas — il se nomme au moment où la discussion choisit de le
    montrer, et pas avant.
    """
    artifact_id = str(artifact_id or "").strip()
    if not artifact_id:
        return ""
    known = cache.get(artifact_id, "")
    if known:
        return known
    from backend.search import docrefs as _docrefs

    try:
        label = _public_id(_docrefs.public_id_for_artifact(db, conv_id, artifact_id))
    except Exception:
        return ""
    if not label:
        return ""
    try:
        db.commit()  # un numéro attribué doit survivre à la fermeture de la session
    except Exception:
        return ""
    cache[artifact_id] = label
    return label


# ---------- RÉFÉRENCE DE L'UTILISATEUR : « la dernière image que je t'ai envoyée » ----------
#
# LE CŒUR DE LA CHOSE n'est pas le mot « dernière » : c'est un ORDRE TOTAL des pièces
# jointes de la discussion. Sans lui, « la dernière » est une devinette qui change
# d'un appel à l'autre. Trois termes, du plus signifiant au moins signifiant :
#
#   1. `Message.created_at` — l'horloge de l'UTILISATEUR. Seule elle dit QUAND il a
#      envoyé le fichier. Les deux autres horloges sont celles de la machine :
#      `MessageAttachment.created_at` est l'écriture de la ligne, et `Artifact.created_at`
#      l'import. Les trois se ressemblent, donc les départager au hasard choisirait
#      parfois l'import au lieu de l'envoi.
#   2. `MessageAttachment.ordinal` — le rang dans le message, écrit par `main.py`
#      comme `enumerate(attachment_ids)` : l'ordre réel à l'écran. Il n'ordonne
#      qu'à l'intérieur d'un message, d'où le terme 1 devant lui.
#   3. `MessageAttachment.id` — le DÉPARTAGE. Les identifiants sont uniques, donc le
#      triplet est TOTAL : à base égale, deux exécutions rendent le même fichier. Il
#      ne dit RIEN de l'intention de l'utilisateur, et c'est pourquoi
#      `_ref_non_separable` signale les cas où c'est lui seul qui tranche.
#
# MESURÉ, pas supposé : `Message.created_at` est écrit par `server_default=func.now()`,
# donc en SQLite il est résolu à la SECONDE. Deux pièces jointes dans la même seconde
# partagent le terme 1 — le cas n'est pas rare, il est ordinaire.


def _pieces_de_la_discussion(db, conv_id: str, depuis_utilisateur: bool) -> list[dict]:
    """Pièces jointes de CETTE discussion, dans l'ordre total d'ajout. Une ligne par pièce.

    L'ordre est le MÊME que celui de `loop.build_history_artifact_entries`, qui construit le
    registre historique : les deux lecteurs de l'ordre de la discussion doivent dire la même
    chose, sinon « la dernière image » désignerait deux fichiers dans un même tour.

    Le DÉPÔT de l'espace n'apparaît pas et ne peut pas apparaître : un fichier versé
    au dépôt n'a de ligne `MessageAttachment` que s'il a été montré dans un message,
    et il n'en a pas. C'est ce qui rend « la dernière image » une référence de
    DISCUSSION, et non une recherche dans tout ce que la discussion peut lire.

    `depuis_utilisateur` restreint aux messages de RÔLE `user` : c'est la lecture de
    « que je t'ai envoyée ». Le directeur d'une image générée par l'application est
    écrit dans un message `assistant` (`main.py`, finalisation de trace) — sans ce
    filtre, « la dernière image que je t'ai envoyée » rendrait une image que
    l'utilisateur n'a jamais envoyée.
    """
    from backend.db.models import Artifact as _Art, Message as _Msg, MessageAttachment as _MA

    if not conv_id:
        return []
    conditions = [_Msg.conv_id == str(conv_id)]
    if depuis_utilisateur:
        conditions.append(_Msg.role == "user")
    rows = (
        db.query(_MA.artifact_id, _MA.ordinal, _MA.id, _Msg.created_at)
        .join(_Msg, _Msg.id == _MA.message_id)
        .join(_Art, _Art.id == _MA.artifact_id)
        .filter(*conditions, _Art.deleted_at.is_(None))
        .order_by(_Msg.created_at.asc(), _MA.ordinal.asc(), _MA.id.asc())
        .all()
    )
    # La clé est une CHAÎNE ISO, pas un `datetime` : deux horizons (naif / conscient) ou
    # deux NULL y donneraient un `TypeError` à la comparaison, c'est-à-dire une réponse
    # qui plante au lieu de répondre. Une clé textuelle se compare toujours.
    return [{"artifact_id": str(artifact_id),
             "cle": (created_at.isoformat() if created_at else "", int(ordinal), str(matt_id))}
            for artifact_id, ordinal, matt_id, created_at in rows]


def _ref_non_separable(pieces: list[dict], position: int) -> bool:
    """L'ordinal visé ne se distingue pas de son voisin par l'ordre UTILE. Pur.

    Les termes 1 et 2 portent l'intention de l'utilisateur ; le terme 3 ne porte
    que la stabilité. Quand deux pièces voisines partagent ces deux termes, le choix
    entre elles est ARBITRAIRE : la réponse est bien rendue — il le faut, pour ne
    jamais laisser l'utilisateur sans rien — mais elle est marquée comme incertaine.
    """
    if position < 0 or position >= len(pieces):
        return False
    utile = pieces[position]["cle"][:2]
    for voisin in (position - 1, position + 1):
        if 0 <= voisin < len(pieces) and pieces[voisin]["cle"][:2] == utile:
            return True
    return False


def _type_des_pieces(db, pieces: list[dict]) -> list[dict]:
    """Copie des pièces,NOM et TYPE renseignés. Base: lecture des artefacts. Ne mute rien."""
    from backend.db.models import Artifact as _Art

    ids = sorted({p["artifact_id"] for p in pieces})
    noms: dict[str, tuple[str, str]] = {}
    for artifact in db.query(_Art).filter(_Art.id.in_(ids)).all() if ids else ():
        nom = _public_text(_public_name(artifact.original_filename or artifact.display_name))
        noms[str(artifact.id)] = (nom, _type_de(nom, artifact.kind))
    return [{**p, "name": noms[p["artifact_id"]][0], "type": noms[p["artifact_id"]][1]}
            if p["artifact_id"] in noms else p for p in pieces]


def _ref_reste_ambigue(reference: "ReferenceMedia", conformes: list[dict],
                       toutes: list[dict]) -> str:
    """Pourquoi la référence ne se résout pas, en mots — ou "" si elle se résout. Pur.

    Les trois motifs sont distincts et ne se recouvrent pas : ce n'est pas la même
    chose qu'aucun fichier ne soit joint (le dossier est vide), que le type demandé
    soit absent (des fichiers existent, mais pas de ce type — il faut le dire, sinon
    l'orchestrateur conclura « l'utilisateur n'a rien envoyé »), et que la position
    demandée dépasse la liste (« la deuxième image » alors qu'il n'y en a qu'une).
    """
    from backend.inference.media_policy import index_reference, intitule_reference

    if not toutes:
        return "aucun fichier n'est joint à cette discussion."
    if not conformes:
        presents = ", ".join(sorted({str(p.get("type") or "?") for p in toutes}))
        return (f"aucun fichier de type {' ou '.join(reference.types)} n'est joint à cette "
                f"discussion ; elle contient {presents}.")
    if index_reference(reference, len(conformes)) is None:
        return (f"« {intitule_reference(reference)} » ne correspond à aucun des "
                f"{len(conformes)} fichiers de ce type joints à cette discussion.")
    return ""


class FindFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    types: list[str] | None = None
    recency: str | None = None

    @field_validator("types")
    @classmethod
    def _check_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for t in v:
            if str(t) not in _ALLOWED_TYPES:
                raise ValueError("[ENUM_INVALID] Argument mal rempli : types = liste parmi image, pdf, sheet, text ; recency ∈ recent, today.")
        return list(v)

    @field_validator("recency")
    @classmethod
    def _check_recency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if str(v) not in _ALLOWED_RECENCY:
            raise ValueError("[ENUM_INVALID] Argument mal rempli : types = liste parmi image, pdf, sheet, text ; recency ∈ recent, today.")
        return v

    @model_validator(mode="after")
    def _check_criteria(self):
        t = (self.query or "").strip()
        if t and len(t) > int(TOOL_LIMITS["query_max_chars"]):
            raise ValueError("[TOO_LONG] Le texte de recherche est limité à 150 caractères maximum — reformule en mots-clés courts.")
        if not t and not self.types and not self.recency:
            raise ValueError("[EMPTY_QUERY] Écris une description courte (≤ 150 caractères) ou utilise types/recency seuls.")
        object.__setattr__(self, "query", t or None)
        return self


def _norm_name(s: str) -> str:
    import unicodedata as _u
    q = _u.normalize("NFC", s or "").lower()
    q = "".join(c for c in _u.normalize("NFD", q) if not _u.combining(c))
    return "".join(c if c.isalnum() else "" for c in q)


_TYPE_LABEL = {"image": "image", "pdf": "pdf", "sheet": "tableur", "text": "texte"}


def _ligne_resultat(r: dict) -> str:
    """Une fiche rendue en une ligne : rang, nom, type, date, `doc_N`, origine, titre. Pur."""
    public_id = _public_id(r.get("public_id") or r.get("artifact_id") or r.get("doc_label"))
    if not public_id:
        return ""
    typ = _TYPE_LABEL.get(str(r.get("type", "")), str(r.get("type", "")))
    date = ""
    try:
        dd = str(r.get("date", ""))
        if len(dd) >= 10:
            date = f"{dd[8:10]}/{dd[5:7]}"
    except Exception:
        pass
    name = _public_text(_public_name(r.get("name")))
    titre = _public_text(_public_name(r.get("title"))) if r.get("title") else ""
    origine = _ORIGINE_FR.get(str(r.get("origin") or ""), "")
    ligne = f"{r.get('rank')}. {name} — {typ}, {date} — id {public_id}"
    if origine:
        ligne += f" — {origine}"
    return ligne + (f" — {titre}" if titre else "")


def render_find_files(results: list[dict], registry_size: int, ambiguous: bool, query: str | None, filt_types: list[str] | None) -> str:
    """Rendu texte spec 02 (listes, near-miss, registre vide, captions si ambigu). Pur."""
    if registry_size == 0:
        return ("[find_files] aucun fichier n'est enregistré pour l'instant.\n"
                "Suggestion : réponds depuis tes connaissances, ou demande une clarification à l'utilisateur.\n[fin]")
    if not results:
        if filt_types:
            return (f"[find_files] aucune {filt_types[0]} ne correspond.\n"
                    f"Par contre : voir suggestions type/recency.\n"
                    "Vérifie avec l'utilisateur s'il cherchait un autre type.\n[fin]")
        q = _public_text(query or "").strip()
        return (f"[find_files] 1 recherche — aucun fichier ne correspond à « {q} ».\n"
                "Suggestions : reformule le nom, cherche par type avec types et recency, ou cherche dans le contenu avec search_info.\n[fin]")
    lines = [f"[find_files] {len(results)} correspondance{'s' if len(results) > 1 else ''}"]
    for r in results:
        ligne = _ligne_resultat(r)
        if ligne:
            lines.append(ligne)
        if ambiguous and r.get("caption"):
            lines.append(f"   « {_public_text(r.get('caption'))} »")
    wide = any("recherche large" in str(r.get("note", "")) for r in results)
    if wide:
        lines.append("Note : recherche large — précise le nom ou le type.")
    lines.append("[fin] toutes les correspondances affichées")
    return "\n".join(lines)


# Rendu du mode `reference`. La NOTE sur le dépôt est TOUJOURS présente : une
# référence d'ajout ne parle que des pièces jointes de la discussion, et le dire est
# la seule façon d'éviter que l'orchestrateur conclude « ce fichier du dépôt n'existe
# pas » en lisant une absence qui n'en est pas une.
_NOTE_DEPOT = ("Note : une référence d'ajout porte sur les pièces jointes de CETTE "
               "discussion ; le dépôt de l'espace n'est pas concerné.")


def render_reference(reference: dict, results: list[dict], motif: str, ambiguous: bool) -> str:
    """Rendu d'une référence d'ordre : le fichier visé, ou pourquoi il n'y en a pas. Pur.

    L'AMBIGUÏTÉ est écrite, jamais dévorée : quand le choix entre deux pièces
    jointes de même horodatage n'est pas une connaissance mais un départage, le
    modèle doit pouvoir le dire à l'utilisateur au lieu de l'affirmer comme un fait.
    """
    demande = reference.get("demande") or "cette référence"
    if not results:
        lignes = [f"[find_files] « {demande} » : {motif}",
                  "Vérifie avec l'utilisateur, ou reformule la demande.",
                  _NOTE_DEPOT,
                  "[fin]"]
        return "\n".join(lignes)
    lignes = [f"[find_files] « {demande} » → {len(results)} fichier"]
    lignes.extend(ligne for ligne in (_ligne_resultat(r) for r in results) if ligne)
    if ambiguous:
        lignes.append("Ambiguïté : deux pièces jointes de cette discussion portent le même "
                      "horodatage et le même rang ; le choix entre elles est un départage, "
                      "pas une certitude. Confirme avec l'utilisateur que c'est bien celle-là.")
    lignes.append(_NOTE_DEPOT)
    lignes.append("[fin] référence résolue")
    return "\n".join(lignes)


class FindFilesTool(BaseTool[FindFilesArgs]):
    name = "find_files"
    description = (
        "Retrouver un fichier pour obtenir son public_id doc_N — par nom, fiche, caption, type ou récence. "
        "Sert aussi de référence d'ajout : query = « la dernière image que je t'ai envoyée » "
        "la rend sans que l'utilisateur se souvienne du nom. "
        "Rend public_id(s) + fiche(s) (nom, type, date, titre). Pas d'extrait, pas d'evidence_id. "
        "Au moins un critère : query (≤150 caractères), types ou recency."
    )
    input_model = FindFilesArgs
    timeout_s = 5.0
    max_output_chars = 4000

    async def execute(self, args: FindFilesArgs, ctx: ToolContext) -> ToolResult:
        import json as _json
        from datetime import datetime, timedelta, timezone
        try:
            from backend.search import scope as _scope, fts as _fts
            from backend.db.database import SessionLocal as _SL
            from backend.inference.media_policy import reference_fichier
            conv_id = ctx.conv_id or ctx.request_id or ""
            allowed = _scope.resolve_allowed_artifacts(conv_id)
            registry_size = len(allowed)
            # AVANT le registre vide : « la dernière image » sur une discussion sans
            # fichier mérite un motif qui le DIT, pas le « aucun fichier » générique qui
            # ferait croire à une absence d'outil plutôt que d'absence de pièce jointe.
            reference = reference_fichier(args.query or "")
            if reference is not None:
                return _resoudre_reference(reference, args, conv_id, registry_size)
            if registry_size == 0:
                payload = {"tool": "find_files", "ok": True, "mode": "list" if not args.query else "semantic",
                           "ambiguous": False, "candidates": 0, "returned": 0, "results": [], "registry_size": 0}
                return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                                  display_summary=render_find_files([], 0, False, args.query, args.types),
                                  public_ids=[])
            db = _SL()
            try:
                _fts.ensure_fts(db)
                public_map = _public_mapping(db, conv_id, allowed)
                # Tri sans texte : déterministe par date décroissante, aucun modèle (spec 02)
                if not args.query:
                    from backend.db.models import Artifact as _Art
                    from backend.search import cards as _cards
                    arts = db.query(_Art).filter(_Art.id.in_(sorted(allowed))).all() if allowed else []
                    if args.types:
                        wanted = {str(t).lower() for t in args.types}
                        arts = [a for a in arts
                                if _type_de(a.original_filename or a.display_name, a.kind) in wanted]
                    if args.recency:
                        hours = int(TOOL_LIMITS["today_hours"]) if args.recency == "today" else int(TOOL_LIMITS["recent_days"]) * 24
                        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                        filt = []
                        for a in arts:
                            try:
                                c = a.created_at
                                if c is not None:
                                    if c.tzinfo is None:
                                        c = c.replace(tzinfo=timezone.utc)
                                    if c >= cutoff:
                                        filt.append(a)
                            except Exception:
                                continue
                        arts = filt
                    try:
                        arts.sort(key=lambda a: (a.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
                    except Exception:
                        pass
                    results = []
                    # Troncature AVANT nommage : le contexte de l'orchestrateur est
                    # borné par `render_limit`, donc un dépôt de cinquante fichiers
                    # ne peut coûter que cinq numéros — ceux qu'on affiche.
                    affiches = arts[: int(TOOL_LIMITS["render_limit"])]
                    origines = _origines(db, conv_id, [str(a.id) for a in affiches])
                    for a in affiches:
                        public_id = _nom_public(db, conv_id, str(a.id), public_map)
                        if not public_id:
                            continue
                        rank = len(results) + 1
                        card = _cards.get_current_card(db, a.id)
                        nm = _public_text(_public_name(a.original_filename or a.display_name))
                        kd = _type_de(nm, a.kind)
                        results.append({"rank": rank, "artifact_id": public_id, "public_id": public_id,
                                        "name": nm, "type": kd,
                                        "date": a.created_at.date().isoformat() if a.created_at else "",
                                        "title": _public_text(_public_name((card.title if card else None) or nm)),
                                        "caption": _public_text((card.summary if card else None) or ""),
                                        "origin": origines.get(str(a.id)) or "conversation",
                                        "matched_on": ["type"] if args.types else ["recency"],
                                        "score": 1.0})
                    payload = {"tool": "find_files", "ok": True, "mode": "list", "ambiguous": False,
                               "candidates": len(arts), "returned": len(results), "results": results,
                               "registry_size": registry_size}
                    return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                                      display_summary=render_find_files(results, registry_size, False, args.query, args.types),
                                      public_ids=_publies_nommes(results))
                # Avec texte : recherche sémantique (FTS en attendant ColBERT) + boost nom exact + ambiguïté.
                # `search_scopes` et non `search` : le périmètre d'un chat d'espace a
                # deux portées, et un fichier du dépôt n'existe que sous la seconde.
                page = None
                for _attempt in range(2):
                    try:
                        page = _fts.search_scopes(db, query=args.query or "", conversation_id=conv_id,
                                                  allowed_ids=allowed, types=None, limit=10)
                        break
                    except Exception:
                        if _attempt == 1:
                            raise
                raw = (page.get("results") or []) if (page is not None and page.get("ok")) else []
                # Pré-filtre type/recency côté serveur
                if args.types:
                    wanted = {str(t).lower() for t in args.types}
                    tmp = [r for r in raw if _type_de(r.get("name"), r.get("kind")) in wanted]
                    if not tmp:
                        # Near-miss : autres types récents. Les ids cités restent ceux
                        # DÉJÀ nommés : nommer des fichiers que la discussion n'a pas
                        # choisis dépenserait son contexte pour rien.
                        try:
                            alt = _fts.search_scopes(db, query=args.query or "", conversation_id=conv_id,
                                                     allowed_ids=allowed, limit=5)
                            alts = (alt.get("results") or [])[:3]
                            public_ids = [
                                public_map.get(str(a.get("artifact_id") or "").strip(), "")
                                for a in alts
                            ]
                            public_ids = [public_id for public_id in public_ids if public_id]
                            ids = ", ".join(public_ids) if public_ids else "—"
                            txt = (f"[find_files] aucune {args.types[0]} ne correspond.\nPar contre : {len(public_ids)} pdf récents ({ids}).\n"
                                   "Vérifie avec l'utilisateur s'il cherchait un pdf.\n[fin]")
                            payload = {"tool": "find_files", "ok": True, "mode": "semantic", "ambiguous": False,
                                       "candidates": 0, "returned": 0, "results": [], "registry_size": registry_size}
                            return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                                              display_summary=txt, public_ids=list(public_ids))
                        except Exception:
                            pass
                    raw = tmp
                if args.recency:
                    hours = int(TOOL_LIMITS["today_hours"]) if args.recency == "today" else int(TOOL_LIMITS["recent_days"]) * 24
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                    from backend.db.models import Artifact as _Art2
                    tmp = []
                    for r in raw:
                        try:
                            a = db.query(_Art2).filter(_Art2.id == r.get("artifact_id")).first()
                            if a is None or a.created_at is None:
                                continue
                            c = a.created_at
                            if c.tzinfo is None:
                                c = c.replace(tzinfo=timezone.utc)
                            if c >= cutoff:
                                tmp.append(r)
                        except Exception:
                            continue
                    raw = tmp
                # Boost nom exact : texte == nom de fichier après normalisation → rank 1 (spec 02)
                qn = _norm_name(args.query or "")
                def _is_exact(r: dict) -> bool:
                    return _norm_name(str(r.get("name", "")).rsplit(".", 1)[0]) == qn or _norm_name(str(r.get("name", ""))) == qn
                raw.sort(key=lambda r: (0 if _is_exact(r) else 1, -float(r.get("score", 0.0))))
                # Ambiguïté : écart top1-top2 < 0.05 → captions (spec 02)
                ambiguous = False
                try:
                    if len(raw) >= 2:
                        ambiguous = abs(float(raw[0].get("score", 0)) - float(raw[1].get("score", 0))) < float(TOOL_LIMITS["ambiguity_gap"])
                except Exception:
                    pass
                from backend.search import cards as _cards2
                results = []
                affiches = raw[: int(TOOL_LIMITS["render_limit"])]
                origines = _origines(db, conv_id, [str(r.get("artifact_id") or "") for r in affiches])
                for r in affiches:
                    aid = str(r.get("artifact_id") or "").strip()
                    public_id = _nom_public(db, conv_id, aid, public_map)
                    if not public_id:
                        continue
                    rank = len(results) + 1
                    card = _cards2.get_current_card(db, aid)
                    nm = _public_text(_public_name(r.get("name")))
                    kd = _type_de(nm, r.get("kind"))
                    try:
                        from backend.db.models import Artifact as _A3
                        a3 = db.query(_A3).filter(_A3.id == aid).first()
                        date = a3.created_at.date().isoformat() if a3 and a3.created_at else ""
                    except Exception:
                        date = ""
                    results.append({"rank": rank, "artifact_id": public_id, "public_id": public_id,
                                    "name": nm, "type": kd, "date": date,
                                    "title": _public_text(_public_name((card.title if card else None) or nm)),
                                    "caption": _public_text((card.summary if card else None) or ""),
                                    "origin": origines.get(aid) or "conversation",
                                    "matched_on": r.get("match_reasons") or ["name", "caption"],
                                    "score": round(float(r.get("score", 0.0)), 2)})
                payload = {"tool": "find_files", "ok": True, "mode": "semantic", "ambiguous": bool(ambiguous),
                           "candidates": len(raw), "returned": len(results), "results": results,
                           "registry_size": registry_size}
                return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                                  display_summary=render_find_files(results, registry_size, bool(ambiguous), args.query, args.types),
                                  public_ids=_publies_nommes(results))
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except TimeoutError as e:
            # Précédent ask_files : le filet `except Exception` déguisait TOUTE
            # panne (KeyError, erreur DB) en `délai dépassé` — un motif FAUX que
            # la boucle relançait. Seul le délai VRAI garde ce nom ici ; toute
            # autre panne atteint le filet honnête de `BaseTool.call`
            # (`[INTERNAL_ERROR] {type}`), que le déclencheur de la boucle attend.
            return ToolResult(ok=False, display_summary="find_files: timeout",
                              error=f"[{ToolErrorCode.TIMEOUT}] délai dépassé. Action : réessaie, ou cherche par type et recency sans texte. ({str(e)[:120]})")


def _resoudre_reference(reference: "ReferenceMedia", args: FindFilesArgs, conv_id: str,
                       registry_size: int) -> ToolResult:
    """« la dernière image que je t'ai envoyée » → le `doc_N` de CETTE pièce jointe.

    Bible §2.5 l. 153 et l. 167 : retrouver un fichier ne doit pas exiger que
    l'utilisateur se souvienne de son nom. C'est une RÉSOLUTION D'IDENTITÉ, donc une
    seule réponse ou un motif d'absence — jamais une liste, jamais un choix par défaut.

    Les TROIS filtres de la demande sont appliqués, dans cet ordre : la direction
    (« que je t'ai envoyée » → messages de rôle `user`), le type nommé par les mots,
    et l'ordinal. Filtrer le type AVANT de compter est indispensable : sans cela
    « la dernière image » d'une discussion [image, pdf] rend le pdf.
    """
    import json as _json
    from backend.db.database import SessionLocal as _SL
    from backend.inference.media_policy import index_reference
    from backend.search import cards as _cards

    db = _SL()
    try:
        # 1. La DIRECTION et le périmètre DROIT : quelles pièces de CETTE discussion
        #    l'utilisateur a-t-il montrées, et lesquelles sont encore accessibles ?
        #    Une pièce jointe mise à la corbeille garde sa ligne `MessageAttachment`
        #    et perd son lien actif — sans ce filtre, « la dernière image » rendrait un
        #    fichier que l'utilisateur a retiré.
        toutes = _pieces_de_la_discussion(db, conv_id, reference.sent_by_user)
        origines = _origines(db, conv_id, [p["artifact_id"] for p in toutes])
        toutes = _type_des_pieces(
            db, [p for p in toutes if origines.get(p["artifact_id"]) == "conversation"])
        # 2. Le TYPE : celui que les mots nomment, croisé avec celui que l'appel
        #    déclare. C'est ici, et AVANT de compter, sinon « la dernière image »
        #    d'une discussion [image, pdf] rend le pdf.
        demandes = set(reference.types) | {str(t).lower() for t in (args.types or [])}
        conformes = [p for p in toutes if p.get("type") in demandes] if demandes else list(toutes)
        # 3. L'ORDINAL, sur la liste conforme. `ambigu` se calcule ici et pas plus loin :
        # il ne porte que sur la pièce VISÉE, donc sur une liste et une position déjà
        # connues, et il est faux dès qu'aucun résultat n'est rendu.
        position = index_reference(reference, len(conformes))
        ambigu = _ref_non_separable(conformes, position if position is not None else -1)
        motif = _ref_reste_ambigue(reference, conformes, toutes)
        results: list[dict] = []
        # Un ordinal qui n'EXISTE pas ne rend pas la pièce voisine : le motif est dit
        # et le résultat est vide, sinon « la deuxième image » d'une seule image
        # répondrait avec cette image — une réponse que personne n'a demandée.
        if position is not None:
            visee = conformes[position]
            public_map = _public_mapping(db, conv_id, {visee["artifact_id"]})
            public_id = _nom_public(db, conv_id, visee["artifact_id"], public_map)
            if public_id:
                card = _cards.get_current_card(db, visee["artifact_id"])
                results.append({"rank": 1, "artifact_id": public_id, "public_id": public_id,
                                "name": visee.get("name"), "type": visee.get("type"), "date": "",
                                "title": _public_text(_public_name((card.title if card else None)
                                                                  or visee.get("name"))),
                                "caption": _public_text((card.summary if card else None) or ""),
                                "origin": "conversation", "matched_on": ["reference"], "score": 1.0})
            else:
                motif = "ce fichier n'a pas d'identifiant public dans cette discussion."
        elif not motif:
            motif = "cette référence ne désigne aucun fichier joint."
        payload = {
            "tool": "find_files", "ok": True, "mode": "reference",
            "ambiguous": bool(results) and ambigu,
            "candidates": len(conformes), "returned": len(results), "results": results,
            "registry_size": registry_size,
            "reference": {"demande": _public_text(args.query or ""), "position": reference.position,
                          "rang": reference.rang, "types": list(reference.types),
                          "envoyee_par_l_utilisateur": reference.sent_by_user, "motif": motif},
        }
        return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                          display_summary=render_reference(payload["reference"], results, motif,
                                                          payload["ambiguous"]),
                          public_ids=_publies_nommes(results))
    except (TypeError, ValueError) as e:
        # SEUL l'échec de RÉSOLUTION de la référence est un motif utilisateur —
        # un ordinal hors registre ou des mots sans média. Une panne interne
        # (KeyError, erreur DB, AttributeError) n'est PAS une formulation
        # maladroite : la renommer « référence illisible » en fabriquerait un
        # motif FAUX que la boucle traiterait en relance inutile. Elle atteint le
        # filet honnête de `BaseTool.call` (`[INTERNAL_ERROR] {type}`), même
        # mécanique que les précédents `ask_files.py` et `search_info.py`.
        return ToolResult(ok=False, display_summary="find_files: référence illisible",
                          error=f"[BAD_ARGS] La référence demandée n'a pas pu être résolue. Action : "
                                f"reformule, ou demande un nom de fichier. ({str(e)[:120]})")
    finally:
        try:
            db.close()
        except Exception:
            pass

# tools/search_info.py — Recherche sémantique (spec 01/02)
# Schéma : queries 1-3 x ≤150 (dure ColBERT 32 tokens), types subset image|pdf|sheet|text, recency recent|today.
# Robustesse spec 02 : EMPTY_QUERY / TOO_MANY / TOO_LONG / ENUM_INVALID / UNKNOWN_ARG / EMPTY_CALL,
# dégénérées dédupliquées silencieusement, jamais une erreur ; rendus vides / near-miss / index vide.
from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, model_validator, field_validator

from backend.config.defaults import TOOL_LIMITS
from backend.tools.base import BaseTool, ToolContext, ToolResult, ToolStatus
from backend.tools.errors import ToolErrorCode

_ALLOWED_TYPES = ("image", "pdf", "sheet", "text")
_ALLOWED_RECENCY = ("recent", "today")


class SearchInfoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str]
    types: list[str] | None = None
    recency: str | None = None

    @field_validator("queries")
    @classmethod
    def _trim_queries(cls, v: list[str]) -> list[str]:
        lim = int(TOOL_LIMITS["query_max_chars"])  # 150 — dure ColBERT
        qmax = int(TOOL_LIMITS["queries_max"])  # 3
        raw = list(v or [])
        if len(raw) > qmax:
            raise ValueError("[TOO_MANY] Un appel est limité à 3 requêtes maximum.")
        out: list[str] = []
        for q in raw:
            t = (q or "").strip()
            if not t:
                continue  # chaîne vide retirée silencieusement
            if len(t) > lim:
                raise ValueError(
                    "[TOO_LONG] Une requête est limitée à 150 caractères maximum — reformule en mots-clés courts."
                )
            out.append(t)
        dedup = list(
            dict.fromkeys(out)
        )  # requêtes identiques : déduplication silencieuse
        if not dedup:
            raise ValueError(
                "[EMPTY_QUERY] Écris 1 à 3 questions de recherche factuelles par appel."
            )
        return dedup

    @model_validator(mode="before")
    @classmethod
    def _exiger_queries(cls, data: object) -> object:
        # Obligation explicite : un validateur de champ ne s'exécute pas sur une
        # valeur ABSENTE, donc un appel sans `queries` rendrait un pydantic
        # « Field required » sans code, puis un BAD_ARGS mou. Le code est devant :
        # sans exécutable, un appel vide est une demande à reformuler.
        if isinstance(data, Mapping) and "queries" not in data:
            raise ValueError(
                "[MISSING_REQUIRED] queries est obligatoire : 1 à 3 questions de recherche par appel."
            )
        return data

    @field_validator("types")
    @classmethod
    def _check_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for t in v:
            if str(t) not in _ALLOWED_TYPES:
                raise ValueError(
                    "[ENUM_INVALID] Argument mal rempli : types = liste parmi image, pdf, sheet, text ; recency ∈ recent, today."
                )
        return list(v)

    @field_validator("recency")
    @classmethod
    def _check_recency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if str(v) not in _ALLOWED_RECENCY:
            raise ValueError(
                "[ENUM_INVALID] Argument mal rempli : types = liste parmi image, pdf, sheet, text ; recency ∈ recent, today."
            )
        return v


_TYPE_LABEL = {"image": "image", "pdf": "pdf", "sheet": "tableur", "text": "texte"}
_PUBLIC_ID_RE = re.compile(r"doc_[1-9]\d*\Z")
_PUBLIC_EVIDENCE_RE = re.compile(r"doc_[1-9]\d*:[0-9]+\Z")
_TECHNICAL_ID_RE = re.compile(r"(?:art_|sti_)[A-Za-z0-9_]+\Z", re.IGNORECASE)


def _public_id(value: object) -> str:
    """Lit un label public, sans jamais accepter un identifiant de stockage."""
    candidate = str(value or "").strip()
    return candidate if _PUBLIC_ID_RE.fullmatch(candidate) else ""


def _public_name(value: object) -> str:
    """Évite qu'un nom technique devienne le fallback visible d'un résultat."""
    name = str(value or "").strip()
    if not name or _TECHNICAL_ID_RE.fullmatch(name):
        return "fichier"
    return name


def _public_text(value: object) -> str:
    """Retire les IDs techniques qui pourraient être présents dans un texte indexé."""
    return _TECHNICAL_ID_RE.sub("[identifiant interne retiré]", str(value or ""))


def _public_mapping(db, conv_id: str, allowed: set[str]) -> dict[str, str]:
    """Retourne la seule mapping autorisée : artefact interne → doc_N local."""
    from backend.search import docrefs as _docrefs

    try:
        _docrefs.backfill_from_links(db, conv_id)
    except Exception:
        pass
    try:
        return {
            str(row.get("artifact_id") or "").strip(): _public_id(
                row.get("public_id") or row.get("doc_label")
            )
            for row in _docrefs.list_for_conv(db, conv_id)
            if str(row.get("artifact_id") or "").strip() in allowed
            and _public_id(row.get("public_id") or row.get("doc_label"))
        }
    except Exception:
        return {}


# `origin` : la portée qui autorise CETTE discussion à nommer le fichier. Le dépôt
# de l'espace n'est pas « joint à ce message » — sans cette distinction, l'orchestrateur
# attribuerait à l'utilisateur un fichier qu'il n'a pas joint ici.
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
        rows = (
            db.query(_Link.artifact_id, _Link.scope_kind, _Link.scope_id)
            .filter(
                _Link.artifact_id.in_(ids),
                _Link.removed_at.is_(None),
            )
            .all()
        )
    except Exception:
        return {}
    out: dict[str, str] = {}
    for artifact_id, scope_kind, scope_id in rows:
        if str(scope_kind or "") == "conversation" and str(scope_id or "") == str(
            conv_id or ""
        ):
            out[str(artifact_id)] = "conversation"
    for artifact_id, scope_kind, _scope_id in rows:
        if str(scope_kind or "") == "workspace":
            out.setdefault(str(artifact_id), "workspace")
    return out


def _nom_public(db, conv_id: str, artifact_id: str, cache: dict[str, str]) -> str:
    """``doc_N`` connu, ou alloué MAINTENANT parce que cette ligne va être rendue.

    C'est le contrat d'allocation paresseuse : un dépôt de cinquante fichiers ne
    coûte un numéro que pour ceux que la recherche montre. Un fichier sans label
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


def render_search_info(by_query: list[dict], index_size: int) -> str:
    """Rendu texte spec 02 (groupé, preuves doc_N:k, duplicate_of_query, near-miss, index vide). Pur."""
    if index_size == 0:
        return (
            "[search_info] aucun document n'est indexé pour l'instant.\n"
            "Suggestion : réponds depuis tes connaissances, ou demande une clarification à l'utilisateur.\n[fin]"
        )
    if not by_query:
        return (
            "[search_info] 1 recherche — aucune correspondance.\n"
            "Suggestions : reformule avec d'autres termes, ou cherche le fichier par type avec find_files.\n[fin]"
        )
    # Cas mono-requête sans résultat → near-miss possible via champ dédié.
    # Une fiche de mémoire peut tout à fait être là : elle n'est pas un résultat
    # de fichier, et le fait que la question n'ait rien indexé ne doit pas la
    # masquer.
    if (
        len(by_query) == 1
        and not (by_query[0].get("results") or [])
        and not (by_query[0].get("memoire") or [])
    ):
        q = by_query[0]
        near = _public_text(q.get("near_miss") or "")
        if near:
            return f"[search_info] 1 recherche — {near}\n[fin]"
        return (
            "[search_info] 1 recherche — aucune correspondance.\n"
            "Suggestions : reformule avec d'autres termes, ou cherche le fichier par type avec find_files.\n[fin]"
        )
    lines = [
        f"[search_info] {len(by_query)} recherche{'s' if len(by_query) > 1 else ''}"
    ]
    seen_ids: set[str] = set()
    for i, q in enumerate(by_query, start=1):
        query = _public_text(q.get("query", ""))
        cands = int(q.get("candidates", 0))
        results = q.get("results") or []
        ret = len(results)
        if len(by_query) > 1:
            lines.append(
                f"— Recherche {i} : « {query} » — {cands} correspondances, {ret} affichées"
                if cands != ret
                else f"— Recherche {i} : « {query} » — {cands} correspondances"
            )
        else:
            lines.append(
                f"— Recherche 1 : « {query} » — {cands} correspondances, {ret} affichées"
                if cands != ret
                else f"— Recherche : « {query} » — {cands} correspondances"
            )
        for r in results:
            public_id = _public_id(
                r.get("public_id") or r.get("artifact_id") or r.get("doc_label")
            )
            if not public_id:
                continue
            name = _public_text(_public_name(r.get("name", "?")))
            typ = _TYPE_LABEL.get(str(r.get("type", "")), str(r.get("type", "")))
            date = (
                str(r.get("date", ""))[:5].replace("-", "/") if r.get("date") else "?"
            )
            try:
                from datetime import date as _d

                dd = str(r.get("date", ""))
                if len(dd) >= 10:
                    date = f"{dd[8:10]}/{dd[5:7]}"
            except Exception:
                pass
            evid = str(r.get("evidence_id") or "").strip()
            if _PUBLIC_EVIDENCE_RE.fullmatch(evid) is None or not evid.startswith(
                f"{public_id}:"
            ):
                evid = "—"
            snip = _public_text(r.get("snippet", "")).strip()
            dup = r.get("duplicate_of_query")
            origine = _ORIGINE_FR.get(str(r.get("origin") or ""), "")
            ligne = f"  {r.get('rank', '?')}. {name} — {typ}, {date} — id {public_id}"
            if origine:
                ligne += f", {origine}"
            if dup is not None:
                ligne += f" (déjà en recherche {int(dup) + 1})"
            lines.append(ligne + f", preuve {evid}")
            if snip:
                lines.append(f"     « {snip} »")
        for k, m in enumerate(q.get("memoire") or [], start=1):
            texte = _public_text(str(m.get("text") or "")).strip()
            statut = str(m.get("statut") or "")
            entry_id = str(m.get("entry_id") or "")
            if not texte and not entry_id:
                continue  # fiche muette : rien à rapporter
            lines.append(
                f"{k}. Mémoire — {texte or 'fiche sans extrait'} — statut {statut} (entry {entry_id})"
            )
        note = _public_text(q.get("note") or "").strip()
        if note:
            lines.append(f"  Note : {note}")
    lines.append("[fin] toutes les correspondances affichées")
    return "\n".join(lines)


def _resultats_par_requete(by_query: list[dict]) -> list[dict]:
    """Tous les résultats affichés, à plat — l'ordre de rendu est l'ordre de citation. Pur."""
    return [r for q in (by_query or []) for r in (q.get("results") or [])]


def _preuves_citees(by_query: list[dict]) -> list[str]:
    """`doc_N:k` réellement cités par cette page, dans l'ordre, sans doublon.

    Une preuve absente (`evidence_id` nul) n'est pas une preuve : elle est donc
    OMISE, pas rendue comme telle. La boucle n'a aucun moyen de la distinguer d'un
    identifiant valide, et une entrée vide lui ferait croire qu'elle peut la citer.
    Pur.
    """
    vues: list[str] = []
    for r in _resultats_par_requete(by_query):
        evid = str(r.get("evidence_id") or "").strip()
        if evid and evid not in vues:
            vues.append(evid)
    return vues


def _publies_cites(by_query: list[dict]) -> list[str]:
    """`doc_N` nommés par cette page, dans l'ordre, sans doublon. Pur."""
    vus: list[str] = []
    for r in _resultats_par_requete(by_query):
        public_id = str(r.get("public_id") or "").strip()
        if public_id and public_id not in vus:
            vus.append(public_id)
    return vus


def _memoires_du_espace(db, workspace_id: str | None, query: str) -> list[dict]:
    """Fiches de mémoire pertinentes pour une question, normalisées pour le payload.

    Deux registres, une seule question : la recherche de fichiers répond à « où
    se trouve l'information », la mémoire à « qu'est-ce que l'on sait ». Posée sur
    le même texte, la seconde complète la première — une discussion peut être sans
    fichier indexé pour la question, et pourtant porter une fiche pertinente.

    L'entrée est normalisée en `{text, statut, entry_id, relevance, sources_count}` :
    c'est le vocabulaire que le payload et le rendu partagent, jamais celui du stockage.
    Le `include_stale=True` fait que `stale` n'est pas masqué : une fiche révisable
    reste lisible, avec son statut, et n'est pas présentée comme un fait actuel.

    Sans espace ou sans question : liste vide. L'enrichissement ne doit pas faire
    échouer une recherche qui aurait pu aboutir — le registre mémoire est un second
    axe de lecture, pas un prérequis de validité.
    """
    if not workspace_id or not (query or "").strip():
        return []
    try:
        from backend.services import memoire as _memoire

        page = _memoire.search(db, workspace_id, query, include_stale=True, limit=5)
        if not page.get("ok"):
            return []
        items: list[dict] = []
        for r in (page.get("results") or [])[:5]:
            items.append(
                {
                    "text": str(r.get("content") or ""),
                    "statut": str(r.get("status") or ""),
                    "entry_id": str(r.get("entry_id") or ""),
                    "relevance": round(float(r.get("relevance") or 0.0), 4),
                    "sources_count": int(r.get("sources_count") or 0),
                    "category": r.get("category"),
                    "affirmation_status": r.get("affirmation_status"),
                }
            )
        return items
    except Exception:
        return []


class SearchInfoTool(BaseTool[SearchInfoArgs]):
    name = "search_info"
    description = (
        "Recherche sémantique dans tout le contenu indexé (observations, markdown, captions, schémas). "
        "Rend passages pertinents + public_id doc_N + evidence_id doc_N:k + fiche inline. "
        "queries : 1 à 3 questions factuelles ≤150 caractères. "
        "types : liste parmi image, pdf, sheet, text. recency : recent (7j) ou today (24h)."
    )
    input_model = SearchInfoArgs
    timeout_s = 10.0
    max_output_chars = 6000

    async def execute(self, args: SearchInfoArgs, ctx: ToolContext) -> ToolResult:
        import json as _json
        from datetime import datetime, timedelta, timezone

        try:
            from backend.search import scope as _scope, fts as _fts
            from backend.db.database import SessionLocal as _SL

            conv_id = ctx.conv_id or ctx.request_id or ""
            allowed = _scope.resolve_allowed_artifacts(conv_id)
            workspace_id = None
            index_size = len(allowed)
            db = _SL()
            try:
                _fts.ensure_fts(db)
                # Espace de la discussion : dérivé serveur des liens, jamais fourni.
                # La mémoire est retrouvable depuis une discussion de son espace —
                # même périmètre que celui qui autorise le fichier.
                if conv_id:
                    try:
                        from backend.services import espaces as _esp

                        workspace_id = _esp.conversation_workspace(db, conv_id)
                    except Exception:
                        workspace_id = None
                if index_size == 0:
                    # Aucun artefact n'est indexé dans le périmètre : la question peut
                    # porter une fiche de mémoire sans qu'aucun document ne soit indexé.
                    # Le registre mémoire reste donc lisible dans cet état — il complète
                    # la recherche de fichiers, il ne dépend pas de l'index.
                    by_query = []
                    for q in (args.queries or []):
                        memoire_items = _memoires_du_espace(db, workspace_id, q)
                        if not memoire_items:
                            continue
                        by_query.append(
                            {
                                "query": q,
                                "candidates": 0,
                                "returned": 0,
                                "results": [],
                                "memoire": memoire_items,
                            }
                        )
                    return ToolResult(
                        ok=True,
                        content=_json.dumps(
                            {
                                "tool": "search_info",
                                "ok": True,
                                "by_query": by_query,
                                "index_size": 0,
                            },
                            ensure_ascii=False,
                        ),
                        display_summary=render_search_info(by_query, 0),
                    )
                public_map = _public_mapping(db, conv_id, allowed)
                # Pré-filtre serveur : type + recency + owner (spec 01 : pré-filtre avant scoring)
                rec = (args.recency or "").strip() or None
                cutoff = None
                if rec == "recent":
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=int(TOOL_LIMITS["recent_days"])
                    )
                elif rec == "today":
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        hours=int(TOOL_LIMITS["today_hours"])
                    )
                by_query: list[dict] = []
                seen_snippets: dict[str, int] = {}
                for qi, q in enumerate(args.queries):
                    # Une retente serveur sur timeout embedder (spec 02 pannes backend).
                    # `search_scopes` et non `search` : le périmètre d'un chat d'espace
                    # a deux portées, et un fichier du dépôt n'existe que sous la seconde.
                    page = None
                    for _attempt in range(2):
                        try:
                            page = _fts.search_scopes(
                                db,
                                query=q,
                                conversation_id=conv_id,
                                allowed_ids=allowed,
                                limit=5,
                            )
                            break
                        except Exception:
                            if _attempt == 1:
                                raise
                    if page is None or not page.get("ok"):
                        # Une panne de moteur n'est pas « 0 correspondance » : l'entrée
                        # reste rendue, porte une note explicite, et si le moteur est
                        # tombé pour TOUTES les requêtes le résultat est un échec
                        # (SERVICE_UNAVAILABLE) — jamais un succès sans résultat.
                        by_query.append(
                            {
                                "query": q,
                                "candidates": 0,
                                "returned": 0,
                                "results": [],
                                "panne": True,
                                "note": "la recherche a échoué côté service — reformule ou conclus avec ce qui est déjà obtenu.",
                            }
                        )
                        continue
                    raw = page.get("results") or page.get("candidates") or []
                    # Filtre recency côté serveur (date artefact)
                    if cutoff is not None:
                        from backend.db.models import Artifact as _Art

                        # Un seul round-trip : les artefacts sont lus par lot, jamais
                        # dans la boucle (N+1). Le comportement reste identique : un
                        # artefact absent ou sans date exclut le candidat.
                        recency_ids = [r.get("artifact_id") for r in (raw or [])]
                        try:
                            arts_by_id = {
                                a.id: a
                                for a in db.query(_Art)
                                .filter(_Art.id.in_(recency_ids))
                                .all()
                            }
                        except Exception:
                            arts_by_id = {}
                        filt = []
                        for r in raw:
                            try:
                                art = arts_by_id.get(r.get("artifact_id"))
                                if art is None or art.created_at is None:
                                    continue
                                created = art.created_at
                                if created.tzinfo is None:
                                    created = created.replace(tzinfo=timezone.utc)
                                if created >= cutoff:
                                    filt.append(r)
                            except Exception:
                                continue
                        raw = filt
                    # Map kinds internes → types spec (text inclut csv/docx legacy ; sheet distingué si xlsx/csv/ods)
                    if args.types:
                        wanted = {str(t).lower() for t in args.types}
                        mapped = []
                        for r in raw:
                            kind = str(r.get("kind", "text")).lower()
                            nm = str(r.get("name", "")).lower()
                            typ = (
                                "sheet"
                                if (
                                    nm.endswith((".xlsx", ".ods", ".csv"))
                                    or kind == "sheet"
                                )
                                else kind
                            )
                            if typ in wanted or kind in wanted:
                                rr = dict(r)
                                rr["kind"] = typ
                                mapped.append(rr)
                            elif kind == "text" and "text" in wanted:
                                mapped.append(r)
                        raw = mapped
                    # Near-miss : filtre trop strict sans résultat → pistes autres types.
                    # Les ids cités restent ceux DÉJÀ nommés : nommer des fichiers que la
                    # discussion n'a pas choisis dépenserait son contexte pour rien.
                    if not raw and args.types:
                        try:
                            alt = _fts.search_scopes(
                                db,
                                query=q,
                                conversation_id=conv_id,
                                allowed_ids=allowed,
                                limit=5,
                            )
                            alts = [x for x in (alt.get("results") or [])][:3]
                            if alts:
                                public_ids = [
                                    public_map.get(
                                        str(a.get("artifact_id") or "").strip(), ""
                                    )
                                    for a in alts
                                ]
                                public_ids = [
                                    public_id for public_id in public_ids if public_id
                                ]
                                if public_ids:
                                    ids = ", ".join(public_ids)

                                    # Libellé dérivé des kinds RÉELS des candidats de
                                    # repli — jamais figé sur « pdf ». Un unique kind
                                    # donne un libellé précis ; plusieurs donnent «
                                    # types disponibles (...) ».
                                    def _kind_public(a: dict) -> str:
                                        nm = str(a.get("name", "")).lower()
                                        k = str(a.get("kind", "text")).lower()
                                        if (
                                            nm.endswith((".xlsx", ".ods", ".csv"))
                                            or k == "sheet"
                                        ):
                                            return "sheet"
                                        return k or "text"

                                    alt_kinds = [_kind_public(a) for a in alts]
                                    distinct = sorted(set(alt_kinds))
                                    if len(distinct) == 1:
                                        suffix = distinct[0]
                                        sollicitation = f"un {distinct[0]}"
                                    else:
                                        suffix = (
                                            "types disponibles ("
                                            + ", ".join(distinct)
                                            + ")"
                                        )
                                        sollicitation = "un autre type"
                                    by_query.append(
                                        {
                                            "query": q,
                                            "candidates": 0,
                                            "returned": 0,
                                            "results": [],
                                            "near_miss": (
                                                f"aucune {args.types[0]} ne correspond. "
                                                f"Par contre : {len(public_ids)} {suffix} ({ids}). "
                                                f"Vérifie avec l'utilisateur s'il cherchait {sollicitation}."
                                            ),
                                        }
                                    )
                                    continue
                        except Exception:
                            pass
                    # La mémoire du DÉPÔT : même question, registre des faits de l'espace.
                    memoire_items = _memoires_du_espace(db, workspace_id, q)
                    # Façonne les résultats avec le label public local, jamais l'artifact_id brut.
                    shaped: list[dict] = []
                    # Troncature AVANT nommage : `results_limit` borne le contexte de
                    # l'orchestrateur, donc un dépôt de cinquante fichiers ne coûte
                    # un numéro qu'aux cinq affichés.
                    affiches = raw[: int(TOOL_LIMITS["results_limit"])]
                    origines = _origines(
                        db, conv_id, [str(r.get("artifact_id") or "") for r in affiches]
                    )
                    # Façonnage des cartes : un seul chargement d'artefacts par lot,
                    # au lieu d'un `db.query` par résultat affiché.
                    from backend.db.models import Artifact as _A2_batch

                    _shape_ids = [
                        str(r.get("artifact_id") or "").strip() for r in affiches
                    ]
                    try:
                        _arts_by_id = {
                            a.id: a
                            for a in db.query(_A2_batch)
                            .filter(_A2_batch.id.in_(_shape_ids))
                            .all()
                        }
                    except Exception:
                        _arts_by_id = {}
                    for r in affiches:
                        art_id = str(r.get("artifact_id") or "").strip()
                        public_id = _nom_public(db, conv_id, art_id, public_map)
                        if not public_id:
                            continue
                        rank = len(shaped) + 1
                        key = (
                            str(r.get("name", ""))
                            + "|"
                            + str((r.get("card") or {}).get("summary", ""))
                        )
                        dup = seen_snippets.get(key)
                        if dup is None:
                            seen_snippets[key] = qi
                        card = r.get("card") or {}
                        snippet = card.get("summary", "") or ""
                        from backend.search import chunks as _chunks

                        ordinal = _chunks.ordinal_of_quote(
                            db, art_id, str(snippet or "")
                        )
                        evid = f"{public_id}:{ordinal}" if ordinal is not None else None
                        try:
                            from backend.db.models import Artifact as _A2

                            a2 = _arts_by_id.get(art_id)
                            date = (
                                a2.created_at.date().isoformat()
                                if a2 and a2.created_at
                                else ""
                            )
                            nm = _public_text(
                                _public_name(
                                    a2.original_filename or a2.display_name
                                    if a2
                                    else r.get("name", "")
                                )
                            )
                            kd = (
                                (a2.kind or r.get("kind", "text"))
                                if a2
                                else r.get("kind", "text")
                            )
                            if str(nm).lower().endswith((".xlsx", ".ods", ".csv")):
                                kd = "sheet"
                        except Exception:
                            date, nm, kd = (
                                "",
                                _public_text(_public_name(r.get("name", ""))),
                                r.get("kind", "text"),
                            )
                        item: dict = {
                            "rank": rank,
                            "artifact_id": public_id,
                            "public_id": public_id,
                            "evidence_id": evid,
                            "name": nm,
                            "type": kd,
                            "date": date,
                            "origin": origines.get(art_id) or "conversation",
                            "score": round(float(r.get("score", 0.0)), 2),
                            "snippet": _public_text(snippet)[:300],
                        }
                        if dup is not None and dup != qi:
                            item["duplicate_of_query"] = dup
                        shaped.append(item)
                    entry: dict = {
                        "query": q,
                        "candidates": len(raw),
                        "returned": len(shaped),
                        "results": shaped,
                    }
                    entry["memoire"] = memoire_items
                    # Recherche trop large : discrimination moyenne basse → note (spec 02 dégénérées)
                    try:
                        if len(shaped) >= 2:
                            scores = [float(s.get("score", 0)) for s in shaped]
                            if max(scores) - min(scores) < 0.1:
                                entry["note"] = (
                                    "recherche large — affine les termes pour de meilleurs résultats."
                                )
                    except Exception:
                        pass
                    by_query.append(entry)
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
            return ToolResult(
                status=ToolStatus.FAILED,
                display_summary="search_info: timeout",
                error=f"[{ToolErrorCode.TIMEOUT}] délai dépassé. Action : réessaie, ou rédige le brief avec les résultats déjà obtenus. ({str(e)[:120]})",
            )
        import json as _json2

        # Une requête tombée au milieu d'un appel à plusieurs : partiel explicite
        # (contrat commun §3.3 : « éléments utiles obtenus, mais une partie de
        # l'opération n'a pas abouti »). Toutes tombées : échec, jamais « aucun
        # résultat ». Les notes sont déjà rendues par `render_search_info`.
        pannes = [q for q in by_query if q.get("panne")]
        if pannes:
            degrade = f"[{ToolErrorCode.SERVICE_UNAVAILABLE}] La recherche n'a pas pu être exécutée pour {len(pannes)} requête(s) sur {len(args.queries)}."
            if len(pannes) == len(args.queries):
                return ToolResult(
                    status=ToolStatus.FAILED,
                    content=_json.dumps(
                        {
                            "tool": "search_info",
                            "ok": False,
                            "by_query": by_query,
                            "index_size": index_size,
                        },
                        ensure_ascii=False,
                    ),
                    display_summary=render_search_info(by_query, index_size),
                    error=degrade,
                )
        payload = {
            "tool": "search_info",
            "ok": not pannes,
            "by_query": by_query,
            "index_size": index_size,
        }
        # Les identifiants sortent EN DEHORS du JSON : `call()` tronque `content`, et
        # un `search_info` à trois requêtes le coupe presque toujours. La boucle doit
        # pouvoir citer une réponse tronquée, donc elle lit ces deux listes sur le
        # `ToolResult` et jamais dans un payload coupé en plein milieu.
        return ToolResult(
            status=ToolStatus.PARTIAL if pannes else ToolStatus.SUCCESS,
            content=_json2.dumps(payload, ensure_ascii=False),
            display_summary=render_search_info(by_query, index_size),
            evidence_ids=_preuves_citees(by_query),
            public_ids=_publies_cites(by_query),
        )

# tools/web_research.py — Recherche web bornée (Phase 4, C15/C16/C18)
# L'orchestrateur formule des REQUÊTES ; il ne choisit pas le moteur, ni le nombre de
# pages, ni les bornes. `url` est la seule exception, et elle ne lit que si l'adresse
# vient du message de l'utilisateur ou d'un résultat antérieur (règle de provenance).
from __future__ import annotations

import json as _json

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.config.defaults import TOOL_LIMITS
from backend.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.web.research import current_mode, run as _run, WebOutcome
from backend.services.web.types import FRESHNESS_REUSE, FRESHNESS_VALUES

_ALLOWED_FRESHNESS = tuple(FRESHNESS_VALUES)


# NOTE DE DÉVELOPPEMENT, volontairement hors du schéma : une docstring de
# classe finit dans la description PUBLIÉE au modèle, et le test
# `test_bornes_de_longueur_publiees_en_prose_seulement` interdit qu'y apparaisse
# une borne de longueur sous forme de mot-clé de schéma. Le convertisseur
# JSON-schema → GBNF déplie une borne de 2000 caractères en 2000 niveaux et fait
# tomber le sampler NULL de llama.cpp, donc `_publishable_schema` convertit ces
# bornes en phrase française à la volée. La validation, elle, s'applique toujours,
# via `input_model`. Les bornes vivent dans `TOOL_LIMITS`, jamais ici.
class WebResearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str]
    # Le sélecteur de source n'existe PAS dans la signature : la fiche n'en nomme
    # aucun (arXiv, OpenAlex et Wikipédia sont les sources de la phase 1, et le
    # moteur reste un choix du backend), donc le schéma ne le publie pas. La
    # classe s'appuie sur `general` en interne — `extra="forbid"` refuse
    # l'argument au modèle. Les valeurs `news` et `official`, qu'aucun moteur ne
    # couvre ici, ne sont donc plus connues de personne : c'est l'intention de la
    # fiche, pas un silence par accident.
    freshness: str | None = None
    # La fiche web_research §2.1 (v1.2) rend `purpose` OBLIGATOIRE : « Expliquer
    # l'information manquante ou la vérification visée ». La liste no-default du
    # champ le publie au contrat (`required` du schéma) ; le validateur
    # `mode="before"` traduit l'ABSENCE en [MISSING_REQUIRED], car un
    # `field_validator` ne s'exécute pas sur une clé manquante — une recherche
    # sans trace d'intention ne part jamais.
    purpose: str = Field(
        description="Ce que cette recherche doit établir, en une phrase, sans donnée privée.",
    )
    # Le champ `url` N'EXISTE PLUS (décision D4, fiche web_research §2.3) : la
    # consultation directe d'une URL n'appartient pas à la signature minimale
    # tant qu'un contrat de consultation explicite — provenance, domaines
    # admis, redirections — n'est pas établi. Le mécanisme interne de la
    # couche service (`research.run`, règle de provenance, URL_NOT_ADMITTED)
    # reste en place pour la lecture d'adresse écrite par l'utilisateur dans
    # son message et pour le jour où le contrat existra ; il n'est simplement
    # plus publié au modèle. `extra="forbid"` refuse la clé, alors nommée.
    #

    @model_validator(mode="before")
    @classmethod
    def _exiger_champs_obligatoires(cls, data: object) -> object:
        # Obligations explicites : un `field_validator` ne s'exécute pas sur une
        # valeur ABSENTE. La fiche rend `queries` obligatoire — la liste des
        # formulations est ce qu'un appel de recherche EST — et `purpose` au
        # même titre : la trace d'intention est ce qui permet de juger une
        # requête web.
        if isinstance(data, Mapping):
            if "queries" not in data:
                raise ValueError(
                    "[MISSING_REQUIRED] queries est obligatoire : 1 à 15 requêtes par appel, découpées en lots internes de 5 (3 max) par appel.")
            if data.get("purpose") is None:
                raise ValueError(
                    "[MISSING_REQUIRED] purpose est obligatoire : une phrase qui dit ce que cette "
                    "recherche doit établir, sans aucune donnée privée.")
        return data

    @field_validator("queries")
    @classmethod
    def _borne_requetes(cls, v: list[str]) -> list[str]:
        lim = int(TOOL_LIMITS["web_queries_max"])
        brut = [q for q in (v or []) if (q or "").strip()]
        if len(brut) > lim:
            raise ValueError(f"[TOO_MANY] Un appel est limité à {lim} requêtes maximum.")
        if not brut:
            raise ValueError("[EMPTY_QUERY] Écris 1 à 15 requêtes par appel, découpées en lots internes de 5 (3 max), en 150 caractères maximum chacune.")
        out: list[str] = []
        for q in brut:
            texte = q.strip()
            if len(texte) > int(TOOL_LIMITS["query_max_chars"]):
                raise ValueError(
                    f"[TOO_LONG] Une requête est limitée à {int(TOOL_LIMITS['query_max_chars'])} caractères "
                    f"maximum — reformule en mots-clés courts."
                )
            out.append(texte)
        return list(dict.fromkeys(out))  # doublons : dédupliqués, pas rejetés

    @field_validator("freshness")
    @classmethod
    def _check_freshness(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if str(v) not in _ALLOWED_FRESHNESS:
            raise ValueError(
                f"[ENUM_INVALID] freshness : {', '.join(_ALLOWED_FRESHNESS)}. "
                "reuse_if_sufficient relit une source conservée suffisante ; "
                "verify_current exige une vérification extérieure, un refus réseau est annoncé."
            )
        return str(v)

    @field_validator("purpose")
    @classmethod
    def _check_purpose(cls, v: str) -> str:
        texte = (v or "").strip()
        if not texte:
            raise ValueError("[MISSING_REQUIRED] purpose est obligatoire et ne peut pas être vide.")
        if len(texte) > int(TOOL_LIMITS["web_purpose_max_chars"]):
            raise ValueError(
                f"[TOO_LONG] purpose est limité à {int(TOOL_LIMITS['web_purpose_max_chars'])} caractères."
            )
        return texte


class WebResearchTool(BaseTool[WebResearchArgs]):
    name = "web_research"
    description = (
        "Cherche une information qui n'est pas dans les pièces jointes : définitions, faits "
        " publics, articles scientifiques. Ne lit JAMAIS une URL directe : le champ n'existe "
        "pas tant qu'un contrat de consultation ne règle provenance, domaines et redirections. "
        "Rend des extraits datés avec un evidence_id citable ; garde les recherches de la "
        "conversation, donc une question déjà posée ne redemande rien. "
        "queries : 1 à 15 requêtes par appel, découpées en lots internes de 5 (3 max), 150 caractères maximum chacune. "
        "Si une source manque, l'appel le dit ; ne comble jamais un trou par ce que tu sais."
    )
    input_model = WebResearchArgs
    timeout_s = 25.0
    max_output_chars = 6000

    async def execute(self, args: WebResearchArgs, ctx: ToolContext) -> ToolResult:
        conv_id = ctx.conv_id or ctx.request_id or ""
        message = ""
        try:
            from backend.db.database import SessionLocal as _SL
            from backend.db.models import Message as _M
            db = _SL()
            try:
                row = (
                    db.query(_M)
                    .filter(_M.run_id == ctx.request_id, _M.role == "user")
                    .order_by(_M.created_at.desc())
                    .first()
                )
                message = str(row.content or "") if row is not None else ""
            finally:
                db.close()
        except Exception:
            message = ""  # le message utilisateur n'est pas disponible : on continue sans lui

        import time

        requetes = list(args.queries)
        lots = [requetes[i:i + 5] for i in range(0, len(requetes), 5)]

        mode = current_mode()
        deadline = time.monotonic() + float(TOOL_LIMITS.get("web_timeout_s", 8)) * 3

        outcome = WebOutcome()
        for position, lot in enumerate(lots, start=1):
            lot_outcome = _run(
                queries=list(lot),
                # Le sélecteur de source n'existe pas dans la signature : le moteur
                # reste un choix du backend, jamais du modèle.
                source_type="general",
                freshness=args.freshness or FRESHNESS_REUSE,
                purpose=args.purpose,
                conv_id=conv_id,
                user_message=message,
                mode=mode,
                deadline=deadline,
            )
            if position == 1:
                outcome = lot_outcome
            else:
                outcome.ok = bool(outcome.ok and lot_outcome.ok)
                outcome.text = (outcome.text or "").rstrip() + f"\n\n--- web_research (lot {position}/{len(lots)}) ---\n" + (lot_outcome.text or "")
                outcome.cache_hits += lot_outcome.cache_hits
                outcome.network_queries += lot_outcome.network_queries
                outcome.pages_read += lot_outcome.pages_read
                outcome.hosts = list(dict.fromkeys([*outcome.hosts, *lot_outcome.hosts]))
                outcome.skipped.extend(lot_outcome.skipped)
                for couverture in lot_outcome.coverage:
                    if couverture not in outcome.coverage:
                        outcome.coverage.append(couverture)
                outcome.urls_read.extend(
                    url for url in lot_outcome.urls_read if url not in outcome.urls_read
                )
                for final_url, artifact_id in getattr(lot_outcome, "conserves", {}).items():
                    outcome.conserves[final_url] = artifact_id
                outcome.public_ids.extend(
                    public_id for public_id in lot_outcome.public_ids
                    if public_id not in outcome.public_ids
                )
                outcome.evidence_ids.extend(
                    evidence_id for evidence_id in lot_outcome.evidence_ids
                    if evidence_id not in outcome.evidence_ids
                )
                if outcome.code is None and lot_outcome.code is not None:
                    outcome.code = lot_outcome.code
        outcome.latency_ms = lot_outcome.latency_ms

        payload = outcome.as_result()
        return ToolResult(
            ok=bool(outcome.ok),
            content=_json.dumps(payload, ensure_ascii=False),
            display_summary=outcome.text,
        )

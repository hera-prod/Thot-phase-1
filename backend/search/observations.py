# backend/search/observations.py — Écriture systématique des observations (annexe 11, Lot A)
#
# D'OÙ VIENT CE FICHIER : l'annexe 11 exige une Observation persistée après CHAQUE
# Vision/OCR/lecture — avant, les résultats ne vivaient qu'en mémoire du tour.
# OÙ VA QUOI : record_tool_observation() est appelée par loop.py après chaque exécution
# (Phase 0 déterministe + Phase 1 orchestrateur), puis revise_card_after_observation().
# CONTRAT : la PERSISTANCE lève (ObservationNonÉcrite) ; les ENRICHISSEMENTS
# (fiche, réindexation, fragments) restent au mieux et sont rejouables. Voir la
# fonction pour pourquoi la frontière est là.
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable

from sqlalchemy import text as _sql_text

from backend.db.models import Observation
from backend.ingestion.erreurs import BailPerdu

log = logging.getLogger(__name__)

EXCERPT_CHARS = 4000  # extrait structuré pour le prompt (result_json)
PAYLOAD_CHARS = 16000  # référence bornée hors prompt (payload_ref)
RETENTION_NON_CITEES = 5  # union : 5 dernières non citées ∪ dernier completed ∪ citées

# Les outils dont le résultat EST le contenu d'un fichier. Leur observation
# appartient à l'extraction : elle est append-only, et seul `ok=True` alimente
# l'index. Les autres outils restent idempotents par empreinte.
OUTILS_EXTRACTION: frozenset[str] = frozenset(
    {"attachment_ingest", "doc_ocr", "vision", "read_text", "read_document"}
)


class ObservationNonÉcrite(RuntimeError):
    """L'observation n'a pas pu être persistée.

    Elle porte un nom et non un `None` parce que l'échec doit être traité :
    avaler cet échec revenait à laisser un dépôt « réussir » sans fiche, sans
    fragment et sans index, sans que personne — ni l'utilisateur, ni l'opérateur,
    ni les logs — n'en sache rien.
    """


def fingerprint(
    blob_sha: str | None, tool: str, model: str | None, params: str, query: str | None
) -> str:
    """Empreinte d'entrée : blob + outil + modèle + paramètres + query (annexe 11)."""
    raw = "|".join([blob_sha or "", tool or "", model or "", params or "", query or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _non_écrite(
    tool_name: str, artifact_id: str, exc: Exception
) -> ObservationNonÉcrite:
    """Le motif d'un échec d'écriture, toujours le même.

    Il nomme l'OUTIL et l'ARTEFACT parce qu'un message « base indisponible »
    seul ne dit pas ce qui a été perdu : l'opérateur qui reçoit le journal n'a
    que la ligne, pas la pile.
    """
    return ObservationNonÉcrite(
        f"observation non écrite ({tool_name} sur {artifact_id}) : "
        f"{type(exc).__name__}: {exc}"
    )


def record_tool_observation(
    artifact_id: str,
    tool_name: str,
    query: str | None,
    result_text: str,
    ok: bool,
    *,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    model_id: str | None = None,
    latency_ms: int = 0,
    tool_version: str | None = None,
    claim_token: str | None = None,
    representation_id: str | None = None,
) -> Observation | None:
    """Persiste une Observation immuable + révise la fiche.

    LEVE `ObservationNonÉcrite` si la ligne n'a pas pu être écrite. Le contrat
    « ne lève jamais » a été retiré parce qu'il rendait l'échec INDISTINGUABLE
    d'une absence : l'appelant recevait `None` dans les deux cas, et n'avait rien
    à regarder. Or une observation perdue, c'est un fichier sans fiche, sans
    fragments et sans ligne d'index — donc un dépôt « réussi » et muet, ce que
    la Bible interdit explicitement. Qui appelle décide quoi faire de l'échec ;
    ce module ne le décide plus à sa place.

    Rend l'observation créée (ou, pour un outil NON-extraction, celle déjà
    présente, voir l'idempotence), et `None` UNIQUEMENT quand il n'y avait rien
    à écrire — pas d'artefact demandé, ou artefact inconnu. `None` ne signifie
    donc plus « échec ».

    Pour l'EXTRACTION (`OUTILS_EXTRACTION`), cette fonction délègue à
    `record_extraction_observation` : deux artefacts partageant un blob ne
    doivent pas fusionner, et une tentative ratée ne doit jamais masquer la
    réussite suivante. Les autres outils gardent l'idempotence par empreinte.

    Les ENRICHISSEMENTS — fiche d'import, révision N+1, réindexation, fragments —
    restent au mieux et n'élèvent pas : ils sont dérivés, rejouables, et leur
    perdre ne perd pas le résultat. C'est la ligne d'observation qui est la
    preuve, et une preuve absente doit se voir. Ils ne sont appelés que pour
    `ok=True` : un refus laisse un audit, jamais un index qui prétend avoir lu
    le fichier.
    """
    if not artifact_id:
        return None
    if tool_name in OUTILS_EXTRACTION:
        # Le contrat historique de l'appelant ne change pas, mais la doctrine
        # s'applique : l'extraction est append-only et filtrée par artefact.
        return record_extraction_observation(
            artifact_id,
            tool_name,
            query,
            result_text,
            ok=ok,
            tool_version=tool_version or model_id or f"{tool_name}/phase1",
            model_id=model_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            claim_token=claim_token,
            representation_id=representation_id,
        )
    from backend.db.database import SessionLocal as _SL
    from backend.db.models import Artifact as _Art, Observation as _Obs

    try:
        db = _SL()
    except Exception as exc:
        # Ouvrir la session fait partie de l'écriture : une base qui ne s'ouvre
        # pas est un échec d'écriture, pas une absence. Le dire ici évite que
        # l'appelant distingue deux pannes qu'il ne peut pas traiter
        # différemment.
        raise _non_écrite(tool_name, artifact_id, exc) from exc
    try:
        art = db.query(_Art).filter(_Art.id == artifact_id).first()
        if art is None:
            return None
        blob_sha = None
        try:
            from backend.db.models import Blob as _Blob

            blob = db.query(_Blob).filter(_Blob.id == art.blob_id).first()
            blob_sha = blob.sha256 if blob else None
        except Exception:
            pass
        status = "completed" if ok else "failed"
        full = result_text or ""
        excerpt = full[:EXCERPT_CHARS]
        payload = full[:PAYLOAD_CHARS]
        fp = fingerprint(blob_sha, tool_name, model_id, "", query)
        # Idempotence : même analyse déjà persistée → enrichit le contexte, pas de
        # doublon. Le filtre porte AUSSI sur l'artefact : une empreinte de blob
        # partagée ne doit pas attribuer l'observation du voisin.
        existing = (
            db.query(_Obs)
            .filter(
                _Obs.artifact_id == artifact_id,
                _Obs.input_fingerprint == fp,
            )
            .first()
        )
        if existing is not None:
            touched = False
            if run_id and not existing.run_id:
                existing.run_id = run_id
                touched = True
            if tool_call_id and not existing.tool_call_id:
                existing.tool_call_id = tool_call_id
                touched = True
            if touched:
                db.commit()
            return existing
        obs = _Obs(
            id=f"obs_{uuid.uuid4().hex[:12]}",
            artifact_id=artifact_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_version="phase1",
            model_id=model_id,
            input_fingerprint=fp,
            query=(query or "")[:500],
            status=status,
            result_json=json.dumps(
                {
                    "excerpt": excerpt,
                    "truncated": len(full) > EXCERPT_CHARS,
                    "chars_total": len(full),
                },
                ensure_ascii=False,
            ),
            payload_ref=payload,
        )
        db.add(obs)
        db.flush()
        if ok:
            _indexer_observation_reussie(db, art, obs, excerpt, payload)
        db.commit()
        return obs
    except ObservationNonÉcrite:
        raise
    except Exception as exc:
        # La transaction est abandonnée explicitement : fermer la session sans
        # l'avoir fait laisserait un `commit` en suspens chez SQLAlchemy, et
        # l'appelant déciderait d'un état que personne n'a décrit.
        try:
            db.rollback()
        except Exception:
            pass
        raise _non_écrite(tool_name, artifact_id, exc) from exc
    finally:
        try:
            db.close()
        except Exception:
            pass


def _blob_sha_for(db, art) -> str | None:
    """Empreinte du blob d'un artefact, ou None si elle n'est pas lisible.

    Pure hors base : l'empreinte est ce qui rend deux extractions du même contenu
    comparables. Son absence n'empêche pas d'écrire l'observation — elle donne
    seulement une clé de rétention moins précise, jamais une ligne perdue.
    """
    try:
        from backend.db.models import Blob as _Blob

        blob = db.query(_Blob).filter(_Blob.id == art.blob_id).first()
        return blob.sha256 if blob else None
    except Exception:
        return None


def _lier_representation_a_observation(
    db, artifact_id: str, observation, representation_id: str | None = None
):
    """Rend la représentation courante et lui donne son observation source.

    L'étape A écrit la représentation AVANT l'observation (pour ne pas perdre le
    texte si l'audit échoue). Le lien de provenance est donc posé APRÈS coup,
    dans la transaction de l'observation : soit les deux existent, soit la
    représentation reste avec `source_text_observation_id = NULL`.

    Un lien DÉJÀ POSÉ n'est jamais réécrit : une représentation est une version
    immuable, et changer son observation source falsifierait sa provenance. Un
    `representation_id` qui appartient à un autre artefact est ignoré (avec un
    journal), jamais rattaché de force.
    """
    from backend.db.models import ArtifactRepresentation as _Rep

    try:
        requete = db.query(_Rep).filter(_Rep.artifact_id == artifact_id)
        if representation_id:
            representation = requete.filter(_Rep.id == representation_id).first()
            if representation is None:
                log.warning(
                    "[observations] représentation %s absente pour %s : lien non posé",
                    representation_id,
                    artifact_id,
                )
                return None
        else:
            representation = requete.filter(_Rep.is_current == 1).first()
        if representation is None:
            return None
        if representation.source_text_observation_id is None:
            representation.source_text_observation_id = observation.id
            db.flush()
        return representation
    except Exception as exc:
        # L'indexation refera sa propre lecture : perdre le lien ne perd ni le
        # texte ni l'observation, et le backfill pourra le reposer.
        log.warning(
            "[observations] lien représentation→observation non posé pour %s : %s: %s",
            artifact_id,
            type(exc).__name__,
            exc,
        )
        return None


def _chunks_deja_indexes(db, artifact_id: str, representation) -> bool:
    """Vrai si l'index existe DÉJÀ pour cette représentation ou en repli legacy.

    L'indexation complète (découpage + encodage ColBERT par lots) est le coût
    lourd de l'ingestion. Une observation de LECTURE (`read_document`,
    `doc_ocr`, `read_text`) réécrivait jusqu'ici tous les fragments et rouvrait
    l'encodeur à chaque fois, alors que le texte n'avait pas changé. On ne la
    déclenche donc que si la représentation courante n'a encore aucun fragment ;
    sinon, l'index existant fait foi.

    Un repli legacy déjà écrit (`representation_id IS NULL`) compte aussi comme
    « déjà indexé » : sans cela, une lecture sur une représentation sans
    fragments rejouerait le fallback à chaque observation, alors qu'il ne doit
    avoir lieu qu'UNE fois. Une nouvelle ingestion explicite passe outre, car
    son `representation_id` force la réindexation complète.
    """
    from backend.db.models import ArtifactChunk as _Chk

    try:
        requete = db.query(_Chk.id).filter(_Chk.artifact_id == artifact_id)
        if representation is not None:
            requete = requete.filter(
                (_Chk.representation_id == representation.id)
                | (_Chk.representation_id.is_(None))
            )
        return requete.first() is not None
    except Exception:
        # Une lecture impossible ne doit pas faire croire à un index absent :
        # on préfère indexer (au pire) que servir un document muet.
        return False


def _indexer_observation_reussie(
    db,
    art,
    observation,
    excerpt: str,
    payload: str,
    representation=None,
    *,
    representation_explicite: bool = False,
) -> None:
    """Fiche, fragments, vecteurs et FTS — UNIQUEMENT pour une observation `ok=True`.

    Les enrichissements restent au mieux : leur échec ne retire pas la ligne
    d'observation, qui est la preuve. Mais ils ne sont jamais appelés pour un
    refus (`ok=False`) : une tentative ratée doit laisser un audit, pas un index
    qui prétendrait avoir lu le fichier.

    LE TEXTE INDEXÉ EST CELUI DE LA REPRÉSENTATION COURANTE quand elle existe.
    L'observation reste bornée (`payload_ref` ≤ 16 k) parce qu'elle alimente le
    prompt ; elle ne borne plus la recherche. Sans représentation (artefacts
    legacy), on retombe sur l'ancien comportement borné à `PAYLOAD_CHARS`.
    """
    if representation is None:
        try:
            from backend.ingestion import representations as _reps

            representation = _reps.representation_pour(db, art.id)
        except Exception:
            representation = None
    try:
        from backend.search import cards as _cards

        _cards.ensure_card_on_import(db, art)
        _cards.revise_card_after_observation(db, art, observation, excerpt=excerpt)
    except Exception as exc:
        log.warning(
            "[observations] fiche non révisée pour %s : %s: %s",
            getattr(art, "id", "?"),
            type(exc).__name__,
            exc,
        )
    try:
        from backend.search import chunks as _chk
        from backend.db.models import ArtifactLink as _LinkC

        kind = (art.kind or "").lower()
        texte_representation = (
            (representation.markdown or "").strip()
            if representation is not None
            else ""
        )
        # RÉINDEXATION CONDITIONNELLE. Seule une NOUVELLE représentation
        # (`representation_id` explicitement fourni par l'ingestion) refait
        # l'index complet. Une observation de LECTURE sur un artefact déjà
        # indexé ne réécrit ni les fragments ni les vecteurs : elle ne
        # changerait rien et coûterait un découpage + un encodage complets.
        if (
            representation is not None
            and texte_representation
            and representation_explicite
        ):
            source = texte_representation
            representation_id = representation.id
        elif _chunks_deja_indexes(db, art.id, representation):
            return
        else:
            # Legacy : aucune représentation indexable, ou représentation
            # courante sans fragment. Le texte disponible est l'audit borné ;
            # on le re-borne EXPLICITEMENT pour que « fallback 16 k » soit une
            # décision, pas un effet de bord de l'appelant. Ce repli n'a lieu
            # qu'une fois : dès que des fragments existent, le test ci-dessus
            # coupe la réindexation.
            source = (
                payload
                if kind in ("text", "pdf")
                else (excerpt if len(excerpt) >= 200 else "")
            )[:PAYLOAD_CHARS]
            representation_id = None
        if source and len(source) >= 200:
            links = (
                db.query(_LinkC)
                .filter(_LinkC.artifact_id == art.id, _LinkC.removed_at.is_(None))
                .all()
            )
            scopes = [(l.scope_kind, l.scope_id) for l in links]
            if scopes:
                _chk.ensure_chunk_tables(db)
                _chk.ensure_chunks_for_artifact(
                    db,
                    art,
                    source,
                    scopes,
                    representation_id=representation_id,
                )
    except Exception as exc:
        log.warning(
            "[observations] fragments non écrits pour %s : %s: %s",
            getattr(art, "id", "?"),
            type(exc).__name__,
            exc,
        )


def _reveiller_extraction_documentaire() -> None:
    """Réveille la boucle documentaire APRÈS le commit — jamais bloquant.

    Import différé pour ne pas charger le planificateur (ni ses threads) au
    simple import des observations. Un réveil perdu ne perd pas le document :
    le backfill de 5 min reste le filet.
    """
    try:
        from backend.services import memoire_documentaire as _md

        _md._reveiller_extraction_documentaire()
    except Exception:  # pragma: no cover — le fond ne casse jamais l'écriture
        pass


def _executer_enqueue_documentaire(
    db, observation, hook: Callable[[object, object], None] | None
) -> bool:
    """Enqueue documentaire DANS la transaction de l'observation, sans la casser.

    Le hook s'exécute dans un SAVEPOINT : s'il échoue, seule sa partie est
    annulée, l'observation reste commitée et le backfill périodique recrée la
    ligne manquante. Le compteur et le journal sont tenus par le module
    documentaire — un échec ne doit jamais être silencieux, mais il ne doit
    jamais coûter la preuve d'extraction non plus.

    Rend `True` si le savepoint a réussi : l'appelant ne réveille la boucle
    documentaire qu'APRÈS le commit de l'observation, jamais avant.
    """
    try:
        with db.begin_nested():
            if hook is not None:
                hook(db, observation)
                return True
            from backend.services import memoire_documentaire as _md

            _md.enfiler_observation(db, observation)
        return True
    except Exception as exc:  # noqa: BLE001 — un ledger manquant se rattrape
        try:
            from backend.services import memoire_documentaire as _md

            _md.noter_echec_enqueue(observation, exc)
        except Exception:
            pass
        log.warning(
            "[observations] enqueue documentaire impossible pour %s : %s: %s",
            getattr(observation, "artifact_id", "?"),
            type(exc).__name__,
            exc,
        )
        return False


def _observations_citees(db) -> set[str] | None:
    """Les `obs_…` cités par `memory_entries.evidence_ids`, tous espaces confondus.

    La rétention ne supprime JAMAIS une observation citée : une preuve qui
    disparaît laisse une affirmation sans source. Une entrée illisible est
    ignorée sans bruit — elle ne désigne aucune observation.

    `None` — et non un ensemble vide — quand la LECTURE des citations échoue :
    un ensemble vide supposé autoriserait la purge de preuves peut-être citées.
    L'appelant doit alors annuler la purge.
    """
    from backend.db.models import MemoryEntry as _Entry

    citees: set[str] = set()
    try:
        lignes = (
            db.query(_Entry.evidence_ids).filter(_Entry.evidence_ids.isnot(None)).all()
        )
    except Exception as exc:
        log.warning(
            "[observations] preuves citées illisibles : %s: %s", type(exc).__name__, exc
        )
        return None
    for (brut,) in lignes:
        try:
            valeurs = json.loads(brut) if isinstance(brut, str) else brut
        except Exception:
            continue
        if not isinstance(valeurs, list):
            continue
        citees.update(
            str(valeur) for valeur in valeurs if str(valeur or "").startswith("obs_")
        )
    return citees


def _purger_observations_anciennes(
    db, artifact_id: str, input_fingerprint: str
) -> None:
    """Rétention d'une clé `(artifact_id, input_fingerprint)` — best-effort, journalisée.

    L'union conservée est celle de la doctrine :
      1. les `RETENTION_NON_CITEES` dernières non citées ;
      2. le dernier `completed` pour cette clé ;
      3. toute observation citée par `memory_entries.evidence_ids`.

    Le dernier `completed` est conservé MÊME s'il est plus vieux que cinq refus :
    purger le dernier succès après une série d'échecs supprimerait le texte que
    les lecteurs doivent continuer à servir. L'ordre total est
    `created_at DESC, rowid DESC` : SQLite n'a qu'une résolution à la seconde,
    sans ce départage « le dernier » serait ambigu.
    """
    from backend.db.models import Observation as _Obs

    try:
        lignes = (
            db.query(_Obs)
            .filter(
                _Obs.artifact_id == artifact_id,
                _Obs.input_fingerprint == input_fingerprint,
            )
            .order_by(_Obs.created_at.desc(), _sql_text("observations.rowid DESC"))
            .all()
        )
        if not lignes:
            return
        citees = _observations_citees(db)
        if citees is None:
            # Fail-closed : on ne purge pas sur un ensemble vide supposé.
            log.warning(
                "[observations] purge rétention ANNULÉE pour %s (%s) : "
                "les observations citées n'ont pas pu être lues",
                artifact_id,
                input_fingerprint,
            )
            return
        garder: set[str] = {
            observation.id for observation in lignes if observation.id in citees
        }
        non_citees = 0
        for observation in lignes:
            if observation.id in citees:
                continue
            if non_citees >= RETENTION_NON_CITEES:
                break
            garder.add(observation.id)
            non_citees += 1
        for observation in lignes:
            if (observation.status or "").lower() == "completed":
                garder.add(observation.id)
                break
        for observation in lignes:
            if observation.id not in garder:
                db.delete(observation)
        db.flush()
    except Exception as exc:
        # Best-effort, mais BRUYANT : une purge qui échoue en silence laisserait
        # croire que la borne est tenue alors qu'elle ne l'est pas.
        log.warning(
            "[observations] purge rétention impossible pour %s (%s) : %s: %s",
            artifact_id,
            input_fingerprint,
            type(exc).__name__,
            exc,
        )


def record_extraction_observation(
    artifact_id: str,
    tool_name: str,
    query: str | None,
    payload: str,
    *,
    ok: bool,
    tool_version: str,
    claim_token: str | None = None,
    model_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    enqueue_documentaire: Callable[[object, object], None] | None = None,
    representation_id: str | None = None,
) -> Observation | None:
    """Insère TOUJOURS une observation d'extraction — jamais d'upsert.

    Une extraction est une TENTATIVE : la muter ou fusionner deux tentatives
    contredit l'immutabilité de la trace. Chaque appel ajoute donc une ligne,
    avec son propre `status` (`completed` si `ok`, `failed` sinon).

    Seul `ok=True` déclenche la fiche, les chunks, les vecteurs et les lignes
    FTS. Un refus (`ok=False`) n'écrit QUE l'observation d'audit : un contenu
    refusé ne doit jamais produire d'index.

    `representation_id` lie l'observation à la représentation complète écrite
    AVANT elle. Le lien (`artifact_representations.source_text_observation_id`)
    est posé dans la même transaction, uniquement pour un succès : si
    l'observation échoue, la représentation reste valide avec un lien NULL. Le
    texte indexé est alors le Markdown COMPLET, pas le `payload_ref` borné.

    `claim_token` active le FENCING (Phase 3, tâche 3.3) : quand il est fourni,
    un UPDATE de garde sur `artifacts.ingest_claim_token` est exécuté DANS LA
    MÊME transaction que l'insertion. Zéro ligne signifie que le bail a été
    perdu : `BailPerdu` est levée et la transaction est annulée. Le worker doit
    alors s'arrêter sans écrire de résultat. Les écritures dérivées (fiche,
    fragments, vecteurs, FTS) restent dans cette même transaction, donc elles
    tombent avec l'observation si le bail est perdu.
    """
    if not artifact_id:
        return None
    from backend.db.database import SessionLocal as _SL
    from backend.db.models import Artifact as _Art, Observation as _Obs

    try:
        db = _SL()
    except Exception as exc:
        raise _non_écrite(tool_name, artifact_id, exc) from exc
    try:
        if claim_token:
            # Garde de FENCING : un UPDATE conditionnel ouvre l'écriture et
            # vérifie le jeton DANS LA MÊME transaction que l'insertion qui suit.
            # Un worker expiré ne peut donc pas insérer une observation ni ses
            # écritures dérivées.
            from backend.ingestion.etat import utc_naif as _utc_naif

            garde = (
                db.query(_Art)
                .filter(_Art.id == artifact_id, _Art.ingest_claim_token == claim_token)
                .update(
                    {"ingest_heartbeat_at": _utc_naif()},
                    synchronize_session=False,
                )
            )
            if not garde:
                db.rollback()
                raise BailPerdu(artifact_id, claim_token)
        art = db.query(_Art).filter(_Art.id == artifact_id).first()
        if art is None:
            return None
        blob_sha = _blob_sha_for(db, art)
        input_fingerprint = fingerprint(blob_sha, tool_name, tool_version, "", query)
        full = payload or ""
        excerpt = full[:EXCERPT_CHARS]
        observation = _Obs(
            id=f"obs_{uuid.uuid4().hex[:12]}",
            artifact_id=artifact_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_version=tool_version,
            model_id=model_id,
            input_fingerprint=input_fingerprint,
            query=(query or "")[:500],
            status="completed" if ok else "failed",
            result_json=json.dumps(
                {
                    "excerpt": excerpt,
                    "truncated": len(full) > EXCERPT_CHARS,
                    "chars_total": len(full),
                    **(
                        {"representation_id": representation_id}
                        if representation_id
                        else {}
                    ),
                },
                ensure_ascii=False,
            ),
            payload_ref=full[:PAYLOAD_CHARS],
        )
        db.add(observation)
        db.flush()
        # Le lien n'est posé QUE pour une extraction réussie. Une observation
        # d'échec (`ok=False`) ne doit pas s'approprier une représentation :
        # elle reste valide avec `source_text_observation_id = NULL`, et le
        # lien sera posé par la tentative qui réussira. Il est écrit DANS la
        # transaction de l'observation : si elle échoue, la représentation
        # (déjà commitée) garde un lien NULL.
        representation = None
        if ok:
            if representation_id:
                representation = _lier_representation_a_observation(
                    db, artifact_id, observation, representation_id
                )
            elif tool_name == "attachment_ingest":
                # Appelant historique : l'outil d'ingestion est le seul dont
                # l'observation audite la représentation courante. Les autres
                # lectures indexent le texte complet sans s'en déclarer source.
                representation = _lier_representation_a_observation(
                    db, artifact_id, observation, None
                )
        enqueue_reussi = False
        if ok:
            _indexer_observation_reussie(
                db,
                art,
                observation,
                excerpt,
                full,
                representation=representation,
                representation_explicite=bool(representation_id),
            )
            # Enqueue APRÈS l'indexation : l'exécution documentaire lira les
            # chunks créés par cette même transaction. Le savepoint interne
            # garantit qu'un échec d'enqueue ne défait jamais l'observation.
            enqueue_reussi = _executer_enqueue_documentaire(
                db, observation, enqueue_documentaire
            )
        _purger_observations_anciennes(db, artifact_id, input_fingerprint)
        db.commit()
        if enqueue_reussi:
            # APRÈS le commit : la boucle doit voir la ligne, pas un fantôme.
            _reveiller_extraction_documentaire()
        return observation
    except BailPerdu:
        # FENCING : la perte de bail n'est pas une panne d'écriture à envelopper.
        # Elle doit remonter telle quelle pour que le worker s'arrête.
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except ObservationNonÉcrite:
        raise
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        raise _non_écrite(tool_name, artifact_id, exc) from exc
    finally:
        try:
            db.close()
        except Exception:
            pass


def record_figure_observation(
    artifact_id: str,
    figure_id: str,
    description: str,
    *,
    crop_sha256: str | None,
    prompt_version: str,
    representation_id: str | None = None,
    page: int | None = None,
    region: int | None = None,
    claim_token: str | None = None,
    model_id: str | None = "LFM2.5-VL-3B",
    tool_version: str = "attachment_figures/phase1",
    db=None,
) -> Observation | None:
    """Ajoute l'observation d'audit d'une figure décrite — AUDIT SEUL.

    Cette fonction n'est PAS une extraction de texte : elle n'appelle ni la
    fiche (`ensure_card_on_import` / `revise_card_after_observation`), ni les
    fragments, ni les vecteurs, ni le FTS, ni l'enqueue mémoire. `tool_name`
    vaut `attachment_figures`, absent de `OUTILS_EXTRACTION` : les lecteurs de
    texte ne peuvent pas le confondre avec une extraction.

    L'empreinte inclut `figure_id`, `crop_sha256` et la version du prompt : deux
    crops différents, ou deux consignes différentes, ne partagent jamais une
    clé d'audit. La ligne est append-only comme toute observation.

    Quand `db` est fourni, l'insertion participe à la transaction de l'appelant
    (patch de représentation + statut de figure) et n'est pas commitée ici.
    Sinon, la fonction ouvre une session courte et committe elle-même. Le lien
    `artifact_figures.description_observation_id` est posé sous le
    `claim_token` quand il est fourni : un worker expiré lève `BailPerdu`.
    """
    texte = (description or "").strip()
    if not texte:
        raise ValueError("description vide : aucune observation de figure à écrire")
    if not artifact_id or not figure_id:
        return None

    propre = db is None
    session = db
    if session is None:
        from backend.db.database import SessionLocal as _SL

        try:
            session = _SL()
        except Exception as exc:
            raise _non_écrite("attachment_figures", artifact_id, exc) from exc
    try:
        from backend.db.models import Artifact as _Art, ArtifactFigure as _Fig

        art = session.query(_Art).filter(_Art.id == artifact_id).first()
        if art is None:
            return None
        blob_sha = _blob_sha_for(session, art)
        parametres = (
            f"figure={figure_id}|crop={crop_sha256 or ''}|prompt={prompt_version}"
        )
        empreinte = fingerprint(
            blob_sha, "attachment_figures", model_id, parametres, None
        )
        ligne = Observation(
            id=f"obs_{uuid.uuid4().hex[:12]}",
            artifact_id=artifact_id,
            run_id=None,
            tool_call_id=None,
            tool_name="attachment_figures",
            tool_version=tool_version,
            model_id=model_id,
            input_fingerprint=empreinte,
            query=f"figure={figure_id}",
            status="completed",
            result_json=json.dumps(
                {
                    "excerpt": texte[:EXCERPT_CHARS],
                    "truncated": len(texte) > EXCERPT_CHARS,
                    "chars_total": len(texte),
                    "figure_id": figure_id,
                    "representation_id": representation_id,
                    "page": page,
                    "region": region,
                    "crop_sha256": crop_sha256,
                    "prompt_version": prompt_version,
                },
                ensure_ascii=False,
            ),
            payload_ref=texte[:PAYLOAD_CHARS],
        )
        session.add(ligne)
        session.flush()

        if claim_token:
            garde = (
                session.query(_Fig)
                .filter(
                    _Fig.id == figure_id,
                    _Fig.claim_token == claim_token,
                )
                .update(
                    {"description_observation_id": ligne.id},
                    synchronize_session=False,
                )
            )
            if not garde:
                session.rollback()
                raise BailPerdu(artifact_id, claim_token)
        if propre:
            session.commit()
        return ligne
    except BailPerdu:
        raise
    except ObservationNonÉcrite:
        raise
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        if propre:
            raise _non_écrite("attachment_figures", artifact_id, exc) from exc
        raise
    finally:
        if propre:
            try:
                session.close()
            except Exception:
                pass

"""
Compaction Opencode-inspired — ne compacte que l'historique partagé user↔assistant
q-r.md §Prompt 3 + §Architecture finale des contextes fait foi.

But : éviter de perdre le fil dans les longues conversations (>100K tokens) sans exploser le 128K.
- On ne compacte JAMAIS les tool traces (ils sont éphémères)
- On garde les 15K tokens récents intacts, on résume seulement l'ancien
- Le compacteur reçoit : historique visible à compacter + briefs validés + refs fichiers.
  Il ne reçoit jamais : appels d'outils bruts, réflexions, registre complet, art_...
- Le checkpoint remplace l'ancien couvert ; les messages complets restent en DB.

Matrice contextes (q-r.md) :
- Orchestrateur : dernier message + historique récent + checkpoint précédent + pièces
  jointes actuelles + fichiers actifs du checkpoint + résultats d'outils du tour.
- Générateur : dernier message + historique récent + checkpoint précédent + brief validé.
- Compacteur : historique à compacter + briefs validés + refs fichiers structurées.
"""

import re

from backend.config.defaults import CHARS_PER_TOKEN

PRUNE_PROTECT = 40_000
# Seuil préventif par défaut pour les autres appelants (tests historiques) : compaction à
# 85 % du budget. `maybe_auto_compact` passe explicitement `threshold=1.0` pour ne pas
# cumuler cette marge avec CONTEXT_OVERHEAD_TOKENS (réserve unique de 20 000).
COMPACT_THRESHOLD = 0.85
PRESERVE_RECENT = 15_000

# Réserve unique retirée de la fenêtre réelle AVANT de décider la compaction.
# Calcul : prompt système du générateur ≈ 3,7K (11 211 car. à 3 car./token), sortie
# maximale du générateur 8,2K → ~11,9K ; orchestrateur + schémas d'outils ≈ 9,6K
# (jamais simultanés avec la sortie du générateur) ; le reste est la marge d'estimation.
# Une seule réserve : ne pas doubler avec COMPACT_THRESHOLD (voir maybe_auto_compact).
# Le plafonnement dur reste runtime.complete_chat (CONTEXT_WINDOW_TOO_SMALL).
CONTEXT_OVERHEAD_TOKENS = 20_000

CHECKPOINT_HEADER = "[Checkpoint de continuité — échanges antérieurs résumés]"

# Source unique du prompt : backend/prompts/compaction.py (q-r.md §Prompt 3).
# Import local pour éviter le cycle prompts -> inference.
try:
    from backend.prompts.compaction import COMPACTION_CONFIG as _CFG

    SYSTEM_COMPACTION = _CFG.system
except Exception:  # pragma: no cover - repli si prompts indisponibles
    SYSTEM_COMPACTION = """Tu es le module de compaction de Thot."""


def count_tokens(text: str) -> int:
    # Estimation à CHARS_PER_TOKEN caractères par token (partagée, prudente pour
    # le français). Le tokenizer réel n'est pas branché : le compte reste une
    # approximation, jamais une mesure — voir C06. Ne pas réintroduire d'import
    # du manager sans le brancher.
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_tokens_messages(messages: list[dict]) -> int:
    return sum(count_tokens(m.get("content", "")) for m in messages)


def should_compact(
    history_tokens: int, max_tokens: int = 128_000, threshold: float | None = None
) -> bool:
    """Le seuil est franchi selon le facteur ``threshold`` appliqué au BUDGET.

    `max_tokens` n'est pas la fenêtre totale du modèle : c'est le budget d'historique
    une fois retirés les prompts fixes et les sorties (voir `CONTEXT_OVERHEAD_TOKENS`).
    Le défaut 128K ne vaut que pour un appelant qui ignore la fenêtre réelle.

    ``threshold`` vaut ``COMPACT_THRESHOLD`` (0,85) par défaut : compaction préventive
    avec marge. ``threshold=1.0`` signifie que l'historique a atteint le budget, sans
    marge supplémentaire — le budget porte déjà la réserve (`CONTEXT_OVERHEAD_TOKENS`),
    donc aucune double précaution. Voir `maybe_auto_compact`.
    """
    seuil = COMPACT_THRESHOLD if threshold is None else threshold
    return history_tokens / max_tokens >= seuil


def get_active_snapshot(conversation_id: str) -> dict | None:
    """Retourne le snapshot actif C06 si présent (DB)."""
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import ContextSnapshot

        db = SessionLocal()
        try:
            snap = (
                db.query(ContextSnapshot)
                .filter(ContextSnapshot.conversation_id == conversation_id)
                .order_by(ContextSnapshot.revision.desc())
                .first()
            )
            if snap:
                return {
                    "id": snap.id,
                    "revision": snap.revision,
                    "covered_through_message_id": snap.covered_through_message_id,
                    "summary": snap.summary,
                }
        finally:
            try:
                db.close()
            except:
                pass
    except Exception:
        pass
    return None


def apply_snapshot_to_history(
    history: list[dict], snapshot: dict | None, recent_tokens: int = PRESERVE_RECENT
) -> tuple[list[dict], str | None]:
    """Injecte le checkpoint en tête et retire l'ancien couvert (q-r.md §matrice contextes).

    history : [{role, content}] ; snapshot : {summary, covered_through_message_id}.
    Retourne (historique, texte_checkpoint | None). Pur, jamais bloquant.
    - snapshot absent/vide → inchangé, rien à injecter.
    - déjà injecté (tête système checkpoint/résumé) → inchangé.
    - covered connu et retrouvé (par id) → on coupe après lui.
    - sinon repli fenêtre récente : le checkpoint couvre l'ancien ; si rien ne dépasse
      la fenêtre, on n'injecte rien (pas de duplication d'un historique déjà complet).
    """
    try:
        summary = str((snapshot or {}).get("summary") or "").strip()
        if not summary:
            return history, None
        hist = list(history or [])
        if hist and hist[0].get("role") == "system":
            head = str(hist[0].get("content") or "")
            if head.startswith(
                (
                    CHECKPOINT_HEADER,
                    "# CHECKPOINT_CONVERSATION",
                    "[Résumé conversation]",
                )
            ):
                return hist, None
        covered = (snapshot or {}).get("covered_through_message_id")
        if covered:
            idx = max(
                [
                    i
                    for i, m in enumerate(hist)
                    if isinstance(m, dict) and m.get("id") == covered
                ],
                default=None,
            )
            if idx is not None:
                return hist[idx + 1 :], f"{CHECKPOINT_HEADER}\n{summary}"
        kept: list[dict] = []
        tot = 0
        for m in reversed(hist):
            t = max(1, len(str((m or {}).get("content") or "")) // CHARS_PER_TOKEN)
            if kept and tot + t > recent_tokens:
                break
            kept.append(m)
            tot += t
        rest = list(reversed(kept))
        if len(rest) >= len(hist):
            return hist, None
        return rest, f"{CHECKPOINT_HEADER}\n{summary}"
    except Exception:
        return history, None


def prune_old_tool_results(messages: list[dict]) -> list[dict]:
    total = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "tool":
            total += count_tokens(messages[i].get("content", ""))
            if total > PRUNE_PROTECT:
                messages[i] = {
                    **messages[i],
                    "content": "[Ancien résultat outil effacé]",
                }
    return messages


_PUBLIC_REF_RE = re.compile(r"doc_[1-9]\d*\Z")
_INTERNAL_TOKEN_RE = re.compile(r"\b(?:art_|sti_)[A-Za-z0-9_]+\b", re.IGNORECASE)
_OBSERVATION_REF_RE = re.compile(r"obs_[A-Za-z0-9_]+(?::[A-Za-z0-9_-]+)?")


def _is_internal_id(value: str) -> bool:
    """Identifiant technique interne, jamais exposé aux modèles (q-r.md §Preuves). Pur."""
    v = (value or "").strip()
    lowered = v.lower()
    return lowered.startswith("art_") or lowered.startswith("sti_")


def _redact_internal_ids(value: str) -> str:
    """Retire les IDs techniques des sections visibles avant le compacteur."""
    return _INTERNAL_TOKEN_RE.sub("[identifiant interne retiré]", value or "")


def _contains_internal_id(value: str) -> bool:
    return _INTERNAL_TOKEN_RE.search(value or "") is not None


def _sanitize_ref_id(value: str | None) -> str:
    """Renvoie uniquement un label public ``doc_N`` conversation-local."""
    v = (value or "").strip()
    if not v or _is_internal_id(v) or _PUBLIC_REF_RE.fullmatch(v) is None:
        return ""
    return v


def _public_ref_id(ref: dict) -> str:
    """Lit le champ public canonique, avec une compatibilité minimale des anciens alias.

    ``public_id`` est prioritaire. Les alias ``doc_label``/``artifact_id``/``id`` ne
    sont acceptés que s'ils contiennent déjà un ``doc_N`` public ; un ``art_`` ou
    ``sti_`` ne peut jamais être promu par ce chemin de compatibilité.
    """
    if "public_id" in ref:
        return _sanitize_ref_id(str(ref.get("public_id") or ""))
    for key in ("doc_label", "artifact_id", "id"):
        candidate = _sanitize_ref_id(str(ref.get(key) or ""))
        if candidate:
            return candidate
    return ""


def _summary_has_only_allowed_public_ids(
    summary: str, file_refs: list[dict] | None
) -> bool:
    allowed = {
        public_id
        for ref in (file_refs or [])
        if isinstance(ref, dict)
        for public_id in [_public_ref_id(ref)]
        if public_id
    }
    mentioned = set(re.findall(r"\bdoc_[1-9]\d*\b", summary or ""))
    return mentioned.issubset(allowed)


def _persisted_observations(observation_ids: set[str]) -> set[str]:
    """Ids d'observation réellement en base. Une panne de DB vaut zéro : on refuse."""
    if not observation_ids:
        return set()
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import Observation

        db = SessionLocal()
        try:
            rows = (
                db.query(Observation.id)
                .filter(Observation.id.in_(sorted(observation_ids)))
                .all()
            )
            return {str(row[0]) for row in rows}
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        return set()


def observations_absentes(summary: str) -> tuple[str, ...]:
    """Les ``obs_…`` que le checkpoint cite et que la base ne contient pas.

    Vide = checkpoint recevable. On rend l'ÉCART et non un booléen parce qu'il
    faut pouvoir le revérifier plus tard SANS rappeler le modèle : c'est
    exactement ce que `_refus_encore_valable` fait pour ne pas rejouer un refus.

    Un suffixe de position (``obs_abc:1``) est toléré si l'observation elle-même
    existe. Une panne de DB vaut zéro observation persistée, donc zéro
    réception : le fail-closed du départ est ici, intact.
    """
    mentionne = set(_OBSERVATION_REF_RE.findall(summary or ""))
    if not mentionne:
        return ()
    recherche = mentionne | {ref.split(":", 1)[0] for ref in mentionne}
    persistees = _persisted_observations(recherche)
    # On rend l'identifiant de l'OBSERVATION, jamais sa position : c'est elle qui
    # manque, et c'est elle — sans suffixe — qu'un SELECT doit retrouver plus tard
    # pour savoir si la cause du refus tient encore.
    return tuple(
        sorted(
            {
                ref.split(":", 1)[0]
                for ref in mentionne
                if ref not in persistees and ref.split(":", 1)[0] not in persistees
            }
        )
    )


def _summary_has_only_persisted_observations(summary: str) -> bool:
    """Un checkpoint ne doit propager aucune observation inventée.

    Le résumé est réinjecté dans tous les tours suivants : une preuve ``obs_…`` qui
    n'existe pas en base deviendrait une référence permanente.
    """
    return not observations_absentes(summary)


# Les checkpoints REFUSÉS, mémorisés par conversation : les ``obs_…`` que le dernier
# d'entre eux citait et que la base ne contenait pas.
#
# Un refus n'est pas une panne passagère. Tant que sa cause tient, le compacteur ne
# va pas « essayer mieux » : il relit le même historique et renomme les mêmes preuves
# inexistantes. Le rappeler à chaque tour pour obtenir le même refus coûte un appel
# modèle par tour — et pendant ce temps le CONTEXTE, lui, grossit, sans que personne
# sache pourquoi il ne se compacte jamais.
#
# Mémoire du PROCESS, volontairement : la cause est une ligne d'observation absente,
# donc un fait que la base détient et qu'un redémarrage doit RECONSTATER plutôt que de
# croire sur parole. Rien à persister, rien à migrer.
_REFUS: dict[str, tuple[str, ...]] = {}
_REFUS_MAX_CONVERSATIONS = 512


def _memoriser_refus(conversation_id: str | None, fantomes: tuple[str, ...]) -> None:
    """Retient le refus, borné pour qu'un long processus n'accumule pas les
    conversations déjà fermées. L'éviction est FIFO, pas LRU : un refus vieux de
    mille conversations ne vaut plus rien — il suffit qu'il tienne le tour où il a
    été prononcé."""
    if not conversation_id or not fantomes:
        return
    if conversation_id not in _REFUS and len(_REFUS) >= _REFUS_MAX_CONVERSATIONS:
        del _REFUS[next(iter(_REFUS))]
    _REFUS[conversation_id] = fantomes


def _refus_encore_valable(conversation_id: str | None) -> str | None:
    """Motif du refus mémorisé s'il tient toujours ; ``None`` s'il faut réessayer.

    Un seul SELECT pour le savoir, jamais un appel modèle — c'est tout l'intérêt.
    La cause d'un refus est une ABSENCE : si ces observations sont désormais en
    base, la cause a été levée entre-temps (une écriture qui n'avait pas abouti,
    une reprise), le refus ne vaut plus rien et le checkpoint est réessayé. Le
    fail-closed, lui, ne bouge pas : tant que la preuve manque, rien n'est ni
    persisté ni injecté.
    """
    if not conversation_id:
        return None
    fantomes = _REFUS.get(conversation_id)
    if not fantomes:
        return None
    if _persisted_observations(set(fantomes)) >= set(fantomes):
        del _REFUS[conversation_id]
        return None
    return "observation absente de la base : " + ", ".join(fantomes)


def _journaliser(motif: str) -> None:
    """Tout rejet passe par ici, sur stderr.

    Un seul point d'écriture : les trois motifs existaient en trois copies du même
    bloc try/except, et le troisième — celui qu'on oublie de faire évoluer — est
    précisément celui qui devient muet. Jamais bloquant : un `log` qui lèverait
    interromprait la compaction pour un problème de journalisation.
    """
    try:
        from backend.inference.ipc import log as _log

        _log(f"[compaction] {motif}")
    except Exception:
        pass


def _signaler(refus: list[str] | None, motif: str) -> None:
    """Dépose le motif dans la boîte que l'appelant a fournie.

    `compact_history` rend un HISTORIQUE, pas un diagnostic : y ajouter une rubrique
    « pourquoi il n'y a pas de checkpoint » la ferait entrer dans le prompt du tour
    suivant, et son `None` ne distingue pas « rien à signaler » de « panne ». Le
    refus sort donc par un paramètre que l'appelant fournit — absent, il est jeté,
    comme tout diagnostic que personne n'a demandé.
    """
    if refus is not None:
        refus.append(motif)


def build_compaction_prompt(
    old: list[dict],
    briefs: list[str] | None = None,
    file_refs: list[dict] | None = None,
) -> str:
    """Construit l'entrée du compacteur (q-r.md §Prompt 3) : historique + briefs + refs.

    Pur, testable. Le compacteur ne reçoit jamais :
    - traces d'outils brutes (seul l'historique visible user↔assistant est transmis),
    - réflexions, erreurs runtime, registre complet,
    - identifiants internes art_... / sti_... (filtrés ici, jamais transmis).
    Seuls les briefs validés structurés et les références doc_N / obs_ sont transmis.
    """
    # Historique visible uniquement : jamais de role tool, jamais de pseudo-appel.
    safe_old: list[dict] = []
    for m in old or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") not in ("user", "assistant", "system"):
            continue
        content = str(m.get("content") or "")
        if "[Ancien résultat outil effacé]" in content:
            continue
        safe_message = dict(m)
        safe_message["content"] = _redact_internal_ids(content)
        safe_old.append(safe_message)
    conv = "\n\n".join(
        f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in safe_old
    )
    safe_briefs = [
        str(brief)
        for brief in (briefs or [])
        if str(brief or "").strip() and not _contains_internal_id(str(brief))
    ]
    briefs_txt = "\n\n".join(
        f"[Brief validé {i + 1}]:\n{brief}" for i, brief in enumerate(safe_briefs)
    )
    refs_lines: list[str] = []
    for r in (file_refs or [])[:20]:
        if not isinstance(r, dict):
            continue
        public_id = _public_ref_id(r)
        kind = _redact_internal_ids(str(r.get("kind") or r.get("type") or "")).strip()
        name = _redact_internal_ids(str(r.get("name") or "")).strip()
        if not public_id and not name and not kind:
            continue
        refs_lines.append(
            f"- public_id: {public_id or '—'} | "
            f"type: {kind or '—'} | "
            f"nom: {name or '—'} | "
            f"statut: {'disponible' if r.get('available', True) else 'indisponible'}"
        )
    refs_txt = "\n".join(refs_lines) if refs_lines else "(aucune référence fournie)"
    parts = [
        SYSTEM_COMPACTION,
        "\n\n## Historique visible à compacter\n" + (conv or "(vide)"),
        "\n\n## Briefs validés\n" + (briefs_txt or "(aucun)"),
        "\n\n## Références de fichiers (backend)\n" + refs_txt,
        "\n\nProduis uniquement le checkpoint au format imposé.",
    ]
    prompt = "\n".join(parts)
    # Garde-fou final : aucun id interne ne doit fuiter dans l'entrée compacteur.
    # (Le SYSTEM_COMPACTION lui-même mentionne art_... uniquement pour l'interdire.)
    return prompt


def load_recent_briefs(conversation_id: str, limit: int = 10) -> list[str]:
    """Charge les payloads JSON persistés, sans les re-rendre.

    La colonne ``brief`` est une vue UI rendue et peut avoir été tronquée ou reformulée.
    Le compacteur doit consommer exactement ``Message.brief_json`` ; une ancienne ligne
    sans ce champ est volontairement ignorée plutôt que reconstruite depuis le rendu.
    """
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import Message

        db = SessionLocal()
        try:
            rows = (
                db.query(Message)
                .filter(
                    Message.conv_id == conversation_id,
                    Message.role == "assistant",
                    Message.brief_json.isnot(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
                .all()
            )
            out = [
                r.brief_json
                for r in rows
                if isinstance(r.brief_json, str) and r.brief_json.strip()
            ]
            return list(reversed(out))
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        pass
    return []


def compact_history(
    messages: list[dict],
    llm,
    conversation_id: str | None = None,
    revision: int | None = None,
    briefs: list[str] | None = None,
    file_refs: list[dict] | None = None,
    refus: list[str] | None = None,
    preserve_recent: int | None = None,
    max_summary_tokens: int = 2048,
) -> list[dict]:
    """Compaction C06 / q-r.md §Prompt 3 : produit un checkpoint, persiste ContextSnapshot.

    - Ne compacte que l'historique partagé user↔assistant ; les tool traces sont prunées.
    - Garde les PRESERVE_RECENT tokens récents intacts (ou `preserve_recent`),
      résume seulement l'ancien.
    - Le résumé persisté est le checkpoint markdown (# CHECKPOINT_CONVERSATION...) ;
      l'injection en tête utilise CHECKPOINT_HEADER + résumé (cf. apply_snapshot_to_history).
    - Panne LLM → historique pruné seul, jamais de perte de faits user/assistant.

    `refus` est le réceptacle des rejets : la fonction y dépose une phrase par
    checkpoint écarté, jamais `None` pour « rien à signaler ». Le retour reste un
    historique — un rejet doit se voir SANS entrer dans le prompt du tour suivant.
    Un appelant qui ne le fournit pas perd ce diagnostic, et rien d'autre.
    """
    messages = prune_old_tool_results(messages[:])

    # AVANT l'appel modèle, et c'est le seul endroit où l'économie est possible : on
    # ne peut pas savoir ce que le compacteur dirait sans le payer. Un refus déjà
    # prononcé et toujours valable ne le rappelle donc pas — même verdict, même
    # repli sûr, sans payer une seconde fois pour le découvrir.
    motif = _refus_encore_valable(conversation_id)
    if motif:
        _journaliser(
            f"checkpoint écarté : {motif} — compacteur non rappelé, cause inchangée"
        )
        _signaler(
            refus,
            f"checkpoint écarté ({motif}) : compacteur non rappelé, cause inchangée",
        )
        return messages

    if briefs is None and conversation_id:
        briefs = load_recent_briefs(conversation_id)

    # split recent
    recent_limit = (
        PRESERVE_RECENT if preserve_recent is None else max(1, preserve_recent)
    )
    recent_tokens = 0
    split_idx = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        t = count_tokens(messages[i].get("content", ""))
        if recent_tokens + t <= recent_limit:
            recent_tokens += t
            split_idx = i
        else:
            break

    old = messages[:split_idx]
    recent = messages[split_idx:]

    if not old:
        return messages

    prompt = build_compaction_prompt(old, briefs=briefs, file_refs=file_refs)
    try:
        summary = llm.create_completion(
            prompt=prompt, max_tokens=max_summary_tokens, temperature=0.3
        )["choices"][0]["text"].strip()
    except Exception:
        # Échec → conserver ancien snapshot C06
        return messages
    if not summary:
        return messages
    # Anti-fuite : un checkpoint contenant un id interne art_... / sti_... n'est jamais
    # persisté ni injecté. On garde l'historique pruné seul (l'ancien snapshot reste actif).
    if _INTERNAL_TOKEN_RE.search(summary) is not None:
        _journaliser("checkpoint rejeté : id interne art_/sti_ détecté")
        return messages

    if file_refs is not None and not _summary_has_only_allowed_public_ids(
        summary, file_refs
    ):
        _journaliser("checkpoint rejeté : identifiant public hors inventaire")
        return messages

    # Une observation inventée dans le checkpoint deviendrait une preuve permanente
    # pour tous les tours suivants : elle n'est jamais persistée. Le refus est EN PLUS
    # mémorisé, pour que le tour suivant n'achète pas au modèle le même constat.
    fantomes = observations_absentes(summary)
    if fantomes:
        motif = "observation absente de la base : " + ", ".join(fantomes)
        _memoriser_refus(conversation_id, fantomes)
        _journaliser(f"checkpoint rejeté : {motif}")
        _signaler(refus, f"checkpoint rejeté ({motif})")
        return messages

    # Persiste snapshot si contrat rempli
    if conversation_id and revision is not None:
        try:
            from backend.db.database import SessionLocal
            from backend.db.models import ContextSnapshot
            import uuid

            db = SessionLocal()
            try:
                covered = (
                    old[-1].get("id")
                    if old and isinstance(old[-1], dict) and old[-1].get("id")
                    else None
                )
                snap = ContextSnapshot(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    revision=revision,
                    covered_through_message_id=covered,
                    summary=summary,
                )
                db.add(snap)
                db.commit()
            except Exception:
                db.rollback()
            finally:
                try:
                    db.close()
                except:
                    pass
        except Exception:
            pass

    return [{"role": "system", "content": f"{CHECKPOINT_HEADER}\n{summary}"}] + recent


def maybe_auto_compact(
    manager,
    conversation_id: str,
    history: list[dict],
    revision: int,
    file_refs: list[dict] | None = None,
    max_tokens: int = 128_000,
    runtime=None,
    refus: list[str] | None = None,
) -> tuple[list[dict], str | None]:
    """Déclenchement automatique (q-r.md : selon mécanisme existant + budget de contexte).

    - Le déclenchement se fait à la réserve près (fenêtre − 20 000 tokens), pas à 85 %
      d'un budget déjà réduit (le budget porte déjà la réserve).
    - Si l'historique (checkpoint inclus) dépasse le budget, génère un nouveau checkpoint
      via le compacteur (même LFM2.5-8B, autre paquet de contexte), le persiste, puis
      remplace l'ancien couvert via apply_snapshot_to_history.
    - Retourne (historique_à_utiliser, texte_checkpoint | None). Jamais bloquant :
      en cas de panne, retourne l'historique pruné seul.
    - Les messages complets restent en DB ; seul le contexte actif est remplacé.

    `refus` relaie les rejets de `compact_history`. C'est LE seul moyen pour
    l'orchestrateur d'apprendre qu'il n'y a pas de checkpoint alors qu'il en
    attendait un : sans lui, un rejet répété ne se voyait que sur stderr, et un
    tour comme l'autre se lisaient exactement pareil.
    """
    try:
        total = count_tokens_messages(history or [])
        budget = max(1, int(max_tokens))
        # Le déclenchement se fait à la réserve près (fenêtre − 20 000) : le budget
        # porte déjà la réserve, donc pas de marge supplémentaire à 85 % (sinon la
        # réserve doublerait). `threshold=1.0` désactive la marge du facteur.
        if not should_compact(total, budget, threshold=1.0):
            return history, None
        # Ce qu'on garde intact est borné par le budget réel, pas par la constante
        # 15K pensée pour 128K : sur une fenêtre 32K, garder 15K ne laisserait
        # presque rien à résumer. Le résumé lui-même reste court pour tenir dans
        # la fenêtre du compacteur.
        preserve = min(PRESERVE_RECENT, max(1_024, budget // 2))
        resume_max = min(2_048, max(512, budget // 8))
        briefs = load_recent_briefs(conversation_id) if conversation_id else []
        try:
            if runtime is not None:
                from backend.inference.runtime import RuntimeModelAdapter

                compacted = compact_history(
                    list(history or []),
                    RuntimeModelAdapter(runtime),
                    conversation_id=conversation_id,
                    revision=revision,
                    briefs=briefs,
                    file_refs=file_refs,
                    refus=refus,
                    preserve_recent=preserve,
                    max_summary_tokens=resume_max,
                )
            else:
                with manager.use_model(
                    "LFM2.5-8B", estimated_tokens=max(2_048, total + 4_096)
                ) as llm:
                    compacted = compact_history(
                        list(history or []),
                        llm,
                        conversation_id=conversation_id,
                        revision=revision,
                        briefs=briefs,
                        file_refs=file_refs,
                        refus=refus,
                        preserve_recent=preserve,
                        max_summary_tokens=resume_max,
                    )
        except Exception:
            return prune_old_tool_results(list(history or [])), None
        # compact_history a déjà persisté le snapshot ; recharge le snapshot actif
        # pour couper proprement après covered_through_message_id.
        try:
            snap = get_active_snapshot(conversation_id)
            trimmed, checkpoint = apply_snapshot_to_history(
                list(history or []), snap, recent_tokens=preserve
            )
            if checkpoint:
                return [{"role": "system", "content": checkpoint}] + [
                    {"role": m.get("role", "user"), "content": m.get("content", "")}
                    for m in trimmed
                    if m.get("role") in ("user", "assistant", "system")
                ], checkpoint
        except Exception:
            pass
        return compacted, (
            compacted[0].get("content")
            if compacted and compacted[0].get("role") == "system"
            else None
        )
    except Exception:
        try:
            return prune_old_tool_results(list(history or [])), None
        except Exception:
            return history, None

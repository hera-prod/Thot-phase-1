# services/document_extraction_llm.py — extraction documentaire par LLM (8.4).
#
# D'OÙ VIENT CE FICHIER : `memoire_documentaire` portait déjà le hook
# `extracteur_structure` et le repli déterministe, mais aucun extracteur réel ne
# l'alimentait. Ce module est cet extracteur : il appelle UN outil forcé,
# valide un schéma strict, vérifie chaque citation contre le fragment DÉSIGNÉ,
# et rend des `FaitDocument` prêts pour le remplacement versionné.
#
# DÉCISION « LLM OU DÉTERMINISTE » :
#   - le LLM n'est JAMAIS appelé sur un texte vide : l'appelant
#     (`extraire_faits_document`) sort avant ;
#   - il est appelé sur un texte BORNÉ à 16 000 caractères (défense) mais le
#     prompt ne porte que des PHRASES CANDIDATES (~4 000 caractères) choisies
#     dans les fragments persistés de la révision courante : le modèle n'a pas
#     besoin de relire un document entier pour citer un fait ;
#   - sans aucune phrase candidate (ni chiffre, ni entité, ni verbe déclaratif),
#     le LLM n'est PAS appelé : le déterministe est le seul moteur qui puisse
#     encore trouver quelque chose, et un prompt vide ne fait qu'inventer ;
#   - une panne, un schéma invalide, un `chunk_id` inconnu ou une citation qui
#     n'appartient pas au fragment désigné rendent `None` (aucun fait valide à
#     croire) : `memoire_documentaire` replie alors sur le déterministe ;
#   - une liste `faits=[]` VALIDE est une réponse : le document n'a rien de
#     mémorable. Elle n'est pas un échec et ne déclenche pas de repli.
#
# CE MODULE NE CHARGE AUCUN MODÈLE À L'IMPORT. Le runtime est injecté (tests) ou
# fourni par le sidecar via `installer_extracteur_llm(get_chat_runtime)`. Le
# manager appelle `use_model(kind="ingestion")`, qui acquiert le coordinateur GPU
# avec l'attente courte de l'ingestion (10 s) : une extraction mémoire cède au
# chat au lieu de tenir le verrou `activity` pendant 120 s. `ResourceBusy`
# REMONTE telle quelle pour que le ledger marque `deferred` (contention, pas
# échec).
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from backend.inference.resource_coordinator import ResourceBusy
from backend.inference.runtime import RuntimeRequest
from backend.prompts.document_facts import (
    EXTRACTION_DOCUMENT_TOOL,
    MAX_ENTREE_LLM_CHARS,
    MAX_FAITS_LLM,
    SYSTEM_DOCUMENT_FACTS,
    FaitSchema,
    construire_message_utilisateur,
)
from backend.services import memoire
from backend.services import memoire_documentaire as _md

log = logging.getLogger(__name__)

VERSION_EXTRACTEUR_LLM = "doc-faits-llm/1"
NOM_OUTIL = "submit_document_facts"
MODELE_EXTRACTION = "LFM2.5-8B"
# Doit rester STRICTEMENT sous `memoire_documentaire.BUDGET_SECONDES` (60 s) :
# un document ne peut pas consommer plus que le budget d'une passe. La branche
# `manager` ne peut pas annuler un `create_chat_completion` en cours (l'API
# llama_cpp n'expose pas de timeout) ; cette borne garantit qu'un appel dérivé
# du budget reste compatible, et `RuntimeRequest.timeout_s` porte le timeout dur
# sur le chemin runtime.
#
# 45 s couvre le GPU (640 tokens ≈ 13–14 s). Sur CPU, 640 tokens peuvent
# approcher ~52 s : le timeout reste un MAJORANT, l'appel dépasse alors 45 s et
# retombe en repli déterministe. On ne relève pas le timeout : les deux chemins
# doivent rester sous le budget de passe de 60 s.
TIMEOUT_LLM_S = 45.0
# Borne DURE de génération, transmise aux DEUX chemins (`runtime.complete_chat`
# et `manager.create_chat_completion`). Le schéma autorise 5 faits ; sur
# `2206.07682v2.pdf`, une réponse naturelle fait 529 tokens et le modèle
# s'arrête seul. 512 tronquait un run sur deux ; 640 couvre cette queue sans
# ajouter de temps quand la génération se termine normalement. Une coupure en
# plein JSON reste INVALIDE : `finish_reason="length"` OU un nombre de tokens
# générés égal au plafond rendent `None`, et le déterministe reprend.
MAX_TOKENS_LLM = 640

# Pré-filtrage d'entrée : le benchmark a montré que les 16 000 caractères bruts
# coûtaient 4,1–4,7 k tokens de prompt et dégradaient les citations. On ne
# transmet que des phrases candidates réparties entre les fragments, sous cette
# borne. `MAX_ENTREE_LLM_CHARS` (16 000) reste la défense sur le texte source.
MAX_PASSAGES_LLM_CHARS = 4_000
MIN_PASSAGE_LLM_CHARS = 40
MAX_PASSAGE_LLM_CHARS = 300

_NOMBRE_RE = re.compile(r"\d")
_PHRASE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
# Verbes déclaratifs réels, français ET anglais : un document technique alterne
# souvent les deux (abstract anglais, corps français, ou l'inverse). Le repli
# déterministe garde ses propres motifs ; celui-ci ne sert qu'à choisir les
# passages à montrer au modèle.
_VERBE_DECLARATIF_FR_EN_RE = re.compile(
    r"\b(?:est|sont|était|étaient|utilise|utilisent|permet|permettent|"
    r"réduit|réduisent|doit|doivent|contient|contiennent|exige|exigent|"
    r"mesure|mesurent|coûte|coûtent|offre|offrent|améliore|améliorent|"
    r"augmente|augmentent|supporte|supportent|produit|produisent|"
    r"stocke|stockent|conserve|conservent|correspond|correspondent|"
    r"représente|représentent|définit|définissent|"
    r"is|are|was|were|uses|use|allows|allow|reduces|reduce|requires|"
    r"require|contains|contain|provides|provide|improves|improve|"
    r"increases|increase|supports|support|enables|enable|has|have|"
    r"can|may|must|will|shows|show|achieves|achieve|outperforms|outperform)\b",
    re.IGNORECASE,
)


def _estimer_tokens(messages: tuple[Mapping[str, Any], ...]) -> int:
    """Estimation volontairement large : le schéma d'outil compte aussi."""
    contenu = sum(len(str(message.get("content") or "")) for message in messages)
    return (contenu + len(json.dumps(EXTRACTION_DOCUMENT_TOOL))) // 4 + 1024


def _charger_fragments(
    db: Any,
    *,
    artifact_id: str,
    card_revision: int,
) -> list[tuple[str, int, str]]:
    """Fragments PERSISTÉS de la révision courante, dans l'ordre des ordinaux.

    `card_revision > 0` est une exigence de provenance : une citation de la
    révision courante ne doit jamais être adossée à un fragment homonyme d'une
    révision antérieure. Sans révision connue (0), on lit ce qui est persisté.
    """
    from backend.db.models import ArtifactChunk as _Chk

    requete = db.query(_Chk).filter(_Chk.artifact_id == artifact_id)
    if int(card_revision or 0) > 0:
        requete = requete.filter(_Chk.card_revision == int(card_revision))
    rows = requete.order_by(_Chk.ordinal.asc(), _Chk.id.asc()).all()
    return [
        (str(row.id), int(row.ordinal or 0), str(row.text or ""))
        for row in rows
        if str(row.text or "").strip()
    ]


def _normaliser(texte: Any) -> str:
    """Forme de comparaison : espaces réduits, casse repliée."""
    return " ".join(str(texte or "").lower().split())


def _fragments_dans_texte(
    fragments: list[tuple[str, int, str]], texte: str
) -> list[tuple[str, int, str]]:
    """Ne garde que les fragments réellement contenus dans le texte borné.

    Les fragments persistés sont la source citable, mais ils doivent appartenir
    à l'observation qu'on extrait : un fragment d'une révision antérieure ou
    d'un texte plus long ne doit pas produire un fait « courant ». Le texte est
    déjà borné à 16 000 caractères par l'appelant.
    """
    texte_normalise = _normaliser(texte)
    return [
        fragment
        for fragment in fragments
        if _normaliser(fragment[2]) in texte_normalise
    ]


def _normaliser_passage(texte: Any) -> str:
    """Espaces réduits, sans toucher aux caractères citables."""
    return " ".join(str(texte or "").split())


def _passage_candidat(phrase: str) -> bool:
    """Une phrase mérite-t-elle d'être montrée au LLM ?

    Trois signaux suffisent et se cumulent par OU : un chiffre, une entité
    (mot capitalisé après le premier), ou un verbe déclaratif FR/EN. Une phrase
    interrogative n'affirme rien : elle est exclue.
    """
    if not (MIN_PASSAGE_LLM_CHARS <= len(phrase) <= MAX_PASSAGE_LLM_CHARS):
        return False
    if phrase.endswith("?"):
        return False
    return bool(
        _NOMBRE_RE.search(phrase)
        or _md._a_entite(phrase)
        or _VERBE_DECLARATIF_FR_EN_RE.search(phrase)
    )


def _selectionner_passages_llm(
    fragments: list[tuple[str, int, str]],
    max_chars: int = MAX_PASSAGES_LLM_CHARS,
) -> list[tuple[str, int, str]]:
    """Phrases candidates des fragments, réparties et bornées — fonction PURE.

    Le prompt ne doit pas rejouer le document : il doit offrir au modèle des
    phrases assez denses pour porter un fait. La sélection procède par TOURS
    (une phrase par fragment au premier tour, puis la deuxième, etc.) pour
    qu'un fragment bavard ne consomme pas tout le budget, puis elle rétablit
    l'ordre source. `chunk_id` et `ordinal` sont conservés tels quels : ce sont
    eux que le modèle doit citer, et eux que la validation vérifiera.

    Budget : `max_chars` compte les phrases et leur séparateur, jamais les
    en-têtes de fragment. `construire_message_utilisateur` applique ensuite sa
    propre borne, qui inclut ces en-têtes.
    """
    if not fragments:
        return []
    budget = max(0, int(max_chars))
    if budget <= 0:
        return []

    candidats: list[list[tuple[str, int, str]]] = []
    for chunk_id, ordinal, texte in fragments:
        phrases = []
        for brut in _PHRASE_SPLIT_RE.split(str(texte or "")):
            phrase = _normaliser_passage(brut)
            if _passage_candidat(phrase):
                phrases.append((str(chunk_id), int(ordinal or 0), phrase))
        candidats.append(phrases)

    # Ordre des TOURS : (tour, index du fragment). On trie ensuite la sélection
    # par (fragment, tour) pour retrouver l'ordre du document.
    plats = [
        (rang, index_fragment, passage)
        for index_fragment, liste in enumerate(candidats)
        for rang, passage in enumerate(liste)
    ]
    plats.sort(key=lambda item: (item[0], item[1]))

    selection: list[tuple[int, int, tuple[str, int, str]]] = []
    deja_vus: set[str] = set()
    for rang, index_fragment, passage in plats:
        phrase = passage[2]
        cle = phrase.lower()
        if cle in deja_vus:
            # Les fragments se recouvrent (~15 %) : une phrase déjà offerte ne
            # doit pas consommer le budget une seconde fois.
            continue
        cout = len(phrase) + 1  # séparateur
        if cout > budget:
            continue
        deja_vus.add(cle)
        budget -= cout
        selection.append((index_fragment, rang, passage))

    selection.sort(key=lambda item: (item[0], item[1]))
    return [passage for _index, _rang, passage in selection]


def _normaliser_chunk_id(valeur: Any) -> str | None:
    """Nettoie un `chunk_id` de modèle et n'accepte qu'un id `chk_…`.

    Le benchmark a observé `"chk_abc  ordinal=3"` : le modèle recopie parfois
    l'en-tête du fragment avec l'identifiant. Ce n'est pas une invention, c'est
    une mise en forme. On retire donc guillemets, suffixes `ordinal=`/`chunk=`
    et espaces internes AVANT de vérifier l'appartenance à la révision. Un id
    qui ne commence pas par `chk_` reste inconnu : la validation en base
    tranche, pas cette fonction.
    """
    texte = str(valeur or "").strip().strip("\"'`«»“”")
    if not texte:
        return None
    texte = re.split(
        r"[\s,;]+(?:ordinal|chunk)\s*=", texte, maxsplit=1, flags=re.IGNORECASE
    )[0]
    texte = re.sub(r"\s+", "", texte).strip("\"'`«»“”").rstrip(",;")
    if not texte.startswith("chk_"):
        return None
    if not re.fullmatch(r"chk_[A-Za-z0-9_.:-]+", texte):
        return None
    return texte


def _appeler_modele(
    *,
    messages: tuple[Mapping[str, Any], ...],
    runtime: Any | None,
    manager: Any | None,
    timeout_s: float,
) -> Any:
    """Un seul chemin d'appel, `tool_choice` nommé, timeout dur côté runtime.

    - runtime fourni : `complete_chat` porte déjà `manager.use_model` et le
      coordinateur dans `LlamaCppRuntime` ; `RuntimeRequest.timeout_s` y est
      vérifié à l'issue de la génération. `kind="ingestion"` y est transmis :
      l'extraction mémoire est un travail de fond, elle cède vite (10 s) au
      chat au lieu de tenir le verrou `activity` pendant 120 s ;
    - sinon manager fourni : on reproduit le motif de `_forced_brief_phase`,
      avec le même outil forcé et `kind="ingestion"`. LIMITE ASSUMÉE :
      `create_chat_completion` (llama_cpp) n'expose aucun délai d'abandon et
      un appel en cours n'est pas annulable ; `TIMEOUT_LLM_S` reste donc borné
      à 45 s, sous le budget de passe de 60 s, plutôt que d'être appliqué par
      cette branche.
    """
    choix = {"type": "function", "function": {"name": NOM_OUTIL}}
    if runtime is not None:
        return runtime.complete_chat(
            RuntimeRequest(
                messages=messages,
                tools=(EXTRACTION_DOCUMENT_TOOL,),
                tool_choice=choix,
                temperature=0.1,
                max_tokens=MAX_TOKENS_LLM,
                timeout_s=float(timeout_s),
                kind="ingestion",
            )
        )
    if manager is None:
        return None
    with manager.use_model(
        MODELE_EXTRACTION,
        estimated_tokens=_estimer_tokens(messages),
        kind="ingestion",
    ) as modele:
        return modele.create_chat_completion(
            messages=list(messages),
            tools=[EXTRACTION_DOCUMENT_TOOL],
            tool_choice=choix,
            temperature=0.1,
            max_tokens=MAX_TOKENS_LLM,
        )


def _payload_reponse(reponse: Any) -> Mapping[str, Any] | None:
    """Charge utile OpenAI d'une réponse runtime, sans dépendre de sa classe."""
    if isinstance(reponse, Mapping):
        return reponse
    raw = getattr(reponse, "raw", None)
    return raw if isinstance(raw, Mapping) else None


def _completion_tokens(reponse: Any) -> int | None:
    """Tokens générés déclarés par la réponse, ou None si `usage` est absent.

    `llama-cpp-python 0.3.34` coupe un tool call sur `max_tokens` en renvoyant
    `finish_reason="tool_calls"` : le compteur de tokens est alors le seul signal
    fiable. On accepte la forme normalisée (`reponse.usage`) et la charge OpenAI
    brute (`payload["usage"]`). Une valeur illisible équivaut à une absence :
    elle ne doit pas transformer une réponse valide en repli.
    """
    usage = getattr(reponse, "usage", None)
    if usage is None:
        payload = _payload_reponse(reponse)
        usage = payload.get("usage") if payload is not None else None
    if usage is None:
        return None
    valeur = (
        usage.get("completion_tokens")
        if isinstance(usage, Mapping)
        else getattr(usage, "completion_tokens", None)
    )
    try:
        return int(valeur) if valeur is not None else None
    except (TypeError, ValueError):
        return None


def _reponse_tronquee(reponse: Any, *, max_tokens: int = MAX_TOKENS_LLM) -> bool:
    """La génération a-t-elle été coupée par le plafond `max_tokens` ?

    Deux signaux, car `llama-cpp-python 0.3.34` renvoie
    `finish_reason="tool_calls"` (et non `"length"`) quand un tool call est coupé
    par le plafond : le `finish_reason="length"` explicite, ou
    `usage.completion_tokens >= max_tokens`. Une réponse coupée peut encore
    ressembler à un JSON valide (le modèle a refermé l'objet avant la fin
    logique) : on ne l'accepte donc jamais partiellement. `finish_reason` est lu
    sur la forme normalisée puis sur la charge OpenAI brute, sans dépendre de la
    classe du runtime. Si `usage` est absent, on ne lève pas : on retombe sur le
    seul `finish_reason`.
    """
    raison = getattr(reponse, "finish_reason", None)
    if not raison:
        payload = _payload_reponse(reponse)
        choices = payload.get("choices") if payload is not None else None
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            raison = choices[0].get("finish_reason")
    if str(raison or "").strip().lower() == "length":
        return True
    completion_tokens = _completion_tokens(reponse)
    if completion_tokens is None:
        return False
    return completion_tokens >= int(max_tokens)


def _arguments_outil(reponse: Any) -> dict[str, Any] | None:
    """Arguments du SEUL appel d'outil attendu, ou None si la forme est fausse.

    Deux formes réelles sont acceptées : le `RuntimeResponse` normalisé
    (`tool_calls` typés) et la charge OpenAI brute. Tout autre nombre d'appels,
    tout autre nom d'outil ou des arguments illisibles rendent None.
    """
    appels = getattr(reponse, "tool_calls", None)
    if appels:
        if len(appels) != 1:
            return None
        appel = appels[0]
        if str(getattr(appel, "name", "") or "") != NOM_OUTIL:
            return None
        arguments = getattr(appel, "arguments", None)
        return dict(arguments) if isinstance(arguments, Mapping) else None

    payload = _payload_reponse(reponse)
    if payload is None:
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return None
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if not isinstance(message, Mapping):
        return None
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        return None
    function = calls[0].get("function") if isinstance(calls[0], Mapping) else None
    if (
        not isinstance(function, Mapping)
        or str(function.get("name") or "") != NOM_OUTIL
    ):
        return None
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, RecursionError):
            return None
    return dict(arguments) if isinstance(arguments, Mapping) else None


def _fait_depuis_schema(
    schema: Any,
    *,
    db: Any,
    artifact_id: str,
    workspace_id: str,
    observation_id: str | None,
    extractor_version: str,
    card_revision: int,
) -> Any | None:
    """Convertit un fait validé en `FaitDocument`, ou le rejette.

    Le point non négociable : `quote` doit EXISTER dans le fragment désigné.
    Un `chunk_id` inconnu, un fragment d'une autre révision ou une citation
    reformulée rejettent CE fait — pas le document entier.
    """
    from backend.db.models import ArtifactChunk as _Chk

    identifiant = _normaliser_chunk_id(schema.chunk_id)
    if not identifiant:
        return None
    requete = db.query(_Chk).filter(
        _Chk.id == identifiant, _Chk.artifact_id == artifact_id
    )
    if int(card_revision or 0) > 0:
        requete = requete.filter(_Chk.card_revision == int(card_revision))
    chunk = requete.first()
    if chunk is None:
        return None
    if not _md._quote_appartient_au_chunk(db, artifact_id, identifiant, schema.quote):
        return None

    content = str(schema.content or "").strip()[: _md.MAX_FAIT_CHARS]
    if not content:
        return None
    empreinte = memoire.hash_contenu(content)
    chunk_id = str(chunk.id)
    origin = (
        f"document:{artifact_id}:observation:{observation_id or '-'}:chunk:{chunk.id}"
    )
    preuves = [
        valeur for valeur in (str(observation_id or "").strip(), chunk.id) if valeur
    ]
    return _md.FaitDocument(
        content=content,
        quote=str(schema.quote).strip(),
        category=str(schema.category),
        affirmation_status=str(schema.affirmation_status),
        entry_key=f"doc:{workspace_id}:{artifact_id}:{empreinte[:16]}",
        origin_context=origin,
        evidence_ids=preuves,
        source_artifact_ids=[artifact_id],
        observation_id=observation_id,
        chunk_id=chunk_id or None,
        chunk_ordinal=int(getattr(chunk, "ordinal", 0) or 0),
        card_revision=int(getattr(chunk, "card_revision", card_revision) or 0),
        extractor_version=extractor_version,
        method="llm",
    )


def extraire_faits_llm(
    *,
    db: Any,
    artifact_id: str,
    workspace_id: str,
    texte: str,
    observation_id: str | None,
    extractor_version: str,
    card_revision: int = 0,
    runtime: Any | None = None,
    manager: Any | None = None,
    timeout_s: float = TIMEOUT_LLM_S,
) -> list[Any] | None:
    """Extrait jusqu'à 5 faits cités, ou `None` pour laisser le repli.

    `None` couvre : pas de texte, pas de fragment, aucune phrase candidate
    (on n'appelle alors PAS le modèle), aucun runtime disponible, erreur LLM,
    sortie non conforme, ou aucun fait dont la citation est vérifiée. `[]` est
    réservé au cas où le modèle déclare explicitement qu'aucun fait n'est
    mémorable — c'est une réponse, pas une panne.
    """
    texte_borne = str(texte or "")[:MAX_ENTREE_LLM_CHARS]
    if not texte_borne.strip():
        return None
    identifiant_artefact = str(artifact_id or "").strip()
    if not identifiant_artefact:
        return None
    if runtime is None and manager is None:
        return None

    fragments = _fragments_dans_texte(
        _charger_fragments(
            db,
            artifact_id=identifiant_artefact,
            card_revision=card_revision,
        ),
        texte_borne,
    )
    if not fragments:
        return None

    passages = _selectionner_passages_llm(fragments, MAX_PASSAGES_LLM_CHARS)
    if not passages:
        # Aucune phrase candidate (chiffre, entité ou verbe déclaratif) : le
        # prompt serait vide ou du bruit. Le LLM n'a rien à citer de mieux que
        # le repli déterministe, on ne paie donc pas un appel.
        return None

    message_utilisateur = construire_message_utilisateur(
        passages, max_chars=MAX_PASSAGES_LLM_CHARS
    )
    messages = (
        {"role": "system", "content": SYSTEM_DOCUMENT_FACTS},
        {"role": "user", "content": message_utilisateur},
    )
    try:
        reponse = _appeler_modele(
            messages=messages,
            runtime=runtime,
            manager=manager,
            timeout_s=timeout_s,
        )
    except ResourceBusy:
        # Contention GPU : le ledger doit différer, pas replier.
        raise
    except Exception as exc:  # noqa: BLE001 — le repli est la garantie
        log.warning(
            "[memoire-doc] LLM indisponible pour %s : %s: %s",
            identifiant_artefact,
            type(exc).__name__,
            exc,
        )
        return None

    if reponse is None:
        return None
    if _reponse_tronquee(reponse, max_tokens=MAX_TOKENS_LLM):
        # Une sortie coupée n'est pas « quelques faits » : c'est une réponse
        # incomplète. On ne garde rien, le déterministe reprend.
        log.warning(
            "[memoire-doc] réponse LLM tronquée pour %s (max_tokens=%s) : repli",
            identifiant_artefact,
            MAX_TOKENS_LLM,
        )
        return None
    arguments = _arguments_outil(reponse)
    if arguments is None:
        return None

    faits_bruts = arguments.get("faits")
    if not isinstance(faits_bruts, list):
        log.warning(
            "[memoire-doc] enveloppe LLM invalide pour %s : `faits` absent ou "
            "non-liste",
            identifiant_artefact,
        )
        return None
    if len(faits_bruts) > MAX_FAITS_LLM:
        # Le schéma dit `maxItems=5`. Une liste plus longue est hors schéma :
        # la tronquer accepterait partiellement une sortie invalide.
        log.warning(
            "[memoire-doc] enveloppe LLM hors schéma pour %s : %s faits > %s",
            identifiant_artefact,
            len(faits_bruts),
            MAX_FAITS_LLM,
        )
        return None
    if not faits_bruts:
        # Liste vide explicite : le document n'affirme rien de durable. C'est
        # une réponse valide, pas un échec.
        return []

    # Chaque fait est validé SÉPARÉMENT : un seul item mal formé ne doit pas
    # jeter les autres. L'enveloppe n'est rejetée que si aucun fait ne survit.
    schemas: list[FaitSchema] = []
    for fait_brut in faits_bruts:
        if not isinstance(fait_brut, Mapping):
            continue
        try:
            schemas.append(FaitSchema(**fait_brut))
        except (ValidationError, TypeError, ValueError) as exc:
            log.warning(
                "[memoire-doc] fait LLM invalide ignoré pour %s : %s",
                identifiant_artefact,
                exc,
            )
    if not schemas:
        log.warning("[memoire-doc] aucun fait LLM valide pour %s", identifiant_artefact)
        return None

    retenus: list[Any] = []
    empreintes: set[str] = set()
    for schema in schemas:
        fait = _fait_depuis_schema(
            schema,
            db=db,
            artifact_id=identifiant_artefact,
            workspace_id=str(workspace_id),
            observation_id=observation_id,
            extractor_version=extractor_version,
            card_revision=card_revision,
        )
        if fait is None:
            continue
        if fait.entry_key in empreintes:
            continue
        empreintes.add(fait.entry_key)
        retenus.append(fait)
    return retenus or None


def installer_extracteur_llm(
    fournisseur_runtime: Callable[[], Any | None],
) -> Callable[..., list[Any] | None]:
    """Branche l'extracteur LLM et rend sa version ACTIVE pour le ledger.

    Appelé au démarrage du sidecar, APRÈS `init_db` et AVANT le planificateur :
    un module importé en test n'installe rien et ne charge aucun modèle. Le
    setter de version est ce qui fait basculer le backfill des documents `done`
    déterministes vers `doc-faits-llm/1` (réarmement par `_deja_couvrante`).
    """

    def extracteur(**kwargs: Any) -> list[Any] | None:
        return extraire_faits_llm(runtime=fournisseur_runtime(), **kwargs)

    _md.EXTRACTEUR_STRUCTURE = extracteur
    _md.definir_version_extracteur_active(VERSION_EXTRACTEUR_LLM)
    return extracteur

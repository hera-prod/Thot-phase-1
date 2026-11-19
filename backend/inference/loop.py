"""
Chat loop Phase 1 — 2 couches orchestrator → brief → générateur
Notes Futur/Notes.md § Agent loop 2 couches + double historique

But : orchestrator prépare la demande, générateur produit la réponse.
- Orchestrateur : LFM2.5-8B, peut appeler vision/doc_ocr (2 outils), max 5 iter, 15s/timeout, séquentiel
- Générateur : LFM2.5-8B, JAMAIS de tools, reçoit le brief SEUL (pré-réponse)
- Double historique : history_shared = user↔assistant uniquement, jamais tool traces passées
  → tool_results du tour en cours uniquement en mémoire, préparés (≤8K brut, 8-16K tronqué, >16K résumé)
- 7 events : message_started, thinking, tool_call, tool_result, answer, message_completed, error
"""

import base64
import json
import re
import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from backend.inference.ipc import emit, log
from backend.inference.resource_coordinator import ResourceBusy
from backend.image_engine.runner.memprof import RunRecord as _MEMPROF_RUN_RECORD

# --- SharedContext C03 (une seule base versionnée par tour) ---
from dataclasses import dataclass


@dataclass
class SharedContext:
    """Base commune orchestrator/générateur C03 : même révision, ajouts distincts."""

    conversation_id: str
    revision: int  # incrémental (compte messages) pour valider compaction
    history: list[dict]  # user↔assistant ordonnés, sans tool traces passées
    registry: list[dict]  # catalogue compact
    message: str  # message courant exactement une fois


MAX_VISION_SIDE = 1536


def prepare_vision_image(input_path: Path, output_path: Path) -> Path:
    """Prétraite image pour VL : EXIF transpose + max 1536px + JPEG 88 (NaFlex 512)
    Tracer réduction §151 backend_outils : ne pas imposer réglage universel, mais log transformation.
    """
    try:
        from PIL import Image, ImageOps

        image = Image.open(input_path)
        orig_w, orig_h = image.size
        image = ImageOps.exif_transpose(image)
        # thumbnail garde ratio, ne grossit pas si plus petit (budget pixels borné, pas Mo fixe)
        before = image.size
        image.thumbnail((MAX_VISION_SIDE, MAX_VISION_SIDE))
        after = image.size
        if before != after:
            log(
                f"[prepare_vision_image] resize {orig_w}x{orig_h} -> {after[0]}x{after[1]} (NaFlex 512 patches, tracer)"
            )
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if image.width * image.height > 0:
            image.save(output_path, format="JPEG", quality=88, optimize=True)
            return output_path
    except Exception as e:
        log(f"[prepare_vision_image] fallback original: {e}")
    return input_path


# --- Sanitizer backend (filet permanent, tickets llama.cpp #23838/#23852) ---
# LFM2.5-8B peut fuiter <think> inline et des pseudo-appels textuels si template non détecté.
# TOUTES les familles d'outils sont strippées (annexe 12 §plan point 2) : un pseudo-appel
# ne doit jamais devenir thinking, brief, delta ou message final — ni être exécuté (C04).
# Front fait déjà stripThinkingLeak, mais backend garantit DB/history propres.
_THINK_BLOCK_RE = re.compile(
    r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.IGNORECASE | re.DOTALL
)
_THINK_TAG_RE = re.compile(r"<\s*/?\s*think\s*>", re.IGNORECASE)
# Le sanitizer neutralise la syntaxe d'appel entre crochets que le modèle
# recopie depuis ses données d'entraînement. La liste reste donc PLUS LARGE que
# le registre, et conserve des noms retirés comme `get_source` et
# `get_document_card` : ce filtre défend contre une hallucination de FORME, pas
# contre une déclaration de capacité. Le rétrécir laisserait passer
# `[get_source()]` émis par un 8B qui l'a vu ailleurs — précisément ce qu'il
# existe pour empêcher.
_TOOL_FAMILIES = r"vision|doc_ocr|get_source|read_text|search_documents|get_document_card|read_document|search_info|find_files|ask_files|generate_image|web_research|submit_brief"
_TEXTUAL_TOOL_RE = re.compile(
    r"\[?\s*(?:" + _TOOL_FAMILIES + r")\s*\([^\)]*\)\s*\]?", re.IGNORECASE
)
_CONTROL_TOKEN_RE = re.compile(
    r"<\|(?:im_start|im_end|tool_call_start|tool_call_end|start_header_id|end_header_id|eot_id)\|>",
    re.IGNORECASE,
)
_ENGLISH_THINK_MARKERS = [
    "Je vais",
    "Je va",
    "Cette image",
    "Voici",
    "Bien sûr",
    "Pour une",
    "Les arbres",
    "Bonjour",
    "Salut",
]

# --- Anti-auto-présentation VL (annexe 12 T-VL-05, cas q-r.md image) ---
# Le VL parle parfois de lui (« LFM2.5-VL-3B est un modèle… ») au lieu de décrire.
# Marqueurs modèle SANS vocabulaire visuel → off_topic : jamais une preuve réussie.
_OFF_TOPIC_MARKERS = (
    "je suis un modèle",
    "je suis une ia",
    "liquid ai",
    "je peux analyser",
    "en tant que modèle",
    "mes capacités",
    "i am a model",
    "as an ai",
    "mit csail",
    "modèle de vision et de langage développé",
)
_VISUAL_VOCAB = (
    "image",
    "photo",
    "montre",
    "montrent",
    "voit",
    "visible",
    "couleur",
    "pixels",
    "arrière-plan",
    "premier plan",
    "personne",
    "objet",
    "scène",
    "écran",
    "picture",
    "shows",
    "color",
)


def _looks_off_topic(text: str) -> bool:
    """Vrai si le texte parle du modèle sans aucun constat visuel (filet qualité, pas corpus)."""
    try:
        import re as _re

        low = (text or "").lower()
        if not low.strip():
            return False
        # Mots entiers : "images" (pluriel générique) ne compte pas comme constat visuel
        has_marker = any(
            _re.search(r"\b" + _re.escape(m) + r"\b", low) for m in _OFF_TOPIC_MARKERS
        )
        has_visual = any(
            _re.search(r"\b" + _re.escape(v) + r"\b", low) for v in _VISUAL_VOCAB
        )
        return bool(has_marker and not has_visual)
    except Exception:
        return False


def _neutralize_control_tokens(content: str) -> str:
    return _CONTROL_TOKEN_RE.sub("[control-token]", content or "")


def _sanitize_content(content: str) -> tuple[str, str | None]:
    """Retourne (clean, thinking_extrait). Strip <think>, tags isolés, et préfixe anglais The user..."""
    if not content:
        return content, None
    # Retirer d'abord les wrappers provider : neutraliser leurs marqueurs avant
    # le parsing les rendrait invisibles au sanitizer.
    content = sanitize_provider_content(content)
    content = _neutralize_control_tokens(content)
    thinking = None
    # 1. <think>...</think>
    m = _THINK_BLOCK_RE.search(content)
    if m:
        thinking = re.sub(_THINK_TAG_RE, "", m.group(0)).strip()
        content = _THINK_BLOCK_RE.sub("", content, count=1).strip()
    else:
        # tags isolés
        if _THINK_TAG_RE.search(content):
            content = _THINK_TAG_RE.sub("", content).strip()
    # 2. Préfixe anglais The user asks... (fuite sans tags) — cherche le dernier marqueur français après le préfixe
    if content.lower().startswith("the user"):
        lower = content.lower()
        last_idx = None
        last_marker = None
        for marker in _ENGLISH_THINK_MARKERS:
            idx = lower.rfind(marker.lower())
            if idx != -1 and idx > 20:
                if last_idx is None or idx > last_idx:
                    last_idx = idx
                    last_marker = marker
        # Priorise "Je vais" s'il existe après le préfixe, sinon le dernier trouvé
        if last_idx is not None:
            prefix = content[:last_idx].strip()
            if len(prefix) > 20 and "the user" in prefix.lower():
                thinking = (thinking + "\n\n" + prefix) if thinking else prefix
                content = content[last_idx:].lstrip()
                content = (
                    content.replace("Jevaistrèsbien", "Je vais très bien")
                    .replace("Jeva", "Je va")
                    .replace("Pourune", "Pour une")
                )
    content = sanitize_provider_content(content)
    return content.strip(), thinking


def _detect_textual_tool_calls(content: str) -> list[dict]:
    """Annexe 12 : tout pseudo-appel textuel (7 familles) est INVALID_DECISION, jamais exécuté (C04)."""
    m = _TEXTUAL_TOOL_RE.search(content or "")
    if m:
        log(
            f"[INVALID_DECISION] pseudo-appel textuel détecté — refusé, ne jamais exécuter ni diffuser"
        )
    return []


def _strip_textual_tool_calls(content: str) -> str:
    return sanitize_provider_content(content)


from backend.prompts.orchestrator import (
    BRIEF_CONFIG,
    ORCHESTRATOR_CONFIG,
    SUBMIT_BRIEF_TOOL,
    SYSTEM_ORCHESTRATOR,
)
from backend.prompts.generator import SYSTEM_GENERATOR
from backend.prompts.vision import SYSTEM_VISION

# Le vocabulaire des noms d'image vit dans le service (source UNIQUE) : le
# détecteur de promesses ci-dessous doit nommer EXACTEMENT ce que l'admission
# admet, sinon un des deux gardiens laisse passer ce que l'autre bloque.
from backend.services.image_service import IMAGE_CLAIM_NOUNS
from backend.services.web import net as _web_net
from backend.services.web import settings as _web_settings
from backend.services.web.types import FRESHNESS_REUSE

# Autrefois : prompts GLM-OCR (le GGUF n'a jamais été livré — le worker
# PaddleOCR-VL utilise ses prompts de tâche officiels ; import gardé pour compat).
from backend.prompts.ocr import SYSTEM_OCR, build_system_ocr, resolve_ocr_task  # noqa: F401
from backend.utils.files import cleanup_request
from backend.tools.registry import get_default_registry
from backend.inference.tool_calls import (
    ToolCallParseError,
    append_canonical_tool_round,
    as_openai_tool_call,
    ToolCallRecord,
    carries_nameless_envelope,
    stable_call_id,
    textual_call_args,
    normalize_native_tool_calls,
    normalize_lfm25_content,
    normalize_named_tool_response,
    sanitize_provider_content,
    sanitize_tool_arguments,
    textual_tool_intent,
)
from backend.inference.loop_guards import LoopGuard
from backend.config.defaults import MAX_TOOL_CALLS, MAX_TOOL_CALLS_BY_TOOL
from backend.inference.tool_executor import ToolExecutor
from backend.inference.runtime import (
    LlamaCppRuntime,
    RuntimeProtocolError,
    RuntimeRequest,
)
from backend.inference.brief import (
    build_space_synthesis,
    render_brief,
    render_brief_json,
    validate_submit_brief,
)

MAX_ITER = 16
TOOL_TIMEOUT = 15  # s
MAX_RETRY = 1
MAX_TOOL_TOKENS = 8000
TOTAL_TIMEOUT = 300  # s phase 1
DISCOVERY_TOOL_NAMES = frozenset(
    {
        "search_info",
        "find_files",
        "ask_files",
        "generate_image",
        "web_research",
    }
)
# Le seul nom de la constante ci-dessus qu'un réglage peut retirer de l'offre.
# La constante reste le CONTRAT (elle est épinglée des deux côtés) ; c'est
# l'offre qui se réduit, jamais elle.
WEB_RESEARCH_TOOL_NAME = "web_research"
HEAVY_TOOL_NAMES = frozenset(
    {
        "generate_image",
    }
)
# POURQUOI CES OUTILS-LOURS SONT HORS BUDGET DE TEXTE : une diffusion T2I légitime
# dure 60 à 600 s (chargement du checkpoint ~19 Go compris). La compter contre
# TOTAL_TIMEOUT faisait produire l'image PUIS remplacer la réponse par « la réponse
# complète n'a pas pu être terminée dans le délai imparti », en ne laissant ensuite
# que 0,1 s au générateur. Le budget borne le temps de RAISONNEMENT, rien d'autre.
# CE QUE TOTAL_TIMEOUT N'EST PLUS : un plafond d'horloge. Le temps des outils
# lourds en est soustrait, donc la durée totale d'un tour image est
# « raisonnement (≤ TOTAL_TIMEOUT) + diffusion », pas TOTAL_TIMEOUT.
# CE QUI BORNE RÉELLEMENT UNE DIFFUSION : flux_spike.RUN_TIMEOUT_S, dans le worker.
# PAS le timeout de l'outil — ToolExecutor.execute_call court-circuite
# generate_image hors de son wrapper de timeout (une diffusion ne s'interrompt pas
# proprement), donc GenerateImageTool.timeout_s ne borne rien sur le chemin vivant.
RETRY_EXEMPT_TOOLS = frozenset(
    {
        "generate_image",
    }
)
# Un timeout de diffusion est un verdict, pas une infortune : le retry relançait une
# seconde diffusion de 600 s dans le même tour (jusqu'à 20 min pour une seule image).
# Un job lourd qui échoue échoue UNE fois, bruyamment — jamais deux fois en silence.


# Nature d'un événement d'usage posé par un outil de recherche — recherche_locale.md §6.4.
# - `retour` : une ressource a été effectivement rendue (search_info / find_files).
# - `interrogation` : une question a été réellement traitée sur une ressource (ask_files).
# La consultation utilisateur vit ailleurs (interface) et ne passe pas par ce signal.
EVENEMENT_SIGNAL_RETOUR = "retour"
EVENEMENT_SIGNAL_INTERROGATION = "interrogation"
OUTILS_SIGNAL_USAGE = frozenset({"find_files", "search_info", "ask_files"})


def _text_budget_remaining_s(
    started_at: float, heavy_tool_wall_s: float = 0.0
) -> float:
    """Secondes de RAISONNEMENT restantes dans TOTAL_TIMEOUT (négatif = épuisé).

    Le temps d'horloge des outils de HEAVY_TOOL_NAMES en est soustrait : il mesure
    une diffusion, pas une réflexion. Source UNIQUE du calcul — la boucle
    (découverte, re-ask d'un pseudo-appel, brief forcé) et le générateur lisent la
    même fonction, sinon ils divergent sur ce qui reste autorisé.
    """
    elapsed = max(0.0, time.perf_counter() - started_at)
    return TOTAL_TIMEOUT - (elapsed - max(0.0, heavy_tool_wall_s))


def _text_budget_spent(started_at: float, heavy_tool_wall_s: float = 0.0) -> bool:
    """Le budget de texte est-il épuisé ? (voir _text_budget_remaining_s)."""
    return _text_budget_remaining_s(started_at, heavy_tool_wall_s) <= 0.0


def _remaining_run_timeout(started_at: float, heavy_tool_wall_s: float = 0.0) -> float:
    """Retourne le budget natif restant sans autoriser un délai négatif."""
    return max(0.1, _text_budget_remaining_s(started_at, heavy_tool_wall_s))


def _ok_result_texts(tool_results: list) -> list[str]:
    """Textes des résultats réussis (base du repli minimal et de la régénération). Pur."""
    try:
        out = [
            str(r.get("result", "") or "").strip()
            for r in (tool_results or [])
            if r.get("ok")
        ]
        return [r for r in out if r]
    except Exception:
        return []


def _minimal_brief_data(tool_results: list) -> dict:
    """Repli dernier recours (pas de régénération possible/ratée) : les résultats
    réussis recopiés dans le brief.
    Jamais d'invention, jamais de vide (fiche §4.2 : « Le backend ne doit pas
    générer librement une nouvelle conclusion documentaire »). Pur. Retourne un
    brief NORMALISÉ (fiche v1.2 : un seul champ).
    """
    try:
        facts = " ; ".join(
            sanitize_provider_content(r)[:500]
            for r in _ok_result_texts(tool_results)[:3]
        )
        texte = (
            "Éléments préparés par le backend (enquête partielle) : "
            + (
                facts
                if facts
                else "aucun résultat utilisable n'a été obtenu pendant l'enquête."
            )
        ) + " ; preuves non rattachées."
        return {"brief": texte}
    except Exception:
        return {"brief": "Brief minimal de repli : aucun contenu exploitable."}


_IMAGE_STATUS_FACT = "Une image a été générée et publiée pour cette demande."
_IMAGE_STATUS_DEFAULT_LIMIT = "La génération d’image n’a pas pu être terminée."
# La PROMESSE d'une image n'est recevable du backend QUE s'il détient un artefact
# publié. L'orchestrateur 8B inventait « Une image photo réaliste en macro d'un
# papillon a été générée. [établie] » pour un effet qui n'avait JAMAIS tourné ; le
# générateur s'en servait alors pour annoncer une image fantôme.
#
# C'est un DÉTECTEUR DE PROMESSES, pas un analyseur de français : quatre cadres,
# tous prononcés par le 8B sur un échec réel, et rien d'autre. Le nom vient de
# image_service (source UNIQUE du vocabulaire, y compris « rendu ») : un second
# lexique ici laissait passer « La photo d'un papillon a été générée ».
_CLAIM_NOUN = r"\b(?:" + IMAGE_CLAIM_NOUNS + r")s?\b"
# Le même lexique, SANS verbe : sert à demander « la réponse parle-t-elle d'un
# support visuel ? ». Une promesse exige un verbe de production, un simple nom
# suffit ici : « Voici l'image que j'ai créée » et « regardez cette photo »
# appartiennent tous deux à la réponse qui DÉCLARE l'image.
_IMAGE_NOUN_RE = re.compile(_CLAIM_NOUN, re.IGNORECASE)
# Fenêtre nom ↔ verbe : la PHRASE, pas un compte de caractères. Le lien entre le nom
# et le verbe est le complément de nom (« la photo d'un papillon a été générée »), et
# rien ne borne sa longueur autrement que la fin de la phrase. Une borne en caractères
# (48, mesurée un jour sur deux exemples) est un faux invariant : l'E2E du 27/09 a laissé
# passer « L'image d'un papillon en macro, réalisée de façon réaliste, a bien été générée
# et publiée. » — 49 caractères de complément, promesse inventée, image inexistante.
# `_SENTENCE_SPLIT_RE` découpe déjà par phrase AVANT d'appeler ce motif : une promesse
# ne s'assemble donc jamais à cheval sur deux phrases, et rien ne peut la fuir par
# longueur.
_CLAIM_LINK = r"[^.!?…]*?"
_CLAIM_ADVERB = r"(?:bien\s+|déjà\s+|maintenant\s+)?"
# Participes de création, et leurs jumeaux non accentués : le canal écrit
# parfois « la photo a ete generee », une promesse non accentuée reste une promesse.
_CLAIM_PAST = (
    r"(?:générée?s?|créée?s?|produite?s?|réalisée?s?|faite?s?|dessinée?s?"
    r"|peinte?s?|illustrée?s?|composée?s?|fabriquée?s?|inventée?s?"
    r"|generees?|crees?|produites?|realisees?|faite?s?|dessinees?"
    r"|peinte?s?|illustrees?|composees?|fabriquees?|inventees?)\b"
)
_CLAIM_HOLDER = (
    r"(?:j['’]ai|je\s+vais|je\s+peux|nous\s+avons|tu\s+as|il\s+a|elle\s+a|on\s+a)"
)
_IMAGE_SUCCESS_CLAIM_RE = re.compile(
    "|".join(
        (
            # 1. Passif — « La photo d'un papillon a été générée. »
            _CLAIM_NOUN
            + _CLAIM_LINK
            + r"\b(?:a|ont)\s+"
            + _CLAIM_ADVERB
            + r"été\s+"
            + _CLAIM_PAST,
            # 2. Actif — « J'ai généré une image de papillon. »
            r"\b"
            + _CLAIM_HOLDER
            + r"\s+"
            + _CLAIM_ADVERB
            + _CLAIM_PAST
            + _CLAIM_LINK
            + _CLAIM_NOUN,
            # 3. Relatif — « Voici l'image que j'ai créée. »
            _CLAIM_NOUN
            + _CLAIM_LINK
            + r"\b"
            + _CLAIM_HOLDER
            + r"\s+"
            + _CLAIM_ADVERB
            + _CLAIM_PAST,
            # 4. État — « L'image est prête et disponible au téléchargement. »
            _CLAIM_NOUN
            + _CLAIM_LINK
            + r"\b(?:est|sont|serait)\s+(?:désormais\s+|maintenant\s+)?(?:pr[êe]t(?:e)?s?|disponibles?)\b",
            # 5. Tête nominale au passé — « Image générée selon la synthèse… »,
            #    « Visuel créé pour la page… ». Ancrée en DÉBUT de phrase pour ne
            #    pas attraper « L'image générée précédemment… » (adjectif) ni une
            #    phrase au milieu d'un texte. Le générateur ouvrait sa réponse par
            #    ce titre alors qu'aucune image n'existait.
            r"^\s*" + _CLAIM_NOUN + r"\s+" + _CLAIM_ADVERB + _CLAIM_PAST,
        )
    ),
    re.IGNORECASE,
)
# --- MIROIR : le REFUS d'une image RÉELLEMENT publiée -----------------------
# D'OÙ CET INVARIANT : `_without_unsupported_image_claims` retire la promesse
# d'une image absente. Le MANQUE était comblé, pas le CONTRADICTOIRE. Mesuré en
# E2E : rejeu d'un effet publié, image attachée au message, et l'8B répondait
# « Je ne dispose pas des capacités nécessaires pour créer des images, donc je ne
# peux pas les générer. » — parce que le brief portait un fait `established`
# (« une image a été générée et publiée ») CONTRE une limite générique écrite par
# l'orchestrateur. Le 8B pondère la limite plus que le fait. Le backend détient la
# vérité : il n'a donc pas à laisser ce désaccord exister.
#
# Noyau du refus : un verbe de PRODUCTION d'image nommé par son objet. C'est ce
# qui sépare « je ne peux pas générer d'image » (interdit par le prompt) de
# « je ne peux pas éditer les images » ou « je ne sais pas faire d'animation »,
# qui sont des limites légitimes et doivent survivre intactes. La liste est celle
# du prompt générateur — elle est écrite ici, pas relue d'un fichier de prompt.
_IMAGE_DENIAL_PRODUCTION = (
    r"(?:générer|generer|créer|creer|dessiner|produire|fabriquer|composer|réaliser|realiser)"
    r"\s*(?:une?\s+|des\s+|de\s+|d['’]\s*|l['’]\s*)?" + _CLAIM_NOUN
)
# Le passif exige un participe : « générée », « créée », « produite ». La liste
# d'infinitifs ne suffit pas, mais le groupe nominal reste exigé — c'est lui qui
# distingue « l'image n'a pas été générée » (contradiction) de « je ne peux pas
# éditer les images » (limite légitime).
_IMAGE_PRODUCTION_PARTICIPLE = (
    r"(?:génér|cré|dessin|produ|fabriqu|compos|réalis)"
    r"(?:[ée]|e|es|i|ie|ies|ue|ues)?"
)
_IMAGE_DENIAL_RE = re.compile(
    "|".join(
        (
            # « je ne peux pas générer d'image », « je ne sais pas créer des images »
            r"\bje\s+(?:ne\s+)?(?:peux|pourrais|dois|sais|saurai|vais)\s*(?:pas|plus)?\s*"
            + _IMAGE_DENIAL_PRODUCTION,
            # « je suis incapable de générer une image », « impossible de la créer »
            r"\b(?:je\s+suis\s+)?(?:incapable\s+de|impossible\s+de)\s+"
            + _IMAGE_DENIAL_PRODUCTION,
            # « je ne dispose pas des capacités nécessaires pour créer des images »
            r"\bje\s+ne\s+dispose\s+pas\s+(?:des\s+|de\s+l['’]\s*|d['’]\s*)?"
            r"capacit[ée]s?\s+(?:nécessaires\s+)?pour\s+" + _IMAGE_DENIAL_PRODUCTION,
            # « je n'ai pas la capacité de générer une image »
            r"\bje\s+n['’]ai\s+pas\s+(?:la\s+|les\s+)?capacit[ée]\s+de\s+"
            + _IMAGE_DENIAL_PRODUCTION,
            # « je n'ai pas pu générer l'image » — passé composé. Forme réellement
            # produite par le modèle après un échec d'outil suivi d'une réussite
            # (q-r.md) : le présent seul ne rattrape rien, et la réponse passait
            # en « je n'ai pas pu » alors qu'une image venait d'être publiée.
            r"\bje\s+n['’]ai\s+pas\s+pu\s+" + _IMAGE_DENIAL_PRODUCTION,
            # « je n'ai pas su produire… » (tour valide, rare)
            r"\bje\s+n['’]ai\s+pas\s+s[uû]\s+" + _IMAGE_DENIAL_PRODUCTION,
            # « l'image n'a pas pu être générée » / « la photo n'a pas été créée ».
            # Au PASSIF le verbe est un participe, pas un infinitif : « générée » ne
            # matche pas la liste d'infinitifs, il faut donc une forme dédiée.
            r"\b"
            + _CLAIM_NOUN
            + r"\s+n['’]a\s+pas\s+(?:pu\s+)?[êeé]t(?:re|[ée])\s+"
            + _IMAGE_PRODUCTION_PARTICIPLE,
        )
    ),
    re.IGNORECASE,
)
# Frontière de phrase : une promesse se retire PHRASE par phrase, jamais au
# milieu d'une autre.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
# Un mot de moins de quatre lettres est presque toujours un mot-outil
# (« or », « non », « elle ») : c'est le seuil qui distingue « il reste une
# réponse » de « il ne reste qu'un hochement de tête ».
_MEANINGFUL_WORD_MIN_CHARS = 4


def _image_status_attempts(image_status: dict | list | None) -> list[dict]:
    if not image_status:
        return []
    if isinstance(image_status, list):
        return [attempt for attempt in image_status if isinstance(attempt, dict)]
    attempts = image_status.get("attempts", image_status.get("results"))
    if isinstance(attempts, list) and attempts:
        return [attempt for attempt in attempts if isinstance(attempt, dict)]
    return [image_status]


def _attempt_published(attempt: dict) -> bool:
    if "published" in attempt:
        return bool(attempt.get("published"))
    if "image_published" in attempt:
        return bool(attempt.get("image_published"))
    if "ok" in attempt:
        return bool(attempt.get("ok"))
    if "success" in attempt:
        return bool(attempt.get("success"))
    if "status" in attempt:
        return str(attempt.get("status") or "").lower() in {
            "published",
            "success",
            "ok",
        }
    return bool(attempt.get("artifact_id"))


def _image_failure_limit(result: dict) -> str:
    """La limite à dire à l'utilisateur pour un résultat image non publié.

    Extraite du résultat AU MOMENT où il entre dans `tool_results` : reconstruire
    l'état image plus tard depuis ces entrées ne verrait plus ce code, et une
    limite spécifique (« moteur indisponible ») retombait sur le libellé
    générique. Écrire ici = la source UNIQUE du texte affiché.
    """
    return str(
        result.get("user_limit")
        or (
            "La génération d’image n’est pas disponible dans cet environnement."
            if "SERVICE_UNAVAILABLE" in str(result.get("result", ""))
            else _IMAGE_STATUS_DEFAULT_LIMIT
        )
    )


def _record_image_status(image_status: dict | None, result: dict) -> dict:
    """Ajoute un résultat image sans écraser les tentatives précédentes.

    Un même artifact_id n'est PAS une nouvelle tentative : la Phase 0 peut avoir
    publié l'image puis l'orchestrateur redemander generate_image (dédupliqué par
    _already_published_image). Compter les deux ferait annoncer « 2 images » au
    générateur pour un seul artefact.
    """
    artifact_id = str(result.get("artifact_id") or "")
    previous = _image_status_attempts(image_status)
    if artifact_id and any(
        str(item.get("artifact_id") or "") == artifact_id for item in previous
    ):
        return {**(image_status or {})}
    published = bool(
        artifact_id
        and (
            result.get("published")
            or result.get("image_published")
            or result.get("ok")
            or result.get("success")
        )
    )
    failure_limit = _image_failure_limit(result)
    attempt = {
        "ordinal": len(previous),
        "published": published,
        "artifact_id": artifact_id,
        "user_limit": "" if published else failure_limit,
        "tool_call_id": str(result.get("tool_call_id") or ""),
        "result": sanitize_provider_content(str(result.get("result", ""))),
    }
    attempts = [*previous, attempt]
    return {
        "attempts": attempts,
        "published": any(_attempt_published(item) for item in attempts),
        "complete": all(_attempt_published(item) for item in attempts),
        "user_limit": next(
            (
                str(item.get("user_limit"))
                for item in attempts
                if not _attempt_published(item) and item.get("user_limit")
            ),
            _IMAGE_STATUS_DEFAULT_LIMIT,
        ),
    }


def _aggregate_image_status(tool_results: list[dict] | None) -> dict | None:
    """Reconstruit l'état image depuis la séquence ordonnée des tool results."""
    status: dict | None = None
    for entry in tool_results or []:
        if not isinstance(entry, dict) or entry.get("tool") != "generate_image":
            continue
        raw_metadata = entry.get("metadata")
        metadata: dict = raw_metadata if isinstance(raw_metadata, dict) else {}
        result = dict(entry)
        if not result.get("artifact_id") and metadata.get("artifact_id"):
            result["artifact_id"] = metadata["artifact_id"]
        status = _record_image_status(status, result)
    return status


def _has_published_image(image_status: dict | list | None) -> bool:
    """Le backend a-t-il réellement publié un pixel pour ce tour ?"""
    return any(
        _attempt_published(attempt) for attempt in _image_status_attempts(image_status)
    )


def _carries_an_answer(text: str) -> bool:
    """Reste-t-il une réponse après le retrait ? Un mot plein fait la différence.

    En dessous de quatre lettres, un mot français est presque toujours un
    mot-outil (« or », « non », « mais », « elle ») : ce qui resterait ne
    répondrait à rien et l'utilisateur lirait une bulle vide.
    """
    return any(
        len(word) >= _MEANINGFUL_WORD_MIN_CHARS for word in re.findall(r"\w+", text)
    )


def _recorded_image_limit(image_status: dict | list | None) -> str:
    """La limite ENREGISTRÉE pour un échec image — jamais une phrase de modèle."""
    for attempt in _image_status_attempts(image_status):
        limit = str(attempt.get("user_limit") or "").strip()
        if limit and not _attempt_published(attempt):
            return limit
    return _IMAGE_STATUS_DEFAULT_LIMIT


def _without_unsupported_image_claims(
    text: str, image_status: dict | list | None
) -> str:
    """Retire les phrases qui promettent une image que le backend n'a pas publiée.

    D'OÙ CET INVARIANT : le prompt interdit au générateur de nier une image ET
    lui interdit d'en annoncer une — les deux, inconditionnellement, y compris
    quand la diffusion a échoué. Un 8B n'obéit pas toujours à la première
    interdiction, et l'utilisateur n'a AUCUN rempart si le backend se tait. La
    vérité est ici : un `image_status` sans tentative publiée signifie AUCUN
    pixel, quelle que soit la prose.

    Le retrait est chirurgical : seule la phrase fautive part, le reste de la
    réponse vaut encore. Si plus rien ne répond, la réponse devient la limite
    ENREGISTRÉE (écrite par le backend), jamais une phrase de modèle.
    """
    if not text or not text.strip():
        return text
    if _has_published_image(image_status):
        return text
    sentences = [
        sentence for sentence in _SENTENCE_SPLIT_RE.split(text) if sentence.strip()
    ]
    kept = [
        sentence
        for sentence in sentences
        if not _IMAGE_SUCCESS_CLAIM_RE.search(sentence)
    ]
    if len(kept) == len(sentences):
        return text  # rien à retirer : le texte n'est même pas reassemblé
    if _carries_an_answer(" ".join(kept)):
        return " ".join(kept)
    return _recorded_image_limit(image_status)


def _without_false_image_denial(text: str, image_status: dict | list | None) -> str:
    """Retire les phrases qui NIENT une image que le backend a réellement publiée.

    MIROIR EXACT de `_without_unsupported_image_claims`, même style, même place :
    là où le backend silence une promesse fausse, il corrige ici une négation
    fausse. La VÉRITÉ qu'il substitue est celle qu'il a lui-même écrite — le fait
    réservé ``_IMAGE_STATUS_FACT``, jamais une phrase de modèle.

    Le motif est volontairement étroit : il n'attrape que les formulations que
    le prompt interdit déjà, et seulement si un verbe de PRODUCTION est nommé
    par son objet. « je ne peux pas éditer les images » et « je ne sais pas
    faire d'animation » ne contiennent ni l'un ni l'autre : ils survivent.

    Une réponse qui nie la génération SANS être entièrement faite de ce refus
    garde le reste ; si plus rien ne répond, le backend écrit LUI-MÊME l'état de
    l'image.
    """
    if not text or not text.strip():
        return text
    if not _has_published_image(image_status):
        return text
    sentences = [
        sentence for sentence in _SENTENCE_SPLIT_RE.split(text) if sentence.strip()
    ]
    kept = [sentence for sentence in sentences if not _IMAGE_DENIAL_RE.search(sentence)]
    if len(kept) == len(sentences):
        return text  # rien à retirer : le texte n'est même pas reassemblé
    if _carries_an_answer(" ".join(kept)):
        return " ".join(kept)
    return _IMAGE_STATUS_FACT


# --- Le générateur ne doit jamais nommer son échafaudage --------------------
# D'OÙ CE FILTRE : le prompt interdit « Selon le brief… » / « Le brief
# indique… », et le backend n'a pas à croire le 8B sur son propre prompt. Mesuré
# en E2E : « Selon le brief, une image a été générée et publiée pour cette
# demande. » — factuellement vrai, et lu par l'utilisateur comme une machine qui
# récite ses propres pièces. Le fait est vrai, la SOURCE ne doit pas l'être.
_BRIEF_META_LEAD_RE = re.compile(
    r"^(?:"
    r"(?:d['’]apr[èe]s|selon|suivant|conform[ée]ment\s+[àa])\s+(?:le|ce|les|l['’]|mon)\s+briefs?\b"
    r"|(?:le|ce|ce\s+dernier)\s+brief\s+(?:indique|mentionne|pr[ée]cise|signale|contient|rapporte|établit)\b"
    r"|les\s+donn[ée]es\s+(?:fournies|re[çc]ues|du\s+brief)\s+(?:indiquent|mentionnent|disent)\b"
    r"|(?:le\s+syst[èe]me|l['’]orchestrateur)\s+(?:a|ont)\s+(?:trouv[ée]|d[ée]termin[ée]|identifi[ée])\b"
    r")\s*[,:]?\s*(?:que\s+|qu['’]on\s+|ce\s+que\s+)?",
    re.IGNORECASE,
)
# La queue d'une phrase : une concession (« … , BIEN QUE le brief n'ait pas pu être
# validé ») qui excuse la phrase par le MÉCANISME. Deux composants obligatoires —
# un connecteur de concession PUIS un sujet-mécanisme — parce que « …, bien que la
# version finale reste à valider » est une réponse normale sur un document.
_BRIEF_MACHINERY_SUBJECT = (
    r"(?:le|ce|cet|la|l['’]|les)?\s*"
    r"(?:briefs?|validations?|orchestrateurs?|orchestrations?|syst[èe]mes?|m[ée]canismes?)"
    r"(?:\s+(?:de\s+(?:la\s+)?(?:validation|brief|synth[èe]se)|interne))?"
)
# Verbes d'APPRÉCIATION d'un brief : ce qu'on en attendait, pas ce qu'il dit.
# « le brief indique que… » en tête de phrase est déjà traité par _BRIEF_META_LEAD_RE.
_BRIEF_MACHINERY_VERB = (
    r"(?:n['’]a\s+|n['’]ait\s+|a\s+|ont\s+)?(?:pas\s+)?(?:pu\s+)?(?:[êe]tre\s+)?"
    r"(?:valid\w*|accept\w*|refus\w*|fourni|obtenu|r[ée]dig\w*|produit|g[ée]n[ée]r\w*"
    r"|pr[ée]par\w*|[ée]chou\w*|correct\w*|fiable|incomplet\w*|s[ûu]r\w*|exploitable)"
)
_BRIEF_META_TRAIL_RE = re.compile(
    r"[,;]\s*"
    r"(?:bien\s+que|bien\s+qu['’]il\s+ne\s+soit|m[êe]me\s+si|malgr[ée]|quoique|alors\s+que"
    r"|tandis\s+que|pourtant|or)\s+"
    + _BRIEF_MACHINERY_SUBJECT
    + r"\s+"
    + _BRIEF_MACHINERY_VERB
    + r"\b"
    r"[^.!?…]*[.!?…]?\s*\Z",
    re.IGNORECASE,
)


def _without_brief_meta_clause(sentence: str) -> str:
    """Retire la clause finale qui justifie la phrase par le mécanisme du brief.

    Le reste de la phrase garde sa ponctuation finale : une réponse amputée d'un
    point se lirait comme une phrase tronquée. Si la clause ocupaït toute la
    phrase, il ne reste rien — le appelant remplace alors le texte par la vérité du
    backend, jamais par un résidu vide.
    """
    match = _BRIEF_META_TRAIL_RE.search(sentence)
    if match is None:
        return sentence
    kept = sentence[: match.start()].rstrip(" ,;")
    if not kept:
        return sentence
    terminal = (
        sentence[match.end() - 1]
        if match.group(0) and match.group(0)[-1] in ".!?…"
        else ""
    )
    return kept + (terminal if terminal and kept[-1] not in ".!?…" else "")


def _without_brief_meta_reference(text: str) -> str:
    """Réécrit « Selon le brief, X » en « X », et retire la clause qui EXCUSE le brief.

    Deux formes, deux regex, un seul filtre — l'échafaudage est une information sur
    la façon dont la réponse a été fabriquée, jamais sur son contenu :

    * la TÊTE de phrase (« Selon le brief, X ») : on retire le connecteur ;
    * la CLAUSE finale qui justifie la phrase par le mécanisme (« X, BIEN QUE le
      brief n'ait pas pu être validé après plusieurs tentatives. ») : on retire la
      clause entière. Mesuré verbatim : « Une image a bien été générée et publiée
      pour votre demande, bien que le brief n'ait pas pu être validé après
      plusieurs tentatives. » — un fait vrai, une source machine, lisible par
      l'utilisateur comme une fuite de journal.

    Le motif de queue est volontairement À DEUX COMPOSANTS : une concession
    (« bien que », « même si », « quoique »…) ET un sujet qui est le MACHINISME
    lui-même. « …, bien que la version finale reste à valider » ne parle pas d'un
    brief : il survive, octet pour octet. Si rien ne correspond, le texte est rendu
    TEL QUEL (même objet) : une réponse sans auto-référence ne subit aucune
    normalisation.
    """
    if not text or not text.strip():
        return text
    sentences = [
        sentence for sentence in _SENTENCE_SPLIT_RE.split(text) if sentence.strip()
    ]
    rewritten: list[str] = []
    for sentence in sentences:
        stripped = _BRIEF_META_LEAD_RE.sub("", sentence, count=1)
        if not stripped:
            rewritten.append(sentence)
            continue
        stripped = stripped[:1].upper() + stripped[1:] if stripped else sentence
        rewritten.append(_without_brief_meta_clause(stripped))
    if rewritten == sentences:
        return text
    return " ".join(rewritten)


def _reserved_image_fact(published: list[dict]) -> str:
    """LA phrase que le backend écrit quand il a publié un pixel. Source UNIQUE.

    Elle part dans le brief (fait réservé) ET dans la réponse quand le modèle
    l'a omise : deux copies de la vérité ne peuvent pas diverger. Un seul artefact
    publié → la formulation au singulier ; plusieurs → on les compte vraiment.
    """
    if len(published) <= 1:
        return _IMAGE_STATUS_FACT
    return f"{len(published)} images ont été générées et publiées pour cette demande."


def _with_published_image_mention(text: str, image_status: dict | list | None) -> str:
    """Une image PUBLIÉE se dit toujours. Le sous-claim est aussi un mensonge.

    MIROIR ENTIER des deux gardes ci-dessus, du même point de vue : l'utilisateur
    a une image ATTACHÉE à son message ; une réponse qui n'en parle pas ne ment
    pas, mais lui laisse croire qu'il a reçu des mots. Mesuré en E2E : image
    générée et jointe, réponse = une description de chat en prose, zéro mot sur
    l'image.

    La VÉRITÉ substituée est celle du backend — le fait réservé, jamais une phrase
    de modèle. Elle est en TÊTE : l'image EST la réponse à la demande, la
    description ne vient qu'après. Le filtre ne s'allume que sur une publication
    réelle ET une réponse qui ne nomme aucun support : sans image, rien n'est
    ajouté, et une réponse qui dit déjà « l'image » passe octet pour octet.
    """
    if not text or not text.strip():
        return text
    published = [
        attempt
        for attempt in _image_status_attempts(image_status)
        if _attempt_published(attempt)
    ]
    if not published or _IMAGE_NOUN_RE.search(text):
        return text
    fact = _reserved_image_fact(published)
    if fact in text:
        return text
    return f"{fact} {text}"


def _with_image_truth_guards(text: str, image_status: dict | list | None) -> str:
    """Le SEUL rempart du backend entre la réponse du 8B et l'utilisateur.

    Un seul des deux invariants peut s'appliquer : le backend a-t-il publié un
    pixel, oui ou non. Le dispatch explicite rend l'asymétrie lisible — pas de
    phrase sans garde, jamais les deux gardes en competition.
    """
    if not text or not text.strip():
        return text
    if _has_published_image(image_status):
        return _with_published_image_mention(
            _without_false_image_denial(text, image_status), image_status
        )
    return _without_unsupported_image_claims(text, image_status)


def _sanitize_brief_data(brief_data: dict | None) -> dict | None:
    """Retire les artefacts provider du champ textuel du brief (fiche v1.2 : un seul champ)."""
    if not isinstance(brief_data, dict):
        return None
    normalized = dict(brief_data)
    normalized["brief"] = sanitize_provider_content(
        str(normalized.get("brief") or "")
    ).strip()
    return normalized


def _with_image_status(
    brief_data: dict | None, image_status: dict | list | None
) -> dict | None:
    """Reconcilie tous les résultats image après la boucle d'outils, DANS le brief.

    La promesse réservée (image publiée) entre en tête ; les limites des
    tentatives échouées suivent, et tout échec fait dire explicitement que la
    couverture est partielle (fiche §4.3 : « Une sortie dégradée est un état de
    traitement, pas une raison de masquer les résultats déjà obtenus »).
    """
    normalized = _sanitize_brief_data(brief_data) or {"brief": ""}
    texte = str(normalized.get("brief") or "").strip()
    if not image_status:
        # Aucune tentative image : le brief doit obéir au même garde qu'une
        # tentative échouée. Sans cela, « Image générée selon… » survivait alors
        # qu'aucun pixel n'avait été publié (q-r.md).
        normalized["brief"] = _without_unsupported_image_claims(texte or "", None)
        return normalized
    attempts = _image_status_attempts(image_status)
    published = [attempt for attempt in attempts if _attempt_published(attempt)]
    failed = [attempt for attempt in attempts if not _attempt_published(attempt)]
    morceaux: list[str] = []
    if published:
        # La promesse réservée du backend, jamais une réécriture du modèle.
        morceaux.append(_reserved_image_fact(published))
    if texte and _IMAGE_STATUS_FACT not in texte:
        if failed and not published:
            texte = _without_unsupported_image_claims(texte, image_status)
        morceaux.append(texte)
    if failed:
        for attempt in failed:
            limite = str(attempt.get("user_limit") or _IMAGE_STATUS_DEFAULT_LIMIT)
            morceaux.append(f"Limite : {limite}")
        morceaux.append("La couverture de cette demande est partielle.")
    normalized["brief"] = "\n".join(m for m in morceaux if m.strip())[:6000]
    return normalized


# --- Helpers brief structurel (submit_brief) : normalisation + preuve + phase forcée ---
# Les appels exécutables sont normalisés depuis la structure native du provider.


def _native_to_calls(native: list | None) -> list[dict]:
    """Normalise les tool_calls natifs runtime → [{name, args, id}]. Noms bruts (allowlist
    appliquée par l'appelant via registre/parse). Pur, testable."""
    out: list[dict] = []
    try:
        for tc in native or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", tc) or {}
            name = (func.get("name") or tc.get("name") or "").strip()
            if not name:
                continue
            raw = func.get("arguments", "{}")
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except Exception:
                continue
            if not isinstance(args, dict):
                continue
            out.append({"name": name, "args": args, "id": tc.get("id")})
    except Exception:
        pass
    return out


def _discovery_tool_names() -> frozenset[str]:
    """Les outils de découverte RÉELLEMENT proposés au modèle, pour CE tour.

    `DISCOVERY_TOOL_NAMES` est le contrat : elle dit ce que la découverte
    comporte, elle est épinglée par des tests des deux côtés, et le filtre se
    fait donc ici, au moment de composer `tool_defs` — jamais sur la constante.

    Le seul retrait possible est la fermeture de la recherche web. Proposer
    `web_research` à un modèle dont l'utilisateur a fermé la recherche, c'est
    lui offrir à chaque tour un appel que rien ne pourra satisfaire, et lui
    prendre une place dans son choix pour un outil absent. Le réglage est relu
    à chaque tour : un utilisateur qui ferme la recherche n'a rien à relancer.

    La valeur par défaut est « actif » : sur une installation sans fichier de
    réglages, les cinq outils sont proposés comme avant.
    """
    if _web_settings.read().enabled:
        return DISCOVERY_TOOL_NAMES
    return DISCOVERY_TOOL_NAMES - {WEB_RESEARCH_TOOL_NAME}


def _discovery_records(records: tuple) -> tuple:
    """Filtre le canal de découverte avant toute persistance ou exécution."""
    return tuple(record for record in records if record.name in DISCOVERY_TOOL_NAMES)


def _decision_calls(message: dict) -> list[dict]:
    if not isinstance(message, dict):
        return []
    content = str(message.get("content") or "")
    calls = _native_to_calls(message.get("tool_calls"))
    if "<|tool_call_start|>" not in content and _TEXTUAL_TOOL_RE.search(content):
        log("[INVALID_DECISION] pseudo-appel textuel détecté — refusé, jamais exécuté")
    return calls


def _can_parse_lfm_fallback(runtime) -> bool:
    return runtime is None or getattr(runtime, "runtime_id", "llama_cpp") == "llama_cpp"


def _arguments_exploitable(arguments: object) -> bool:
    """Ces arguments contiennent-ils autre chose que du vide ?

    La barre est volontairement basse : on ne juge pas la VALIDITÉ ici — le
    validateur de l'outil le fait, et c'est lui qui a les bornes. On juge
    seulement s'il y a quelque chose à valider. Sans cela, un appel natif à
    arguments vides passerait pour un appel, et le filet ne partirait jamais.
    """
    if not isinstance(arguments, Mapping):
        return False
    for valeur in arguments.values():
        if valeur in (None, "", [], {}, ()):
            continue
        return True
    return False


def _records_from_textual_args(
    tool_name: str,
    arguments: dict,
    *,
    run_id: str,
    round_index: int,
) -> tuple:
    """Construit un record d'appel depuis des arguments littéraux récupérés.

    Le record porte la provenance `textual_fallback`, qui est **distincte** de
    `native` : la trace doit dire d'où vient la décision. Un appel obtenu par ce
    filet n'a pas la même valeur probante qu'un appel natif, et le dire permet
    de mesurer le filet au lieu de le confondre avec la règle.
    """
    arguments = {str(k): v for k, v in arguments.items()}
    return (
        ToolCallRecord(
            id=stable_call_id(run_id, round_index, 0, tool_name, arguments),
            name=tool_name,
            arguments=arguments,
            ordinal=round_index,
            provenance="textual_fallback",
        ),
    )


def _recover_named_tool_call(
    runtime,
    messages: list[dict],
    tool_defs: list[dict],
    tool_name: str,
    *,
    run_id: str,
    round_index: int,
    timeout_s: float,
    allow_wrapper: bool = False,
) -> tuple:
    """Récupère un choix nommé, sans jamais exécuter son JSON source.

    Le canal wrapper est réservé aux appelants qui l'acceptent explicitement
    (le handoff forcé).  La découverte reste sur le canal natif : une réponse
    contenant une autre fonction, ou un JSON libre, est refusée en bloc.

    `timeout_s` est EXIGÉ, pas optionnel : un re-ask natif coûte un appel modèle
    complet, et un appel sans budget prendrait le défaut de ``RuntimeRequest``
    (300 s) — soit un budget neuf au milieu d'un tour qui en a déjà brûlé un.
    C'est le cas le plus fréquent sur un tour image, où un pseudo-appel
    textuel EST le chemin attendu.
    """
    if runtime is None or getattr(runtime, "runtime_id", "llama_cpp") != "llama_cpp":
        return ()
    definition = next(
        (
            item
            for item in tool_defs
            if isinstance(item, Mapping)
            and isinstance(item.get("function"), Mapping)
            and item["function"].get("name") == tool_name
        ),
        None,
    )
    if definition is None:
        return ()
    try:
        response = runtime.complete_chat(
            RuntimeRequest(
                messages=tuple(messages),
                tools=(definition,),
                tool_choice={"type": "function", "function": {"name": tool_name}},
                temperature=ORCHESTRATOR_CONFIG.temperature,
                max_tokens=ORCHESTRATOR_CONFIG.max_tokens,
                timeout_s=max(0.1, float(timeout_s)),
            )
        ).raw
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
        ):
            return ()
        message = choices[0].get("message", {})
        return normalize_named_tool_response(
            message,
            expected_name=tool_name,
            run_id=run_id,
            round_index=round_index,
            allow_wrapper=allow_wrapper and _can_parse_lfm_fallback(runtime),
        )
    except RuntimeProtocolError as e:
        if "CONTEXT_WINDOW_TOO_SMALL" in str(e):
            emit(
                "error",
                {
                    "request_id": run_id,
                    "code": "context_window_too_small",
                    "message": (
                        "Le contexte de cette discussion dépasse la fenêtre du modèle. "
                        "Réduisez les pièces jointes ou démarrez une nouvelle discussion."
                    ),
                    "detail": str(e)[:300],
                },
            )
            log(f"[toolcall] named native recovery context window too small: {e}")
        else:
            log(f"[toolcall] named native recovery failed for {tool_name}: {e}")
        return ()
    except Exception as exc:
        log(f"[toolcall] named native recovery failed for {tool_name}: {exc}")
        return ()


def _reask_unconstrained_tool_call(
    runtime,
    messages: list[dict],
    tool_defs: list[dict],
    *,
    run_id: str,
    round_index: int,
    timeout_s: float,
    allow_wrapper: bool = False,
) -> tuple:
    """Re-ask natif NON contraint : le modèle choisit l'outil ET écrit les arguments.

    Troisième canal de la chaîne de récupération, après le natif et le wrapper
    LFM puis le re-ask NOMMÉ. Il ne part que sur le signal de
    ``carries_nameless_envelope`` : une enveloppe d'arguments correcte et
    complète, mais sans la clé ``name``.

    CE QUE CE CANAL NE FAIT PAS, et ne fera jamais : déduire l'outil de la
    forme des arguments. Le backend n'a pas de table ``prompt``+``format`` ⇒
    ``generate_image``, parce qu'un outil choisi par la forme d'un JSON n'est pas
    un choix du modèle. Ici les 4 définitions de découverte sont proposées
    ENSEMBLE, ``tool_choice="required"`` oblige un appel, et c'est le modèle qui
    nomme la fonction et écrit les arguments. L'enveloppe nue n'a fourni que le
    MOMENT du re-ask ; l'enregistrement exécuté est l'appel du modèle.

    ``required`` n'est pas une grammaire sur ce runtime : llama-cpp-python
    0.3.34 ne construit une grammaire que pour un choix NOMMÉ
    (``llama_chat_format.chat_formatter_to_chat_completion_handler``), et c'est
    précisément le choix nommé que ce canal refuse d'inventer. L'obligation est
    donc portée par le message correctif, sur le modèle du retry de
    ``_forced_brief_phase`` — le même levier, sans le nom d'outil.

    Sortie : UN SEUL enregistrement, nommé par le modèle, dont le nom est un
    outil de découverte. Deux appels, un autre outil, un refus ou un silence :
    RIEN n'est enregistré. Un refus reste un refus — on ne le relance pas en
    exigeant un appel, et un tour ne peut donc pas empiler deux re-ask.
    """
    if runtime is None or getattr(runtime, "runtime_id", "llama_cpp") != "llama_cpp":
        return ()
    offered = tuple(tool for tool in tool_defs if isinstance(tool, Mapping))
    if not offered:
        return ()
    try:
        response = runtime.complete_chat(
            RuntimeRequest(
                messages=tuple(messages)
                + (
                    {
                        "role": "system",
                        "content": (
                            "L'appel d'outil n'a pas été transmis. Appelle UNE SEULE fonction "
                            "parmi celles proposées, avec ses arguments, et rien d'autre."
                        ),
                    },
                ),
                tools=offered,
                tool_choice="required",
                temperature=ORCHESTRATOR_CONFIG.temperature,
                max_tokens=ORCHESTRATOR_CONFIG.max_tokens,
                timeout_s=max(0.1, float(timeout_s)),
            )
        ).raw
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
        ):
            return ()
        message = choices[0].get("message") or {}
        if not isinstance(message, Mapping):
            return ()
        native = message.get("tool_calls")
        if not isinstance(native, list) or len(native) != 1:
            return ()
        records = normalize_native_tool_calls(
            native,
            run_id=run_id,
            round_index=round_index,
            allowed_names=DISCOVERY_TOOL_NAMES,
        )
        if not records and allow_wrapper:
            # Le wrapper est le même canal que lit déjà le tour de découverte ;
            # il reste un appel du modèle (il nomme l'outil et ses arguments),
            # jamais les arguments de l'enveloppe nue.
            records = normalize_lfm25_content(
                str(message.get("content") or ""),
                run_id=run_id,
                round_index=round_index,
                provenance="lfm25_wrapper",
                allowed_names=DISCOVERY_TOOL_NAMES,
            )
        return records if len(records) == 1 else ()
    except RuntimeProtocolError as e:
        if "CONTEXT_WINDOW_TOO_SMALL" in str(e):
            emit(
                "error",
                {
                    "request_id": run_id,
                    "code": "context_window_too_small",
                    "message": (
                        "Le contexte de cette discussion dépasse la fenêtre du modèle. "
                        "Réduisez les pièces jointes ou démarrez une nouvelle discussion."
                    ),
                    "detail": str(e)[:300],
                },
            )
            log(f"[toolcall] unconstrained native re-ask context window too small: {e}")
        else:
            log(f"[toolcall] unconstrained native re-ask failed: {e}")
        return ()
    except Exception as exc:
        log(f"[toolcall] unconstrained native re-ask failed: {exc}")
        return ()


# Les observations ne se « vérifient » plus pour une citation : la preuve
# persistée est écrite par `observations.record_tool_observation` (ci-dessus),
# et son existence vit dans le rendu outils, plus dans un inventaire figé.
# Les anciens vérificateurs (`_obs_exists`, `_doc_evidence_parts`,
# `_web_evidence_parts`, `_fragment_exists`, `_observation_or_blob_exists`,
# `_web_evidence_exists`) sont supprimés avec la signature v1.2 du brief :
# leur seul consommateur était la validation des ``evidence_ids`` de ``facts``.
# La lecture d'une preuve web conservée reste `research.resolve_evidence` (services/web).
# Vue directe de la base : Obs.* vit dans `search/observations.py`.


def _forced_brief_phase(
    manager,
    messages_orch: list,
    request_id: str,
    conv_id: str,
    runtime=None,
    timeout_s: float = 300.0,
    user_message: str | None = None,
    tool_results: list | None = None,
) -> tuple[str, dict] | None:
    """Phase brief forcée : submit_brief seul, appel NOMMÉ (fiche v1.2 §3.1).

    Réutilise la délibération accumulée (registre + historique + résultats d'outils déjà
    dans messages_orch), seul le système change. 1er échec retry, 2e retry dernier essai,
    3e abandon → brief de repli déterministe (fiche §4.1, filet de sécurité jamais vide).
    « Cette construction ne requiert pas que le modèle réussisse un ultime appel d'outil. »
    Retourne (brief RENDU texte, brief NORMALISÉ), jamais None sauf panne LLM totale.
    """
    try:
        msgs: list[dict] = [{"role": "system", "content": SYSTEM_ORCHESTRATOR}] + list(
            messages_orch[1:]
        )
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            emit(
                "thinking",
                {
                    "request_id": request_id,
                    "text": "[Orchestrateur] Rédaction du brief...",
                    "iteration": 90 + attempt,
                },
            )
            try:
                if runtime is not None:
                    resp = runtime.complete_chat(
                        RuntimeRequest(
                            messages=tuple(msgs),
                            tools=(SUBMIT_BRIEF_TOOL,),
                            tool_choice={
                                "type": "function",
                                "function": {"name": "submit_brief"},
                            },
                            temperature=BRIEF_CONFIG.temperature,
                            max_tokens=BRIEF_CONFIG.max_tokens,
                            timeout_s=max(0.1, remaining),
                        )
                    ).raw
                else:
                    with manager.use_model("LFM2.5-8B", estimated_tokens=2048) as llm:
                        resp = llm.create_chat_completion(
                            messages=msgs,
                            tools=[SUBMIT_BRIEF_TOOL],
                            tool_choice={
                                "type": "function",
                                "function": {"name": "submit_brief"},
                            },
                            temperature=BRIEF_CONFIG.temperature,
                            max_tokens=BRIEF_CONFIG.max_tokens,
                        )
            except ResourceBusy:
                # La ressource est momentanément occupée : ce n'est pas une
                # panne du brief, c'est une contention passagère. On laisse
                # remonter pour que le chat la traite comme retryable, au lieu
                # de la transformer en échec générique puis de réessayer ici.
                raise
            except RuntimeProtocolError as e:
                if "CONTEXT_WINDOW_TOO_SMALL" in str(e):
                    emit(
                        "error",
                        {
                            "request_id": request_id,
                            "code": "context_window_too_small",
                            "message": (
                                "Le contexte de cette discussion dépasse la fenêtre du modèle. "
                                "Réduisez les pièces jointes ou démarrez une nouvelle discussion."
                            ),
                            "detail": str(e)[:300],
                        },
                    )
                    log(f"[brief] context window too small: {e}")
                    raise
                else:
                    emit(
                        "error",
                        {
                            "request_id": request_id,
                            "code": "brief_llm_error",
                            "message": str(e)[:300],
                        },
                    )
                    continue
            except Exception as e:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "brief_llm_error",
                        "message": str(e)[:300],
                    },
                )
                continue
            m = resp.get("choices", [{}])[0].get("message") or {}
            if time.monotonic() >= deadline:
                break
            clean_content, _ = _sanitize_content(str(m.get("content") or ""))
            try:
                # Le handoff forcé accepte uniquement le canal natif ou le
                # wrapper LFM explicitement compatible.  Un JSON libre, un
                # texte pseudo-appel ou un autre outil est rejeté sans trace.
                records = normalize_named_tool_response(
                    m,
                    expected_name="submit_brief",
                    run_id=request_id,
                    round_index=90 + attempt,
                    allow_wrapper=_can_parse_lfm_fallback(runtime),
                )
            except ToolCallParseError as exc:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "invalid_brief_tool_call",
                        "message": str(exc)[:300],
                    },
                )
                continue
            if len(records) != 1:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            "Le brief n'a pas été fourni. Appelle la fonction submit_brief."
                        ),
                    }
                )
                continue
            record = records[0]
            _begin_toolcall(
                request_id,
                record.id,
                record.name,
                record.arguments,
                record.ordinal,
                record.provenance,
            )
            ok, code, norm = validate_submit_brief(record.arguments)
            result_text = f"[submit_brief: {code}]"
            emit(
                "tool_call",
                {
                    "request_id": request_id,
                    "tool_call_id": record.id,
                    "tool": record.name,
                    "arguments": sanitize_tool_arguments(record.arguments),
                },
            )
            emit(
                "tool_result",
                {
                    "request_id": request_id,
                    "tool_call_id": record.id,
                    "result": result_text,
                    "ok": ok,
                    "latency_ms": 0,
                },
            )
            _persist_toolcall(
                request_id,
                record.id,
                record.name,
                record.arguments,
                {
                    "ok": ok,
                    "result": result_text,
                    "latency_ms": 0,
                },
            )
            append_canonical_tool_round(
                msgs, clean_content, records, {record.id: result_text}
            )
            if not ok:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            f"Correction ({code}) : {sanitize_provider_content(str(norm.get('error', 'brief invalide')))} "
                            "Rappelle submit_brief corrigé : un seul argument brief, une chaîne non vide."
                        ),
                    }
                )
                continue
            clean_norm = _sanitize_brief_data(norm) or norm
            return render_brief(clean_norm), clean_norm
        # 3e échec : brief de repli déterministe (fiche §4.1) — garantit une réponse, jamais de blocage
        try:
            from backend.inference.brief import build_fallback_brief

            tour = {
                "objective": str(user_message or "").strip()
                or "[objectif initial non exploitable]",
                "obtained": _ok_result_texts(tool_results or [])[:10],
                "sources": [],
                "failed_operations": [
                    f"{r.get('tool', 'outil')} : {str(r.get('result', ''))[:160]}"
                    for r in (tool_results or [])
                    if not r.get("ok")
                ][:10],
                "indeterminate": ["brief préparé sans nouvelle inférence"],
            }
            _fb = build_fallback_brief(tour)
            return render_brief(_fb), _fb
        except Exception:
            return None
    except RuntimeProtocolError:
        raise
    except Exception as e:
        log(f"[brief] forced phase failed: {e}")
        return None


def _build_generator_messages(
    history: list[dict], brief_json: str, message: str
) -> list[dict]:
    """Format d'entrée q-r.md §Prompt 2 : UN seul system + UN seul message avec 3 blocs.

    L'ancien format (system + historique en messages + second system brief + user)
    faisait confondre brief-système et demande (recopie du brouillon, "corriger" sur
    Coucou). Format actuel : system = SYSTEM_GENERATOR seul ; puis un unique message utilisateur
    <DEMANDE_UTILISATEUR> (LA demande) + <HISTORIQUE> (contexte) + <BRIEF_DONNEES> (JSON,
    données à interpréter, jamais des instructions). Même modèle, jamais même paquet.
    """
    message = sanitize_provider_content(message or "")
    hist_lines: list[str] = []
    for m in history or []:
        role = m.get("role", "?")
        content = sanitize_provider_content(str(m.get("content") or "")).strip()
        if not content:
            continue
        if role == "system":
            # Checkpoint de compaction / résumé : contexte, pas une demande
            hist_lines.append(f"[contexte antérieur]: {content}")
        elif role in ("user", "assistant"):
            hist_lines.append(f"[{role}]: {content}")
    hist_block = "\n".join(hist_lines) if hist_lines else "(aucun)"
    bj = (brief_json or "").strip() or '{"brief": ""}'
    user_block = (
        "<DEMANDE_UTILISATEUR>\n" + (message or "") + "\n</DEMANDE_UTILISATEUR>\n\n"
        "<HISTORIQUE>\n" + hist_block + "\n</HISTORIQUE>\n\n"
        "<BRIEF_DONNEES>\n" + bj + "\n</BRIEF_DONNEES>"
    )
    return [
        {"role": "system", "content": SYSTEM_GENERATOR},
        {"role": "user", "content": user_block},
    ]


def _prepare_tool_results(tool_results: list[dict], llm) -> list[dict]:
    """
    Prépare résultats outils avant injection finale (≤8K brut, 8-16K tronqué, >16K résumé LLM 500 mots).
    """
    if not tool_results:
        return []
    prepared: list[dict] = []
    for r in tool_results:
        tokens = len(r.get("result", "")) // 4
        if tokens <= MAX_TOOL_TOKENS:
            prepared.append(r)
        elif tokens <= 16000:
            truncated = r["result"][: MAX_TOOL_TOKENS * 4]
            prepared.append(
                {
                    **r,
                    "result": f"{truncated}\n\n[Tronqué : {tokens}→{MAX_TOOL_TOKENS} tokens conservés]",
                }
            )
        else:
            try:
                prompt = f"Résume en 500 mots (garde faits et chiffres) :\n{r['result'][:50000]}"
                s = llm.create_completion(
                    prompt=prompt, max_tokens=1024, temperature=0.3
                )["choices"][0]["text"].strip()
                prepared.append({**r, "result": f"[Résumé] : {s}"})
            except Exception:
                prepared.append(
                    {**r, "result": r["result"][: MAX_TOOL_TOKENS * 4] + "\n[Tronqué]"}
                )
    return prepared


def select_attachment_tool(image_paths: list[str] | None) -> str | None:
    """Routage déterministe : pdf→doc_ocr, image→vision, rien→None (P0)."""
    if not image_paths:
        return None
    if any(Path(p).suffix.lower() == ".pdf" for p in image_paths):
        return "doc_ocr"
    return "vision"


def asks_for_text_extraction(message: str) -> bool:
    # Unifié avec media_policy.OCR_REQUEST_TERMS (q-r.md : "lire ce qu'il y a d'écrit en japonais")
    try:
        from backend.inference.media_policy import asks_for_text as _asks

        return _asks(message)
    except Exception:
        text = (message or "").lower()
        ocr_terms = (
            "ocr",
            "extrais le texte",
            "transcris",
            "transcrire",
            "tableau",
            "facture",
            "scan",
        )
        return any(term in text for term in ocr_terms)


def resolve_allowed_tools(message: str, image_paths: list[str] | None) -> list[str]:
    has_pdf = any(Path(p).suffix.lower() == ".pdf" for p in (image_paths or []))
    has_image = any(
        Path(p).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        for p in (image_paths or [])
    )
    if has_pdf:
        return ["doc_ocr"]
    if has_image and asks_for_text_extraction(message):
        return ["doc_ocr"]
    if has_image:
        return ["vision"]
    return []


def is_chitchat(
    message: str, attachment_registry: list | None, history: list | None = None
) -> bool:
    """Bavardage pur : salutation/politesse courte, sans fichier, sans question factuelle. Pur.

    q-r.md : "Coucou !" déclenchait 2/2 outils → bruit + trace robotique. Un bavardage ne
    nécessite aucune recherche : le brief avec les connaissances suffit, zéro appel.
    """
    try:
        if attachment_registry:
            return False
        text = (message or "").strip()
        if not text or len(text) > 120:
            return False
        import re as _re, unicodedata as _u

        norm = _u.normalize("NFC", text.lower())
        norm = "".join(c for c in _u.normalize("NFD", norm) if not _u.combining(c))
        norm = _re.sub(r"[^\w\s']", "", norm).strip()
        greetings = (
            "coucou",
            "bonjour",
            "bonsoir",
            "salut",
            "hello",
            "hey",
            "yo",
            "merci",
            "merci beaucoup",
            "au revoir",
            "a bientot",
            "a plus",
            "ca va",
            "comment ca va",
            "quoi de neuf",
            "bienvenue",
        )
        if norm in greetings:
            return True
        # "coucou comme introduction" / "je disais juste bonjour..." → bavardage
        if len(norm.split()) <= 12 and any(
            g in norm for g in ("bonjour", "bonsoir", "coucou", "salut", "hello", "hey")
        ):
            # Mais "bonjour, quel est le chiffre..." reste une vraie question → pas bavardage
            if "?" in text or any(
                w in norm
                for w in (
                    "quel",
                    "quelle",
                    "combien",
                    "pourquoi",
                    "comment faire",
                    "explique",
                    "analyse",
                    "lis",
                    "cherche",
                    "trouve",
                )
            ):
                return False
            return True
        return False
    except Exception:
        return False


def build_attachment_brief(message: str, result: dict) -> str:
    return f"Analyse reussie pour : {message}\n\n{result.get('result', '')[:4000]}"


def plan_media_tools(message: str, current_entries: list[dict]) -> list[dict]:
    """Plan déterministe pur (testable sans modèle) : quelle outil pour chaque artefact du tour.

    Règle P0 : pdf→doc_ocr, image→vision, image + demande explicite de texte→doc_ocr.
    Seuls les artefacts disponibles (available) sont planifiés.
    Retourne [{tool, artifact_id, name}] — jamais de LLM.

    >>>generate_image N'APPARTIENT PAS À CE PLAN, ET NE DOIT JAMAIS Y ÊTRE RAJOUTÉ.<<<
    L'orchestrateur est le SEUL point de décision d'une création d'image. Il y a eu ici
    un filet déterministe : un mot-clé (« génère » + « image ») suffisait à lancer une
    diffusion de 60 à 600 s AVANT tout appel modèle. Mesuré sur du français ordinaire,
    ce prédicat se trompait dans 4 cas sur 11 (faux positifs : « fais un tableau visuel des
    ventes », « crée un rendu du tableau de bord ») et manquait 10 demandes sur 12 (faux
    négatifs : « je voudrais une photo de papillon », « rends-moi un portrait de chat ») —
    il coûtait donc un job GPU entier pour deviner une décision que l'orchestrateur prend
    déjà, et mieux. Le propriétaire a tranché : l'orchestrateur décide, point final.
    Un observateur d'artefact (vision/doc_ocr) reste un FAIT, pas une décision ; c'est la
    seule chose que cette Phase 0 fait. Ne pas « réintroduire » un raccourci mot-clé ici.
    """
    plan: list[dict] = []
    try:
        from backend.inference.media_policy import resolve_tool_for as _resolve_tool
    except Exception:
        _resolve_tool = None
    for e in current_entries or []:
        if not e.get("available", True):
            continue
        art_id = e.get("id")
        if not art_id:
            continue
        if _resolve_tool is not None:
            tool = _resolve_tool(e.get("kind") or "", message, asks_for_text_extraction)
        else:
            kind = (e.get("kind") or "").lower()
            tool = (
                "doc_ocr"
                if (kind == "pdf" or asks_for_text_extraction(message))
                else "vision"
            )
        plan.append(
            {"tool": tool, "artifact_id": art_id, "name": e.get("name") or art_id}
        )
    return plan


def pick_reinspection_entry(message: str, history_entries: list[dict]) -> dict | None:
    # Délègue à la politique officielle (media_policy.pick_target) — compat conservée
    try:
        from backend.inference.media_policy import pick_target as _pick

        return _pick(message, history_entries)
    except Exception:
        avail = [
            e
            for e in (history_entries or [])
            if e.get("available", True) and e.get("id")
        ]
        return avail[-1] if avail else None


def requires_media_reinspection(message: str) -> bool:
    # Délègue à la politique officielle (media_policy.py, 3 couches) — compat conservée
    try:
        from backend.inference.media_policy import needs_reinspection as _needs

        return _needs(message)
    except Exception:
        # Filet de sécurité : sans la politique officielle, on retombe sur les collocations
        # les plus fréquentes. L'ORDINALITÉ n'y figure pas — elle a besoin du parseur, donc
        # si l'import échoue elle est perdue. C'est le prix d'un chemin qui ne doit pas
        # exister, et le dire vaut mieux que de prétendre le contraire.
        text = message.lower()
        reference_terms = (
            "cette image",
            "cette photo",
            "dans l'image",
            "sur l'image",
            "première image",
            "deuxième image",
            "dernière image",
        )
        return any(term in text for term in reference_terms)


def resolve_reinspection_tool(message: str) -> str:
    text = message.lower()
    ocr_terms = (
        "texte",
        "écrit",
        "écriture",
        "lettres",
        "mot",
        "phrase",
        "lis",
        "lire",
        "ocr",
        "transcris",
        "transcrire",
        "panneau",
        "sous-titre",
        "titre",
    )
    if any(term in text for term in ocr_terms):
        return "doc_ocr"
    return "vision"


def _get_past_source_path(conv_id: str, message: str) -> tuple[str, str] | None:
    """Résout 'première image' etc. vers chemin cache persistant. Retourne (abs_path, original_name) ou None."""
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import Source, Message
        from sqlalchemy import select

        db = SessionLocal()
        try:
            # Tous les sources de la conversation ordonnés
            stmt = (
                select(Source)
                .join(Message, Source.msg_id == Message.id)
                .where(Message.conv_id == conv_id)
                .order_by(Source.created_at.asc(), Source.id.asc())
            )
            rows = list(db.execute(stmt).scalars().all())
            # Filtre image vs pdf selon message
            images = [
                r
                for r in rows
                if (
                    r.media_kind == "image"
                    or (
                        r.path
                        and r.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                    )
                )
            ]
            pdfs = [
                r
                for r in rows
                if (
                    r.media_kind == "pdf"
                    or (r.path and r.path.lower().endswith(".pdf"))
                )
            ]
            # Détermine cible ordinale
            low = message.lower()
            target = None
            if (
                "première image" in low
                or "premiere image" in low
                or "premier pdf" in low
            ):
                target = images[0] if images else (pdfs[0] if pdfs else None)
            elif (
                "deuxième image" in low or "seconde image" in low or "second pdf" in low
            ):
                target = images[1] if len(images) > 1 else None
            elif (
                "dernière image" in low
                or "derniere image" in low
                or "précédente" in low
                or "precedente" in low
                or "avant" in low
            ):
                target = images[-1] if images else None
            else:
                # Par défaut : dernière image (si une seule ambiguïté, on prend dernière)
                if images:
                    target = images[-1]
                elif pdfs:
                    target = pdfs[-1]
            if target is None:
                # Ambigu : plusieurs images et générique "cette image" sans ordinal clair -> None pour demander clarification
                if (
                    len(images) > 1
                    and "cette image" in low
                    and "première" not in low
                    and "deuxième" not in low
                    and "dernière" not in low
                ):
                    return None  # caller demandera clarification
                target = images[-1] if images else None
            if target is None:
                return None
            # Résout chemin absolu depuis relative_path (portable C19)
            if target.relative_path:
                try:
                    from backend.config.paths import get_data_dir

                    data_dir = get_data_dir()
                    # relative_path vient de la table (media/{hash}.ext legacy,
                    # blobs/<2 hex>/{hash}.bin ou blobs/sha256/<2 hex>/{hash}.bin) ;
                    # on essaie le chemin tel que stocké, puis les racines connues
                    for base in [data_dir, data_dir / "media", data_dir / "blobs"]:
                        cand = (
                            base / Path(target.relative_path).name
                            if "/" not in target.relative_path
                            else data_dir / target.relative_path
                        )
                        # Essai direct
                        cand2 = data_dir / target.relative_path
                        if cand2.exists():
                            return str(cand2), target.original_name or target.path
                        try:
                            from backend.db.media_cache import get_media_root

                            mr = get_media_root()
                            abs_path2 = mr / Path(target.relative_path).name
                            if abs_path2.exists():
                                return str(
                                    abs_path2
                                ), target.original_name or target.path
                        except Exception:
                            pass
                    # fallback legacy home
                    mr_legacy = (
                        Path.home() / ".local" / "share" / "thot" / target.relative_path
                    )
                    if mr_legacy.exists():
                        return str(mr_legacy), target.original_name or target.path
                except Exception:
                    pass
                # dernier fallback : media_root direct
                try:
                    from backend.db.media_cache import get_media_root

                    mr = get_media_root()
                    abs_path = mr / Path(target.relative_path).name
                    if abs_path.exists():
                        return str(abs_path), target.original_name or target.path
                except Exception:
                    pass
            if target.path and Path(target.path).exists():
                return target.path, target.original_name or target.path
            return None
        finally:
            try:
                db.close()
            except:
                pass
    except Exception as e:
        log(f"[reinspection] resolve failed: {e}")
        return None
    return None


def build_attachment_registry(
    conv_id: str, image_paths: list[str] | None = None
) -> list[dict]:
    """Registre structuré des sources pour l'orchestrateur — visible à chaque tour (Réponses.md n°8-10)."""
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import Source, Message, ToolCall
        from sqlalchemy import select

        db = SessionLocal()
        try:
            stmt = (
                select(Source)
                .join(Message, Source.msg_id == Message.id)
                .where(Message.conv_id == conv_id)
                .order_by(Source.created_at.asc(), Source.id.asc())
            )
            rows = list(db.execute(stmt).scalars().all())
            # Map message_id -> index pour message_index
            msg_stmt = (
                select(Message)
                .where(Message.conv_id == conv_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
            msgs = list(db.execute(msg_stmt).scalars().all())
            msg_index_map = {m.id: idx + 1 for idx, m in enumerate(msgs)}
            registry = []
            for i, s in enumerate(rows):
                # previous_analyses depuis ToolCall
                prev = []
                try:
                    tc_stmt = select(ToolCall).where(ToolCall.msg_id == s.msg_id)
                    tcs = list(db.execute(tc_stmt).scalars().all())
                    for tc in tcs:
                        summary = (tc.result or "")[:120].strip()
                        prev.append(
                            {
                                "tool": tc.tool,
                                "status": "success" if tc.ok else "failed",
                                "summary": summary,
                            }
                        )
                    # Fallback global si aucun sur ce message mais source hash match ailleurs
                    if not prev and s.sha256:
                        all_tc = db.execute(select(ToolCall)).scalars().all()
                        for tc in all_tc:
                            args = tc.args or {}
                            if s.sha256 in str(args) or (
                                s.path and s.path in str(args)
                            ):
                                prev.append(
                                    {
                                        "tool": tc.tool,
                                        "status": "success" if tc.ok else "failed",
                                        "summary": (tc.result or "")[:120],
                                    }
                                )
                                break
                except Exception:
                    pass
                abs_available = False
                try:
                    if s.relative_path:
                        from backend.db.media_cache import get_media_root
                        from backend.config.paths import get_data_dir

                        # Teste plusieurs emplacements portables
                        data_dir = get_data_dir()
                        cand = data_dir / s.relative_path
                        mr = get_media_root()
                        abs_available = (
                            cand.exists()
                            or (mr / Path(s.relative_path).name).exists()
                            or (mr / s.relative_path).exists()
                        )
                        if not abs_available:
                            abs_available = (
                                Path.home()
                                / ".local"
                                / "share"
                                / "thot"
                                / s.relative_path
                            ).exists()
                    elif s.path:
                        abs_available = Path(s.path).exists()
                except Exception:
                    pass
                registry.append(
                    {
                        "id": s.id,
                        "ordinal": i + 1,
                        "index": i + 1,
                        "kind": s.media_kind
                        or (
                            "pdf"
                            if (s.path and s.path.lower().endswith(".pdf"))
                            else "image"
                        ),
                        "name": s.original_name or Path(s.path).name
                        if s.path
                        else f"source_{i + 1}",
                        "path": s.relative_path or s.path,
                        "first_seen_in_message": s.msg_id,
                        "message_index": msg_index_map.get(s.msg_id, 0),
                        "available": abs_available,
                        "previous_analyses": prev,
                    }
                )
            existing_paths = {r.get("path") for r in registry}
            for p in image_paths or []:
                if p not in existing_paths and p not in {
                    r.get("relative_path") for r in registry
                }:
                    registry.append(
                        {
                            "id": f"pending_{len(registry) + 1}",
                            "ordinal": len(registry) + 1,
                            "index": len(registry) + 1,
                            "kind": "pdf" if p.lower().endswith(".pdf") else "image",
                            "name": Path(p).name,
                            "path": p,
                            "first_seen_in_message": "current",
                            "message_index": len(msgs) + 1,
                            "available": Path(p).exists(),
                            "previous_analyses": [],
                        }
                    )
            return registry
        finally:
            try:
                db.close()
            except:
                pass
    except Exception as e:
        log(f"[registry] build failed: {e}")
        return [
            {
                "id": "pending_1",
                "ordinal": 1,
                "index": 1,
                "kind": "image",
                "name": Path(p).name,
                "path": p,
                "first_seen_in_message": "current",
                "message_index": 1,
                "available": True,
                "previous_analyses": [],
            }
            for p in (image_paths or [])
        ]


def _registry_entry(
    db, conv_id: str, artifact, first_seen: str, message_index: int, ordinal: int
) -> dict | None:
    """Entrée de registre pour un artefact, ou ``None`` s'il n'est plus adressable.

    Le label public est RENDU par ``public_id_for_artifact`` : lien de scope actif +
    artefact vivant sont vérifiés en base. Un artefact supprimé ou retiré de la
    conversation n'a pas de ``doc_N`` — il ne réapparaît donc pas au registre.
    """
    from backend.search import docrefs as _docrefsR
    from backend.db.models import Blob as _BlobR
    from backend.config.paths import get_data_dir as _gddR

    artifact_id = str(artifact.id or "").strip()
    if not artifact_id:
        return None
    public_id = _docrefsR.public_id_for_artifact(db, conv_id, artifact_id)
    if public_id is None:
        return None
    blob = db.query(_BlobR).filter(_BlobR.id == artifact.blob_id).first()
    available = False
    if blob and blob.relative_path:
        try:
            available = (_gddR() / blob.relative_path).exists()
        except Exception:
            pass
    return {
        "id": artifact_id,
        "public_id": public_id,
        "ordinal": ordinal,
        "index": ordinal,
        "kind": artifact.kind,
        "name": artifact.original_filename or artifact.display_name or artifact_id,
        "path": blob.relative_path if blob else "",
        "first_seen_in_message": first_seen,
        "message_index": message_index,
        "available": available,
        "previous_analyses": [],
    }


def build_current_artifact_entries(
    conv_id: str, attachment_ids: list[str] | None
) -> list[dict]:
    """Registre du tour courant : les artefacts joints, dans l'ordre du frontend.

    Même schéma que le registre historique (format_attachment_registry fait des accès
    directs `r["id"]`). Un artefact hors périmètre ou retiré est ignoré : il ne
    récupère jamais un ``doc_N`` par la lecture du registre.
    """
    entries: list[dict] = []
    if not attachment_ids:
        return entries
    try:
        from backend.db.database import SessionLocal as _SLC
        from backend.db.models import Artifact as _ArtC

        db = _SLC()
        try:
            for art_id in attachment_ids:
                art = db.query(_ArtC).filter(_ArtC.id == art_id).first()
                if art is None or art.deleted_at is not None:
                    continue
                entry = _registry_entry(
                    db, conv_id, art, "current", 1, len(entries) + 1
                )
                if entry is not None:
                    entries.append(entry)
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[registry] artifact build failed: {e}")
    return entries


def build_history_artifact_entries(
    conv_id: str, exclude_ids: set[str] | None = None
) -> list[dict]:
    """Registre historique annexe 10 : artefacts des tours précédents via MessageAttachment.

    Sans ça, un follow-up texte ("et là ?") ne voit aucun fichier : attachment_ids est vide
    et build_attachment_registry ne connaît que la table legacy Source → tool_defs=[].
    Retourne des entrées au même schéma que le registre courant (id/ordinal/name/...).

    Une pièce jointe retirée (corbeille) garde sa ligne MessageAttachment mais perd son
    lien de scope : elle est donc exclue, sans jamais récupérer son ancien ``doc_N``.

    L'ordre du registre est un ORDRE TOTAL, le même que celui que `find_files`
    applique pour « la dernière image » : `Message.created_at` (l'horloge de
    l'utilisateur), puis `MessageAttachment.ordinal` (le rang dans le message), puis
    `MessageAttachment.id` (le départage, unique par construction). Sans le troisième
    terme, deux pièces jointes de la même seconde — cas ordinaire en SQLite, où
    `func.now()` est résolu à la seconde — seraient rendues dans un ordre laissé au
    hasard du plan d'exécution, et `media_policy.pick_target` rendrait « la dernière »
    différent d'un appel à l'autre.
    """
    entries: list[dict] = []
    try:
        from backend.db.database import SessionLocal as _SLH
        from backend.db.models import (
            Artifact as _ArtH,
            Message as _MsgH,
            MessageAttachment as _MAH,
        )

        excluded = exclude_ids or set()
        _db = _SLH()
        try:
            rows = (
                _db.query(_MAH, _MsgH)
                .join(_MsgH, _MAH.message_id == _MsgH.id)
                .filter(_MsgH.conv_id == conv_id)
                .order_by(_MsgH.created_at.asc(), _MAH.ordinal.asc(), _MAH.id.asc())
                .all()
            )
            # message_id -> index pour message_index
            msgs = (
                _db.query(_MsgH)
                .filter(_MsgH.conv_id == conv_id)
                .order_by(_MsgH.created_at.asc(), _MsgH.id.asc())
                .all()
            )
            msg_index = {m.id: idx + 1 for idx, m in enumerate(msgs)}
            seen: set[str] = set()
            for ma, m in rows:
                if ma.artifact_id in excluded or ma.artifact_id in seen:
                    continue
                art = _db.query(_ArtH).filter(_ArtH.id == ma.artifact_id).first()
                if not art:
                    continue
                seen.add(ma.artifact_id)
                entry = _registry_entry(
                    _db, conv_id, art, ma.message_id, msg_index.get(ma.message_id, 0), 0
                )
                if entry is None:
                    continue
                entry["ordinal"] = 0  # renuméroté à la fusion
                entry["index"] = 0
                entries.append(entry)
            _db.commit()
        finally:
            try:
                _db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[registry] history build failed: {e}")
    return entries


def format_attachment_registry(registry: list[dict]) -> str:
    if not registry:
        return "Aucun fichier dans cette conversation."
    lines = ["SOURCES DISPONIBLES DANS CETTE CONVERSATION"]
    public_entries: list[dict] = []
    for r in registry:
        prev = r.get("previous_analyses") or []
        prev_str = (
            f"Analyse précédente : {prev[0]['tool']} {prev[0]['status']} — {prev[0]['summary']}"
            if prev
            else "Analyse précédente : aucune"
        )
        public_id = r.get("public_id")
        identifier = (
            f"doc_id={public_id}"
            if public_id
            else "identifiant public à obtenir via find_files"
        )
        lines.append(
            f" [{r.get('ordinal')}] {identifier} Type : {r.get('kind')} Nom : {r.get('name')} Tour d'origine : {r.get('message_index')} Disponible : {'oui' if r.get('available') else 'non'} {prev_str} Résumé : {prev[0]['summary'] if prev else ''}".strip()
        )
        public_entries.append(
            {
                "doc_id": public_id,
                "ordinal": r["ordinal"],
                "kind": r["kind"],
                "name": r["name"],
                "message_index": r["message_index"],
                "available": r["available"],
                "previous_analyses": r["previous_analyses"],
            }
        )
    return (
        "\n".join(lines)
        + "\n\n"
        + json.dumps(public_entries, ensure_ascii=False, indent=2)
    )


def _depot_announcement(conv_id: str) -> str:
    """La ligne qui dit à l'orchestrateur que cette discussion vit dans un espace peuplé.

    Une ligne, ou rien. Un dépôt vide n'a rien à annoncer, et « cet espace contient 0
    fichier » ne serait pas une information mais un énoncé que le modèle lirait comme
    une contrainte : les deux régressions à éviter sont donc un chat simple et un
    espace sans dépôt. La découverte reste le rôle de `find_files` — la ligne donne le
    NOMBRE et le moyen d'y aller, jamais la liste : un dépôt de cinquante fichiers ne
    doit pas coûter cinquante lignes de contexte.

    L'annonce ne conditionne PAS une création : un espace peuplé n'est pas un prérequis,
    et ses fichiers ne sont atteignables par `find_files` que si la demande porte sur
    leur contenu. La formulation le dit explicitement pour qu'un espace rempli ne se
    lise pas comme « consulte d'abord le dépôt ».

    Elle ne dépend pas du registre du tour : le cas principal est une discussion sans
    aucune pièce jointe, où le registre est vide et où le dépôt est justement ce qu'il
    reste à découvrir.

    D'OÙ LE NOMBRE : `espaces.depot()`, l'inventaire que rend l'onglet Dépôt. Une
    annonce qui contredirait l'écran serait une deuxième vérité sur le même espace.
    L'espace est déduit de la conversation en base, jamais reçu d'un appelant, et un
    espace en corbeille n'annonce rien : la corbeille est un état, pas une portée
    (`conversation_workspace`).
    """
    if not conv_id:
        return ""
    try:
        from backend.db.database import SessionLocal as _SL
        from backend.services import espaces as _esp

        db = _SL()
        try:
            workspace_id = _esp.conversation_workspace(db, conv_id)
            if not workspace_id:
                return ""
            count = len(_esp.depot(db, workspace_id)["files"])
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[depot] annonce impossible: {e}")
        return ""
    if count <= 0:
        return ""
    depose = "1 fichier déposé" if count == 1 else f"{count} fichiers déposés"
    cible = "l'atteindre" if count == 1 else "les atteindre"
    return (
        f"[Espace — {depose} dans cet espace, hors pièces jointes de ce message. "
        "Ces fichiers ne sont pas requis pour une création d'image ; "
        f"si la demande porte sur leur contenu, utilise find_files pour {cible}.]"
    )


def _horodatage(valeur: Any) -> float:
    """Instant comparable d'un horodatage ISO, ou -inf si illisible."""
    from datetime import datetime

    if valeur is None:
        return float("-inf")
    if isinstance(valeur, datetime):
        return valeur.timestamp()
    try:
        return datetime.fromisoformat(str(valeur)).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _tronquer_synthese(texte: str, maximum: int = 6000) -> str:
    """Borne le texte sur une frontière de mot, ellipsis comprise."""
    if len(texte) <= maximum:
        return texte
    coupe = texte[: maximum - 1]
    frontiere = coupe.rfind(" ")
    if frontiere >= maximum // 2:
        coupe = coupe[:frontiere]
    return coupe.rstrip() + "…"


def _synthese_espace(conv_id: str) -> str:
    """La synthèse courte de l'espace, pour l'orchestrateur — début d'une discussion.

    Au début d'une discussion d'espace, l'orchestrateur reçoit une synthèse courte de
    l'espace pour comprendre le projet et ses orientations (orchestration.md §2.1,
    contexte_et_brief.md §2.1) : cette synthèse garde son caractère de contexte
    documentaire, pas d'instruction prioritaire — les corrections et changements
    explicites du tour courant priment, l'orchestrateur sélectionne les éléments
    utiles et les transmet dans le brief, et la synthèse n'est pas injectée
    séparément en bloc dans le prompt du générateur.

    La synthèse est un gabarit déterministe : elle ne fait que condenser les
    orientations connues du registre d'espace, sans inférence, sans modèle. Elle ne
    dépend pas du registre du tour — c'est la mémoire du workspace qui porte
    l'état — et un espace sans orientation reçoit l'absence assumée, jamais un
    silence.
    """
    if not conv_id:
        return ""
    try:
        from backend.db.database import SessionLocal as _SL
        from backend.services import espaces as _esp
        from backend.services import memoire as _memoire

        db = _SL()
        try:
            workspace_id = _esp.conversation_workspace(db, conv_id)
            if not workspace_id:
                return ""
            faits = _memoire.faits_courants(
                db, str(workspace_id), statuts=("accepted", "stale")
            )
            # Une fiche invalidée par suppression de discussion n'est plus une
            # orientation de l'espace, même si elle reste `stale` pour la trace.
            faits = [
                fait
                for fait in faits
                if not (
                    fait.get("status") == "stale"
                    and fait.get("stale_reason") == "discussion supprimée"
                )
            ]
            courante = _memoire.synthese_courante(db, str(workspace_id))
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[synthese] impossible: {e}")
        return ""

    if not faits:
        # Une synthèse en cache peut encore décrire la fiche supprimée : ne pas
        # la servir. L'absence est l'état réel, et elle se dit.
        return build_space_synthesis([])["synthese"]

    # La synthèse courante est un texte DÉJÀ rendu : si elle est au moins aussi
    # récente que la fiche courante la plus récente, on la rend telle quelle. La
    # repasser dans le gabarit ajouterait un second préfixe.
    if courante is not None:
        plus_recente = max(
            (_horodatage(f.get("created_at") or f.get("freshness")) for f in faits),
            default=float("-inf"),
        )
        if (
            plus_recente == float("-inf")
            or _horodatage(courante.get("created_at")) >= plus_recente
        ):
            return str(courante.get("content") or "")

    acceptes = [f for f in faits if f.get("status") == "accepted"]
    stale = [f for f in faits if f.get("status") != "accepted"]
    acceptes.sort(
        key=lambda f: _horodatage(f.get("freshness") or f.get("created_at")),
        reverse=True,
    )
    stale.sort(
        key=lambda f: _horodatage(f.get("freshness") or f.get("created_at")),
        reverse=True,
    )
    faits = (acceptes + stale)[:12]
    return _tronquer_synthese(build_space_synthesis(faits)["synthese"])


# --- Query vision factuelle (q-r.md cas oiseau : bonne pratique outils sans contexte) ---
# Un outil n'a QUE la question + l'image : ni historique, ni conversation, ni avis à donner.
# Le message brut (« …C'est une bonne nouvelle ? ») fait répondre à l'avis au lieu de décrire.
# Le suffixe impose la description factuelle ; le jugement se fait dans le BRIEF.
_FACTUAL_VISION_SUFFIX = (
    " — Décris précisément les éléments visibles (sujet, détails, scène), "
    "sans donner d'avis ni poser de question."
)


def _factual_vision_query(query: str) -> str:
    """Nettoie + force factuel. Idempotent : ne duplique pas le suffixe. Pure, testable."""
    try:
        q = (query or "").strip()
        if not q:
            return "Décris cette image en détail."
        if "sans donner d'avis" in q:
            return q
        return (q + _FACTUAL_VISION_SUFFIX)[:1200]
    except Exception:
        return query or "Décris cette image en détail."


def _execute_vision(
    manager,
    tool_call: dict,
    image_paths: list[str] | None,
    request_id: str,
    attachment_ids: list[str] | None = None,
) -> dict:
    """Exécute vision séquentiel via ModelManager — annexe 10 : privilégie artifact_id durable."""
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        # Cadrage VLM : question autonome, FACTUELLE, sans déixis LLM ("la précédente image", ...).
        # D'où vient la query sale : Phase 0 passe message[:500] brut (avec avis « bonne nouvelle ? »,
        # déixis, refs historique), et l'orchestrateur LLM peut passer "revoit l'image".
        # Où va la query propre : clean déixis + SUFFIXE factuel imposé — le VL ne voit qu'une
        # image + une question, aucun contexte (q-r.md cas oiseau : sans suffixe il répondait à
        # l'avis au lieu de décrire). L'avis/le jugement se construit dans le BRIEF, jamais demandé
        # à l'outil. SYSTEM_VISION.format(query) + message user du VL, SANS historique.
        try:
            from backend.inference.media_policy import clean_media_query as _clean

            query = _factual_vision_query(_clean(args.get("query", "")))
        except Exception:
            query = args.get("query", "") or "Décris cette image en détail."
        # Annexe 10 : artifact_id durable (cible), image_path compat déprécié
        requested_artifact = args.get("artifact_id") or args.get("artifactId") or ""
        requested = args.get("image_path") or ""
        # Si artifact_id demandé, résoudre via Blob
        if requested_artifact:
            try:
                from backend.db.database import SessionLocal
                from backend.db.models import Artifact, Blob
                from backend.config.paths import get_data_dir

                db = SessionLocal()
                try:
                    art = (
                        db.query(Artifact)
                        .filter(Artifact.id == requested_artifact)
                        .first()
                    )
                    if art:
                        blob = db.query(Blob).filter(Blob.id == art.blob_id).first()
                        if blob:
                            data_dir = get_data_dir()
                            cand = data_dir / blob.relative_path
                            if cand.exists():
                                requested = str(cand)
                            else:
                                # Fallback staged : cherche fichier par hash prefix (toute extension)
                                from backend.config.paths import get_temp_dir

                                staged_dir = get_temp_dir() / "staged"
                                for entry in staged_dir.iterdir():
                                    if entry.name.startswith(blob.sha256):
                                        requested = str(entry)
                                        break
                finally:
                    try:
                        db.close()
                    except:
                        pass
            except Exception as e:
                log(f"[vision] resolve artifact failed: {e}")
        # Fallback : si attachment_ids fournis par run, construire valid_paths depuis blobs
        valid_from_artifacts: list[str] = []
        if attachment_ids:
            try:
                from backend.db.database import SessionLocal as _SL
                from backend.db.models import Artifact as _Art, Blob as _Blob
                from backend.config.paths import get_data_dir as _gdd

                _db = _SL()
                try:
                    for aid in attachment_ids:
                        art = _db.query(_Art).filter(_Art.id == aid).first()
                        if art:
                            blob = (
                                _db.query(_Blob).filter(_Blob.id == art.blob_id).first()
                            )
                            if blob:
                                p = _gdd() / blob.relative_path
                                if p.exists():
                                    valid_from_artifacts.append(str(p))
                finally:
                    try:
                        _db.close()
                    except:
                        pass
            except Exception:
                pass
        # Validation : si le LLM hallucine un chemin hors zone autorisée, fallback sur le vrai fichier fourni (annexe 10 : artifact_id)
        # Évite la hallucination vue dans conv.png : image_path='/tmp/thot/bea62abc-...png' flat qui n'existe pas
        valid_paths = [
            p for p in (image_paths or []) if p and Path(p).exists()
        ] + valid_from_artifacts
        if requested and valid_paths:
            # Vérifie que le chemin demandé est exactement l'un des vrais fichiers fournis
            if requested not in valid_paths:
                # Tentative resolve + check si fichier existe quand même (tolérance si hash identique)
                if not Path(requested).exists():
                    # Fallback strict : utilise le premier vrai fichier (évite hallucination)
                    img = valid_paths[0]
                else:
                    # Fichier existe mais n'était pas dans la liste (rare) — l'accepter si sous THOT_TMP
                    from backend.config.paths import THOT_TMP

                    try:
                        if str(Path(requested).resolve()).startswith(
                            str(THOT_TMP.resolve())
                        ):
                            img = requested
                        else:
                            img = valid_paths[0]
                    except Exception:
                        img = valid_paths[0]
            else:
                img = requested
        elif requested and Path(requested).exists():
            img = requested
        else:
            img = valid_paths[0] if valid_paths else ""
        if not img or not Path(img).exists():
            # Anti-fuite 01 : jamais de chemin absolu au LLM — nom seul, le chemin
            # reste privé aux exécuteurs.
            try:
                _safe = Path(requested or img).name if (requested or img) else "fichier"
            except Exception:
                _safe = "fichier"
            return {
                "ok": False,
                "result": f"[Erreur vision: fichier introuvable ({_safe})]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        # Prétraite : max 1536px + JPEG 88 (évite 1.8M tokens base64 comme texte si handler broken, réduit VRAM)
        try:
            prepared = prepare_vision_image(
                Path(img), Path(f"/tmp/thot/{request_id}/vision_prepared.jpg")
            )
            img_for_vlm = str(prepared)
        except Exception:
            img_for_vlm = img
        # Encode en base64 data URL pour le handler multimodal (ne doit pas être injecté comme texte brut)
        try:
            with open(img_for_vlm, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            # Toujours JPEG après préparation
            mime = "image/jpeg"
            data_url = f"data:{mime};base64,{b64}"
            if len(b64) > 4 * 1024 * 1024:
                log(
                    f"[vision] base64 large {len(b64)} chars (>4M), risque token blowup"
                )
        except Exception as e:
            return {
                "ok": False,
                "result": f"[Erreur vision: lecture image {e}]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        with manager.use_model("LFM2.5-VL-3B") as vlm:
            try:
                r = vlm.create_chat_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": SYSTEM_VISION.format(query=query),
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": query or "Décris cette image en détail.",
                                },
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        },
                    ],
                    temperature=0.1,
                    max_tokens=512,
                )
            except Exception as e:
                # Erreur typique si handler absent : Requested tokens (1885477) exceed context window
                if (
                    "exceed context" in str(e)
                    or "1885477" in str(e)
                    or "Requested tokens" in str(e)
                ):
                    return {
                        "ok": False,
                        "result": "VISION_MULTIMODAL_NOT_INITIALIZED: base64 traite comme texte, mmproj/handler incompatible. Verifiez mmproj et chat_handler LFM2.5-VL-3B.",
                        "latency_ms": int((time.perf_counter() - start) * 1000),
                    }
                raise
            text = r["choices"][0]["message"]["content"]
            # Annexe 12 T-VL-05 : auto-présentation sans constat visuel → pas une preuve réussie.
            # ok=False → brief honnête (« analyse échouée »), jamais « pas accès » inventé.
            if _looks_off_topic(text):
                log(
                    "[VISION_OFF_TOPIC] auto-présentation VL sans constat visuel — rejetée"
                )
                return {
                    "ok": False,
                    "result": f"[VISION_OFF_TOPIC: le modèle a parlé de lui au lieu de décrire. Extrait : {text[:300]}]",
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            return {
                "ok": True,
                "result": text,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
    except Exception as e:
        if "VISION_MULTIMODAL" in str(e):
            return {
                "ok": False,
                "result": str(e),
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        return {
            "ok": False,
            "result": f"[Erreur vision: {e}]",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


def _qualifier_image_par_vision(manager, png_bytes: bytes) -> str:
    """Pose la qualification initiale au LFM2.5-VL-3B et renvoie le verdict brut.

    Tâche 4.2 — le décision est celle du modèle de vision, jamais d'un comptage
    de caractères (docs/intentions/modeles/lfm2.5-vl-3b.md §1.2 : « Qualification
    initiale : déterminer si une image à ingérer représente un document à lire
    ou une image générale, puis orienter vers la chaîne de traitement adaptée »).
    Même voie que `_execute_vision` : manager.use_model("LFM2.5-VL-3B"), image
    réellement transmise en data URL (mecanismes/ingestion/images.md §2.4). Le
    verdatage (`verdict_qualification`) est fait par l'appelant — ici on renvoie
    la réponse pour que les erreurs de format restent attribuables à la vision.
    """
    import io

    from PIL import Image

    from backend.ingestion.qualification import PROMPT_QUALIFICATION

    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    with manager.use_model("LFM2.5-VL-3B") as vlm:
        r = vlm.create_chat_completion(
            messages=[
                {"role": "system", "content": PROMPT_QUALIFICATION},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT_QUALIFICATION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=8,
        )
        return r["choices"][0]["message"]["content"]


def _execute_doc_ocr(manager, tool_call: dict, request_id: str) -> dict:
    """OCR brouillon : natif direct si dense, sinon PaddleOCR-VL via gate (jamais de GLM-OCR).

    Autrefois : on chargeait GLM-OCR (GGUF Vision-document ; jamais livré) page par page
    via llama.cpp. MAINTENANT : qualification par vision (LFM2.5-VL-3B tranche image
    générale vs document AVANT tout OCR — voir _qualifier_image_par_vision), gate
    image/PDF (natif PyMuPDF si dense, détection régions PP-DocLayoutV3, sinon worker
    PaddleOCR-VL persistant) ; image générale → caption VL-3B (lui voit ce que l'OCR
    ne lirait pas). Limite 50 pages, render 200 DPI.
    `manager` sert à la qualification, au repli caption et à l'OCR documentaire.
    """
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        # Annexe 10 : artifact_id durable accepté comme vision (routage déterministe)
        requested_artifact = args.get("artifact_id") or args.get("artifactId") or ""
        pdf_path = args.get("pdf_path") or args.get("image_path", "")
        # Cadrage OCR : même nettoyage déictique que vision
        try:
            from backend.inference.media_policy import clean_media_query as _cleanO

            query = _cleanO(args.get("query", ""))
        except Exception:
            query = args.get("query", "")
        if requested_artifact and (not pdf_path or not Path(pdf_path).exists()):
            try:
                from backend.db.database import SessionLocal as _SLO
                from backend.db.models import Artifact as _ArtO, Blob as _BlobO
                from backend.config.paths import get_data_dir as _gddO

                _db = _SLO()
                try:
                    _art = (
                        _db.query(_ArtO).filter(_ArtO.id == requested_artifact).first()
                    )
                    if _art is not None:
                        _blob = (
                            _db.query(_BlobO).filter(_BlobO.id == _art.blob_id).first()
                        )
                        if _blob is not None:
                            _cand = _gddO() / _blob.relative_path
                            if _cand.exists():
                                pdf_path = str(_cand)
                finally:
                    try:
                        _db.close()
                    except Exception:
                        pass
            except Exception as e:
                log(f"[doc_ocr] resolve artifact failed: {e}")
        if not pdf_path or not Path(pdf_path).exists():
            # Anti-fuite 01 : jamais de chemin absolu au LLM — nom seul.
            try:
                _safe_pdf = Path(pdf_path).name if pdf_path else "fichier"
            except Exception:
                _safe_pdf = "fichier"
            return {
                "ok": False,
                "result": f"[Erreur doc_ocr: fichier introuvable ({_safe_pdf})]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        from backend.ingestion import gate as _gate

        ext = Path(pdf_path).suffix.lower()
        # Si image directe → gate 3 issues (document/table/scene)
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            with open(pdf_path, "rb") as f:
                raw = f.read()
            # Tâche 4.2 : le verdict de vision arrive AVANT tout OCR ; une erreur
            # de qualification traverse (échec bruyant, jamais de route muette).
            from backend.ingestion.qualification import verdict_qualification

            def _par_vision(png_bytes: bytes) -> str:
                return verdict_qualification(
                    _qualifier_image_par_vision(manager, png_bytes)
                )

            try:
                issue, text = _gate.gate_image(raw, qualifier=_par_vision)
            except Exception as e:
                return {
                    "ok": False,
                    "result": f"[Erreur doc_ocr: {e}]",
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            if issue == "scene":
                # Scène naturelle : l'OCR n'a rien à lire → caption VL-3B (repli documenté).
                cap = _execute_vision(
                    manager,
                    {
                        "args": {
                            "artifact_id": requested_artifact,
                            "query": query or "Décris cette image.",
                        }
                    },
                    None,
                    request_id,
                    None,
                )
                if cap.get("ok"):
                    return {
                        "ok": True,
                        "result": cap["result"],
                        "latency_ms": int((time.perf_counter() - start) * 1000),
                    }
                return {
                    "ok": False,
                    "result": "[Erreur doc_ocr: image sans texte et caption indisponible]",
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            return {
                "ok": True,
                "result": text,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        # PDF → gate (natif direct si dense, sinon PaddleOCR-VL page par page, max 50)
        try:
            _mode, _pages = _gate.ocr_pdf(pdf_path, query=query)
        except Exception as e:
            return {
                "ok": False,
                "result": f"[Erreur doc_ocr: {e}]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        return {
            "ok": True,
            "result": "\n\n--- Page suivante ---\n\n".join(_pages),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "result": f"[Erreur doc_ocr: {e}]",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


def _execute_read_text(tool_call: dict) -> dict:
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        source_id = args.get("source_id")
        max_chars = int(args.get("max_chars", 10000))
        if not source_id:
            return {
                "ok": False,
                "result": "[read_text: source_id manquant]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        from backend.db.database import SessionLocal
        from backend.db.models import Source
        from backend.config.paths import get_data_dir
        from backend.db.media_cache import get_media_root

        db = SessionLocal()
        try:
            # Annexe 10 : résout Artifact d'abord (registre annonce art_xxx)
            try:
                from backend.db.models import Artifact as _ArtRT, Blob as _BlobRT

                _art = db.query(_ArtRT).filter(_ArtRT.id == source_id).first()
                if _art is not None:
                    _blob = db.query(_BlobRT).filter(_BlobRT.id == _art.blob_id).first()
                    if _blob is not None:
                        _cand = get_data_dir() / _blob.relative_path
                        if _cand.exists():
                            text = _cand.read_text(encoding="utf-8", errors="replace")
                            if len(text) > max_chars:
                                text = text[:max_chars] + "\n[Tronqué]"
                            return {
                                "ok": True,
                                "result": text,
                                "latency_ms": int((time.perf_counter() - start) * 1000),
                            }
                        return {
                            "ok": False,
                            "result": "[read_text: fichier indisponible]",
                            "latency_ms": int((time.perf_counter() - start) * 1000),
                        }
            except Exception:
                pass
            src = db.get(Source, source_id)
            if not src:
                return {
                    "ok": False,
                    "result": f"[read_text: {source_id} introuvable]",
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            working_path = None
            if src.relative_path:
                data_dir = get_data_dir()
                for cand in [
                    data_dir / src.relative_path,
                    get_media_root() / Path(src.relative_path).name,
                    get_media_root() / src.relative_path,
                    Path.home() / ".local" / "share" / "thot" / src.relative_path,
                ]:
                    if cand.exists():
                        working_path = str(cand)
                        break
            if not working_path and src.path and Path(src.path).exists():
                working_path = src.path
            if not working_path:
                return {
                    "ok": False,
                    "result": "[read_text: fichier indisponible]",
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            text = Path(working_path).read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n[Tronqué]"
            return {
                "ok": True,
                "result": text,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        finally:
            try:
                db.close()
            except:
                pass
    except Exception as e:
        return {
            "ok": False,
            "result": f"[read_text: {e}]",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


def _check_cancel(cancel_flag) -> bool:
    return bool(cancel_flag and cancel_flag.is_set())


# ---------- Annexe 11 Lot A : persistance observations + fiches + recherche ----------
# D'OÙ VIENT CE BLOC : avant, les résultats d'outils ne vivaient qu'en mémoire du tour.
# OÙ VA QUOI : _record_observation() après chaque exécution (Phase 0 + Phase 1),
# _ensure_cards_for_registry() après construction du registre,
# _execute_search_documents() comme 5e exécuteur.
# POURQUOI ICI : un seul point d'appel, jamais bloquant (la DB ne casse pas le tour).


def _record_observation(
    tool_name: str,
    artifact_id: str,
    query: str | None,
    result: dict,
    run_id: str,
    tool_call_id: str,
    model_id: str | None = None,
) -> str | None:
    """Persiste Observation immuable + révise la fiche N+1. Ne lève jamais. Retourne l'obs_id."""
    try:
        if not artifact_id:
            return (
                None  # list-all / search : pas d'analyse mono-fichier, rien à persister
            )
        from backend.search import observations as _obsmod

        obs = _obsmod.record_tool_observation(
            artifact_id,
            tool_name,
            query,
            (result or {}).get("result", ""),
            bool((result or {}).get("ok")),
            run_id=run_id,
            tool_call_id=tool_call_id,
            model_id=model_id,
            latency_ms=int((result or {}).get("latency_ms", 0) or 0),
        )
        try:
            return getattr(obs, "id", None) if obs is not None else None
        except Exception:
            return None
    except Exception as e:
        log(f"[observation] record failed: {e}")
        return None


def _evenement_usage(tool_name: str) -> str | None:
    """Nature de la sollicitation d'un outil de recherche (recherche_locale.md §6.4).

    - `retour` : la ressource a été effectivement rendue (search_info / find_files).
    - `interrogation` : la question a été réellement traitée sur une ressource (ask_files).

    Les autres outils ne portent pas ce signal : la consultation utilisateur
    vit ailleurs et une erreur d'outil ne compte jamais comme un usage.
    """
    if tool_name in ("find_files", "search_info"):
        return EVENEMENT_SIGNAL_RETOUR
    if tool_name == "ask_files":
        return EVENEMENT_SIGNAL_INTERROGATION
    return None


def _ressources_pour_signal(
    db: Any, conv_id: str, tool_name: str, args: dict, result: dict
) -> list[str]:
    """Ressources réellement rendues / interrogées, en identifiants internes.

    `ask_files` interroge : ses ressources sont les `resource_ids` posés dans les
    arguments (`doc_N`), que le résultat répertorie via `public_ids`. `search_info`
    et `find_files` publient des `doc_N` dans les métadonnées du résultat (le rendu
    texte, jamais l'`art_xxx` brut). Le signal ne compte donc jamais une ressource
    sans identité exploitable.
    """
    from backend.search import docrefs as _docrefs

    ressources: list[str] = []

    def _pusser(identifiant: object) -> None:
        valeur = str(identifiant or "").strip()
        if valeur and valeur not in ressources:
            ressources.append(valeur)

    if tool_name == "ask_files":
        # Les deux sources portent la même cible : le résultat (`public_ids`) et
        # les args (`resource_ids`). Parcourir dans cet ordre puis dédupliquer par
        # artefact résolu : un fichier interrogé deux fois ne compte qu'une fois.
        for candidat in [
            *(result.get("public_ids") or []),
            *(args.get("resource_ids") or []),
        ]:
            valeur = str(candidat or "").strip()
            if not valeur:
                continue
            interne = valeur
            if _docrefs.is_public_id(valeur):
                interne = str(_docrefs.resolve_doc_label(db, conv_id, valeur) or valeur)
            _pusser(interne)
        # Fallback : identifiant brut (art_xxx / source_xxx) — image / ocr.
        _pusser(
            args.get("artifact_id") or args.get("artifactId") or args.get("source_id")
        )
        return ressources
    for public_id in result.get("public_ids") or []:
        candidat = str(public_id or "").strip()
        if not candidat:
            continue
        interne = candidat
        if _docrefs.is_public_id(candidat):
            interne = str(_docrefs.resolve_doc_label(db, conv_id, candidat) or candidat)
        _pusser(interne)
    return ressources


def _signal_importance(
    tool_name: str,
    args: dict,
    result: dict,
    conv_id: str,
    response_id: str,
) -> None:
    """Signal d'importance (recherche_locale.md §6.4) : un usage réel, jamais un bruit.

    Un outil de recherche qui réussit a sollicité une ressource — rendue
    (`retour`) ou interrogée (`interrogation`). Cette sollicitation compte pour
    le signal d'importance de la fiche liée : c'est l'usage réel qui pèse, pas
    l'intention. Le signal est un plus : il ne lève jamais et ne fait pas
    échec le tour qui le porte.
    """
    evenement = _evenement_usage(tool_name)
    if evenement is None or not result.get("ok"):
        return
    try:
        from backend.db.database import SessionLocal as _SLC
        from backend.services import espaces as _esp
        from backend.services import memoire as _memoire

        db = _SLC()
        try:
            workspace_id = _esp.conversation_workspace(db, conv_id)
            if not workspace_id:
                return
            ressources = _ressources_pour_signal(db, conv_id, tool_name, args, result)
            if not ressources:
                return
            _memoire.enregistrer_usages(
                db,
                str(workspace_id),
                ressources,
                evenement=evenement,
                response_id=response_id,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[importance] signal d'usage non enregistré : {e}")


def _ensure_cards_for_registry(conv_id: str, attachment_registry: list | None) -> None:
    """Self-healing fiches : garantit fiche rev 1 + index pour chaque id du registre."""
    try:
        ids = [
            e.get("id")
            for e in (attachment_registry or [])
            if isinstance(e, dict) and e.get("id")
        ]
        ids = list(dict.fromkeys(ids))
        if not ids:
            return
        from backend.db.database import SessionLocal as _SLC
        from backend.db.models import Artifact as _ArtC
        from backend.search import cards as _cards, fts as _ftsm

        db = _SLC()
        try:
            _ftsm.ensure_fts(db)
            for aid in ids:
                try:
                    art = db.query(_ArtC).filter(_ArtC.id == aid).first()
                    if art is not None:
                        _cards.ensure_card_on_import(db, art)
                except Exception:
                    continue
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[cards] ensure registry failed: {e}")


def _execute_search_documents(tool_call: dict, conv_id: str | None = None) -> dict:
    """Exécuteur search_documents : scope AVANT retrieval, page bornée, jamais de chemins."""
    start = time.perf_counter()
    try:
        import json as _json

        args = tool_call.get("args") or tool_call.get("arguments") or {}
        query = (args.get("query") or "").strip()
        scope = (args.get("scope") or "conversation").strip().lower()
        recency = (args.get("recency") or "").strip().lower()
        if recency not in ("", "recent", "today"):
            return {
                "ok": False,
                "result": "[ENUM_INVALID] recency doit être recent ou today.",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        if scope not in ("conversation", "workspace"):
            return {
                "ok": False,
                "result": "[SEARCH_SCOPE_INVALID: scope doit être 'conversation' ou 'workspace']",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        if not conv_id:
            return {
                "ok": False,
                "result": "[SEARCH_SCOPE_INVALID: conversation inconnue]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        from backend.search import scope as _scopemod, fts as _ftsmod
        from backend.db.database import SessionLocal as _SLS

        if scope == "workspace":
            # Lot B : recherche d'INFORMATION au niveau espace. L'espace est
            # déduit serveur-side de la conversation — jamais reçu du client.
            # Le corpus est celui de TOUS les chats de l'espace ; en revanche
            # aucun artifact_id n'est rendu, parce qu'un extrait d'information
            # n'est pas une poignée sur le fichier (matrice, ligne 3).
            from backend.services import espaces as _esp

            db0 = _SLS()
            try:
                ws_id = _esp.conversation_workspace(db0, conv_id)
            finally:
                try:
                    db0.close()
                except Exception:
                    pass
            if not ws_id:
                return {
                    "ok": False,
                    "result": "[SEARCH_SCOPE_INVALID: cette conversation n'appartient à aucun espace]",
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            db = _SLS()
            try:
                from backend.search import space_info as _sinfo

                page = _sinfo.search_space_information(
                    db, ws_id, query=query, limit=int(args.get("limit") or 10)
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            return {
                "ok": bool(page.get("ok")),
                "result": "[search_documents:espace] "
                + _json.dumps(page, ensure_ascii=False)[:6000],
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        allowed = _scopemod.resolve_allowed_artifacts(conv_id)
        db = _SLS()
        try:
            _ftsmod.ensure_fts(db)
            page = _ftsmod.search(
                db,
                query=query,
                scope_kind="conversation",
                scope_id=conv_id,
                allowed_ids=allowed,
                types=args.get("types"),
                status=args.get("status") or "any",
                limit=args.get("limit") or 10,
                cursor=args.get("cursor"),
            )
            if recency and page.get("ok"):
                from datetime import datetime, timedelta, timezone
                from backend.config.defaults import TOOL_LIMITS
                from backend.db.models import Artifact as _RecencyArtifact

                hours = (
                    int(TOOL_LIMITS["today_hours"])
                    if recency == "today"
                    else int(TOOL_LIMITS["recent_days"]) * 24
                )
                cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                raw_results = list(page.get("results") or page.get("candidates") or [])
                kept: list[dict] = []
                for candidate in raw_results:
                    artifact_id = (
                        candidate.get("artifact_id")
                        if isinstance(candidate, dict)
                        else ""
                    )
                    if not artifact_id:
                        continue
                    artifact = (
                        db.query(_RecencyArtifact)
                        .filter(_RecencyArtifact.id == artifact_id)
                        .first()
                    )
                    created = (
                        getattr(artifact, "created_at", None) if artifact else None
                    )
                    if created is None:
                        continue
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created >= cutoff:
                        kept.append(candidate)
                page = dict(page)
                page["results"] = kept
                page["candidates"] = len(kept)
                page["returned_count"] = len(kept)
        finally:
            try:
                db.close()
            except Exception:
                pass
        if not page.get("ok"):
            return {
                "ok": False,
                "result": f"[{page.get('code', 'SEARCH_ERROR')}: {page.get('message', '')}]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        return {
            "ok": True,
            "result": _json.dumps(page, ensure_ascii=False),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "result": f"[search_documents: {e}]",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


def _execute_read_document(tool_call: dict, conv_id: str | None = None) -> dict:
    """Exécuteur read_document : extrait localisé + evidence_id (Lot B)."""
    start = time.perf_counter()
    try:
        import json as _json

        args = tool_call.get("args") or tool_call.get("arguments") or {}
        from backend.search import reader as _readermod, scope as _scopemod
        from backend.db.database import SessionLocal as _SLR

        allowed = _scopemod.resolve_allowed_artifacts(conv_id) if conv_id else set()
        # BORNE OUTIL : `read_document` est un outil de LECTURE de prompt, donc
        # borné à 16 k par `ReadDocumentArgs.max_chars` (`le=16000`). Le chemin
        # `loop.py` ne passe pas par pydantic pour cet exécuteur ; sans ce
        # plafond, un appel direct pouvait demander 200 k — la borne interne du
        # `reader` existe pour `offset:N`/`page:N`, pas pour un outil.
        max_chars = args.get("max_chars") or 8000
        try:
            max_chars = max(500, min(int(max_chars), 16000))
        except (TypeError, ValueError):
            max_chars = 8000
        db = _SLR()
        try:
            payload = _readermod.read_payload(
                db,
                args.get("artifact_id") or "",
                allowed,
                query=args.get("query"),
                locator=args.get("locator"),
                max_chars=max_chars,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass
        if not payload.get("ok"):
            return {
                "ok": False,
                "result": f"[{payload.get('code', 'READ_ERROR')}: {payload.get('message', '')}]",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        # Lecture = travail réutilisable → observation (idempotente par empreinte ;
        # le hook Phase 1 enrichira run_id/tool_call_id s'il suit).
        try:
            from backend.search import observations as _obsRD

            _obsRD.record_tool_observation(
                args.get("artifact_id") or "",
                "read_document",
                args.get("query") or "",
                payload.get("excerpt", "") or "",
                True,
            )
        except Exception as e:
            log(f"[observation] read_document record failed: {e}")
        return {
            "ok": True,
            "result": _json.dumps(payload, ensure_ascii=False),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "result": f"[read_document: {e}]",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


# ---------- Exécuteurs brouillon (3 natures) ----------
# D'OÙ VIENT CE BLOC : le brouillon remplace 7 outils art_xxx par 3 outils doc_N
# (search_info = contenu, find_files = identité, ask_files = lecture ciblée).
# OÙ VA QUOI : chaque exécuteur valide via son input_model, mappe doc_N → art_xxx
# (docrefs), délègue au moteur existant (FTS/cards/lecteurs), rend du texte au modèle.
# POURQUOI PROVISOIRE : le fond ColBERT (chunks doc_N:k) arrive au lot suivant ;
# en attendant, les preuves ask_files sont doc_N:1 provisoires, jamais inventées au-delà.
# Sous-outils : _execute_read_text/_execute_vision/_execute_doc_ocr/_execute_read_document/
# _execute_search_documents sont câblés et appellent le modèle sans passer par lui.
# `get_source` et `get_document_card` ont été retirés : aucun appelant, donc aucune
# capacité — ils ne servaient qu'à annoncer des actions que le modèle ne pouvait
# pas exécuter.

_DOC_EVIDENCE_RE = re.compile(r"(doc_[1-9]\d*):([0-9]+)\Z")
_WEB_EVIDENCE_RE = re.compile(r"(web_[0-9a-f]{12}):([0-9]+)\Z")


def _persisted_evidence_id(
    db, artifact_id: str, doc_label: str, quote: str
) -> str | None:
    """Preuve citable d'un contenu : fragment PERSISTÉ, sinon observation source.

    Jamais ``doc_N:0`` ni un rang de résultat : une preuve qu'on ne peut pas relire
    est pire qu'une absence de preuve. Sans fragment ni observation, ``None``.
    """
    try:
        from backend.search import cards as _cards, chunks as _chunks

        ordinal = _chunks.ordinal_of_quote(db, artifact_id, quote)
        if ordinal is not None:
            return f"{doc_label}:{ordinal}"
        card = _cards.get_current_card(db, artifact_id)
        source_observation = str(
            getattr(card, "source_observation_id", "") or ""
        ).strip()
        return source_observation or None
    except Exception:
        return None


def _execute_web_research(
    tool_call: dict,
    conv_id: str | None = None,
    *,
    user_message: str = "",
    admis: set[str] | None = None,
) -> dict:
    """web_research : la seule sortie réseau de la boucle, et elle est bornée.

    Le chemin vivant passe par ici, pas par `WebResearchTool.call()` : c'est la
    même architecture à deux couches que `search_info`. La validation est celle
    du schéma publié (`WebResearchArgs`), donc un argument hors liste rend
    `[UNKNOWN_ARG]` et une borne trop longue rend `[TOO_LONG]` — jamais
    `BAD_ARGS` générique. Toute l'exécution est dans
    `backend/services/web/research.py`, qui porte le cache de conversation, le
    choix des moteurs, le fetcher fermé et le rendu.
    """
    from pydantic import ValidationError

    from backend.services.web import research as _web
    from backend.tools.web_research import WebResearchArgs

    start = time.perf_counter()
    args = tool_call.get("args") or tool_call.get("arguments") or {}
    if not isinstance(args, dict):
        return {
            "ok": False,
            "result": "[BAD_ARGS] Arguments illisibles.",
            "latency_ms": 0,
        }
    try:
        modeles = WebResearchArgs.model_validate(args)
    except ValidationError as exc:
        detail = _premier_code_spec(exc)
        # On nomme l'APPEL fautif : un message qui dit seulement « purpose est
        # obligatoire » laisse l'orchestrateur sans savoir quel de ses appels
        # corriger, et il recommence la même erreur sur l'outil suivant.
        # Le CODE reste en tete du message : c'est lui que la boucle relit et
        # que la page affiche. Nommer l'outil en second n'ajoute que le
        # contexte, ca ne decale pas le code.
        return {
            "ok": False,
            "result": f"{detail or '[BAD_ARGS]'} (outil : web_research)",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    outcome = _web.run(
        queries=list(modeles.queries),
        # Le sélecteur de source n'existe pas dans la signature publiée : le
        # moteur reste un choix du backend (fiche web_research).
        source_type="general",
        freshness=modeles.freshness or FRESHNESS_REUSE,
        purpose=modeles.purpose,
        conv_id=conv_id or "",
        user_message=user_message,
        admitted_before=frozenset(admis or ()),
    )
    if admis is not None:
        # Mémoire de la règle de provenance pour la suite du tour : une URL que
        # le modèle n'a vue qu'ici reste lisible, une URL jamais vue ne l'est pas.
        admis.update(outcome.urls_read)
    payload = outcome.as_result()
    payload["latency_ms"] = outcome.latency_ms
    return payload


def _premier_code_spec(exc: object) -> str:
    """Le code `[CODE]` qu'un validateur a écrit, rendu tel quel.

    Même règle que `BaseTool.call()` : on préserve le code exact plutôt que de
    substituer un `BAD_ARGS` qui ferait perdre au modèle la raison précise.
    """
    erreurs = getattr(exc, "errors", None)
    if not callable(erreurs):
        return ""
    detail = erreurs()
    if not isinstance(detail, (list, tuple)):
        return ""
    for erreur in detail:
        if not isinstance(erreur, Mapping):
            continue
        ctx = erreur.get("ctx") or {}
        brut = str(ctx.get("error") or "") if isinstance(ctx, Mapping) else ""
        match = re.search(r"\[([A-Z_]+)\]\s*(.+)", brut, re.DOTALL)
        if match:
            return f"[{match.group(1)}] {match.group(2).strip()}"
    # Aucun code métier dans l'erreur : on délègue au MÊME extracteur que
    # `ToolExecutor`. Sans cela un enum violé rendait `[BAD_ARGS] Arguments hors
    # contrat` par cette porte et `[ENUM_INVALID]` par l'autre : deux réponses
    # pour une même faute, selon le chemin emprunté.
    from backend.tools.base import BaseTool as _BaseTool

    try:
        # `_extract_spec_error` est typé `ValidationError` mais ne fait qu'appeler
        # `.errors()` — ce que `_premier_code_spec` a DÉJÀ vérifié ci-dessus. Le cast
        # dit cette vérif d'une manière que le lecteur puisse vérifier, au lieu de
        # laisser une erreur de type qui fait croire que l'appel est faux.
        return _BaseTool._extract_spec_error(cast(Any, exc))[0]
    except Exception:
        return ""


# ---------- Adaptateurs : la logique vit dans `backend/tools/` ----------
# OÙ EST LA LOGIQUE : `SearchInfoTool` (`backend/tools/search_info.py`) et
# `AskFilesTool` (`backend/tools/ask_files.py`) l'implémentent déjà, et RICHEment :
# périmètre résolu par `scope.resolve_allowed_artifacts`, recherche par
# `fts.search_scopes` (les DEUX portées — discussion ET dépôt de l'espace), fiche
# lue dans sa forme IMBRIQUÉE `r["card"]["summary"]`, `ID_UNKNOWN` distingué de
# `ID_OUT_OF_SCOPE`, timeout et troncature dans `BaseTool.call`.
#
# POURQUOI UN ADAPTATEUR : `_execute_search_info` et `_execute_find_files` étaient une
# SECONDE implémentation, plus pauvre. Elle interrogeait la seule portée
# `conversation` — donc le dépôt de l'espace était INVISIBLE, alors que la Bible
# §2.5 (l. 143) promet « les fichiers du dépôt de l'espace auquel appartient la
# discussion » — et lisait la fiche À PLAT (`r["summary"]`), là où elle est
# imbriquée : aucun extrait, donc aucune preuve, donc `[preuve indisponible]` sur
# chaque ligne. Doubler une implémentation ne la rend pas plus fidèle : cela
# garantit qu'un jour les deux divergeront, et c'est exactement ce qui était
# arrivé. Un adaptateur, lui, ne peut plus diverger.
#
# OÙ EN EST `search_info` : fait. `_execute_search_info` ci-dessous délègue, et le
# test de parité (`tests/backend/test_parite_outils.py`) compare les deux portes.
# OÙ EN EST `find_files` : fait aussi. L'ancien chemin n'interrogeait que la portée
# `conversation` (§2.5 l. 143 et l. 147) et recalculait ses `doc_N` à la main, donc il
# perdait le dépôt de l'espace ET les identifiants que la boucle alimente.
# OÙ EN EST `ask_files` : fait. Il demandait au MODÈLE de relire le fichier à chaque
# question (vision sur les pixels, OCR sur les pages) — ce que la Bible §2.6 l. 189
# interdit explicitement. Voir le bloc de commentaire devant `_execute_ask_files`.

_ID_PUBLIC_RE = re.compile(r"doc_[1-9]\d*\Z")


def _appel_outil_sync(outil, arguments: dict, conv_id: str | None):
    """Exécute `outil.call(arguments, ctx)` depuis ce code SYNCHRONE.

    `call()` est `async` — c'est là que vivent `asyncio.timeout` (le délai de l'outil)
    et la troncature à `max_output_chars` — alors que le dispatch de découverte est
    synchrone et s'exécute dans le worker de `ToolExecutor`. `asyncio.run` est donc
    le pont : il ouvre une boucle NEUVE et la referme, il ne suppose aucun
    `event_loop` ambiant et n'imbrique rien. Le sidecar est une boucle stdin
    synchrone (`backend/main.py`, `handle`) et `ToolExecutor` fournit déjà son propre
    thread par appel : il n'y a donc aucune boucle à réutiliser, et en fabriquer une
    par outil n'aurait rien à gagner. C'est le pont que `_execute_find_files` employait
    déjà pour `FindFilesTool`, par une porte contournant `call()` — d'où le timeout et la
    troncature qu'elle perdait.

    Retourne le `ToolResult` — jamais un `dict` reconstruit : l'implémentation riche
    décide du texte, des preuves et du plafond, et un adaptateur qui recalculerait
    l'un des trois recommencerait le défaut qu'on vient de supprimer.
    """
    from backend.tools.base import ToolContext

    ctx = ToolContext(
        run_id=conv_id or "", request_id=conv_id or "", conv_id=conv_id or None
    )
    return asyncio.run(outil.call(arguments, ctx))


def _resultat_pour_la_boucle(
    outil, tool_call: dict, conv_id: str | None, start: float
) -> dict:
    """Un `dict` de tour (`ok`, `result`, `evidence_ids`, `public_ids`, `latency_ms`).

    C'est le SEUL format que la boucle sait relire : elle l'émet, le persiste,
    l'alimente dans l'inventaire des preuves et le range dans `tool_results`. Un
    adaptateur se justifie par cette frontière — pas pour réécrire le comportement
    de l'outil, qui n'a rien à faire ici.

    Les preuves et les `doc_N` sont RECopiés du `ToolResult`, jamais recalculés : les
    recalculer ressusciterait exactement les deux bugs que la classe `Tool` avait
    supprimés (rang de résultat, `art_xxx` brut). Ils sont lus sur des champs à part
    et non dans `content` parce que `call()` tronque `content` : un `search_info` à
    trois requêtes produit ~6 ko de payload, donc la troncature coupe le JSON en
    plein milieu et l'y rend illisible. Une boucle qui perdrait ses preuves dès que
    le texte dépasse le plafond pourrait quand même citer une réponse tronquée.

    `latency_ms` reste MESURÉ sur ce chemin et non repris du `ToolResult` : c'est la
    latence de l'appel de tour (pont sync compris), celle que la boucle compte dans
    son budget, et non celle du seul `execute`.
    """
    arguments = tool_call.get("args") or tool_call.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    from backend.tools.base import ToolStatus

    resultat = _appel_outil_sync(outil, arguments, conv_id)
    latence = {"latency_ms": int((time.perf_counter() - start) * 1000)}
    preuves = [str(e).strip() for e in resultat.evidence_ids if str(e or "").strip()]
    # Un `public_id` qui n'a pas la forme `doc_N` n'est pas un identifiant
    # public : la boucle le refuse déjà en amont (`public_artifact_ids`), le
    # faire ici évite de lui envoyer un `art_xxx` qu'elle devrait écarter.
    publiques = [
        str(p).strip()
        for p in resultat.public_ids
        if _ID_PUBLIC_RE.fullmatch(str(p or "").strip())
    ]
    if not resultat.ok:
        # Le CODE d'erreur garde la tête de la ligne : c'est lui que la boucle relit
        # et que la page affiche. `display_summary` ne le porte pas.
        if resultat.status == ToolStatus.PARTIAL:
            # Contrat commun §3.3 « éléments utiles obtenus, mais une partie de
            # l'opération n'a pas abouti » et §5.1 « Réussite partielle conservée
            # avec les sous-échecs » ; ask_files.md §6.2 « Le backend conserve les
            # éléments disponibles ». Une réussite partielle rend donc ENCORE ses
            # sous-succès (le display_summary, avec ses résultats par requête) EN
            # PLUS du code d'erreur, et ses preuves restent transportées —
            # mais `ok` reste False : l'arbitrage du tour tient toujours.
            sous_succes = resultat.display_summary or resultat.content
            morceaux = [x for x in ((resultat.error or ""), sous_succes) if x.strip()]
            return {
                "ok": False,
                "result": "\n".join(morceaux) or f"[{outil.name}] échec",
                "evidence_ids": preuves,
                "public_ids": publiques,
                **latence,
            }
        return {
            "ok": False,
            "result": resultat.error
            or resultat.display_summary
            or f"[{outil.name}] échec",
            **latence,
        }
    return {
        "ok": True,
        "result": resultat.display_summary or resultat.content,
        "evidence_ids": preuves,
        "public_ids": publiques,
        **latence,
    }


def _execute_search_info(tool_call: dict, conv_id: str | None = None) -> dict:
    """search_info : un adaptateur. La recherche est dans `SearchInfoTool`.

    Le périmètre passe désormais par `scope.resolve_allowed_artifacts` puis
    `fts.search_scopes` : le dépôt de l'espace redevient visible. Ce n'est pas un
    élargissement arbitraire — `resolve_allowed_artifacts` décide du DROIT (liens
    actifs de CETTE conversation et de SON espace), et `search_scopes` n'ajoute que
    la porte de recherche qui manquait, avec le même `allowed_ids` des deux côtés.

    WHY (ligne Bible §2.5) : « Son périmètre comprend : les fichiers du dépôt de
    l'espace auquel appartient la discussion ». Forcer `scope="conversation"`
    rendait cette ligne fausse pour la moitié espace du contrat.
    """
    from backend.tools.search_info import SearchInfoTool

    return _resultat_pour_la_boucle(
        SearchInfoTool(), tool_call, conv_id, time.perf_counter()
    )


def _execute_find_files(tool_call: dict, conv_id: str | None = None) -> dict:
    """find_files : un adaptateur, comme `_execute_search_info`.

    CE QUE L'ANCIEN CHEMIN PERDAIT, et qui est ici rendu sans rien recalculer :

    - le DÉPÔT de l'espace. Il interrogeait `search_documents` avec
      `scope="conversation"`, une portée où un fichier versé au dépôt n'est pas
      candidat (§2.5 l. 147). MESURÉ avant ce changement : deux fichiers, un mot
      commun, un versé au dépôt et un joint à la discussion — `FindFilesTool.call()`
      en rendait 2, ce chemin 1, et le dépôt était INVISIBLE.
    - les `doc_N`. Il les recalculait avec `_doc_label_for_artifact`, donc il pouvait
      en nommer un que la discussion n'a pas choisi, et n'en nommait aucun en mode
      liste (`"public_ids": []` en dur). La boucle se voyait donc priver de ses
      `public_artifact_ids`, sans le voir : le `doc_N` servait à `ask_files`, puis le
      modèle se heurtait à un identifiant qu'il croyait avoir.
    - le délai et la troncature, qui vivent dans `BaseTool.call` et nulle part ailleurs.

    Le tri par date et le filtre par type du mode liste n'ont jamais été écrits ici : ils
    étaient DÉJÀ dans `FindFilesTool.execute`, et `_find_files_listing` les empruntait en
    contournant `call()`. Cet appel-ci rend cette deuxième porte inutile, donc elle a été
    supprimée avec les deux helpers qu'elle seule utilisait (`_map_types_for_fts`,
    `_doc_label_for_artifact`).
    """
    from backend.tools.find_files import FindFilesTool

    return _resultat_pour_la_boucle(
        FindFilesTool(), tool_call, conv_id, time.perf_counter()
    )


# OÙ EN EST `ask_files` : fait, et c'est le dernier des trois. `_execute_ask_files`
# délègue à `AskFilesTool`, qui lit la REPRÉSENTATION D'INGESTION — `etat.texte_ingeste`
# et `chunks.best_source_text` — sans appeler ni vision, ni OCR, ni le LLM.
#
# CE QUE LE CHEMIN ABANDONNÉ FAISAIT, et que la Bible §2.6 (l. 189) interdit :
# « L'ingestion initiale doit être effectuée à l'ajout du fichier, sans recommencer
# systématiquement l'analyse complète à chaque question. » Il ROUTAIT par type —
# image → `vision` (un VLM sur les pixels), pdf → `doc_ocr` (un modèle OCR) puis
# repli `read_document` — donc CHAQUE question relançait le travail d'ingestion, et
# chaque réponse était freshly recalculée au lieu d'être lue. `§2.5 l. 163` le dit
# autrement : « il s'appuie sur le fichier d'ingestion associé au document ».
#
# Le corollaire est ce que la reprise perdait aussi : un fichier dont l'ingestion
# est `binaire_refuse` ou `echec` n'a rien à interroger, et l'ancien chemin le
# rediscoverait en relançant un modèle pour rien — donc il pouvait répondre à une
# question sur un fichier que rien n'a jamais pu lire. Le motif vient désormais de
# l'état d'ingestion (`etat.phrase_sans_texte`), unique et écrit au dépôt.
#
# LES QUATRE PARAMÈTRES CONSERVÉS SONT MORTS, et c'est le fait à voir : `manager`,
# `image_paths`, `request_id` et `attachment_ids` n'étaient là que pour router vers
# les modèles. On ne les retire pas ici — le dispatch de découverte (`execute_discovery`)
# et ses tests les passent encore positionnellement, et les changer ici mêlerait deux
# tâches. Leur suppression avec le reste du chemin `_execute_*` est l'affaire de 4.5.
def _execute_ask_files(
    tool_call: dict,
    manager=None,
    conv_id: str | None = None,
    image_paths=None,
    request_id: str = "",
    attachment_ids=None,
) -> dict:
    """ask_files : un adaptateur, comme `_execute_search_info` et `_execute_find_files`.

    On rend `public_ids` et `evidence_ids` RECopiés du `ToolResult` — jamais
    recalculés : une preuve doit pointer un fragment PERSISTÉ, et le `doc_N` cité
    doit être celui que l'outil a réellement résolu dans CETTE conversation.
    """
    from backend.tools.ask_files import AskFilesTool

    return _resultat_pour_la_boucle(
        AskFilesTool(), tool_call, conv_id, time.perf_counter()
    )


def _replay_published_image(
    db,
    artifact_id: str,
    run_id: str,
    request_id: str,
    conv_id: str | None,
    start: float,
    relink_into_conversation: bool = False,
) -> dict:
    """Réutilise un effet DÉJÀ publié. Zéro diffusion — le registre a la réponse.

    Redemander la même image est une action légitime (C09/C11 : même demande →
    même sortie). L'artefact publié appartient à la conversation qui l'a demandé ;
    ``relink_into_conversation`` le rattache à CELLE-CI pour qu'il y soit
    visible et attachable, au lieu de laisser la boucle annoncer un échec.

    Fail loud : un artefact absent, non image ou à blob mort est un état
    inexploitable — on lève, on ne fabrique jamais un succès sans pixels.
    """
    from backend.services import image_service
    from backend.services.effect_ledger import (
        artifact_event_key,
        is_artifact_replayable,
        mark_event_published_by_key,
    )

    artifact_id = str(artifact_id or "")
    if not artifact_id:
        raise RuntimeError(
            "[REPLAY_UNUSABLE] Effet publié sans identifiant d'artefact."
        )
    if relink_into_conversation and conv_id:
        if not is_artifact_replayable(db, artifact_id, None):
            raise RuntimeError(
                f"[REPLAY_UNUSABLE] L'artefact {artifact_id} de l'effet publié "
                "est introuvable, n'est pas une image, ou son blob n'est pas disponible."
            )
        image_service.link_artifact_to_conversation(db, artifact_id, conv_id)
    from backend.db.models import Run

    run = db.get(Run, run_id)
    if run is not None:
        run.status = "completed"
        run.output_artifact_id = artifact_id
    db.commit()
    delivered = emit(
        "artifact_created",
        {
            "request_id": request_id,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "experimental": True,
            "deduplicated": True,
        },
        run_id=run_id,
        conversation_id=conv_id,
    )
    if delivered is not False:
        mark_event_published_by_key(db, artifact_event_key(run_id, artifact_id))
    return {
        "ok": True,
        "image_published": True,
        "artifact_id": artifact_id,
        "deduplicated": True,
        "result": f"[generate_image] {artifact_id} (rejeu de l'effet publié, sans nouvelle diffusion)",
        "latency_ms": int((time.perf_counter() - start) * 1000),
    }


_IMAGE_RETIRED_AFTER_DELETE = (
    "l'effet précédent pointait vers une image supprimée de la galerie : "
    "nouvelle diffusion"
)
_DELETED_ARTIFACT_EFFECT_ERROR = (
    "artefact supprimé de la galerie (soft-delete) : aucun pixel rejouable, "
    "l'effet est retraité"
)
_DELETED_ARTIFACT_USER_LIMIT = (
    "L’image déjà publiée pour cette demande avait été supprimée de la galerie ; "
    "la nouvelle génération n’a pas abouti."
)


_ERROR_ENVELOPE_RE = re.compile(r"^\[([A-Za-z_][A-Za-z_0-9]*):\s*(.*)\]$", re.DOTALL)
_ERROR_TAG_RE = re.compile(r"^\[([A-Z][A-Z_0-9]*)\]\s*(.*)$", re.DOTALL)
_ERROR_UNWRAP_MAX_DEPTH = 3


def _flat_error_tag(tool_result: str) -> str:
    """Un SEUL niveau de tag d'erreur dans un résultat d'outil.

    D'OÙ : l'échec du service porte DÉJÀ son code (`[SERVICE_UNAVAILABLE] …`), et
    l'exécuteur l'enveloppait dans `[generate_image: …]`. L'utilisateur lisait
    donc `[generate_image: [SERVICE_UNAVAILABLE] …]` — deux accolades pour un seul
    fait, et le code utile noyé. On RETIRE l'enveloppe : le code le plus
    spécifique est celui qui reste. Idempotent : `[X] …` rend `[X] …`.
    """
    text = str(tool_result or "")
    for _ in range(_ERROR_UNWRAP_MAX_DEPTH):
        envelope = _ERROR_ENVELOPE_RE.match(text)
        if envelope is None:
            break
        text = envelope.group(2).strip()
    tag = _ERROR_TAG_RE.match(text)
    if tag is None or not tag.group(2):
        return text
    return f"[{tag.group(1)}] {tag.group(2)}"


def _retire_unpublished_effect(db, effect_id: str, reason: str) -> None:
    """Laisse un effet abandonné dans un état TERMINAL, jamais `uncertain`.

    D'OÙ : `execute_claimed_image` marque `uncertain` quand la diffusion lève —
    y compris quand elle n'a jamais commencé (`[SERVICE_UNAVAILABLE]`). `uncertain`
    garde l'empreinte dans l'index unique : la demande identique est alors
    refusée `EFFECT_UNCERTAIN` POUR TOUJOURS, dans toutes les conversations,
    alors que rien n'a été produit. Même verdict que
    `retire_deleted_publication` : un effet non réutilisable devient `failed`,
    ce qui libère l'empreinte et laisse la tentative suivante réclamer un effet
    neuf. Fail loud déjà fait son travail si l'effet a atteint un état terminal
    entre-temps (publié) : on ne réécrit pas un effet publié.
    """
    if not effect_id:
        return
    from backend.services.effect_ledger import EffectConflict, mark_failed

    try:
        mark_failed(db, effect_id, reason)
    except EffectConflict:
        pass
    except Exception:
        pass


def _image_failure(tool_result: str, user_limit: str = "", latency_ms: int = 0) -> dict:
    """Échec image HOMONYMEMENT décrit : le statut doit dire pourquoi.

    `user_limit` vide = aucune information supplémentaire : le consommateur
    (_image_failure_limit) retombera alors sur son libellé par défaut.
    """
    return {
        "ok": False,
        "image_published": False,
        "result": _flat_error_tag(tool_result),
        "user_limit": user_limit,
        "latency_ms": latency_ms,
    }


def _execute_generate_image(
    tool_call: dict,
    manager=None,
    conv_id: str | None = None,
    request_id: str = "",
    run_id: str = "",
    cancel_flag=None,
    on_stage=None,
) -> dict:
    """Diffusion T2I du chat. `on_stage` relaie la progression (STAGE: worker +
    publication) vers l'appelant : sans elle, une génération de plusieurs minutes
    s'affiche comme un silence. Le service la consomme telle quelle."""
    start = time.perf_counter()
    args = tool_call.get("args") or tool_call.get("arguments") or {}
    try:
        from pydantic import ValidationError

        from backend.tools.generate_image import GenerateImageArgs

        parsed = GenerateImageArgs.model_validate(args)
    except ValidationError as exc:
        return _image_failure(
            f"[generate_image: {exc}]",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
    except Exception as exc:
        return _image_failure(
            f"[generate_image: {exc}]",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )

    from backend.db.database import SessionLocal
    from backend.db.models import ImageEffect, Run
    from backend.services import image_service
    from backend.services.chat_image_plan import (
        prepare_chat_image,
        trigger_generated_ingestion,
    )
    from backend.services.effect_ledger import (
        EffectConflict,
        EffectInProgress,
        artifact_event_key,
        claim_image_effect,
        execute_claimed_image,
        image_request_fingerprint,
        is_artifact_replayable,
        mark_event_published_by_key,
        retire_deleted_publication,
        set_staged_path,
    )

    effect_run_id = run_id or request_id
    db = SessionLocal()
    plan = None
    # L'effet a-t-il été retiré parce que son image avait été supprimée ? Lisible
    # par les except handlers : un échec de REGÉNÉRATION doit dire cela, pas le
    # libellé générique qui ferait croire à un simple moteur coupé.
    regenerated = False
    effect_id = ""
    try:
        try:
            plan = prepare_chat_image(db, conv_id, parsed)
        except ValueError as exc:
            return _image_failure(
                f"[generate_image: {exc}]",
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
        fingerprint = image_request_fingerprint(
            plan.prompt,
            plan.aspect_ratio,
            plan.reference_ids,
            parsed.seed,
            extra=plan.fingerprint_extra,
        )
        logical_action_id = f"image:{fingerprint[:32]}"
        claim = claim_image_effect(
            db,
            run_id=effect_run_id,
            logical_action_id=logical_action_id,
            output_index=0,
            fingerprint=fingerprint,
            tool_call_id=tool_call.get("id"),
        )
        effect_id = claim.effect_id
        # Ce qu'un effet « published » peut signifier : TROIS états, jamais deux.
        if claim.status == "published" and claim.output_artifact_id:
            if is_artifact_replayable(db, claim.output_artifact_id, None):
                # Les pixels EXISTENT. Qu'ils soient déjà portés par CETTE
                # conversation ou par une autre ne change rien : on rattache
                # (no-op si déjà rattaché) et on rend, zéro diffusion. Un seul
                # chemin pour les deux cas : les distinguer n'aurait produit
                # deux branches strictement identiques.
                try:
                    return _replay_published_image(
                        db,
                        claim.output_artifact_id,
                        effect_run_id,
                        request_id,
                        conv_id,
                        start,
                        relink_into_conversation=True,
                    )
                except Exception as exc:
                    return _image_failure(
                        f"[REPLAY_UNUSABLE: {exc}]",
                        latency_ms=int((time.perf_counter() - start) * 1000),
                    )
            # Les pixels ont DISPARU (suppression depuis la galerie) : on ne
            # rattache pas du vide. L'effet devient un échec, l'empreinte se libère,
            # et la demande identique réclame un effet neuf — sinon l'utilisateur
            # resterait bloqué pour toujours sur ce prompt.
            retire_deleted_publication(
                db, claim.effect_id, _DELETED_ARTIFACT_EFFECT_ERROR
            )
            claim = claim_image_effect(
                db,
                run_id=effect_run_id,
                logical_action_id=logical_action_id,
                output_index=0,
                fingerprint=fingerprint,
                tool_call_id=tool_call.get("id"),
            )
            regenerated = True
            effect_id = claim.effect_id
        if claim.status != "claimed":
            return _image_failure(
                f"[EFFECT_{claim.status.upper()}]",
                _DELETED_ARTIFACT_USER_LIMIT if regenerated else "",
                latency_ms=int((time.perf_counter() - start) * 1000),
            )

        def generate(effective_seed: int | None, effect_id: str, before_commit):
            from backend.config.paths import get_data_dir

            staged_path = str(Path(get_data_dir()) / "staged" / f"{effect_id}.png")
            set_staged_path(db, effect_id, staged_path)
            # `reference_ids` voyage déjà en cinquième position ; le retirer du
            # dictionnaire évite un doublon mot-clé/positionnel.
            service_kwargs = dict(plan.generate_kwargs)
            service_kwargs.pop("reference_ids", None)
            return image_service.generate(
                manager,
                effect_run_id,
                plan.prompt,
                plan.aspect_ratio,
                plan.reference_ids,
                effective_seed,
                conv_id=conv_id,
                idempotency_key=effect_id,
                cancel_flag=cancel_flag,
                on_stage=on_stage,
                before_commit=before_commit,
                origin="chat-tool",
                staged_path=staged_path,
                **service_kwargs,
            )

        def on_publish(session, artifact_id: str) -> None:
            run = session.get(Run, effect_run_id)
            if run is not None:
                run.output_artifact_id = artifact_id

        _claim, pub = execute_claimed_image(db, claim, generate, on_publish)
        artifact_id = str(pub["artifact_id"])
        delivered = emit(
            "artifact_created",
            {
                "request_id": request_id,
                "run_id": effect_run_id,
                "artifact_id": artifact_id,
                "experimental": True,
            },
            run_id=effect_run_id,
            conversation_id=conv_id,
        )
        if delivered is not False:
            mark_event_published_by_key(
                db, artifact_event_key(effect_run_id, artifact_id)
            )
        ingestion = trigger_generated_ingestion(artifact_id, run_id=effect_run_id)
        stats = pub.get("stats", {})
        prefix = (
            f"[generate_image] {_IMAGE_RETIRED_AFTER_DELETE} → "
            if regenerated
            else "[generate_image] "
        )
        notes_suffix = (" ; " + " ".join(plan.notes)) if plan.notes else ""
        return {
            "ok": True,
            "image_published": True,
            "artifact_id": artifact_id,
            "result": f"{prefix}{artifact_id} ({stats.get('width')}x{stats.get('height')}, seed {stats.get('seed')}, {stats.get('duration_s')}s, backend {stats.get('backend')}, ingestion {ingestion}{notes_suffix})",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    except EffectConflict as exc:
        return _image_failure(
            f"[EFFECT_CONFLICT] {exc}",
            _DELETED_ARTIFACT_USER_LIMIT if regenerated else "",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
    except EffectInProgress as exc:
        return _image_failure(
            f"[EFFECT_IN_PROGRESS] {exc}",
            _DELETED_ARTIFACT_USER_LIMIT if regenerated else "",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
    except Exception as exc:
        # La tentative est terminée : son effet ne doit pas rester `uncertain`,
        # qui bloquerait à jamais la prochaine demande identique (voir
        # _retire_unpublished_effect).
        _retire_unpublished_effect(db, effect_id, f"{type(exc).__name__}: {exc}")
        return _image_failure(
            f"[generate_image: {exc}]",
            _DELETED_ARTIFACT_USER_LIMIT if regenerated else "",
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
    finally:
        if plan is not None:
            plan.cleanup()
        db.close()


# D'OÙ VIENT CE BLOC : au reload, la bulle était vide — ni brief, ni thinking, ni plan,
# ni toolcalls n'étaient persistés (table toolcalls jamais écrite). OÙ VA QUOI : la loop
# collecte (contenus itérations + dernier plan + brief) et écrit les ToolCall pendant le
# run (run_id, msg_id inconnu) ; main.py persiste la trace sur le message assistant et
# backfille msg_id AVANT le terminal (C08) ; Electron remonte le tout dans list_messages.
# POURQUOI JAMAIS BLOQUANT : audit/UI, pas chemin critique — try/except total partout.

TRACE_THINKING_CHARS = 4000
TRACE_RESULT_CHARS = 16000


def _ensure_chat_run(
    run_id: str,
    conv_id: str,
    runtime_id: str = "llama_cpp",
    runtime_fingerprint: str | None = None,
    request_fingerprint: str | None = None,
) -> None:
    from backend.db.database import SessionLocal
    from backend.db.models import Run

    fingerprint = runtime_fingerprint or runtime_id
    db = SessionLocal()
    try:
        row = db.query(Run).filter(Run.id == run_id).first()
        if row is None:
            db.add(
                Run(
                    id=run_id,
                    conv_id=conv_id,
                    request_id=run_id,
                    kind="chat",
                    status="running",
                    runtime_id=runtime_id,
                    runtime_fingerprint=fingerprint,
                    fingerprint=request_fingerprint,
                )
            )
        else:
            # Re-vérification au passage à l'exécution (orchestration.md §2.1,
            # tâche 5.2) : « Son passage à l'exécution vérifie à nouveau la
            # discussion, les fichiers, les permissions et la disponibilité
            # courants ». Ici la corbeille des discussions : un Run dont la
            # discussion a été mise en corbeille pendant qu'il était en file ne
            # s'exécute pas — le refus est nommé (code lisible), pas une panne
            # générique — une panne vague n'explique pas le refus.
            from backend.db.models import Conversation as _ConvQ

            conversation_q = db.query(_ConvQ).filter(_ConvQ.id == conv_id).first()
            if conversation_q is None and conv_id != "tmp":
                raise RuntimeError(
                    f"DISCUSSION_ABSENTE:conversation {conv_id} introuvable à "
                    "l'exécution — Run non exécuté"
                )
            if conversation_q is not None and conversation_q.deleted_at is not None:
                raise RuntimeError(
                    f"DISCUSSION_EN_CORBEILLE:conversation {conv_id} en corbeille "
                    "à l'exécution — Run non exécuté"
                )
            # Une ligne en file (`queued`) n'a PAS d'empreinte de runtime : le
            # run n'était pas encore en exécution à l'admission, et le runtime
            # n'était pas résolu. C'est l'exécution qui les apporte — les
            # empreintes attendues sont réellement comparées au même moment
            # (« REQUEST_FINGERPRINT_MISMATCH », plus bas). Une ligne qui n'est
            # PAS en file doit porter son snapshot hérité : lui en inventer un
            # masquerait une exécution dont l'identité n'a jamais été notée
            # (« missing_snapshot », verrouillage existant).
            if row.runtime_id is None and row.runtime_fingerprint is None:
                if row.status == "queued":
                    row.runtime_id = runtime_id
                    row.runtime_fingerprint = fingerprint
            if row.runtime_id is None or row.runtime_fingerprint is None:
                raise RuntimeError("RUNTIME_MISMATCH:missing_snapshot")
            if row.runtime_id != runtime_id:
                raise RuntimeError(f"RUNTIME_MISMATCH:{row.runtime_id}!={runtime_id}")
            if row.runtime_fingerprint != fingerprint:
                raise RuntimeError(
                    f"RUNTIME_MISMATCH:{row.runtime_fingerprint}!={fingerprint}"
                )
            if (
                request_fingerprint is not None
                and row.fingerprint != request_fingerprint
            ):
                raise RuntimeError("REQUEST_FINGERPRINT_MISMATCH")
            if row.conv_id and row.conv_id != conv_id:
                raise RuntimeError("REQUEST_CONVERSATION_MISMATCH")
            # La file d'attente est entrée à `queued` (tâche 5.2) : la
            # transition est ICI, cohérente avec `orchestration.md §2.1`
            # re-vérifiée au passage. Les états TERMINAUX ne retrangent rien :
            # un run terminé relançant reste volontairement rejeté plus haut
            # via REQUEST_FINGERPRINT_MISMATCH.
            if row.status == "queued":
                row.status = "running"
            row.kind = row.kind or "chat"
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _empty_trace() -> dict:
    return {
        "orchestrator_thinking": "",
        "brief": "",
        "brief_json": "",
        "plan": None,
        "image_published": False,
        "image_artifact_id": None,
    }


# ============================================================================
# Barrière d'ingestion — orchestration.md §2.1 (tâche 5.1)
# ============================================================================
#
# « Le message n'est pas traité par l'orchestrateur tant que l'ingestion requise
# de ces fichiers n'est pas achevée et qu'un état cohérent n'est pas publié » —
# et « la barrière concerne la demande et les ressources dont elle dépend ; elle
# ne suspend pas les autres discussions de l'application » (orchestration.md §2.1).
#
# L'ingestion est déjà lancée par la promotion (attachments.py `_declencher_
# ingestion` → `lancer_ingestion`, un fil par artefact). La barrière NE relance
# rien : elle lit `artifacts.ingest_state` (vocabulaire de `etat.py`, seul
# écrivain autorisé) et attend les états TERMINAUX de son propre périmètre. La
# suspension est locale à ce tour — un poll court, pas un verrou global.

# Les types du chemin de dépôt (attachments.py écrit `kind` parmi ceux-ci à la
# promotion, même vocabulaire que attachment_ingest.KIND_* ; les sources web
# conservées arrivent aussi en `text`, cf. web_research.md : « Les ressources
# conservées et leur état d'ingestion. »). Les images DÉPOSÉES comme les images
# CRÉÉES (kind="image") sont exclues : leur ingestion descend parfois sur un
# modèle lourd, et lancer un tel traitement en concurrence avec un tour actif
# est précisément ce que les fiches d'ingestion interdisent — une barrière qui
# attendrait leurs états créerait un attente sans fin possible.
_KINDS_DEPOT = frozenset({"pdf", "sheet", "text"})

# Intervalle entre deux lectures d'état : échantillonnage, pas verrouillage —
# la barrière dort entre deux lectures, elle ne retient ni le fil du tour ni
# le fil d'ingestion.
INTERVALLE_POLL_S = 0.1


def _noms_pieces_jointes(attachment_ids: list[str]) -> dict[str, str]:
    """Le nom affiché de chaque pièce jointe du tour, une fois pour toutes.

    La raison d'un refus doit porter le nom que l'utilisateur reconnaît
    (discussion.md §4.4), pas un id brut. Une pièce jointe disparue reste
    sans nom : aucun refus ne peut la nommer, et `_etats_pieces_jointes`
    la sortirait déjà du périmètre de l'attente.
    """
    noms: dict[str, str] = {}
    try:
        from backend.db.database import SessionLocal as _SLN
        from backend.db.models import Artifact as _ArtN

        db = _SLN()
        try:
            for identifiant in attachment_ids:
                art = db.query(_ArtN).filter(_ArtN.id == identifiant).first()
                if art is not None:
                    noms[identifiant] = (
                        art.display_name or art.original_filename or identifiant
                    )
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[barriere] noms pièces jointes indisponibles: {e}")
    return noms


def _etats_pieces_jointes(
    attachment_ids: list[str],
    request_id: str | None = None,
) -> list[tuple[str, str, str | None]]:
    """L'état de chaque pièce jointe du tour : `(id, kind, ingest_state)`.

    Une pièce jointe absente de la base reste hors liste : elle ne sera ni
    attendue ni réclamée par la barrière (le refus d'un fichier disparu est
    l'affaire de la re-vérification d'exécution, pas de l'attente).

    `request_id` optionnel : il est porté par l'événement `error` d'état
    indéterminé, pour rattacher l'incident à la demande qui a déclenché le tour.
    """
    etats: list[tuple[str, str, str | None]] = []
    try:
        from backend.db.database import SessionLocal as _SLE
        from backend.db.models import Artifact as _ArtE

        db = _SLE()
        try:
            for identifiant in attachment_ids:
                art = db.query(_ArtE).filter(_ArtE.id == identifiant).first()
                if art is None:
                    continue
                etats.append((identifiant, art.kind or "", art.ingest_state))
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[barriere] état ingestion indisponible: {e}")
        payload: dict[str, Any] = {
            "code": "ingestion_etat_indetermine",
            "message": f"État d'ingestion indéterminé : {e}",
            "retryable": False,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        emit("error", payload)
        raise RuntimeError(f"[INGESTION_ETAT_INCONNU] {e}")
    return etats


def barriere_ingestion(
    attachment_ids: list[str] | None,
    request_id: str,
    cancel_flag=None,
    delai_attente_s: float | None = None,
) -> None:
    """Le tour attend la publication d'un état cohérent de SES pièces jointes.

    orchestration.md §2.1 : « le message n'est pas traité par l'orchestrateur
    tant que l'ingestion requise de ces fichiers n'est pas achevée et qu'un état
    cohérent n'est pas publié » ; « Un incident, une capacité manquante ou une
    ingestion interrompue produit un état explicite d'échec ou de blocage ». Le
    refus est donc NOMMÉ (code + nom de fichier), jamais converti en réponse
    basée sur les premières pages.

    Le délai d'attente est PROVISOIRE (`DELAI_BARRIERE_INGESTION` dans
    `backend.config.defaults`, à l'arbitrage produit — §3.6 : « Les seuils sont
    configurables et à mesurer »).

    Le périmètre est strictement celui du tour : les `attachment_ids` passés
    par le contrôleur. Une ingestion due à une AUTRE discussion ne retient rien
    ici — la barrière ne suspend pas les autres discussions.
    """
    from backend.ingestion.etat import (
        ETATS_TERMINAUX as _ETATS_TERMINAUX,
        ETAT_ECHEC as _ETAT_ECHEC,
    )

    ids = [a for a in (attachment_ids or []) if a]
    if not ids:
        return

    from backend.config.defaults import DELAI_BARRIERE_INGESTION

    delai = float(
        delai_attente_s if delai_attente_s is not None else DELAI_BARRIERE_INGESTION
    )
    noms = _noms_pieces_jointes(ids)
    echeance = time.monotonic() + delai

    while True:
        if cancel_flag is not None and cancel_flag.is_set():
            return  # annulation pendant l'attente : plus rien à attendre
        en_cours_debut = _etats_pieces_jointes(ids, request_id)  # l'inventaire du tour
        if not en_cours_debut:
            return  # périmètre vide devant la base de test : rien à attendre

        # Un fichier en ÉCHEC nommé : le tour refuse d'entrée — une réponse
        # fondée sur les premières pages est exactement ce que la fiche interdit.
        echoues = [
            (i, k)
            for i, k, st in en_cours_debut
            if k in _KINDS_DEPOT and st == _ETAT_ECHEC
        ]
        if echoues:
            noms_fichiers = ", ".join(noms.get(i, i) for i, _ in echoues)
            raison = (
                f"L'ingestion du fichier {noms_fichiers} a échoué : le tour ne peut pas"
                " fonder une réponse sur un contenu partiel (orchestration.md §2.1;"
                " discussion.md §4.4). Retenter après vérification du format."
            )
            emit(
                "error",
                {
                    "request_id": request_id,
                    "code": "ingestion_en_echec",
                    "message": raison,
                    "retryable": False,
                },
            )
            raise RuntimeError(f"[INGESTION_EN_ECHEC] {raison}")

        # Un fichier dont l'ingestion n'est pas soldée (état non terminal)
        # retient le tour. Les images déposées comme les images créées
        # (kind="image") ne comptent pas (voir `_KINDS_DEPOT`).
        incident_retenu = [
            (i, k, st)
            for i, k, st in en_cours_debut
            if k in _KINDS_DEPOT and st not in _ETATS_TERMINAUX
        ]
        if not incident_retenu:
            return
        if time.monotonic() >= echeance:
            noms_fichiers = ", ".join(noms.get(i, i) for i, _, _ in incident_retenu)
            etats_fichiers = ", ".join(
                f"{noms.get(i, i)} (état « {st} »)" for i, _, st in incident_retenu
            )
            raison = (
                f"Ingestion non soldée après {delai:.0f}s d'attente : {etats_fichiers}."
                " Le tour est à l'arrêt — aucune réponse ne sera formulée sur un"
                " contenu partiel (orchestration.md §2.1 ; discussion.md §4.4)."
            )
            emit(
                "error",
                {
                    "request_id": request_id,
                    "code": "ingestion_bloquee",
                    "message": raison,
                    "retryable": True,
                },
            )
            raise RuntimeError(f"[INGESTION_BLOQUEE] {raison}")
        time.sleep(INTERVALLE_POLL_S)


def _plan_selected_model(tool: str) -> str:
    """Modèle RÉELLEMENT exécuté par le routage — jamais le nom d'un autre outil.

    Le plan est rechargé tel quel dans la bulle (bulle Orchestrateur) : annoncer
    PADDLEOCR-VL pour une diffusion FLUX.2 Klein y serait un faux visible.
    """
    if tool == "vision":
        return "LFM2.5-VL-3B"
    if tool == "doc_ocr":
        return "PADDLEOCR-VL-1.6"
    if tool == "generate_image":
        try:
            from backend.services.flux_spike import MODEL_NAME
        except Exception:
            return "FLUX.2 Klein 9B"  # libellé de repli : service image injoignable
        return MODEL_NAME
    return "inconnu"


def _emit_plan(payload: dict, trace: dict) -> None:
    """Émet execution_plan ET retient le dernier pour la persistance (reload bulle)."""
    try:
        trace["plan"] = {
            k: payload.get(k)
            for k in (
                "attachment_kind",
                "intent",
                "selected_tool",
                "selected_model",
                "reason",
            )
        }
    except Exception:
        pass
    try:
        emit("execution_plan", payload)
    except Exception as e:
        log(f"[plan] emit failed: {e}")


def _append_orch_thinking(trace: dict, text: str) -> None:
    """Accumule les contenus orchestrateur (pas les labels de statut). Borné."""
    try:
        t, _ = _sanitize_content(text or "")
        if not t:
            return
        cur = trace.get("orchestrator_thinking") or ""
        add = ("\n\n" + t[:2000]) if cur else t[:2000]
        trace["orchestrator_thinking"] = (cur + add)[:TRACE_THINKING_CHARS]
    except Exception:
        pass


def _persistable_tool_args(args: Any) -> dict[str, Any]:
    """Version PERSISTÉE des arguments d'un appel d'outil — la seule définition.

    POURQUOI SANITISER ICI : l'UI rend `ToolCall.args` verbatim (ToolCallDisplay
    lit `args.query` sans le retoucher), donc la ligne reloadée relit la DB.
    Stocker les arguments BRUTS faisait diverger les deux affichages du même
    tour : « [identifiant interne retiré] » pendant le run, art_… en clair après
    rechargement. Vivant et reload passent désormais par le même
    `sanitize_tool_arguments` que l'emit, donc ils ne peuvent plus diverger.

    CE QUE LE MASQUAGE NE TOUCHE PAS : le seul relecteur des arguments en base
    est le repli cache-média de build_attachment_registry, qui cherche un
    sha256 ou un chemin de fichier. Un sha256 est hexadécimal (ni « art_ » ni
    « sti_ » n'y sont exprimables : r, s et t ne sont pas des chiffres hexa, et
    l'underscore n'en est pas un) et un chemin persistant vaut
    `media/{sha256}.ext` (media_cache.persist_attachment). Aucun des deux ne peut
    être masqué, donc ce repli reste exact.
    """
    try:
        parsed = json.loads(json.dumps(args or {}, ensure_ascii=False, default=str))
    except Exception:
        parsed = {"raw": str(args)[:2000]}
    if not isinstance(parsed, Mapping):
        parsed = {"raw": str(parsed)[:2000]}
    return sanitize_tool_arguments(parsed)


def _begin_toolcall(
    run_id: str,
    tool_call_id: str,
    tool: str,
    args: dict,
    ordinal: int = 0,
    provenance: str = "native",
) -> None:
    try:
        import uuid as _uuid
        from backend.db.database import SessionLocal as _SLT
        from backend.db.models import ToolCall as _TC

        args_json = _persistable_tool_args(args)
        db = _SLT()
        try:
            row = (
                db.query(_TC)
                .filter(
                    _TC.run_id == run_id,
                    _TC.tool_call_id == tool_call_id,
                )
                .first()
            )
            if row is None:
                db.add(
                    _TC(
                        id=f"tc_{_uuid.uuid4().hex[:12]}",
                        msg_id=None,
                        tool_call_id=tool_call_id,
                        tool=tool,
                        args=args_json,
                        run_id=run_id,
                        status="running",
                        attempt=1,
                        ordinal=ordinal,
                        logical_action_id=LoopGuard.args_hash(tool, args_json),
                        provenance={"source": provenance},
                    )
                )
            else:
                row.tool = tool
                row.args = args_json
                row.status = "running"
                row.ordinal = ordinal
                row.attempt = max(1, int(row.attempt or 0))
                row.provenance = {"source": provenance}
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[toolcall] begin persist failed: {e}")
        raise


def _persist_toolcall(
    run_id: str, tool_call_id: str, tool: str, args: dict, result: dict
) -> None:
    try:
        import uuid as _uuid
        from backend.db.database import SessionLocal as _SLT
        from backend.db.models import ToolCall as _TC

        args_json = _persistable_tool_args(args)
        res_text = sanitize_provider_content(
            str((result or {}).get("result", "") or "")
        )
        db = _SLT()
        try:
            row = (
                db.query(_TC)
                .filter(
                    _TC.run_id == run_id,
                    _TC.tool_call_id == tool_call_id,
                )
                .first()
            )
            if row is None:
                row = _TC(
                    id=f"tc_{_uuid.uuid4().hex[:12]}",
                    msg_id=None,
                    tool_call_id=tool_call_id,
                    tool=tool,
                    args=args_json,
                    result=res_text[:TRACE_RESULT_CHARS],
                    ok=bool((result or {}).get("ok")),
                    latency_ms=int((result or {}).get("latency_ms", 0) or 0),
                    run_id=run_id,
                    status="completed" if (result or {}).get("ok") else "failed",
                    attempt=1,
                    logical_action_id=LoopGuard.args_hash(tool, args_json),
                )
                db.add(row)
            else:
                row.tool = tool
                row.args = args_json
                row.result = res_text[:TRACE_RESULT_CHARS]
                row.ok = bool((result or {}).get("ok"))
                row.latency_ms = int((result or {}).get("latency_ms", 0) or 0)
                row.status = "completed" if row.ok else "failed"
                row.logical_action_id = row.logical_action_id or LoopGuard.args_hash(
                    tool, args_json
                )
                row.attempt = max(1, int(row.attempt or 0))
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        log(f"[toolcall] persist failed: {e}")
        raise


def finalize_run(run_id: str, request_id: str) -> None:
    """Termine un run : ingestion différée puis nettoyage de la requête.

    Appelée UNIQUEMENT depuis le ``finally`` de ``run_agent_loop``, lui-même
    situé après l'émission de ``message_completed`` : l'ingestion différée
    démarre donc après le terminal du run. Cette fonction ne démontre pas cet
    ordre par elle-même ; elle garantit seulement que l'ingestion est lancée
    avant ``cleanup_request``.
    """
    try:
        from backend.services.chat_image_plan import flush_generated_ingestion

        flush_generated_ingestion(run_id)
    except Exception:
        pass
    cleanup_request(request_id)


def run_agent_loop(
    manager,
    conv_id: str,
    request_id: str,
    message: str,
    image_paths: list[str] | None,
    history_shared: list[dict],
    cancel_flag=None,
    attachment_ids: list[str] | None = None,
    runtime=None,
    request_fingerprint: str | None = None,
    delai_attente_s: float | None = None,
):
    """
    Boucle 2 couches Phase 1 : orchestrator → brief → générateur.
    - Orchestrateur peut appeler vision/doc_ocr (max 5 iter, séquentiel)
    - Générateur ne peut JAMAIS appeler d'outils
    - history_shared : list[{role, content}] user↔assistant compacté, sans tool traces
    """
    if not message or not message.strip():
        if not attachment_ids and not image_paths:
            emit(
                "error",
                {
                    "request_id": request_id,
                    "code": "empty_message",
                    "message": "Message vide",
                },
            )
            return "", _empty_trace()
        message = "Décris le contenu des fichiers joints."

    run_id = request_id
    if runtime is None:
        runtime = LlamaCppRuntime(manager)
    _ensure_chat_run(
        run_id,
        conv_id,
        runtime.runtime_id,
        runtime.runtime_fingerprint,
        request_fingerprint,
    )
    # Trace orchestrateur (bulle UI + persistance main.py AVANT terminal) : thinking
    # (contenus itérations, pas labels), dernier plan, brief. Jamais bloquante.
    trace: dict = {
        "orchestrator_thinking": "",
        "brief": "",
        "brief_json": "",
        "plan": None,
        "image_published": False,
        "image_artifact_id": None,
    }
    t0 = time.perf_counter()
    total_timeout_reported = False

    def report_total_timeout() -> None:
        nonlocal total_timeout_reported
        if total_timeout_reported:
            return
        total_timeout_reported = True
        emit(
            "error",
            {
                "request_id": request_id,
                "code": "total_timeout",
                "message": "300s dépassé",
            },
        )

    emit(
        "message_started",
        {"request_id": request_id, "run_id": run_id, "model": "LFM2.5-8B"},
    )

    # Barrière d'ingestion (orchestration.md §2.1, tâche 5.1) : le tour n'entre
    # en orchestration que lorsqu'un état cohérent est publié pour ses pièces
    # jointes. La suspension est locale — l'autre conversation n'attend rien.
    barriere_ingestion(
        attachment_ids,
        request_id,
        cancel_flag=cancel_flag,
        delai_attente_s=delai_attente_s,
    )

    raw_history = history_shared or []
    assert isinstance(raw_history, list), "history_shared doit être une liste"
    history = [
        {
            **item,
            "content": _sanitize_content(str(item.get("content") or ""))[0],
        }
        for item in raw_history
        if isinstance(item, dict)
    ]

    # Compaction non-destructive C06 : prune avant figeage SharedContext
    try:
        from backend.inference.compaction import prune_old_tool_results

        history = prune_old_tool_results(history)
    except Exception:
        pass

    # Registre d'images pour l'orchestrateur (Réponses.md n°8 / annexe 10) — artifact_ids durables cible
    if attachment_ids:
        # Construit registre depuis Artifact durables (annexe 10) ; lien de scope actif
        # + artefact vivant exigés pour tout doc_N affiché.
        attachment_registry = build_current_artifact_entries(conv_id, attachment_ids)
        # Historique : artefacts des tours précédents (follow-ups sans nouveaux fichiers)
        try:
            current_ids = {str(e.get("id")) for e in attachment_registry if e.get("id")}
            for h in build_history_artifact_entries(conv_id, exclude_ids=current_ids):
                h["ordinal"] = len(attachment_registry) + 1
                h["index"] = len(attachment_registry) + 1
                attachment_registry.append(h)
        except Exception as e:
            log(f"[registry] history merge failed: {e}")
        # Fallback image_paths si registry vide (compat)
        if not attachment_registry and image_paths:
            attachment_registry = build_attachment_registry(conv_id, image_paths)
    else:
        attachment_registry = build_attachment_registry(conv_id, image_paths)
        # Follow-up sans nouveaux fichiers : remonte les artefacts durables de la conv
        if not attachment_registry:
            try:
                for h in build_history_artifact_entries(conv_id):
                    h["ordinal"] = len(attachment_registry) + 1
                    h["index"] = len(attachment_registry) + 1
                    attachment_registry.append(h)
            except Exception as e:
                log(f"[registry] history merge failed: {e}")
    has_any_media = bool(
        attachment_registry
    )  # Gardé pour le plan/observabilité ; ne pilote PLUS les tool_defs (brouillon : les 4 outils sont toujours proposés).
    registry = get_default_registry()
    # Brouillon fait foi — 2 phases, jamais d'auto :
    # - Découverte : tools=[search_info, find_files, ask_files, generate_image], tool_choice auto (l'un des 4 ou rien).
    # - Brief : tools=[submit_brief], tool_choice nommé (phase forcée plus bas).
    # Les sous-outils restent au registre : `ask_files`, `find_files` et
    # `search_info` les appellent en interne. Ils ne sont pas proposés au LLM.
    _all_defs = registry.definitions()
    # Filtre ici, sur les définitions composées — jamais sur la constante.
    _noms_offerts = _discovery_tool_names()
    _disc_defs: list[dict] = []
    for definition in _all_defs:
        if not isinstance(definition, dict):
            continue
        function = definition.get("function")
        if isinstance(function, dict) and function.get("name") in _noms_offerts:
            _disc_defs.append(definition)
    tool_defs = list(_disc_defs)
    # Fiches garanties pour le registre (self-healing : Electron crée l'Artifact, Python assure la fiche)
    try:
        _ensure_cards_for_registry(conv_id, attachment_registry)
    except Exception as e:
        log(f"[cards] ensure failed: {e}")

    # SharedContext C03 : même base versionnée pour orchestrateur et générateur
    revision = len(history) + 1  # simple compteur, valide la compaction
    # Compaction automatique (q-r.md : déclenchée selon mécanisme existant + budget).
    # Le compacteur reçoit historique + briefs validés + refs fichiers (paquet distinct
    # de l'orchestrateur et du générateur). Les messages complets restent en DB ;
    # seul le contexte actif est remplacé par le checkpoint. Jamais bloquant.
    try:
        from backend.inference.compaction import (
            CONTEXT_OVERHEAD_TOKENS,
            maybe_auto_compact,
        )

        # `refus` est la seule voie par laquelle la compaction dit qu'elle n'a PAS
        # produit de checkpoint. Sans elle, un rejet répété ne se voyait que sur
        # stderr, et le tour se lisait comme tous les autres : rien n'expliquait au
        # journal pourquoi ce contexte ne se compacte pas.
        refus_compaction: list[str] = []
        try:
            fenetre = manager.context_window("LFM2.5-8B")
        except Exception:
            fenetre = None
        budget_historique = max(4096, (fenetre or 32768) - CONTEXT_OVERHEAD_TOKENS)
        history, _ckpt = maybe_auto_compact(
            manager,
            conv_id,
            history,
            revision,
            file_refs=attachment_registry,
            runtime=runtime,
            max_tokens=budget_historique,
            refus=refus_compaction,
        )
        if _ckpt:
            log(
                f"[compaction] checkpoint produit (fenêtre={fenetre or 32768}, "
                f"budget={budget_historique})"
            )
        for motif in refus_compaction:
            log(f"[compaction] {motif}")
        revision = len(history) + 1
    except Exception as e:
        log(f"[compaction] auto failed: {e}")
    shared_ctx = SharedContext(
        conversation_id=conv_id,
        revision=revision,
        history=history,
        registry=attachment_registry,
        message=message,
    )
    # Vérifie annulation avant même de commencer
    if _check_cancel(cancel_flag) and not trace.get("image_published"):
        emit(
            "run_cancelled",
            {"request_id": request_id, "run_id": run_id, "conversation_id": conv_id},
        )
        return "", trace
    # Execution plan basé sur registre (observabilité sémantique, pas lexicale)
    if attachment_registry:
        first = attachment_registry[0]
        _emit_plan(
            {
                "request_id": request_id,
                "attachment_kind": first.get("kind"),
                "intent": "media_available",
                "selected_tool": None,
                "selected_model": None,
                "reason": f"{len(attachment_registry)} fichier(s) disponible(s) dans registre, orchestrateur décide",
            },
            trace,
        )
    else:
        _emit_plan(
            {
                "request_id": request_id,
                "attachment_kind": None,
                "intent": "general",
                "selected_tool": None,
                "selected_model": None,
                "reason": "Aucun fichier",
            },
            trace,
        )

    tool_results: list[dict] = []
    brief: str = message  # fallback si pas de brief orchestré (texte UI/DB)
    brief_data: dict | None = (
        None  # brief NORMALISÉ (schéma enrichi) pour le bloc JSON générateur
    )

    # Validation : log image_paths avant run
    if image_paths:
        valid_cnt = sum(1 for p in image_paths if p and Path(p).exists())
        from backend.inference.ipc import log as _log

        _log(
            f"[loop] image_paths={image_paths} valid={valid_cnt}/{len(image_paths)} registry={len(attachment_registry)}"
        )
        if valid_cnt == 0:
            emit(
                "error",
                {
                    "request_id": request_id,
                    "code": "image_paths_invalid",
                    "message": "Aucun fichier joint valide trouvé",
                },
            )
    # Les preuves obtenues ce tour (obs_, doc_N:k, web_) restent rendues aux outils
    # et injectées dans les blocs <DONNEES_OUTILS_NON_FIABLES> ci-dessous : le brief
    # v1.2 ne porte plus de structure de preuves, la provenance vit dans les rendus.

    _web_structural_failure = False
    public_artifact_ids: set[str] = {
        str(entry.get("public_id"))
        for entry in (attachment_registry or [])
        if isinstance(entry, dict) and entry.get("public_id")
    }
    _phase0_evidence_blocks: list[str] = []
    # Cache déterministe : (tool, artifact_id) -> result. D'où vient le besoin : si la
    # Phase 0 a déjà observé art_X et que l'orchestrateur LLM redemande vision(art_X), on
    # rejoue le résultat au lieu de recharger le VL (2s + bascule VRAM). Pourquoi clé sans
    # la query : même image = mêmes pixels ; une nouvelle question sur la même image reste
    # servie par la 1re observation (limite connue, préférable au coût d'un 2e passage).
    deterministic_cache: dict[tuple[str, str], dict] = {}
    loop_guard = LoopGuard()

    # --- Outils lourds : un seul chemin de diffusion, une seule progression ---
    # D'OÙ VIENT CE BLOC : la Phase 0 (routage déterministe) et la découverte de
    # l'orchestrateur convergent vers la même diffusion ; deux implémentations
    # divergeraient sur la progression, sur le décompte de temps (un seul chemin
    # exclu du budget suffit à faire remplacer une réponse obtenue par « délai
    # imparti ») et sur la déduplication.
    heavy_tool_wall_s = 0.0

    def _emit_image_stage(
        stage: str, status: str, step: object = None, total: object = None
    ) -> None:
        """run_stage sur le vocabulaire du studio (C08) : le front n'invente rien.

        Le `seq` de l'enveloppe vient des MOTS-CLÉS run_id/conversation_id
        (ipc.emit), jamais du payload — d'où les deux, comme en main.py.
        """
        try:
            emit(
                "run_stage",
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "stage": stage,
                    "status": status,
                    "step": step,
                    "total": total,
                },
                run_id=run_id,
                conversation_id=conv_id,
            )
        except Exception as e:
            log(f"[image] run_stage emit failed: {e}")

    def _on_image_stage(info: dict) -> None:
        """Relais des STAGE: du worker (étape + step/total) et de la publication."""
        _emit_image_stage(
            str(info.get("id") or "production"),
            str(info.get("label") or "")[:200],
            info.get("step"),
            info.get("total"),
        )

    def _run_heavy_tool(tool: str, args: dict, call_id: str) -> dict:
        """Chemin UNIQUE des outils de HEAVY_TOOL_NAMES (aujourd'hui : generate_image)."""
        nonlocal heavy_tool_wall_s
        if tool not in HEAVY_TOOL_NAMES:
            return {"ok": False, "result": f"[UNKNOWN_TOOL] {tool}", "latency_ms": 0}
        # AVANT la diffusion : l'admission GPU (~30 s, coordinator.acquire) puis le
        # chargement du checkpoint (~19 Go, import torch) précèdent la première ligne
        # STAGE: du worker. Sans ces deux annonces, une génération légitime de
        # plusieurs minutes s'affiche comme un gel côté UI.
        _emit_image_stage("admission", "Attente du moteur image (GPU partagé)")
        _emit_image_stage("production", "started")
        started = time.perf_counter()
        try:
            return _execute_generate_image(
                {"args": args, "id": call_id},
                manager,
                conv_id=conv_id,
                request_id=request_id,
                run_id=run_id,
                cancel_flag=cancel_flag,
                on_stage=_on_image_stage,
            )
        finally:
            heavy_tool_wall_s += time.perf_counter() - started

    def _already_published_image() -> dict | None:
        """Une image déjà publiée CE tour-ci, quelle que soit sa source : la décision est acquise.

        Le registre d'effets ne peut pas dédupliquer ici : deux prompts différents
        donnent deux empreintes différentes, donc deux diffusions. Sans ce garde,
        « une image par tour » n'était qu'un espoir — et l'orchestrateur, seul
        décisionnaire, peut très bien rappeler l'outil au tour suivant de sa boucle.
        """
        artifact_id = str(trace.get("image_artifact_id") or "")
        if not (trace.get("image_published") and artifact_id):
            return None
        return {
            "ok": True,
            "image_published": True,
            "artifact_id": artifact_id,
            "deduplicated": True,
            "result": f"[generate_image] {artifact_id} (déjà publiée ce tour)",
            "latency_ms": 0,
        }

    # ========== Phase 0 : Routage déterministe média (sans LLM) ==========
    # Le routage média déterministe reste une preuve, pas une décision de tool call.
    # OÙ VONT LES RÉSULTATS : tool_results alimente l'orchestrateur Phase 1 par son
    # historique (append_canonical_tool_round) et le générateur par le brief JSON
    # seul (q-r.md : plus de fuite des retours d'outils en clair). Aucune autre
    # collection intermédiaire n'est lue : un inventaire « tool_observations » était
    # écrit ici à chaque appel et ne nourrissait personne.
    # POURQUOI AVANT L'ORCHESTRATEUR : l'observation visuelle est un fait (pixels), pas une
    # décision ; la confier au maillon le moins fiable (8B) causait les "pas accès".
    # MÊME DOCTRINE POUR LA CRÉATION D'IMAGE : AUCUNE. generate_image n'appartient
    # pas à ce plan (voir plan_media_tools) — l'orchestrateur est le seul point de
    # décision, et un mot-clé ne lancera jamais un job GPU ici. Tout ce qui suit est
    # doncobservation d'artefact : vision / doc_ocr, exécutés par _execute_vision /
    # _execute_doc_ocr. L'indisponibilité du moteur n'est PAS testée ici (le plan reste
    # pur et testable) : elle est traitée par l'exécuteur et rapportée au brief comme
    # une limite. Le LLM orchestrateur garde la main pour le brief + get_source/read_text.
    # RÈGLE MÉMOIRE : un seul modèle en VRAM à la fois (manager.use_model) → les exécutions
    # sont strictement séquentielles, max MAX_ITER, avec allers 8B<->VL assumés.
    try:
        _current_entries = [
            e
            for e in attachment_registry
            if e.get("first_seen_in_message") == "current"
        ]
        _history_entries = [
            e
            for e in attachment_registry
            if e.get("first_seen_in_message") != "current"
        ]
        _plan = plan_media_tools(message, _current_entries)
        if not _plan:
            try:
                from backend.inference.media_policy import (
                    decide_reinspection as _decide,
                )

                _dec = _decide(
                    message,
                    _current_entries,
                    _history_entries or attachment_registry,
                    asks_for_text_extraction,
                )
            except Exception as e:
                log(f"[deterministic] decide failed: {e}")
                _dec = None
            if _dec is not None:
                _plan = [_dec]
        for _item in _plan[:MAX_ITER]:
            if _check_cancel(cancel_flag):
                break
            _tool = _item["tool"]
            _aid = str(_item.get("artifact_id") or "")
            if not _aid:
                # vision/doc_ocr observent un artefact : sans id, il n'y a rien à regarder.
                log(f"[deterministic] {_tool} sans artifact_id — entrée ignorée")
                continue
            _exec_args = {"artifact_id": _aid, "query": message[:500]}
            _tcall_id = f"det_{_tool}_{_aid[:12]}"
            emit(
                "tool_call",
                {
                    "request_id": request_id,
                    "tool_call_id": _tcall_id,
                    "tool": _tool,
                    "arguments": sanitize_tool_arguments(_exec_args),
                },
            )
            _res: dict | None = None
            for _attempt in range(MAX_RETRY + 1):
                if _tool == "vision":
                    _res = _execute_vision(
                        manager,
                        {"args": _exec_args},
                        image_paths,
                        request_id,
                        attachment_ids,
                    )
                else:
                    _res = _execute_doc_ocr(manager, {"args": _exec_args}, request_id)
                if _res is not None and (_res.get("ok") or _attempt >= MAX_RETRY):
                    break
                time.sleep(0.5)
            if _res is None:
                _res = {
                    "ok": False,
                    "result": f"[Routage déterministe: {_tool} sans résultat]",
                    "latency_ms": 0,
                }
            _safe_result = sanitize_provider_content(str(_res.get("result", "")))
            emit(
                "tool_result",
                {
                    "request_id": request_id,
                    "tool_call_id": _tcall_id,
                    "result": _safe_result[:5000],
                    "ok": _res.get("ok", False),
                    "latency_ms": _res.get("latency_ms", 0),
                },
            )
            tool_results.append(
                {
                    "tool": _tool,
                    "result": _safe_result,
                    "ok": _res.get("ok", False),
                    "tool_call_id": _tcall_id,
                    **(
                        {"artifact_id": _res.get("artifact_id")}
                        if _res.get("artifact_id")
                        else {}
                    ),
                }
            )
            # Bulle Orchestrateur : ligne ToolCall persistée pendant le run (backfill msg_id par main.py)
            _persist_toolcall(run_id, _tcall_id, _tool, _exec_args, _res)
            # Annexe 11 Lot A : Observation persistée + fiche N+1 (revient au tour suivant).
            _obs_id = _record_observation(
                _tool,
                _aid,
                sanitize_provider_content(message[:500]),
                _res,
                run_id,
                _tcall_id,
                model_id="LFM2.5-VL-3B" if _tool == "vision" else "PADDLEOCR-VL-1.6",
            )
            if _obs_id and _res.get("ok"):
                _phase0_evidence_blocks.append(
                    f"[Preuve obtenue ce tour : {_obs_id} — {_tool} sur {_item.get('name') or _aid}]\n"
                    f"{sanitize_provider_content(str(_res.get('result', '')))[:2000]}"
                )
            if _res.get("ok"):
                deterministic_cache[(_tool, _aid)] = _res
        if _plan:
            _first_tool = _plan[0]["tool"]
            _names = ", ".join(
                f"{p['tool']}({str(p.get('artifact_id') or '')[:16]})" for p in _plan
            )
            _emit_plan(
                {
                    "request_id": request_id,
                    "attachment_kind": (
                        attachment_registry[0].get("kind")
                        if attachment_registry
                        else None
                    ),
                    "intent": "media_available",
                    "selected_tool": _first_tool,
                    "selected_model": _plan_selected_model(_first_tool),
                    "reason": f"Routage déterministe : {_names}",
                },
                trace,
            )
    except Exception as e:
        log(f"[deterministic] pre-execution failed: {e}")

    try:
        # ========== Phase 1 : Orchestrateur (décision sémantique, pas mots-clés) ==========
        user_content = _neutralize_control_tokens(sanitize_provider_content(message))
        public_attachment_ids = [
            str(entry.get("public_id"))
            for entry in attachment_registry
            if entry.get("public_id")
        ]
        if public_attachment_ids:
            user_content += f"\n\n[Fichiers joints — utilise uniquement ces identifiants publics doc_N: {', '.join(public_attachment_ids)}]"
        elif attachment_ids or image_paths:
            user_content += "\n\n[Fichiers joints — retrieve leurs identifiants doc_N avec find_files]"
        # Le dépôt de l'espace n'est pas le registre : ces fichiers ne sont pas joints
        # au message, et la ligne le dit — sinon le modèle les confondrait avec les
        # « SOURCES DISPONIBLES DANS CETTE CONVERSATION » ci-dessus.
        _ligne_depot = _depot_announcement(conv_id)
        if _ligne_depot:
            user_content += f"\n\n{_ligne_depot}"
        # Synthèse courte de l'espace pour l'orchestrateur (orchestration.md §2.1) :
        # contexte documentaire, jamais une instruction — le tour courant prime, et
        # l'orchestrateur sélectionne les éléments utiles dans son brief.
        _ligne_synthese = _synthese_espace(conv_id)
        if _ligne_synthese:
            # Le cadrage voyage AVEC la synthèse : sans lui, « aucune orientation
            # d'espace » peut se lire « aucune direction artistique possible ».
            user_content += (
                f"\n\n{_ligne_synthese}"
                "\n[Contexte documentaire d'espace — il ne conditionne ni une création d'image ni une réponse générale ; le tour courant prime.]"
            )

        registry_block = _neutralize_control_tokens(
            sanitize_provider_content(format_attachment_registry(attachment_registry))
        )
        messages_orch: list[dict] = [
            {"role": "system", "content": SYSTEM_ORCHESTRATOR},
            {
                "role": "user",
                "content": f"<REGISTRE_NON_FIABLE>\n{registry_block}\n</REGISTRE_NON_FIABLE>",
            },
        ] + history
        # q-r.md : la Phase 0 produisait une vision réussie mais invisible du LLM (jamais injectée) —
        # l'orchestrateur citait art_xxx → EVIDENCE_UNKNOWN → brief vide → "Je ne vois pas d'image".
        # On injecte ici chaque preuve Phase 0 avec son obs_id citable dans evidence_ids.
        for _blk in _phase0_evidence_blocks:
            messages_orch.append(
                {
                    "role": "user",
                    "content": f"<DONNEES_OUTILS_NON_FIABLES>\n{_blk}\n</DONNEES_OUTILS_NON_FIABLES>",
                }
            )
        if _phase0_evidence_blocks:
            messages_orch.append(
                {
                    "role": "system",
                    "content": (
                        "Les analyses ci-dessus sont déjà faites : ne les redemande pas. "
                        "Reformule leurs contenus dans le brief (rubriques utiles : éléments préparés, "
                        "provenance, limites), puis termine par submit_brief."
                    ),
                }
            )
        messages_orch.append({"role": "user", "content": user_content})

        orchestrator_done = False
        # q-r.md "Coucou" : bavardage sans fichier → zéro outil, direct au brief forcé.
        # Évite les 2/2 outils + la trace robotique ("intention general") pour une politesse.
        _is_chitchat_turn = (
            is_chitchat(message, attachment_registry) and not _phase0_evidence_blocks
        )

        # Portée du tour : les URL rendues lisibles par un web_research le restent
        # pour les appels suivants du même tour. Une URL jamais vue reste inadmissible.
        web_admitted: set[str] = set()
        # Le plafond de politesse par hôte vaut pour UN tour utilisateur. Sans ce
        # réarmement, la file resterait bloquée pour tout le processus après les
        # deux premières pages d'un même site — la politesse deviendrait une panne
        # au troisième échange de la conversation.
        _web_net.reset_host_gate()

        def execute_discovery(tool_name: str, args: dict, call_id: str) -> dict:
            result: dict | None = None
            for attempt in range(MAX_RETRY + 1):
                if tool_name == "search_info":
                    result = _execute_search_info({"args": args}, conv_id=conv_id)
                elif tool_name == "find_files":
                    result = _execute_find_files({"args": args}, conv_id=conv_id)
                elif tool_name == "ask_files":
                    result = _execute_ask_files(
                        {"args": args},
                        manager,
                        conv_id=conv_id,
                        image_paths=image_paths,
                        request_id=request_id,
                        attachment_ids=attachment_ids,
                    )
                elif tool_name == "web_research":
                    result = _execute_web_research(
                        {"args": args},
                        conv_id=conv_id,
                        user_message=message,
                        admis=web_admitted,
                    )
                elif tool_name == "generate_image":
                    # UNE seule porte d'entrée : l'appel natif (ou sa récupération
                    # textuelle) émis par l'orchestrateur ci-dessus. La Phase 0 ne
                    # prépare plus aucun job image. Une image par tour : si l'orchestrateur
                    # en a déjà publié une, la décision est acquise (_already_published_image).
                    result = _already_published_image() or _run_heavy_tool(
                        tool_name, args, call_id
                    )
                else:
                    result = {
                        "ok": False,
                        "result": f"[UNKNOWN_TOOL] {tool_name}",
                        "latency_ms": 0,
                    }
                if result.get("ok") or attempt >= MAX_RETRY:
                    break
                if tool_name in RETRY_EXEMPT_TOOLS:
                    log(
                        f"[retry] {tool_name} non retrié (RETRY_EXEMPT_TOOLS) : "
                        f"{sanitize_provider_content(str(result.get('result', '')))[:200]}"
                    )
                    break
                if "timeout" in str(result.get("result", "")).lower():
                    time.sleep(1)
                else:
                    break
            return result or {
                "ok": False,
                "result": "[INTERNAL_ERROR] aucun résultat",
                "latency_ms": 0,
            }

        tool_executor = ToolExecutor(
            registry,
            lambda name, arguments, call: execute_discovery(name, arguments, call.id),
            DISCOVERY_TOOL_NAMES,
        )

        def process_tool_round(records: tuple, assistant_content: str) -> bool:
            nonlocal public_artifact_ids, _web_structural_failure
            # La découverte ne possède pas le canal de handoff.  Filtrer avant
            # toute persistance empêche un submit_brief natif anticipé.
            received_records = records
            records = _discovery_records(received_records)
            if not records:
                return bool(received_records)
            round_results: dict[str, str] = {}
            for record in records:
                tool_name = record.name
                call_id = record.id
                args = dict(record.arguments)
                _begin_toolcall(
                    run_id,
                    call_id,
                    tool_name,
                    args,
                    record.ordinal,
                    record.provenance,
                )
                _blocked_retry_this = False
                _blocked_now_this = False
                if len(tool_results) >= MAX_ITER:
                    result = {
                        "ok": False,
                        "result": "[BUDGET_EXHAUSTED]",
                        "latency_ms": 0,
                    }
                elif (
                    tool_name == "generate_image"
                    and _already_published_image() is not None
                ):
                    from typing import cast

                    result = cast(dict, _already_published_image())
                elif tool_name in getattr(loop_guard, "blocked_tools", set()):
                    _blocked_retry_this = True
                    loop_guard.mark_blocked_retry_attempt(tool_name)
                    result = {
                        "ok": False,
                        "result": "[TOOL_BLOCKED] trop d'erreurs d'appel. Outil bloqué pour ce tour.",
                        "latency_ms": 0,
                    }
                elif loop_guard.tool_call_count(
                    tool_name
                ) >= MAX_TOOL_CALLS_BY_TOOL.get(tool_name, 999999):
                    _blocked_now_this = True
                    loop_guard.block_tool(tool_name, reason="capacity")
                    result = {
                        "ok": False,
                        "result": "[TOOL_BLOCKED] trop d'erreurs d'appel. Outil bloqué pour ce tour.",
                        "latency_ms": 0,
                    }
                elif len(loop_guard.calls) >= MAX_TOOL_CALLS:
                    result = {
                        "ok": False,
                        "result": "[TOOL_BLOCKED] trop d'erreurs d'appel. Outil bloqué pour ce tour.",
                        "latency_ms": 0,
                    }
                else:
                    signature = LoopGuard.args_hash(tool_name, args)
                    duplicate = any(
                        entry.get("tool") == tool_name
                        and entry.get("args_hash") == signature
                        for entry in loop_guard.calls[-8:]
                    )
                    if duplicate:
                        result = {
                            "ok": False,
                            "result": "[DUPLICATE_CALL]",
                            "latency_ms": 0,
                        }
                    else:
                        execution = tool_executor.execute_call(record)
                        args = execution.arguments
                        result = {
                            "ok": execution.ok,
                            "result": execution.result,
                            "latency_ms": execution.latency_ms,
                            **execution.metadata,
                        }
                for public_id in result.get("public_ids", []) or []:
                    if isinstance(public_id, str) and re.fullmatch(
                        r"doc_[1-9]\d*", public_id
                    ):
                        public_artifact_ids.add(public_id)
                emit(
                    "tool_call",
                    {
                        "request_id": request_id,
                        "tool_call_id": call_id,
                        "tool": tool_name,
                        "arguments": sanitize_tool_arguments(args),
                    },
                )
                _safe_result = sanitize_provider_content(str(result.get("result", "")))
                _evidence = [
                    e for e in (result.get("evidence_ids") or []) if isinstance(e, str)
                ]
                _public = [
                    p for p in (result.get("public_ids") or []) if isinstance(p, str)
                ]
                emit(
                    "tool_result",
                    {
                        "request_id": request_id,
                        "tool_call_id": call_id,
                        "result": _safe_result[:5000],
                        "ok": bool(result.get("ok")),
                        "latency_ms": int(result.get("latency_ms", 0) or 0),
                        **({"evidence_ids": _evidence} if _evidence else {}),
                        **({"public_ids": _public} if _public else {}),
                    },
                )
                # Distinction pour le brief : les résultats `web_research` indiquent
                # toujours un compteur de pages lues et des evidence_id citables.
                # Sans ce rappel, l'orchestrateur confond "source insuffisante" et
                # "aucune page lue", puis le générateur répète cette inversion.
                if tool_name == "web_research":
                    _web_counters = (
                        result.get("web_counters")
                        if isinstance(result.get("web_counters"), dict)
                        else {}
                    )
                    _pages_lues = int((_web_counters or {}).get("pages_read", 0) or 0)
                    if _pages_lues > 0:
                        _rappel_web = (
                            f"Intégration `web_research` : {_pages_lues} page(s) ont bien été lues dans ce tour. "
                            "Dans le brief, recense au moins un fait observé par page utile avec exactement "
                            "un des evidence_id suivants : "
                            + (
                                ", ".join(_evidence[:8])
                                if _evidence
                                else "(aucun evidence_id retourné) "
                            )
                            + ". Si la profondeur est insuffisante, cela s'écrit comme une limitation factuelle"
                            " (source encyclopédique, article général, pas de données détaillées), "
                            "jamais comme « aucune page lue » ni comme « aucune source trouvée »."
                        )
                        messages_orch.append({"role": "system", "content": _rappel_web})
                    elif _safe_result.startswith(
                        "[TOO_MANY]"
                    ) or _safe_result.startswith("[MISSING_REQUIRED]"):
                        _web_structural_failure = True
                        loop_guard.force_brief("web_research_invalid_action")
                # Le plafond de tour est appliqué DANS `ToolExecutor` (c'est le
                # seul endroit qui voit tous les appels du tour) et son rendu
                # entre déjà dans `execution.result`. Le recopier ici en [:16000]
                # remettait la main au dernier étage et annulait le plafond
                # dès qu'un résultat passait par une troncature de tour.
                round_results[call_id] = _safe_result
                tool_results.append(
                    {
                        "tool": tool_name,
                        "result": _safe_result,
                        "ok": bool(result.get("ok")),
                        "tool_call_id": call_id,
                        **(
                            {"artifact_id": result.get("artifact_id")}
                            if result.get("artifact_id")
                            else {}
                        ),
                        **(
                            {"user_limit": _image_failure_limit(result)}
                            if tool_name == "generate_image"
                            else {}
                        ),
                    }
                )
                _persist_toolcall(
                    run_id, call_id, tool_name, args, {**result, "result": _safe_result}
                )
                loop_guard.record(
                    tool_name,
                    args,
                    ok=bool(result.get("ok")),
                    duration_ms=int(result.get("latency_ms", 0) or 0),
                    blocked_retry=_blocked_retry_this,
                    block_tool_now=_blocked_now_this,
                )
                warning = loop_guard.warn_message()
                if warning:
                    messages_orch.append({"role": "system", "content": warning})
                if tool_name == "generate_image":
                    if result.get("ok") and result.get("artifact_id"):
                        trace["image_published"] = True
                        trace["image_artifact_id"] = result.get("artifact_id")
                if result.get("ok") and tool_name in OUTILS_SIGNAL_USAGE:
                    # Signal d'importance : un outil de recherche qui réussit a
                    # sollicité une ressource — c'est un usage réel, pas une intention.
                    _signal_importance(
                        tool_name=tool_name,
                        args=args,
                        result=result,
                        conv_id=conv_id,
                        response_id=f"{call_id}",
                    )
                if result.get("ok") and tool_name not in {
                    "generate_image",
                    "submit_brief",
                }:
                    try:
                        artifact_id = (
                            args.get("artifact_id")
                            or args.get("artifactId")
                            or args.get("source_id")
                            or ""
                        )
                        # Observation persistée + fiche N+1 : la provenance revient au
                        # tour suivant par l'index et les rendus, pas par un inventaire.
                        _record_observation(
                            tool_name,
                            artifact_id,
                            sanitize_provider_content(
                                str(
                                    args.get("query")
                                    or args.get("purpose")
                                    or message[:500]
                                )
                            ),
                            {**result, "result": _safe_result},
                            run_id,
                            call_id,
                        )
                    except Exception as exc:
                        log(f"[observation] phase1 record failed: {exc}")
            append_canonical_tool_round(
                messages_orch, assistant_content, records, round_results
            )
            return False

        for iteration in range(MAX_ITER):
            if _is_chitchat_turn:
                break  # aucun appel de découverte → phase brief forcée (connaissances propres)
            if _check_cancel(cancel_flag) and not trace.get("image_published"):
                emit(
                    "run_cancelled",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "conversation_id": conv_id,
                    },
                )
                break
            if _text_budget_spent(t0, heavy_tool_wall_s):
                report_total_timeout()
                break

            emit(
                "thinking",
                {
                    "request_id": request_id,
                    "text": f"[Orchestrateur] Analyse de la demande... (itération {iteration + 1})"
                    if iteration == 0
                    else f"[Orchestrateur] Analyse des résultats d'outils... (itération {iteration + 1})",
                    "iteration": iteration,
                },
            )

            try:
                resp = runtime.complete_chat(
                    RuntimeRequest(
                        messages=tuple(messages_orch),
                        tools=tuple(tool_defs),
                        tool_choice="auto" if tool_defs else None,
                        temperature=ORCHESTRATOR_CONFIG.temperature,
                        max_tokens=ORCHESTRATOR_CONFIG.max_tokens,
                        timeout_s=_remaining_run_timeout(t0, heavy_tool_wall_s),
                    )
                ).raw
            except ResourceBusy:
                # Contention de ressource : erreur transitoire retryable.
                # On la laisse sortir de la boucle pour que l'appelant la
                # distingue d'un échec LLM définitif.
                raise
            except RuntimeProtocolError as e:
                if "CONTEXT_WINDOW_TOO_SMALL" in str(e):
                    emit(
                        "error",
                        {
                            "request_id": request_id,
                            "code": "context_window_too_small",
                            "message": (
                                "Le contexte de cette discussion dépasse la fenêtre du modèle. "
                                "Réduisez les pièces jointes ou démarrez une nouvelle discussion."
                            ),
                            "detail": str(e)[:500],
                        },
                    )
                    log(f"[orchestrator] context window too small: {e}")
                    return "", trace
                else:
                    emit(
                        "error",
                        {
                            "request_id": request_id,
                            "code": "llm_error",
                            "message": str(e)[:500],
                        },
                    )
                    break
            except Exception as e:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "llm_error",
                        "message": str(e)[:500],
                    },
                )
                break

            choice = resp["choices"][0]
            msg = choice.get("message", {})
            content = msg.get("content") or ""
            # "auto" peut remonter des tool_calls natifs ; filet texte [fn(k='v')] en secours.
            # Jamais exécuté comme texte, jamais diffusé tel quel.
            _decision_calls(msg)
            raw_content = str(content)

            try:
                records = normalize_native_tool_calls(
                    msg.get("tool_calls"),
                    run_id=run_id,
                    round_index=iteration,
                )
                if not records and _can_parse_lfm_fallback(runtime):
                    records = normalize_lfm25_content(
                        raw_content,
                        run_id=run_id,
                        round_index=iteration,
                        provenance="lfm25_wrapper",
                        allowed_names=DISCOVERY_TOOL_NAMES,
                    )
                if not records and _can_parse_lfm_fallback(runtime):
                    hinted_tool = textual_tool_intent(raw_content)
                    if hinted_tool:
                        log(
                            f"[toolcall] pseudo-appel textuel détecté ({hinted_tool}) → re-ask natif forcé"
                        )
                        records = _recover_named_tool_call(
                            runtime,
                            messages_orch,
                            tool_defs,
                            hinted_tool,
                            run_id=run_id,
                            round_index=iteration,
                            timeout_s=_remaining_run_timeout(t0, heavy_tool_wall_s),
                        )
                        if records and not _arguments_exploitable(records[0].arguments):
                            # Cas mesuré le 28/09, et le plus fréquent : le re-ask
                            # RAMÈNE bien un appel natif — le 8B sait nommer
                            # l'outil quand on le force — mais ses arguments sont
                            # vides, ou portent des valeurs dans la mauvaise clé
                            # (`type=['purpose', 'Définition…']`). Un nom juste
                            # avec des arguments faux n'est pas un appel, c'est
                            # une moitié de réponse : on va chercher l'autre
                            # moitié, que le modèle a déjà écrite en prose.
                            _reprise = textual_call_args(raw_content, hinted_tool)
                            if _reprise and _arguments_exploitable(_reprise):
                                log(
                                    f"[toolcall] arguments natifs inexploitables — repris "
                                    f"du pseudo-appel ({hinted_tool})"
                                )
                                records = _records_from_textual_args(
                                    hinted_tool,
                                    _reprise,
                                    run_id=run_id,
                                    round_index=iteration,
                                )
                        if not records:
                            # Filet de dernier recours, et un motif mesuré : le 8B
                            # écrit un pseudo-appel CORRECT, et le re-ask natif
                            # lui fait rendre une réponse VIDE. Les arguments
                            # étaient là ; les jeter une seconde fois n'est pas
                            # de la sévérité, c'est une perte sèche. On ne
                            # récupère que des LITTÉRAUX (voir
                            # `textual_call_args`), et le sous-programme de
                            # l'outil revérifie tout de suite ses propres bornes.
                            arguments = textual_call_args(raw_content, hinted_tool)
                            if arguments:
                                log(
                                    f"[toolcall] re-ask natif vide — arguments récupérés "
                                    f"du pseudo-appel ({hinted_tool})"
                                )
                                records = _records_from_textual_args(
                                    hinted_tool,
                                    arguments,
                                    run_id=run_id,
                                    round_index=iteration,
                                )
                    elif carries_nameless_envelope(raw_content, tool_defs):
                        # Enveloppe d'arguments SANS nom : aucun nom n'est
                        # lisible, donc le canal nommé n'a rien à demander. Le
                        # backend ne choisit pas l'outil à sa place — il redemande
                        # au modèle, qui nomme l'outil et écrit les arguments.
                        log(
                            "[toolcall] enveloppe d'arguments sans nom → re-ask natif non contraint"
                        )
                        records = _reask_unconstrained_tool_call(
                            runtime,
                            messages_orch,
                            tool_defs,
                            run_id=run_id,
                            round_index=iteration,
                            timeout_s=_remaining_run_timeout(t0, heavy_tool_wall_s),
                            allow_wrapper=_can_parse_lfm_fallback(runtime),
                        )
            except ToolCallParseError as exc:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "invalid_tool_call",
                        "message": str(exc)[:500],
                    },
                )
                break

            clean_content, extracted_thinking = _sanitize_content(raw_content)
            if extracted_thinking:
                log(
                    f"[sanitizer] thinking stripped ({len(extracted_thinking)} chars) — non diffusé (C05)"
                )
            content = clean_content
            _append_orch_thinking(trace, content)

            if not records:
                break
            if process_tool_round(records, content):
                break
            if loop_guard.should_force_brief():
                break
            if len(tool_results) >= MAX_ITER:
                break

        if not orchestrator_done:
            # Phase brief forcée : submit_brief seul, appel NOMMÉ (prouvé sur notre stack :
            # l'auto fuit en texte, le forcé remonte des tool_calls). Retry correctif ×2,
            # puis repli minimal backend. Le texte libre n'est JAMAIS un brief.
            if _web_structural_failure:
                messages_orch.append(
                    {
                        "role": "system",
                        "content": (
                            "Les appels web_research ont échoué pour une erreur de commande "
                            "(5 requêtes envoyées au lieu de 3). Aucune lecture Internet n'a eu lieu. "
                            "LE brief doit le dire tel quel, sans laisser croire qu'une recherche a été menée."
                        ),
                    }
                )
            if _text_budget_spent(t0, heavy_tool_wall_s):
                report_total_timeout()
                _forced = None
            else:
                _forced = _forced_brief_phase(
                    manager,
                    messages_orch,
                    request_id,
                    conv_id,
                    runtime=runtime,
                    timeout_s=_remaining_run_timeout(t0, heavy_tool_wall_s),
                    user_message=message,
                    tool_results=tool_results,
                )
            if _forced is not None:
                brief, brief_data = _forced
                orchestrator_done = True
        if not orchestrator_done:
            if _ok_result_texts(tool_results):
                brief_data = _minimal_brief_data(tool_results)
                brief = render_brief(brief_data)
            elif not brief.strip():
                brief = message

        # Un seul point de réconciliation, après la dernière tool call : un
        # generate_image situé après submit_brief (ou dans un round ultérieur)
        # doit quand même faire partie du brief reçu par le générateur.
        image_status = _aggregate_image_status(tool_results)
        if brief_data is None and image_status is not None:
            brief_data = _minimal_brief_data(tool_results)
        brief_data = _with_image_status(brief_data, image_status)
        # La bulle Orchestrateur est VISIBLE : elle ne promet pas non plus
        # d'image que le backend n'a pas publiée (les événements « thinking »
        # déjà émis en direct ne sont pas réémis, la trace rechargée, elle, est
        # filtrée).
        trace["orchestrator_thinking"] = _with_image_truth_guards(
            str(trace.get("orchestrator_thinking") or ""), image_status
        )
        if brief_data is not None:
            brief = render_brief(brief_data)
        else:
            brief, _ = _sanitize_content(brief)

        # ========== Phase 2 : Générateur (JAMAIS de tools) ==========
        # Handoff visible UI (bulle Orchestrateur) : le brief EXACT transmis au générateur.
        # D'où vient le besoin : la communication orchestrateur→générateur était invisible
        # (thinking + toolcalls épars) ; le brief est le seul contrat entre les deux couches.
        # Le champ thinking joint les contenus itérations (reload identique au live).
        trace["brief"] = brief
        try:
            emit(
                "orchestrator_brief",
                {
                    "request_id": request_id,
                    "brief": brief[:6000],
                    "brief_chars": len(brief),
                    "tool_calls": len(tool_results),
                    "orchestrator_done": bool(orchestrator_done),
                    "thinking": (trace.get("orchestrator_thinking") or "")[:6000],
                },
            )
        except Exception as e:
            log(f"[brief] emit failed: {e}")
        emit(
            "thinking",
            {
                "request_id": request_id,
                "text": "[Générateur] Synthèse de la réponse finale...",
                "iteration": 99,
            },
        )

        _brief_json = (
            render_brief_json(brief_data)
            if brief_data
            else render_brief_json({"brief": ""})
        )
        trace["brief_json"] = _brief_json
        gen_messages = _build_generator_messages(history, _brief_json, message)

        if _text_budget_spent(t0, heavy_tool_wall_s):
            report_total_timeout()
            collected = (
                "La réponse complète n'a pas pu être terminée dans le délai imparti."
            )
        else:
            try:
                resp_gen = runtime.complete_chat(
                    RuntimeRequest(
                        messages=tuple(gen_messages),
                        temperature=0.2,
                        max_tokens=8192,
                        timeout_s=_remaining_run_timeout(t0, heavy_tool_wall_s),
                    )
                ).raw
                collected = resp_gen["choices"][0]["message"]["content"] or ""
                if collected:
                    clean_gen, think_gen = _sanitize_content(collected)
                    if think_gen:
                        emit(
                            "thinking",
                            {
                                "request_id": request_id,
                                "text": think_gen,
                                "iteration": 99,
                            },
                        )
                    collected = clean_gen
                    if _TEXTUAL_TOOL_RE.search(collected):
                        collected = _strip_textual_tool_calls(collected)
                if not collected.strip():
                    collected = "Je n'ai pas pu générer de réponse."
            except ResourceBusy:
                # Contention de ressource : erreur transitoire retryable.
                # La laisser remonter évite de la confondre avec un échec du
                # générateur et de perdre le signal de reprise.
                raise
            except RuntimeProtocolError as e:
                if "CONTEXT_WINDOW_TOO_SMALL" in str(e):
                    emit(
                        "error",
                        {
                            "request_id": request_id,
                            "code": "context_window_too_small",
                            "message": (
                                "Le contexte de cette discussion dépasse la fenêtre du modèle. "
                                "Réduisez les pièces jointes ou démarrez une nouvelle discussion."
                            ),
                            "detail": str(e)[:500],
                        },
                    )
                    log(f"[generator] context window too small: {e}")
                else:
                    emit(
                        "error",
                        {
                            "request_id": request_id,
                            "code": "generator_error",
                            "message": str(e)[:500],
                        },
                    )
                collected = ""
            except Exception as e:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "generator_error",
                        "message": str(e)[:500],
                    },
                )
                collected = ""
        # Dernier rempart AVANT le streaming et la persistance : la réponse que
        # l'utilisateur lit et celle qui est stockée ne doivent ni promettre
        # d'image absente, ni nier une image présente, ni nommer le brief
        # (voir _with_image_truth_guards et _without_brief_meta_reference).
        collected = _with_image_truth_guards(collected, image_status)
        collected = _without_brief_meta_reference(collected)

        if collected:
            chunk_size = 400
            for i in range(0, len(collected), chunk_size):
                emit(
                    "answer",
                    {"request_id": request_id, "text": collected[i : i + chunk_size]},
                )
                time.sleep(0.01)
            trace["answer_emitted"] = True

        emit(
            "message_completed",
            {
                "request_id": request_id,
                "run_id": run_id,
                "tokens_in": len(message) // 4,
                "tokens_out": len(collected) // 4,
                "total_latency_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        return collected, trace

    except RuntimeProtocolError:
        # Erreur déjà émise au point de refus ; on propage la finalisation
        # sans émettre de seconde erreur ni poursuivre la génération.
        try:
            return "", trace
        except Exception:
            return "", _empty_trace()
    except ResourceBusy:
        # Contention de ressource : erreur TRANSITOIRE, jamais un échec de
        # boucle. On la laisse remonter au chat qui doit la traiter comme
        # retryable (code RESOURCE_BUSY, retryable=True). L'absorber ici
        # transformerait une attente en panne définitive.
        raise
    except Exception as e:
        emit(
            "error",
            {"request_id": request_id, "code": "loop_error", "message": str(e)[:800]},
        )
        try:
            return "", trace
        except Exception:
            return "", _empty_trace()
    finally:
        finalize_run(run_id, request_id)

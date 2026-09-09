"""
Chat loop Phase 1 — 2 couches orchestrator → brief → générateur
Notes Futur/Notes.md § Agent loop 2 couches + double historique

But : orchestrator prépare la demande, générateur produit la réponse.
- Orchestrateur : LFM2.5-8B, peut appeler vision/doc_ocr (2 outils), max 5 iter, 15s/timeout, séquentiel
- Générateur : LFM2.5-8B, JAMAIS de tools, reçoit brief + résultats préparés
- Double historique : history_shared = user↔assistant uniquement, jamais tool traces passées
  → tool_results du tour en cours uniquement en mémoire, préparés (≤8K brut, 8-16K tronqué, >16K résumé)
- 7 events : message_started, thinking, tool_call, tool_result, answer, message_completed, error
"""
import base64
import json
import re
import time
from pathlib import Path

from backend.inference.ipc import emit, log

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
            log(f"[prepare_vision_image] resize {orig_w}x{orig_h} -> {after[0]}x{after[1]} (NaFlex 512 patches, tracer)")
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if image.width * image.height > 0:
            image.save(output_path, format="JPEG", quality=88, optimize=True)
            return output_path
    except Exception as e:
        log(f"[prepare_vision_image] fallback original: {e}")
    return input_path

# --- Sanitizer backend (filet permanent, ticket llama.cpp #23852) ---
# LFM2.5-8B peut fuiter <think> inline et [vision(...)] textuel si template non détecté (List of tools: [)
# Front fait déjà stripThinkingLeak, mais backend doit garantir que DB/history ne polluent pas le tour suivant
_THINK_BLOCK_RE = re.compile(r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_TAG_RE = re.compile(r"<\s*/?\s*think\s*>", re.IGNORECASE)
_TEXTUAL_TOOL_RE = re.compile(r"\[?\s*(vision|doc_ocr)\s*\(\s*(?:image_path|pdf_path)\s*=\s*[\"']([^\"']+)[\"'][^\)]*\)\s*\]?", re.IGNORECASE)
_ENGLISH_THINK_MARKERS = ["Je vais", "Je va", "Cette image", "Voici", "Bien sûr", "Pour une", "Les arbres", "Bonjour", "Salut"]


def _sanitize_content(content: str) -> tuple[str, str | None]:
    """Retourne (clean, thinking_extrait). Strip <think>, tags isolés, et préfixe anglais The user..."""
    if not content:
        return content, None
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
                content = content.replace("Jevaistrèsbien", "Je vais très bien").replace("Jeva", "Je va").replace("Pourune", "Pour une")
    return content.strip(), thinking


def _detect_textual_tool_calls(content: str) -> list[dict]:
    """Annexe 10 Lot 1 : tout [vision(...)] textuel est INVALID_DECISION, jamais exécuté (C04)."""
    if _TEXTUAL_TOOL_RE.search(content):
        log("[INVALID_DECISION] [vision(...)] textuel détecté — refusé, ne jamais exécuter ni diffuser")
    return []


def _strip_textual_tool_calls(content: str) -> str:
    return _TEXTUAL_TOOL_RE.sub("", content).strip()
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
from backend.prompts.generator import SYSTEM_GENERATOR
from backend.prompts.vision import SYSTEM_VISION
from backend.prompts.ocr import SYSTEM_OCR, build_system_ocr, resolve_ocr_task
from backend.utils.files import cleanup_request
from backend.tools.registry import get_default_registry

MAX_ITER = 5
TOOL_TIMEOUT = 15  # s
MAX_RETRY = 1
MAX_TOOL_TOKENS = 8000
TOTAL_TIMEOUT = 300  # s phase 1


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
            prepared.append({**r, "result": f"{truncated}\n\n[Tronqué : {tokens}→{MAX_TOOL_TOKENS} tokens conservés]"})
        else:
            try:
                prompt = f"Résume en 500 mots (garde faits et chiffres) :\n{r['result'][:50000]}"
                s = llm.create_completion(prompt=prompt, max_tokens=1024, temperature=0.3)["choices"][0]["text"].strip()
                prepared.append({**r, "result": f"[Résumé] : {s}"})
            except Exception:
                prepared.append({**r, "result": r["result"][: MAX_TOOL_TOKENS * 4] + "\n[Tronqué]"})
    return prepared


def select_attachment_tool(image_paths: list[str] | None) -> str | None:
    """Routage déterministe : pdf→doc_ocr, image→vision, rien→None (P0)."""
    if not image_paths:
        return None
    if any(Path(p).suffix.lower() == ".pdf" for p in image_paths):
        return "doc_ocr"
    return "vision"


def _format_tool_observation(tool: str, result: dict, attachment_name: str) -> str:
    """Observation structurée pour le générateur (jamais JSON tronqué arbitraire)."""
    status = "success" if result.get("ok") else "failed"
    content = result.get("result", "")
    return f"<tool_observation>\ntool: {tool}\nstatus: {status}\nattachment: {attachment_name}\nresult:\n{content}\n</tool_observation>".strip()


def asks_for_text_extraction(message: str) -> bool:
    # Unifié avec media_policy.OCR_REQUEST_TERMS (q-r.md : "lire ce qu'il y a d'écrit en japonais")
    try:
        from backend.inference.media_policy import asks_for_text as _asks
        return _asks(message)
    except Exception:
        text = (message or "").lower()
        ocr_terms = ("ocr","extrais le texte","transcris","transcrire","tableau","facture","scan")
        return any(term in text for term in ocr_terms)

def resolve_allowed_tools(message: str, image_paths: list[str] | None) -> list[str]:
    has_pdf = any(Path(p).suffix.lower() == ".pdf" for p in (image_paths or []))
    has_image = any(Path(p).suffix.lower() in (".png",".jpg",".jpeg",".webp") for p in (image_paths or []))
    if has_pdf:
        return ["doc_ocr"]
    if has_image and asks_for_text_extraction(message):
        return ["doc_ocr"]
    if has_image:
        return ["vision"]
    return []

def build_attachment_brief(message: str, result: dict) -> str:
    return f"Analyse reussie pour : {message}\n\n{result.get('result','')[:4000]}"


def plan_media_tools(message: str, current_entries: list[dict]) -> list[dict]:
    """Plan déterministe pur (testable sans modèle) : quelle outil pour chaque artefact du tour.

    Règle P0 : pdf→doc_ocr, image→vision, image + demande explicite de texte→doc_ocr.
    Seuls les artefacts disponibles (available) sont planifiés.
    Retourne [{tool, artifact_id, name}] — jamais de LLM impliqué.
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
            tool = "doc_ocr" if (kind == "pdf" or asks_for_text_extraction(message)) else "vision"
        plan.append({"tool": tool, "artifact_id": art_id, "name": e.get("name") or art_id})
    return plan


def pick_reinspection_entry(message: str, history_entries: list[dict]) -> dict | None:
    # Délègue à la politique officielle (media_policy.pick_target) — compat conservée
    try:
        from backend.inference.media_policy import pick_target as _pick
        return _pick(message, history_entries)
    except Exception:
        avail = [e for e in (history_entries or []) if e.get("available", True) and e.get("id")]
        return avail[-1] if avail else None

def requires_media_reinspection(message: str) -> bool:
    # Délègue à la politique officielle (media_policy.py, 3 couches) — compat conservée
    try:
        from backend.inference.media_policy import needs_reinspection as _needs
        return _needs(message)
    except Exception:
        text = (message or "").lower()
        reference_terms = ("cette image","cette photo","dans l'image","sur l'image","première image","deuxième image")
        return any(term in text for term in reference_terms)

def resolve_reinspection_tool(message: str) -> str:
    text = message.lower()
    ocr_terms = ("texte","écrit","écriture","lettres","mot","phrase","lis","lire","ocr","transcris","transcrire","panneau","sous-titre","titre")
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
            stmt = select(Source).join(Message, Source.msg_id == Message.id).where(Message.conv_id == conv_id).order_by(Source.created_at.asc(), Source.id.asc())
            rows = list(db.execute(stmt).scalars().all())
            # Filtre image vs pdf selon message
            images = [r for r in rows if (r.media_kind == "image" or (r.path and r.path.lower().endswith((".png",".jpg",".jpeg",".webp"))))]
            pdfs = [r for r in rows if (r.media_kind == "pdf" or (r.path and r.path.lower().endswith(".pdf")))]
            # Détermine cible ordinale
            low = message.lower()
            target = None
            if "première image" in low or "premiere image" in low or "premier pdf" in low:
                target = images[0] if images else (pdfs[0] if pdfs else None)
            elif "deuxième image" in low or "seconde image" in low or "second pdf" in low:
                target = images[1] if len(images) > 1 else None
            elif "dernière image" in low or "derniere image" in low or "précédente" in low or "precedente" in low or "avant" in low:
                target = images[-1] if images else None
            else:
                # Par défaut : dernière image (si une seule ambiguïté, on prend dernière)
                if images:
                    target = images[-1]
                elif pdfs:
                    target = pdfs[-1]
            if target is None:
                # Ambigu : plusieurs images et générique "cette image" sans ordinal clair -> None pour demander clarification
                if len(images) > 1 and "cette image" in low and "première" not in low and "deuxième" not in low and "dernière" not in low:
                    return None  # caller demandera clarification
                target = images[-1] if images else None
            if target is None:
                return None
            # Résout chemin absolu depuis relative_path (portable C19)
            if target.relative_path:
                try:
                    from backend.config.paths import get_data_dir
                    data_dir = get_data_dir()
                    # relative_path = media/{hash}.ext ou blobs/{hash}.ext
                    # Cherche sous data_dir puis sous data_dir/media, data/blobs
                    for base in [data_dir, data_dir / "media", data_dir / "blobs"]:
                        cand = base / Path(target.relative_path).name if "/" not in target.relative_path else data_dir / target.relative_path
                        # Essai direct
                        cand2 = data_dir / target.relative_path
                        if cand2.exists():
                            return str(cand2), target.original_name or target.path
                        try:
                            from backend.db.media_cache import get_media_root
                            mr = get_media_root()
                            abs_path2 = mr / Path(target.relative_path).name
                            if abs_path2.exists():
                                return str(abs_path2), target.original_name or target.path
                        except Exception:
                            pass
                    # fallback legacy home
                    mr_legacy = Path.home() / ".local" / "share" / "thot" / target.relative_path
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
            try: db.close()
            except: pass
    except Exception as e:
        log(f"[reinspection] resolve failed: {e}")
        return None
    return None

def build_attachment_registry(conv_id: str, image_paths: list[str] | None = None) -> list[dict]:
    """Registre structuré des sources pour l'orchestrateur — visible à chaque tour (Réponses.md n°8-10)."""
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import Source, Message, ToolCall
        from sqlalchemy import select
        db = SessionLocal()
        try:
            stmt = select(Source).join(Message, Source.msg_id == Message.id).where(Message.conv_id == conv_id).order_by(Source.created_at.asc(), Source.id.asc())
            rows = list(db.execute(stmt).scalars().all())
            # Map message_id -> index pour message_index
            msg_stmt = select(Message).where(Message.conv_id == conv_id).order_by(Message.created_at.asc(), Message.id.asc())
            msgs = list(db.execute(msg_stmt).scalars().all())
            msg_index_map = {m.id: idx+1 for idx, m in enumerate(msgs)}
            registry = []
            for i, s in enumerate(rows):
                # previous_analyses depuis ToolCall
                prev = []
                try:
                    tc_stmt = select(ToolCall).where(ToolCall.msg_id == s.msg_id)
                    tcs = list(db.execute(tc_stmt).scalars().all())
                    for tc in tcs:
                        summary = (tc.result or "")[:120].strip()
                        prev.append({"tool": tc.tool, "status": "success" if tc.ok else "failed", "summary": summary})
                    # Fallback global si aucun sur ce message mais source hash match ailleurs
                    if not prev and s.sha256:
                        all_tc = db.execute(select(ToolCall)).scalars().all()
                        for tc in all_tc:
                            args = tc.args or {}
                            if s.sha256 in str(args) or (s.path and s.path in str(args)):
                                prev.append({"tool": tc.tool, "status": "success" if tc.ok else "failed", "summary": (tc.result or "")[:120]})
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
                        abs_available = cand.exists() or (mr / Path(s.relative_path).name).exists() or (mr / s.relative_path).exists()
                        if not abs_available:
                            abs_available = (Path.home() / ".local" / "share" / "thot" / s.relative_path).exists()
                    elif s.path:
                        abs_available = Path(s.path).exists()
                except Exception:
                    pass
                registry.append({"id": s.id, "ordinal": i+1, "index": i+1, "kind": s.media_kind or ("pdf" if (s.path and s.path.lower().endswith(".pdf")) else "image"), "name": s.original_name or Path(s.path).name if s.path else f"source_{i+1}", "path": s.relative_path or s.path, "first_seen_in_message": s.msg_id, "message_index": msg_index_map.get(s.msg_id, 0), "available": abs_available, "previous_analyses": prev})
            existing_paths = {r.get("path") for r in registry}
            for p in (image_paths or []):
                if p not in existing_paths and p not in {r.get("relative_path") for r in registry}:
                    registry.append({"id": f"pending_{len(registry)+1}", "ordinal": len(registry)+1, "index": len(registry)+1, "kind": "pdf" if p.lower().endswith(".pdf") else "image", "name": Path(p).name, "path": p, "first_seen_in_message": "current", "message_index": len(msgs)+1, "available": Path(p).exists(), "previous_analyses": []})
            return registry
        finally:
            try: db.close()
            except: pass
    except Exception as e:
        log(f"[registry] build failed: {e}")
        return [{"id": "pending_1", "ordinal": 1, "index": 1, "kind": "image", "name": Path(p).name, "path": p, "first_seen_in_message": "current", "message_index": 1, "available": True, "previous_analyses": []} for p in (image_paths or [])]

def build_history_artifact_entries(conv_id: str, exclude_ids: set[str] | None = None) -> list[dict]:
    """Registre historique annexe 10 : artefacts des tours précédents via MessageAttachment.

    Sans ça, un follow-up texte ("et là ?") ne voit aucun fichier : attachment_ids est vide
    et build_attachment_registry ne connaît que la table legacy Source → tool_defs=[].
    Retourne des entrées au même schéma que le registre courant (id/ordinal/name/...).
    """
    entries: list[dict] = []
    try:
        from backend.db.database import SessionLocal as _SLH
        from backend.db.models import Artifact as _ArtH, Blob as _BlobH, Message as _MsgH, MessageAttachment as _MAH
        from backend.config.paths import get_data_dir as _gddH
        excluded = exclude_ids or set()
        _db = _SLH()
        try:
            rows = (
                _db.query(_MAH, _MsgH)
                .join(_MsgH, _MAH.message_id == _MsgH.id)
                .filter(_MsgH.conv_id == conv_id)
                .order_by(_MsgH.created_at.asc(), _MAH.ordinal.asc())
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
                blob = _db.query(_BlobH).filter(_BlobH.id == art.blob_id).first()
                available = False
                if blob and blob.relative_path:
                    try:
                        available = (_gddH() / blob.relative_path).exists()
                    except Exception:
                        pass
                entries.append({
                    "id": ma.artifact_id,
                    "ordinal": 0,  # renuméroté à la fusion
                    "index": 0,
                    "kind": art.kind,
                    "name": art.original_filename or art.display_name or ma.artifact_id,
                    "path": blob.relative_path if blob else "",
                    "first_seen_in_message": ma.message_id,
                    "message_index": msg_index.get(ma.message_id, 0),
                    "available": available,
                    "previous_analyses": [],
                })
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
    # Manifest compact per spec n°10
    lines = ["SOURCES DISPONIBLES DANS CETTE CONVERSATION"]
    for r in registry:
        prev = r.get("previous_analyses") or []
        prev_str = f"Analyse précédente : {prev[0]['tool']} {prev[0]['status']} — {prev[0]['summary']}" if prev else "Analyse précédente : aucune"
        lines.append(f" [{r.get('ordinal')}] source_id={r.get('id')} artifact_id={r.get('id')} Type : {r.get('kind')} Nom : {r.get('name')} Tour d'origine : {r.get('message_index')} Disponible : {'oui' if r.get('available') else 'non'} {prev_str} Résumé : {prev[0]['summary'] if prev else ''}".strip())
    return "\n".join(lines) + "\n\n" + json.dumps([{"id": r["id"], "artifact_id": r["id"], "ordinal": r["ordinal"], "kind": r["kind"], "name": r["name"], "message_index": r["message_index"], "available": r["available"], "previous_analyses": r["previous_analyses"]} for r in registry], ensure_ascii=False, indent=2)


def _execute_vision(manager, tool_call: dict, image_paths: list[str] | None, request_id: str, attachment_ids: list[str] | None = None) -> dict:
    """Exécute vision séquentiel via ModelManager — annexe 10 : privilégie artifact_id durable."""
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        # Cadrage VLM : question autonome, sans déixis LLM ("la précédente image", ...).
        # D'où vient la query sale : Phase 0 passe message[:500] brut, et l'orchestrateur LLM
        # peut passer "revoit l'image" / "[doc_ocr(...)]" textuel. Où va la query propre :
        # SYSTEM_VISION.format(query) + message user du VL, SANS historique (le VL n'a pas
        # le contexte, par design — il ne voit qu'une image + une question).
        try:
            from backend.inference.media_policy import clean_media_query as _clean
            query = _clean(args.get("query", ""))
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
                    art = db.query(Artifact).filter(Artifact.id == requested_artifact).first()
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
                    try: db.close()
                    except: pass
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
                            blob = _db.query(_Blob).filter(_Blob.id == art.blob_id).first()
                            if blob:
                                p = _gdd() / blob.relative_path
                                if p.exists():
                                    valid_from_artifacts.append(str(p))
                finally:
                    try: _db.close()
                    except: pass
            except Exception:
                pass
        # Validation : si le LLM hallucine un chemin hors zone autorisée, fallback sur le vrai fichier fourni (annexe 10 : artifact_id)
        # Évite la hallucination vue dans conv.png : image_path='/tmp/thot/bea62abc-...png' flat qui n'existe pas
        valid_paths = [p for p in (image_paths or []) if p and Path(p).exists()] + valid_from_artifacts
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
                        if str(Path(requested).resolve()).startswith(str(THOT_TMP.resolve())):
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
            return {"ok": False, "result": f"[Erreur vision: fichier introuvable {requested or img}]", "latency_ms": int((time.perf_counter() - start) * 1000)}
        # Prétraite : max 1536px + JPEG 88 (évite 1.8M tokens base64 comme texte si handler broken, réduit VRAM)
        try:
            prepared = prepare_vision_image(Path(img), Path(f"/tmp/thot/{request_id}/vision_prepared.jpg"))
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
                log(f"[vision] base64 large {len(b64)} chars (>4M), risque token blowup")
        except Exception as e:
            return {"ok": False, "result": f"[Erreur vision: lecture image {e}]", "latency_ms": int((time.perf_counter() - start) * 1000)}
        with manager.use_model("LFM2.5-VL-3B") as vlm:
            try:
                r = vlm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": SYSTEM_VISION.format(query=query)},
                        {"role": "user", "content": [{"type": "text", "text": query or "Décris cette image en détail."}, {"type": "image_url", "image_url": {"url": data_url}}]},
                    ],
                    temperature=0.2,
                    max_tokens=2048,
                )
            except Exception as e:
                # Erreur typique si handler absent : Requested tokens (1885477) exceed context window
                if "exceed context" in str(e) or "1885477" in str(e) or "Requested tokens" in str(e):
                    return {"ok": False, "result": "VISION_MULTIMODAL_NOT_INITIALIZED: base64 traite comme texte, mmproj/handler incompatible. Verifiez mmproj et chat_handler LFM2.5-VL-3B.", "latency_ms": int((time.perf_counter() - start) * 1000)}
                raise
            text = r["choices"][0]["message"]["content"]
            return {"ok": True, "result": text, "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as e:
        if "VISION_MULTIMODAL" in str(e):
            return {"ok": False, "result": str(e), "latency_ms": int((time.perf_counter() - start) * 1000)}
        return {"ok": False, "result": f"[Erreur vision: {e}]", "latency_ms": int((time.perf_counter() - start) * 1000)}


def _execute_doc_ocr(manager, tool_call: dict, request_id: str) -> dict:
    """Exécute doc_ocr page par page séquentiel via PyMuPDF 200 DPI (évite KV blowup 384MB/page)."""
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
                    _art = _db.query(_ArtO).filter(_ArtO.id == requested_artifact).first()
                    if _art is not None:
                        _blob = _db.query(_BlobO).filter(_BlobO.id == _art.blob_id).first()
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
        ocr_task = resolve_ocr_task(query)
        system_prompt = build_system_ocr(task=ocr_task)
        if not pdf_path or not Path(pdf_path).exists():
            return {"ok": False, "result": f"[Erreur doc_ocr: fichier introuvable {pdf_path}]", "latency_ms": int((time.perf_counter() - start) * 1000)}
        ext = Path(pdf_path).suffix.lower()
        # Si image directe → une seule page (base64 pour garantir vision)
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            with open(pdf_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            mime = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else "image/webp"
            data_url = f"data:{mime};base64,{b64}"
            with manager.use_model("GLM-OCR") as ocr:
                r = ocr.create_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": [{"type": "text", "text": "Page 1"}, {"type": "image_url", "image_url": {"url": data_url}}]},
                    ],
                    temperature=0.0,
                    max_tokens=8192,
                )
                return {"ok": True, "result": r["choices"][0]["message"]["content"], "latency_ms": int((time.perf_counter() - start) * 1000)}
        # PDF → pages séquentielles
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        pages: list[str] = []
        with manager.use_model("GLM-OCR") as ocr:
            for i, page in enumerate(doc):
                if i >= 50:
                    break
                pix = page.get_pixmap(dpi=200)  # 200 DPI PNG lossless
                tmp = Path(f"/tmp/thot/{request_id}/page_{i}.png")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                pix.save(str(tmp))
                # Base64 pour garantir que GLM-OCR voit la page (file:// peu fiable)
                with open(tmp, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                data_url = f"data:image/png;base64,{b64}"
                rr = ocr.create_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": [{"type": "text", "text": f"Page {i+1}"}, {"type": "image_url", "image_url": {"url": data_url}}]},
                    ],
                    temperature=0.0,
                    max_tokens=8192,
                )
                pages.append(rr["choices"][0]["message"]["content"])
        return {"ok": True, "result": "\n\n--- Page suivante ---\n\n".join(pages), "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "result": f"[Erreur doc_ocr: {e}]", "latency_ms": int((time.perf_counter() - start) * 1000)}

def _execute_get_source(tool_call: dict, conv_id: str | None = None) -> dict:
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        source_id = args.get("source_id")
        # Annexe 11 Lot A : get_source = compat LIMITÉE (plus de catalogue complet).
        # Sans source_id : 20 fichiers récents max + renvoi vers search_documents.
        if not source_id:
            if not conv_id:
                return {"ok": False, "result": "[get_source: source_id manquant et conversation inconnue]", "latency_ms": int((time.perf_counter() - start)*1000)}
            try:
                entries = build_history_artifact_entries(conv_id)
                legacy = build_attachment_registry(conv_id, None)
                seen = {e.get("id") for e in entries}
                for r in legacy or []:
                    if r.get("id") not in seen:
                        entries.append(r)
                import json as _json
                # Récents d'abord (message_index desc), borné à 20 — jamais le catalogue entier
                entries.sort(key=lambda e: (e.get("message_index") or 0), reverse=True)
                compact = [{"id": e.get("id"), "kind": e.get("kind"), "name": e.get("name"), "message_index": e.get("message_index"), "available": e.get("available")} for e in entries[:20]]
                hint = ""
                if len(entries) > 20:
                    hint = f" … et {len(entries) - 20} autre(s). Pour retrouver un fichier précis, utilise search_documents."
                return {"ok": True, "result": f"Fichiers récents de la conversation ({len(entries)}) : " + _json.dumps(compact, ensure_ascii=False) + hint, "latency_ms": int((time.perf_counter() - start)*1000)}
            except Exception as e:
                return {"ok": False, "result": f"[get_source list-all: {e}]", "latency_ms": int((time.perf_counter() - start)*1000)}
        from backend.db.database import SessionLocal
        from backend.db.models import Source
        from backend.config.paths import get_data_dir
        from backend.db.media_cache import get_media_root
        db = SessionLocal()
        try:
            # Annexe 10 : le registre annonce source_id=art_xxx → résout Artifact d'abord
            # Annexe 11 Lot A : scope-check AVANT lecture + métadonnées SANS working_path
            # (le chemin reste privé aux exécuteurs vision/ocr/read ; jamais au LLM).
            try:
                from backend.db.models import Artifact as _ArtGS, Blob as _BlobGS
                from backend.search import scope as _scopeGS, cards as _cardsGS
                _art = db.query(_ArtGS).filter(_ArtGS.id == source_id).first()
                if _art is not None:
                    _allowed = _scopeGS.resolve_allowed_artifacts(conv_id) if conv_id else {_art.id}
                    _denied = _scopeGS.check_artifact_scope(source_id, _allowed)
                    if _denied is not None:
                        return {"ok": False, "result": f"[{_denied['code']}: {_denied['message']}]", "latency_ms": int((time.perf_counter() - start)*1000)}
                    _blob = db.query(_BlobGS).filter(_BlobGS.id == _art.blob_id).first()
                    _available = False
                    _size = _blob.size_bytes if _blob is not None else None
                    try:
                        from backend.config.paths import get_data_dir as _gddGS
                        if _blob is not None:
                            _available = (_gddGS() / _blob.relative_path).exists()
                    except Exception:
                        pass
                    _card = _cardsGS.get_current_card(db, _art.id)
                    import json as _jsonGS
                    _meta = {
                        "artifact_id": _art.id,
                        "kind": _art.kind,
                        "name": _art.original_filename or _art.display_name,
                        "size_bytes": _size,
                        "available": bool(_available),
                        "card": {"revision": int(_card.revision or 0) if _card else 0,
                                 "summary": (_card.summary if _card else "") or "",
                                 "status": ((_card.analysis_status or _card.status) if _card else "pending")},
                        "available_actions": (["get_document_card", "vision", "doc_ocr"] if (_art.kind == "image") else
                                              ["get_document_card", "read_document", "doc_ocr"] if (_art.kind == "pdf") else
                                              ["get_document_card", "read_document"]),
                    }
                    return {"ok": True, "result": _jsonGS.dumps(_meta, ensure_ascii=False), "latency_ms": int((time.perf_counter() - start)*1000)}
            except Exception:
                pass
            src = db.get(Source, source_id)
            if not src:
                return {"ok": False, "result": f"[get_source: {source_id} introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            # Legacy Source : métadonnées SANS chemin (annexe 11 — jamais de working_path au LLM).
            # La résolution privée du fichier reste dans _execute_vision/_execute_doc_ocr/_execute_read_text.
            _available_src = False
            if src.relative_path:
                data_dir = get_data_dir()
                for cand in [data_dir / src.relative_path, get_media_root() / Path(src.relative_path).name, get_media_root() / src.relative_path, Path.home() / ".local" / "share" / "thot" / src.relative_path]:
                    if cand.exists():
                        _available_src = True
                        break
            if not _available_src and src.path and Path(src.path).exists():
                _available_src = True
            import json as _jsonSRC
            return {"ok": True, "result": _jsonSRC.dumps({"source_id": src.id, "kind": src.media_kind, "name": src.original_name, "available": bool(_available_src)}, ensure_ascii=False), "latency_ms": int((time.perf_counter() - start)*1000)}
        finally:
            try: db.close()
            except: pass
    except Exception as e:
        return {"ok": False, "result": f"[get_source: {e}]", "latency_ms": int((time.perf_counter() - start)*1000)}

def _execute_read_text(tool_call: dict) -> dict:
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        source_id = args.get("source_id")
        max_chars = int(args.get("max_chars", 10000))
        if not source_id:
            return {"ok": False, "result": "[read_text: source_id manquant]", "latency_ms": int((time.perf_counter() - start)*1000)}
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
                            return {"ok": True, "result": text, "latency_ms": int((time.perf_counter() - start)*1000)}
                        return {"ok": False, "result": "[read_text: working_path introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            except Exception:
                pass
            src = db.get(Source, source_id)
            if not src:
                return {"ok": False, "result": f"[read_text: {source_id} introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            working_path = None
            if src.relative_path:
                data_dir = get_data_dir()
                for cand in [data_dir / src.relative_path, get_media_root() / Path(src.relative_path).name, get_media_root() / src.relative_path, Path.home() / ".local" / "share" / "thot" / src.relative_path]:
                    if cand.exists():
                        working_path = str(cand)
                        break
            if not working_path and src.path and Path(src.path).exists():
                working_path = src.path
            if not working_path:
                return {"ok": False, "result": "[read_text: working_path introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            text = Path(working_path).read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n[Tronqué]"
            return {"ok": True, "result": text, "latency_ms": int((time.perf_counter() - start)*1000)}
        finally:
            try: db.close()
            except: pass
    except Exception as e:
        return {"ok": False, "result": f"[read_text: {e}]", "latency_ms": int((time.perf_counter() - start)*1000)}


def _check_cancel(cancel_flag) -> bool:
    return bool(cancel_flag and cancel_flag.is_set())


# ---------- Annexe 11 Lot A : persistance observations + fiches + recherche ----------
# D'OÙ VIENT CE BLOC : avant, les résultats d'outils ne vivaient qu'en mémoire du tour.
# OÙ VA QUOI : _record_observation() après chaque exécution (Phase 0 + Phase 1),
# _ensure_cards_for_registry() après construction du registre,
# _execute_search_documents() comme 5e exécuteur.
# POURQUOI ICI : un seul point d'appel, jamais bloquant (la DB ne casse pas le tour).

def _record_observation(tool_name: str, artifact_id: str, query: str | None,
                        result: dict, run_id: str, tool_call_id: str,
                        model_id: str | None = None) -> None:
    """Persiste Observation immuable + révise la fiche N+1. Ne lève jamais."""
    try:
        if not artifact_id:
            return  # list-all / search : pas d'analyse mono-fichier, rien à persister
        from backend.search import observations as _obsmod
        _obsmod.record_tool_observation(
            artifact_id, tool_name, query, (result or {}).get("result", ""),
            bool((result or {}).get("ok")),
            run_id=run_id, tool_call_id=tool_call_id, model_id=model_id,
            latency_ms=int((result or {}).get("latency_ms", 0) or 0))
    except Exception as e:
        log(f"[observation] record failed: {e}")


def _ensure_cards_for_registry(conv_id: str, attachment_registry: list | None) -> None:
    """Self-healing fiches : garantit fiche rev 1 + index pour chaque id du registre."""
    try:
        ids = [e.get("id") for e in (attachment_registry or []) if isinstance(e, dict) and e.get("id")]
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
        if scope not in ("conversation",):
            # Lot A : seul le scope conversation existe (espaces = Lot B)
            return {"ok": False,
                    "result": "[SEARCH_SCOPE_INVALID: scope 'workspace' non activé (Lot B)]",
                    "latency_ms": int((time.perf_counter() - start) * 1000)}
        if not conv_id:
            return {"ok": False,
                    "result": "[SEARCH_SCOPE_INVALID: conversation inconnue]",
                    "latency_ms": int((time.perf_counter() - start) * 1000)}
        from backend.search import scope as _scopemod, fts as _ftsmod
        from backend.db.database import SessionLocal as _SLS
        allowed = _scopemod.resolve_allowed_artifacts(conv_id)
        db = _SLS()
        try:
            _ftsmod.ensure_fts(db)
            page = _ftsmod.search(
                db, query=query, scope_kind="conversation", scope_id=conv_id,
                allowed_ids=allowed, types=args.get("types"),
                status=args.get("status") or "any",
                limit=args.get("limit") or 10, cursor=args.get("cursor"))
        finally:
            try:
                db.close()
            except Exception:
                pass
        if not page.get("ok"):
            return {"ok": False,
                    "result": f"[{page.get('code', 'SEARCH_ERROR')}: {page.get('message', '')}]",
                    "latency_ms": int((time.perf_counter() - start) * 1000)}
        return {"ok": True, "result": _json.dumps(page, ensure_ascii=False),
                "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "result": f"[search_documents: {e}]",
                "latency_ms": int((time.perf_counter() - start) * 1000)}


def _execute_document_card(tool_call: dict, conv_id: str | None = None) -> dict:
    """Exécuteur get_document_card : fiche, jamais contenu ni chemin (Lot B)."""
    start = time.perf_counter()
    try:
        import json as _json
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        from backend.search import reader as _readermod, scope as _scopemod
        from backend.db.database import SessionLocal as _SLD
        allowed = _scopemod.resolve_allowed_artifacts(conv_id) if conv_id else set()
        db = _SLD()
        try:
            payload = _readermod.get_card_payload(db, args.get("artifact_id") or "", allowed)
        finally:
            try:
                db.close()
            except Exception:
                pass
        if not payload.get("ok"):
            return {"ok": False,
                    "result": f"[{payload.get('code', 'CARD_ERROR')}: {payload.get('message', '')}]",
                    "latency_ms": int((time.perf_counter() - start) * 1000)}
        return {"ok": True, "result": _json.dumps(payload, ensure_ascii=False),
                "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "result": f"[get_document_card: {e}]",
                "latency_ms": int((time.perf_counter() - start) * 1000)}


def _execute_read_document(tool_call: dict, conv_id: str | None = None) -> dict:
    """Exécuteur read_document : extrait localisé + evidence_id (Lot B)."""
    start = time.perf_counter()
    try:
        import json as _json
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        from backend.search import reader as _readermod, scope as _scopemod
        from backend.db.database import SessionLocal as _SLR
        allowed = _scopemod.resolve_allowed_artifacts(conv_id) if conv_id else set()
        db = _SLR()
        try:
            payload = _readermod.read_payload(
                db, args.get("artifact_id") or "", allowed,
                query=args.get("query"), locator=args.get("locator"),
                max_chars=args.get("max_chars") or 8000)
        finally:
            try:
                db.close()
            except Exception:
                pass
        if not payload.get("ok"):
            return {"ok": False,
                    "result": f"[{payload.get('code', 'READ_ERROR')}: {payload.get('message', '')}]",
                    "latency_ms": int((time.perf_counter() - start) * 1000)}
        # Lecture = travail réutilisable → observation (idempotente par empreinte ;
        # le hook Phase 1 enrichira run_id/tool_call_id s'il suit).
        try:
            from backend.search import observations as _obsRD
            _obsRD.record_tool_observation(
                args.get("artifact_id") or "", "read_document",
                args.get("query") or "", payload.get("excerpt", "") or "", True)
        except Exception as e:
            log(f"[observation] read_document record failed: {e}")
        return {"ok": True, "result": _json.dumps(payload, ensure_ascii=False),
                "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "result": f"[read_document: {e}]",
                "latency_ms": int((time.perf_counter() - start) * 1000)}


def run_agent_loop(
    manager,
    conv_id: str,
    request_id: str,
    message: str,
    mode: str,
    image_paths: list[str] | None,
    history_shared: list[dict],
    cancel_flag=None,
    attachment_ids: list[str] | None = None,
):
    """
    Boucle 2 couches Phase 1 : orchestrator → brief → générateur.
    - Orchestrateur peut appeler vision/doc_ocr (max 5 iter, séquentiel)
    - Générateur ne peut JAMAIS appeler d'outils
    - history_shared : list[{role, content}] user↔assistant compacté, sans tool traces
    """
    if not message or not message.strip():
        emit("error", {"request_id": request_id, "code": "empty_message", "message": "Message vide"})
        return ""
    if mode not in ("chat", "agent"):
        emit("error", {"request_id": request_id, "code": "invalid_mode", "message": f"mode {mode} invalide"})
        return ""

    run_id = request_id
    t0 = time.perf_counter()
    emit("message_started", {"request_id": request_id, "run_id": run_id, "model": "LFM2.5-8B"})

    history = list(history_shared) if history_shared else []
    assert isinstance(history, list), "history_shared doit être une liste"

    # Compaction non-destructive C06 : prune avant figeage SharedContext
    try:
        from backend.inference.compaction import prune_old_tool_results
        history = prune_old_tool_results(history)
    except Exception:
        pass

    # Registre d'images pour l'orchestrateur (Réponses.md n°8 / annexe 10) — artifact_ids durables cible
    if attachment_ids:
        # Construit registre depuis Artifact durables (annexe 10)
        try:
            from backend.db.database import SessionLocal as _SL2
            from backend.db.models import Artifact, Blob
            _db = _SL2()
            attachment_registry = []
            for i, art_id in enumerate(attachment_ids):
                art = _db.query(Artifact).filter(Artifact.id == art_id).first()
                if not art:
                    continue
                blob = _db.query(Blob).filter(Blob.id == art.blob_id).first()
                # Vérifie disponibilité du fichier sur disque
                abs_available = False
                if blob and blob.relative_path:
                    try:
                        from backend.config.paths import get_data_dir
                        data_dir = get_data_dir()
                        abs_available = (data_dir / blob.relative_path).exists()
                    except Exception:
                        pass
                # Schéma IMPOSÉ identique à build_attachment_registry (Source legacy) : "id",
                # "ordinal", "name", "message_index", "available", "previous_analyses". Pourquoi :
                # format_attachment_registry() fait des accès directs r["id"] → tout autre
                # schéma = KeyError: 'id' = toast 'Erreur' + réponse vide (bug q-r.md 08/09).
                attachment_registry.append({
                    "id": art_id,
                    "ordinal": i + 1,
                    "index": i + 1,
                    "kind": art.kind,
                    "name": art.original_filename or art.display_name or f"artifact_{i+1}",
                    "path": blob.relative_path if blob else "",
                    "first_seen_in_message": "current",
                    "message_index": 1,
                    "available": abs_available,
                    "previous_analyses": [],
                })
            _db.close()
            # Historique : artefacts des tours précédents (follow-ups sans nouveaux fichiers)
            try:
                current_ids = {e.get("id") for e in attachment_registry if e.get("id")}
                for h in build_history_artifact_entries(conv_id, exclude_ids=current_ids):
                    h["ordinal"] = len(attachment_registry) + 1
                    h["index"] = len(attachment_registry) + 1
                    attachment_registry.append(h)
            except Exception as e:
                log(f"[registry] history merge failed: {e}")
            # Fallback image_paths si registry vide (compat)
            if not attachment_registry and image_paths:
                attachment_registry = build_attachment_registry(conv_id, image_paths)
        except Exception as e:
            log(f"[registry] artifact build failed: {e}")
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
    has_any_media = bool(attachment_registry)
    registry = get_default_registry()
    # Annexe 11 Lot A : search_documents est dispo dès qu'un scope conversation existe
    # (découverte SANS pièce jointe au tour courant : "retrouve la facture EDF") ;
    # vision/doc_ocr/get_source/read_text seulement si média (courant ou historique).
    _all_defs = registry.definitions()
    if has_any_media:
        tool_defs = _all_defs
    else:
        tool_defs = [d for d in _all_defs
                     if isinstance(d, dict) and d.get("function", {}).get("name") == "search_documents"]
    # Fiches garanties pour le registre (self-healing : Rust crée l'Artifact, Python assure la fiche)
    try:
        _ensure_cards_for_registry(conv_id, attachment_registry)
    except Exception as e:
        log(f"[cards] ensure failed: {e}")

    # SharedContext C03 : même base versionnée pour orchestrateur et générateur
    revision = len(history) + 1  # simple compteur, valide la compaction
    shared_ctx = SharedContext(conversation_id=conv_id, revision=revision, history=history, registry=attachment_registry, message=message)
    # Vérifie annulation avant même de commencer
    if _check_cancel(cancel_flag):
        emit("run.cancelled", {"request_id": request_id, "run_id": run_id, "conversation_id": conv_id})
        return ""
    # Execution plan basé sur registre (observabilité sémantique, pas lexicale)
    if attachment_registry:
        first = attachment_registry[0]
        emit("execution_plan", {"request_id": request_id, "attachment_kind": first.get("kind"), "intent": "media_available", "selected_tool": None, "selected_model": None, "reason": f"{len(attachment_registry)} fichier(s) disponible(s) dans registre, orchestrateur décide"})
    else:
        emit("execution_plan", {"request_id": request_id, "attachment_kind": None, "intent": "general", "selected_tool": None, "selected_model": None, "reason": "Aucun fichier"})

    tool_results: list[dict] = []
    brief: str = message  # fallback si pas de brief orchestré

    # Validation : log image_paths avant run
    if image_paths:
        valid_cnt = sum(1 for p in image_paths if p and Path(p).exists())
        from backend.inference.ipc import log as _log
        _log(f"[loop] image_paths={image_paths} valid={valid_cnt}/{len(image_paths)} mode={mode} registry={len(attachment_registry)}")
        if valid_cnt == 0:
            emit("error", {"request_id": request_id, "code": "image_paths_invalid", "message": "Aucun fichier joint valide trouvé"})
    tool_observations: list[str] = []
    # Cache déterministe : (tool, artifact_id) -> result. D'où vient le besoin : si la
    # Phase 0 a déjà observé art_X et que l'orchestrateur LLM redemande vision(art_X), on
    # rejoue le résultat au lieu de recharger le VL (2s + bascule VRAM). Pourquoi clé sans
    # la query : même image = mêmes pixels ; une nouvelle question sur la même image reste
    # servie par la 1re observation (limite connue, préférable au coût d'un 2e passage).
    deterministic_cache: dict[tuple[str, str], dict] = {}

    # ========== Phase 0 : Routage déterministe média (sans LLM) ==========
    # D'OÙ VIENT L'IDÉE : le function-calling natif du GGUF 8B est défaillant (template non
    # reconnu → [vision(...)] textuel au lieu de tool_call, cf. logs INVALID_DECISION).
    # OÙ VONT LES RÉSULTATS : tool_results + tool_observations alimentent la Phase 2
    # (générateur) via obs_block, ET l'orchestrateur Phase 1 les voit dans son historique —
    # donc même si le LLM comprend, il n'a plus besoin d'appeler.
    # POURQUOI AVANT L'ORCHESTRATEUR : l'observation visuelle est un fait (pixels), pas une
    # décision ; la confier au maillon le moins fiable (8B) causait les "pas accès".
    # Le LLM orchestrateur garde la main pour le brief + get_source/read_text.
    # RÈGLE MÉMOIRE : un seul modèle en VRAM à la fois (manager.use_model) → les exécutions
    # sont strictement séquentielles, max MAX_ITER, avec allers 8B<->VL assumés.
    try:
        _current_entries = [e for e in attachment_registry if e.get("first_seen_in_message") == "current"]
        _history_entries = [e for e in attachment_registry if e.get("first_seen_in_message") != "current"]
        _plan = plan_media_tools(message, _current_entries)
        if not _plan:
            try:
                from backend.inference.media_policy import decide_reinspection as _decide
                _dec = _decide(message, _current_entries, _history_entries or attachment_registry, asks_for_text_extraction)
            except Exception as e:
                log(f"[deterministic] decide failed: {e}")
                _dec = None
            if _dec is not None:
                _plan = [_dec]
        for _item in _plan[:MAX_ITER]:
            if _check_cancel(cancel_flag):
                break
            _tool = _item["tool"]
            _aid = _item["artifact_id"]
            _tcall_id = f"det_{_tool}_{_aid[:12] if len(_aid) > 12 else _aid}"
            emit("tool_call", {"request_id": request_id, "tool_call_id": _tcall_id, "tool": _tool, "arguments": {"artifact_id": _aid, "query": message[:500]}})
            _res: dict | None = None
            for _attempt in range(MAX_RETRY + 1):
                if _tool == "vision":
                    _res = _execute_vision(manager, {"args": {"artifact_id": _aid, "query": message[:500]}}, image_paths, request_id, attachment_ids)
                else:
                    _res = _execute_doc_ocr(manager, {"args": {"artifact_id": _aid, "query": message[:500]}}, request_id)
                if _res is not None and (_res.get("ok") or _attempt >= MAX_RETRY):
                    break
                time.sleep(0.5)
            if _res is None:
                _res = {"ok": False, "result": f"[Routage déterministe: {_tool} sans résultat]", "latency_ms": 0}
            emit("tool_result", {"request_id": request_id, "tool_call_id": _tcall_id, "result": _res.get("result", "")[:5000], "ok": _res.get("ok", False), "latency_ms": _res.get("latency_ms", 0)})
            tool_results.append({"tool": _tool, "result": _res.get("result", ""), "ok": _res.get("ok", False), "tool_call_id": _tcall_id})
            tool_observations.append(_format_tool_observation(_tool, _res, _item.get("name") or _aid))
            # Annexe 11 Lot A : Observation persistée + fiche N+1 (revient au tour suivant)
            _record_observation(_tool, _aid, message[:500], _res, run_id, _tcall_id,
                                model_id="LFM2.5-VL-3B" if _tool == "vision" else "GLM-OCR")
            if _res.get("ok"):
                deterministic_cache[(_tool, _aid)] = _res
        if _plan:
            _names = ", ".join(f"{p['tool']}({p['artifact_id'][:16]})" for p in _plan)
            emit("execution_plan", {"request_id": request_id, "attachment_kind": (attachment_registry[0].get("kind") if attachment_registry else None), "intent": "media_available", "selected_tool": _plan[0]["tool"], "selected_model": "LFM2.5-VL-3B" if _plan[0]["tool"] == "vision" else "GLM-OCR", "reason": f"Routage déterministe (function-calling 8B non fiable) : {_names}"})
    except Exception as e:
        log(f"[deterministic] pre-execution failed: {e}")

    try:
        # ========== Phase 1 : Orchestrateur (décision sémantique, pas mots-clés) ==========
        user_content = message
        if attachment_ids:
            user_content += f"\n\n[Fichiers joints — IDs durables annexe 10, utilise artifact_id pour vision/doc_ocr: {', '.join(attachment_ids)}]"
        elif image_paths:
            user_content += f"\n\n[Fichiers joints — compat image_paths déprécié, préfère attachment_ids: {', '.join(image_paths)}]"

        # Injection registre comme bloc système avant historique (spec n°8)
        registry_block = format_attachment_registry(attachment_registry)
        messages_orch: list[dict] = [{"role": "system", "content": SYSTEM_ORCHESTRATOR}, {"role": "system", "content": registry_block}] + history
        messages_orch.append({"role": "user", "content": user_content})

        orchestrator_done = False
        for iteration in range(MAX_ITER):
            if _check_cancel(cancel_flag):
                emit("run.cancelled", {"request_id": request_id, "run_id": run_id, "conversation_id": conv_id})
                break
            if time.perf_counter() - t0 > TOTAL_TIMEOUT:
                emit("error", {"request_id": request_id, "code": "total_timeout", "message": "300s dépassé"})
                break

            emit("thinking", {"request_id": request_id, "text": f"[Orchestrateur] Analyse de la demande... (itération {iteration+1})" if iteration == 0 else f"[Orchestrateur] Analyse des résultats d'outils... (itération {iteration+1})", "iteration": iteration})

            try:
                with manager.use_model("LFM2.5-8B", estimated_tokens=len(user_content) // 4 + 1024) as llm:
                    resp = llm.create_chat_completion(
                        messages=messages_orch,
                        tools=tool_defs if tool_defs else None,
                        tool_choice="auto" if tool_defs else None,
                        temperature=0.2,
                        max_tokens=2048,
                    )
            except Exception as e:
                emit("error", {"request_id": request_id, "code": "llm_error", "message": str(e)[:500]})
                break

            choice = resp["choices"][0]
            msg = choice.get("message", {})
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            # --- Sanitizer annexe 10 Lot 1 : jamais de [vision(...)] textuel, jamais de pensée privée diffusée ---
            if content and "[vision" in content.lower():
                log("[INVALID_DECISION] stripping [vision(...)] textuel (annexe 10)")
                content = _strip_textual_tool_calls(content)
            # Nettoie le contenu de toute fuite <think> / préfixe anglais — ne pas ré-émettre (C05)
            clean_content, extracted_thinking = _sanitize_content(content)
            if extracted_thinking:
                log(f"[sanitizer] thinking stripped ({len(extracted_thinking)} chars) — non diffusé (C05)")
                content = clean_content

            if not tool_calls:
                # Pas d'outil → brief = contenu nettoyé de l'orchestrateur (ou message si vide)
                brief = content.strip() if content.strip() else message
                orchestrator_done = True
                break

            # 1 seul tool par itération (séquentiel strict)
            tc = tool_calls[0]
            func = tc.get("function", tc)
            tool_name = func.get("name") or tc.get("name", "")
            args_raw = func.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except Exception:
                args = {"query": str(args_raw)}

            tool_call_id = tc.get("id") or f"call_{iteration}_{tool_name}"
            emit("tool_call", {"request_id": request_id, "tool_call_id": tool_call_id, "tool": tool_name, "arguments": args})

            # Exécution avec retry 1x (vision/doc_ocr/get_source/read_text)
            # Cache déterministe : si le LLM redemande un artefact déjà observé ce tour, réutilise
            result: dict | None = None
            _cache_key = None
            if tool_name in ("vision", "doc_ocr"):
                _aid = args.get("artifact_id") or args.get("artifactId") or ""
                if _aid:
                    _cache_key = (tool_name, _aid)
                    _cached = deterministic_cache.get(_cache_key)
                    if _cached is not None and _cached.get("ok"):
                        result = dict(_cached)
                        result["tool_call_id"] = tool_call_id
            for attempt in range(MAX_RETRY + 1):
                if result is not None:
                    break
                if tool_name == "vision":
                    result = _execute_vision(manager, {"args": args}, image_paths, request_id, attachment_ids)
                elif tool_name == "doc_ocr":
                    result = _execute_doc_ocr(manager, {"args": args}, request_id)
                elif tool_name == "get_source":
                    result = _execute_get_source({"args": args}, conv_id=conv_id)
                elif tool_name == "read_text":
                    result = _execute_read_text({"args": args})
                elif tool_name == "search_documents":
                    result = _execute_search_documents({"args": args}, conv_id=conv_id)
                elif tool_name == "get_document_card":
                    result = _execute_document_card({"args": args}, conv_id=conv_id)
                elif tool_name == "read_document":
                    result = _execute_read_document({"args": args}, conv_id=conv_id)
                else:
                    result = {"ok": False, "result": f"[Outil inconnu: {tool_name}]", "latency_ms": 0}
                if result["ok"] or attempt >= MAX_RETRY:
                    break
                if "timeout" in result["result"].lower():
                    time.sleep(1)
                else:
                    break

            assert result is not None
            emit("tool_result", {"request_id": request_id, "tool_call_id": tool_call_id, "result": result["result"][:5000], "ok": result["ok"], "latency_ms": result["latency_ms"]})

            if not result["ok"]:
                messages_orch.append({"role": "assistant", "content": f"[Outil {tool_name} a échoué: {result['result'][:200]}]"})
            messages_orch.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
            messages_orch.append({"role": "tool", "tool_call_id": tool_call_id, "content": result["result"]})
            tool_results.append({"tool": tool_name, "result": result["result"], "ok": result["ok"], "tool_call_id": tool_call_id})
            tool_observations.append(_format_tool_observation(tool_name, result, tool_name))
            # Annexe 11 Lot A : Observation persistée + fiche N+1 (hors search : pas d'analyse mono-fichier)
            # Lot B : get_document_card n'enregistre pas (ouverture de fiche, pas travail) ; read_document oui.
            try:
                if tool_name != "get_document_card":
                    _aid1 = (args.get("artifact_id") or args.get("artifactId")
                             or args.get("source_id") or "")
                    _record_observation(tool_name, _aid1, args.get("query") or args.get("purpose") or message[:500],
                                        result, run_id, tool_call_id)
            except Exception as e:
                log(f"[observation] phase1 record failed: {e}")

            if len(tool_results) >= MAX_ITER:
                brief = content.strip() if content.strip() else f"Synthèse nécessaire pour : {message}"
                orchestrator_done = True
                break

            # Si l'orchestrateur a déjà produit un brief textuel avec tool_calls, on continue
            # sinon on boucle pour qu'il décide s'il a assez d'info

        if not orchestrator_done and not brief.strip():
            brief = message

        # ========== Phase 2 : Générateur (JAMAIS de tools) ==========
        emit("thinking", {"request_id": request_id, "text": "[Générateur] Synthèse de la réponse finale...", "iteration": 99})

        with manager.use_model("LFM2.5-8B", estimated_tokens=len(brief) // 4 + 2048) as gen_llm:
            prepared = _prepare_tool_results(tool_results, gen_llm) if tool_results else []
            # Observations structurées (priorité) + fallback JSON préparé
            obs_block = "\n\n".join(tool_observations) if tool_observations else ""
            if not obs_block and prepared:
                obs_block = json.dumps(prepared, ensure_ascii=False)[:12000]

            gen_messages: list[dict] = [{"role": "system", "content": SYSTEM_GENERATOR}] + history
            if obs_block:
                gen_messages.append({"role": "system", "content": f"Observations d'outils:\n{obs_block}"})
            if brief != message:
                gen_messages.append({"role": "system", "content": f"Brief de l'orchestrateur: {brief}"})
            gen_messages.append({"role": "user", "content": message})

            try:
                resp_gen = gen_llm.create_chat_completion(
                    messages=gen_messages,
                    temperature=0.2,
                    max_tokens=8192,
                )
                collected = resp_gen["choices"][0]["message"]["content"] or ""
                # Sanitizer backend aussi côté générateur : strip <think> + préfixe anglais
                if collected:
                    clean_gen, think_gen = _sanitize_content(collected)
                    if think_gen:
                        emit("thinking", {"request_id": request_id, "text": think_gen, "iteration": 99})
                        collected = clean_gen
                    else:
                        collected = clean_gen
                    # Si le générateur a fuité un tool call textuel, le strip
                    if "[vision" in collected or "[doc_ocr" in collected:
                        collected = _strip_textual_tool_calls(collected)
                if not collected.strip():
                    collected = "Je n'ai pas pu générer de réponse."
            except Exception as e:
                emit("error", {"request_id": request_id, "code": "generator_error", "message": str(e)[:500]})
                collected = ""

            # Streaming simulé : on émet en une fois (Phase 1), le front gère l'affichage
            # Pour un vrai streaming, découper en chunks
            if collected:
                # Émet par morceaux pour l'effet streaming côté front
                chunk_size = 400
                for i in range(0, len(collected), chunk_size):
                    chunk = collected[i:i+chunk_size]
                    emit("answer", {"request_id": request_id, "text": chunk})
                    # Petit délai pour ne pas saturer
                    time.sleep(0.01)

        emit("message_completed", {"request_id": request_id, "run_id": run_id, "tokens_in": len(message) // 4, "tokens_out": len(collected) // 4, "total_latency_ms": int((time.perf_counter() - t0) * 1000)})
        return collected

    except Exception as e:
        emit("error", {"request_id": request_id, "code": "loop_error", "message": str(e)[:800]})
        return ""
    finally:
        cleanup_request(request_id)

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

MAX_VISION_SIDE = 1536

def prepare_vision_image(input_path: Path, output_path: Path) -> Path:
    """Prétraite image pour VL : EXIF transpose + max 1536px + JPEG 88 si besoin."""
    try:
        from PIL import Image, ImageOps
        image = Image.open(input_path)
        image = ImageOps.exif_transpose(image)
        # thumbnail garde ratio, ne grossit pas si plus petit
        image.thumbnail((MAX_VISION_SIDE, MAX_VISION_SIDE))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # JPEG 88 est plus léger que PNG pour VL, qualité suffisante
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
    """Détecte [vision(image_path="...")] ou [doc_ocr(pdf_path="...")] textuels → tool_calls structurés (PR #24178 fallback)."""
    calls = []
    for m in _TEXTUAL_TOOL_RE.finditer(content):
        tool = m.group(1).lower()
        path = m.group(2)
        args = {"image_path": path} if tool == "vision" else {"pdf_path": path}
        # extrait query si présent dans même parenthèse (query="...")
        qm = re.search(r"query\s*=\s*[\"']([^\"']+)[\"']", m.group(0), re.IGNORECASE)
        if qm:
            args["query"] = qm.group(1)
        calls.append({"id": f"textual_{tool}_{len(calls)}", "function": {"name": tool, "arguments": json.dumps(args)}, "name": tool})
    return calls


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
    text = message.lower()
    ocr_terms = ("ocr","extrais le texte","extraire le texte","lis le texte","lire le texte","transcris","transcrire","texte sur l'image","texte dans l'image","quel est le texte","que dit ce document","tableau","facture","scan")
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

def requires_media_reinspection(message: str) -> bool:
    text = message.lower()
    reference_terms = ("cette image","cette photo","la première image","la seconde image","la deuxième image","l'image précédente","l'image d'avant","celle-ci","celle-là","dans l'image","sur l'image","dans la photo","dans le document","vérifie","regarde à nouveau","relis","réanalyse","première image","deuxième image")
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
            # Résout chemin absolu depuis relative_path si présent
            if target.relative_path:
                media_root = __import__("pathlib").Path.home() / ".local" / "share" / "thot"
                abs_path = media_root / target.relative_path
                if abs_path.exists():
                    return str(abs_path), target.original_name or target.path
            if target.path and __import__("pathlib").Path(target.path).exists():
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
                        media_root = __import__("pathlib").Path.home() / ".local" / "share" / "thot"
                        abs_available = (media_root / s.relative_path).exists()
                    elif s.path:
                        abs_available = __import__("pathlib").Path(s.path).exists()
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

def format_attachment_registry(registry: list[dict]) -> str:
    if not registry:
        return "Aucun fichier dans cette conversation."
    # Manifest compact per spec n°10
    lines = ["SOURCES DISPONIBLES DANS CETTE CONVERSATION"]
    for r in registry:
        prev = r.get("previous_analyses") or []
        prev_str = f"Analyse précédente : {prev[0]['tool']} {prev[0]['status']} — {prev[0]['summary']}" if prev else "Analyse précédente : aucune"
        lines.append(f" [{r.get('ordinal')}] source_id={r.get('id')} Type : {r.get('kind')} Nom : {r.get('name')} Tour d'origine : {r.get('message_index')} Disponible : {'oui' if r.get('available') else 'non'} {prev_str} Résumé : {prev[0]['summary'] if prev else ''}".strip())
    return "\n".join(lines) + "\n\n" + json.dumps([{"id": r["id"], "ordinal": r["ordinal"], "kind": r["kind"], "name": r["name"], "message_index": r["message_index"], "available": r["available"], "previous_analyses": r["previous_analyses"]} for r in registry], ensure_ascii=False, indent=2)


def _execute_vision(manager, tool_call: dict, image_paths: list[str] | None, request_id: str) -> dict:
    """Exécute vision séquentiel via ModelManager (1 image, 32K, dense) — valide image_path vs image_paths fournis."""
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        query = args.get("query", "")
        requested = args.get("image_path") or ""
        # Validation : si le LLM hallucine un chemin hors /tmp/thot ou hors image_paths, fallback sur le vrai fichier fourni
        # Évite la hallucination vue dans conv.png : image_path='/tmp/thot/bea62abc-...png' flat qui n'existe pas
        valid_paths = [p for p in (image_paths or []) if p and Path(p).exists()]
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
        pdf_path = args.get("pdf_path") or args.get("image_path", "")
        query = args.get("query", "")
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

def _execute_get_source(tool_call: dict) -> dict:
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        source_id = args.get("source_id")
        if not source_id:
            return {"ok": False, "result": "[get_source: source_id manquant]", "latency_ms": int((time.perf_counter() - start)*1000)}
        from backend.db.database import SessionLocal
        from backend.db.models import Source
        db = SessionLocal()
        try:
            src = db.get(Source, source_id)
            if not src:
                return {"ok": False, "result": f"[get_source: {source_id} introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            working_path = None
            if src.relative_path:
                media_root = Path.home() / ".local" / "share" / "thot"
                cand = media_root / src.relative_path
                if cand.exists():
                    working_path = str(cand)
            if not working_path and src.path and Path(src.path).exists():
                working_path = src.path
            if not working_path:
                return {"ok": False, "result": "[get_source: working_path introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            return {"ok": True, "result": f'{{"source_id":"{src.id}","working_path":"{working_path}","media_kind":"{src.media_kind}","name":"{src.original_name}"}}', "latency_ms": int((time.perf_counter() - start)*1000)}
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
        db = SessionLocal()
        try:
            src = db.get(Source, source_id)
            if not src:
                return {"ok": False, "result": f"[read_text: {source_id} introuvable]", "latency_ms": int((time.perf_counter() - start)*1000)}
            working_path = None
            if src.relative_path:
                media_root = Path.home() / ".local" / "share" / "thot"
                cand = media_root / src.relative_path
                if cand.exists():
                    working_path = str(cand)
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


def run_agent_loop(
    manager,
    conv_id: str,
    request_id: str,
    message: str,
    mode: str,
    image_paths: list[str] | None,
    history_shared: list[dict],
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

    # Registre d'images pour l'orchestrateur (Réponses.md n°8) — remplace routeur mots-clés
    attachment_registry = build_attachment_registry(conv_id, image_paths)
    has_any_media = bool(attachment_registry)
    registry = get_default_registry()
    tool_defs = registry.definitions() if has_any_media else []
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

    try:
        # ========== Phase 1 : Orchestrateur (décision sémantique, pas mots-clés) ==========
        user_content = message
        if image_paths:
            user_content += f"\n\n[Fichiers joints — utilise EXACTEMENT ces chemins pour vision/doc_ocr, ne les invente pas: {', '.join(image_paths)}]"

        # Injection registre comme bloc système avant historique (spec n°8)
        registry_block = format_attachment_registry(attachment_registry)
        messages_orch: list[dict] = [{"role": "system", "content": SYSTEM_ORCHESTRATOR}, {"role": "system", "content": registry_block}] + history
        messages_orch.append({"role": "user", "content": user_content})

        orchestrator_done = False
        for iteration in range(MAX_ITER):
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

            # --- Sanitizer backend (ticket #23852) : strip <think> et détection tool textuel ---
            textual_calls = _detect_textual_tool_calls(content) if content else []
            if textual_calls and not tool_calls:
                # Le modèle a émis [vision(...)] en texte au lieu de tool_calls structurés → convertir
                tool_calls = textual_calls
                content = _strip_textual_tool_calls(content)
            # Nettoie le contenu de toute fuite <think> / préfixe anglais
            clean_content, extracted_thinking = _sanitize_content(content)
            if extracted_thinking:
                # Émet la pensée extraite comme thinking pour ne pas la perdre
                emit("thinking", {"request_id": request_id, "text": extracted_thinking, "iteration": iteration})
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
            result: dict | None = None
            for attempt in range(MAX_RETRY + 1):
                if tool_name == "vision":
                    result = _execute_vision(manager, {"args": args}, image_paths, request_id)
                elif tool_name == "doc_ocr":
                    result = _execute_doc_ocr(manager, {"args": args}, request_id)
                elif tool_name == "get_source":
                    result = _execute_get_source({"args": args})
                elif tool_name == "read_text":
                    result = _execute_read_text({"args": args})
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

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

from backend.inference.ipc import emit

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
from backend.prompts.ocr import SYSTEM_OCR, build_system_ocr
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
        # Encode en base64 data URL pour garantir que VL voit l'image (file:// non supporté par tous les templates)
        try:
            with open(img, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            # Détecte MIME via suffix
            mime = "image/png" if Path(img).suffix.lower() in (".png",) else "image/jpeg" if Path(img).suffix.lower() in (".jpg", ".jpeg") else "image/webp"
            data_url = f"data:{mime};base64,{b64}"
        except Exception as e:
            return {"ok": False, "result": f"[Erreur vision: lecture image {e}]", "latency_ms": int((time.perf_counter() - start) * 1000)}
        with manager.use_model("LFM2.5-VL-3B") as vlm:
            r = vlm.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEM_VISION.format(query=query)},
                    {"role": "user", "content": [{"type": "text", "text": query or "Décris cette image en détail."}, {"type": "image_url", "image_url": {"url": data_url}}]},
                ],
                temperature=0.2,
                max_tokens=2048,
            )
            text = r["choices"][0]["message"]["content"]
            return {"ok": True, "result": text, "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "result": f"[Erreur vision: {e}]", "latency_ms": int((time.perf_counter() - start) * 1000)}


def _execute_doc_ocr(manager, tool_call: dict, request_id: str) -> dict:
    """Exécute doc_ocr page par page séquentiel via PyMuPDF 200 DPI (évite KV blowup 384MB/page)."""
    start = time.perf_counter()
    try:
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        pdf_path = args.get("pdf_path") or args.get("image_path", "")
        query = args.get("query", "")
        # Détection de tables dans la requête pour marqueur GLM-OCR approprié
        request_table = any(kw in query.lower() for kw in ("tableau", "table ", "table?", "tabuler", "grille", "csv"))
        system_prompt = build_system_ocr(query, request_table)
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

    registry = get_default_registry()
    # Chat = pas d'outils par défaut, Agent ou image jointe = 2 outils vision
    if mode == "agent" or (image_paths and len(image_paths) > 0):
        tool_defs = registry.definitions()  # vision + doc_ocr
    else:
        tool_defs = []

    tool_results: list[dict] = []
    brief: str = message  # fallback si pas de brief orchestré

    try:
        # ========== Phase 1 : Orchestrateur ==========
        # Construit le message utilisateur avec hint fichiers si présents — chemins EXACTS pour éviter hallucination flat (conv.png)
        user_content = message
        if image_paths:
            # Important : l'orchestrateur DOIT utiliser EXACTEMENT ces chemins, ne jamais en inventer
            user_content += f"\n\n[Fichiers joints — utilise EXACTEMENT ces chemins pour vision/doc_ocr, ne les invente pas: {', '.join(image_paths)}]"

        messages_orch: list[dict] = [{"role": "system", "content": SYSTEM_ORCHESTRATOR}] + history
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

            # Exécution avec retry 1x
            result: dict | None = None
            for attempt in range(MAX_RETRY + 1):
                if tool_name == "vision":
                    result = _execute_vision(manager, {"args": args}, image_paths, request_id)
                elif tool_name == "doc_ocr":
                    result = _execute_doc_ocr(manager, {"args": args}, request_id)
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

            gen_messages: list[dict] = [{"role": "system", "content": SYSTEM_GENERATOR}] + history
            if prepared:
                # Brief + résultats préparés (tronqués/résumés si >8K)
                gen_messages.append({"role": "system", "content": f"Brief de l'orchestrateur: {brief}"})
                gen_messages.append({"role": "system", "content": f"Résultats d'outils:\n{json.dumps(prepared, ensure_ascii=False)[:12000]}"})
                gen_messages.append({"role": "user", "content": message})
            else:
                if brief != message:
                    gen_messages.append({"role": "system", "content": f"Brief: {brief}"})
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

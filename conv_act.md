# conv_act.md — Fonctionnement actuel de la conversation (Phase 1)

> **État observé : `Futur/archive/conv.png` du 31/08** — message utilisateur avec image `Décrit moi cette image.` → 2 blocs : `Générateur — Réflexion` contenant à la fois `[Orchestrateur]` et `[Générateur]` + anglais interne, puis `[vision(image_path='/tmp/...')]` en texte brut. Ce document décrit le fonctionnement **réel implémenté** et le **problème** à corriger.

---

## 1. Vue d'ensemble Phase 1

**Périmètre :** `Thot-phase-1/` standalone, chat seul `chat`/`agent`, 2 outils vision uniquement : `vision` (LFM2.5-VL-3B, 32K, dense) et `doc_ocr` (GLM-OCR, 8K, Q4_K_M) via hash+path `/tmp/thot/{request_id}/{hash}.ext`. Modèles texte pur LFM2.5-8B (8.3B total, 1.5B actifs, MoE 32 experts, 128K) **séquentiel strict** < 7.5 GB.

**2 couches :**
- **Orchestrateur** (`LFM2.5-8B`) : décide `DIRECT_ANSWER` ou `TOOL_CALL`, peut appeler `vision/doc_ocr` max 5 iter séquentiel, produit `brief`.
- **Générateur** (`LFM2.5-8B`) : `NO tools`, reçoit `history_shared + brief + résultats préparés`, produit réponse finale streaming.

**Langue :** `Réponds dans la langue de l'utilisateur` (`prompts/orchestrator.py:5`, `generator.py:5`).

---

## 2. Schéma d'architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Frontend React 19 (Vite :1420)                                  │
│  ChatConversation.tsx  Composer.tsx (trombone + drag-drop)      │
│  ToolCallDisplay.tsx  HistoryPage.tsx                           │
│              ↕ invoke("chat_send") / listen(event)               │
│ Backend Rust Tauri (src-tauri/src/lib.rs)                       │
│  save_upload(requestId, filename, Vec<u8>) → /tmp/thot/...     │
│  chat_send(requestId, message, mode, imagePaths, convId) → stdin│
│              ↕ JSON Lines (NDJSON) sur stdin/stdout             │
│ Backend Python subprocess (backend/main.py)                     │
│  Manager séquentiel  Loop 2 couches  Tools  Prompts  DB         │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 Flux IPC (pas de HTTP, pas de SSE)

```
Frontend                     Rust                          Python
  |                           |                              |
  |---invoke(chat_send {       |                              |
  |    requestId, message,     |---write stdin JSON---------->|  main.py: ligne JSON
  |    mode, imagePaths,       |    {"id":requestId,          |  {"method":"chat.send",...}
  |    convId})-------------->|     "method":"chat.send",     |
  |                           |     "params":{...}}           |
  |                           |                              |--> run_agent_loop()
  |                           |<--stdout JSON Lines-----------|      emit() chaque ligne
  |                           |  {"event":"message_started", |      {"event":...,"payload":...}
  |                           |   "payload":{requestId...}}  |
  |<--window.emit(event)------|  BufReader::lines()          |
  |  listen(thinking)         |  → serde_json → window.emit |
  |  listen(tool_call/result) |                              |
  |  listen(answer)           |                              |
  |  listen(message_completed)|                              |
```

**7 events** : `message_started {requestId,runId,model}`, `thinking {requestId,text,iteration}`, `tool_call {requestId,tool_call_id,tool,arguments}`, `tool_result {requestId,tool_call_id,result,ok,latency_ms}`, `answer {requestId,text}`, `message_completed {requestId,runId,tokens_in,out,latency}`, `error {requestId,code,message}` + `backend_ready {backend,vram,n_gpu_layers}`.

---

## 3. Pseudo-code détaillé

### 3.1 Frontend — dépôt image (trombone / drag-drop)

```ts
// Composer.tsx — trombone + drag-drop dans la barre dockée

const [droppedFiles, setDroppedFiles] = useState<File[]>([]);
const [isDragging, setIsDragging] = useState(false);

// Trombone
<button onClick={() => fileInputRef.current.click()}><Attach /></button>
<input ref={fileInputRef} type=file accept="image/png, image/jpeg, image/webp, application/pdf" multiple hidden onChange={handleFileInput} />

// Drag-drop barre dockée
<div onDragEnter={handleDragEnter} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop}
     style={isDragging ? {border:"1px dashed var(--thot-gold)", bg:"gold-soft"} : {}}>
  {isDragging && <Overlay>Déposez l'image ou le PDF ici</Overlay>}
  <textarea value={message} onChange={...} />
</div>

function handleDragEnter(e) { e.preventDefault(); dragCounter++; setIsDragging(true); }
function handleDragOver(e)  { e.preventDefault(); e.dataTransfer.dropEffect="copy"; }
function handleDrop(e) {
  e.preventDefault(); setIsDragging(false);
  const files = e.dataTransfer.files.length ? e.dataTransfer.files : items.getAsFile();
  processFiles(files); // filtre: isImage→5MB, isPdf→20MB, max 4
  // preview: URL.createObjectURL(image) → 72x72, PDF → 📄
}

function handleSend() {
  onSend(message, droppedFiles.map(d=>d.file)); // → ChatConversation
}
```

```ts
// ChatConversation.tsx — handleSend

async function handleSend(message, files) {
  const requestId = crypto.randomUUID(); // corrèle tout le cycle
  currentRequestIdRef.current = requestId;
  setChatState('sending');

  // preview éphémère
  const previewUrls = files.filter(f=>f.type.startsWith('image/')).map(f=>URL.createObjectURL(f));
  setMessages(prev=> [...prev, {id:`user-${Date.now()}`, role:"user", content:message, images:previewUrls}]);
  setMessages(prev=> [...prev, {id:`assistant-${Date.now()+1}`, role:"assistant", content:"", thinking:undefined}]); // placeholder "Préparation…"

  // upload Rust hash+path (jamais base64)
  let image_paths;
  if (files.length) {
    image_paths = [];
    for (const file of files) {
      const buf = new Uint8Array(await file.arrayBuffer());
      const [path, hash] = await saveUpload(requestId, file.name, buf); // invoke("save_upload",{requestId,filename,data})
      // Rust: magic 89PNG/FFD8FF/%PDF, 5MB/20MB, Path(tmp/thot/requestId/hash.ext), resolve().startsWith(THOT_TMP)
      image_paths.push(path);
    }
  }
  await chatSend({requestId, message, mode, imagePaths:image_paths, convId:id}); // camelCase → Rust snake_case
  setTimeout(()=> currentRequestIdRef.current===requestId && setChatState('streaming'), 600);
}
```

Listeners (7 events, filtrés par `requestId`) :

```ts
onMessageStarted(p => p.requestId===current && setChatState('streaming'));
onThinking(p    => setMessages(last=> last.role==="assistant" ? {...last, thinking:(last.thinking||"")+p.text} : newAssistant));
onToolCall(p    => setMessages(last=> {...last, toolCalls:[...last.toolCalls, {tool:p.tool, arguments:p.arguments, status:"running"}]}));
onToolResult(p  => setMessages(last=> {...last, toolCalls: last.toolCalls.map(t=> t.id===p.tool_call_id ? {status:p.ok?"completed":"error", result:p.result, latency_ms:p.latency_ms} : t)}));
onAnswer(p      => {
  const {thinking,clean}=extractThinkingLeak(p.text); // strip <think>
  if(thinking) setMessages(last=> {...last, thinking: (last.thinking||"")+thinking});
  if(clean)    setMessages(last=> {...last, content: last.content+clean});
});
onMessageCompleted(p => {setChatState('idle'); currentRequestIdRef=null; showToast("Réponse terminée")});
```

`ToolCallDisplay.tsx` : `running` → spinner `🔍 Analyse image…` / `📄 OCR…`, `completed` → `✅ vision terminé (N chars)` collapsible `<pre>preview 500 chars</pre>`, `error` → `❌`.

---

### 3.2 Backend — boucle 2 couches (`backend/inference/loop.py`)

```python
# Pseudo-code simplifié (réel : backend/inference/loop.py:128-307)

def run_agent_loop(manager, conv_id, request_id, message, mode, image_paths, history_shared):
    if not message.strip(): emit("error", code="empty_message"); return ""
    run_id = request_id; t0 = perf_counter()
    emit("message_started", {requestId, runId, model:"LFM2.5-8B"})

    history = list(history_shared)  # user↔assistant seuls, sans tool traces passées
    registry = get_default_registry() # vision + doc_ocr
    tool_defs = registry.definitions() if (mode=="agent" or image_paths) else []

    tool_results = []
    brief = message

    # ---------- Phase Orchestrateur ----------
    user_content = message + (f"\n\n[Fichiers joints: {','.join(image_paths)}]" if image_paths else "")
    messages_orch = [{"role":"system","content":SYSTEM_ORCHESTRATOR}] + history + [{"role":"user","content":user_content}]

    for iteration in range(5):
        if perf_counter()-t0 > 300: emit("error", total_timeout); break
        emit("thinking", {requestId, text: f"[Orchestrateur] Analyse... (itération {iteration+1})", iteration})

        with manager.use_model("LFM2.5-8B", est_tokens=len(user_content)//4) as llm:  # séquentiel strict, décharge précédent
            resp = llm.create_chat_completion(
                messages=messages_orch,
                tools=tool_defs or None, tool_choice="auto" if tool_defs else None,
                temperature=0.2, max_tokens=2048
            )
        content = resp["choices"][0]["message"]["content"] or ""
        tool_calls = resp["choices"][0]["message"].get("tool_calls") or []

        if not tool_calls:
            brief = content.strip() or message
            break  # DIRECT_ANSWER → brief prêt

        tc = tool_calls[0]  # 1 tool / itération
        tool_name = tc["function"]["name"]; args = json.loads(tc["function"]["arguments"])
        tool_call_id = tc.get("id") or f"call_{iteration}_{tool_name}"
        emit("tool_call", {requestId, tool_call_id, tool:tool_name, arguments:args})

        # Exécution séquentielle avec retry 1x, décharge 8B → charge VL/OCR → décharge
        result = None
        for attempt in range(2):
            if tool_name=="vision":   result = _execute_vision(manager, {"args":args}, image_paths, requestId) # LFM2.5-VL-3B, SYSTEM_VISION, file://img
            elif tool_name=="doc_ocr": result = _execute_doc_ocr(manager, {"args":args}, requestId)             # GLM-OCR, PyMuPDF 200DPI page par page, max 50
            else: result = {"ok":False, "result":f"[Outil inconnu]"}
            if result["ok"] or attempt==1: break
            if "timeout" in result["result"]: sleep(1)

        emit("tool_result", {requestId, tool_call_id, result:result["result"][:5000], ok:result["ok"], latency_ms=result["latency_ms"]})

        # Double historique : tool résultat uniquement tour en cours, pas réinjecté futur
        if not result["ok"]:
            messages_orch.append({"role":"assistant","content":f"[Outil {tool_name} a échoué: {result['result'][:200]}]"})
        messages_orch.append({"role":"assistant","content":content, "tool_calls":tool_calls})
        messages_orch.append({"role":"tool","tool_call_id":tool_call_id,"content":result["result"]})
        tool_results.append({"tool":tool_name, "result":result["result"], "ok":result["ok"], "tool_call_id":tool_call_id})

    # ---------- Phase Générateur (JAMAIS tools) ----------
    emit("thinking", {requestId, text:"[Générateur] Synthèse de la réponse finale...", iteration:99})

    with manager.use_model("LFM2.5-8B") as gen_llm:
        prepared = _prepare_tool_results(tool_results, gen_llm) # ≤8K brut, 8-16K tronqué, >16K résumé 500 mots LLM
        gen_messages = [{"role":"system","content":SYSTEM_GENERATOR}] + history
        if prepared:
            gen_messages += [{"role":"system","content":f"Brief de l'orchestrateur: {brief}"},
                             {"role":"system","content":f"Résultats d'outils:\n{json.dumps(prepared)[:12000]}"},
                             {"role":"user","content":message}]
        else:
            if brief!=message: gen_messages.append({"role":"system","content":f"Brief: {brief}"})
            gen_messages.append({"role":"user","content":message})

        resp_gen = gen_llm.create_chat_completion(messages=gen_messages, temperature=0.2, max_tokens=8192)
        collected = resp_gen["choices"][0]["message"]["content"] or "Je n'ai pas pu générer de réponse."

        for chunk in chunks(collected, 400):  # streaming simulé
            emit("answer", {requestId, text:chunk}); sleep(0.01)

    emit("message_completed", {requestId, runId, tokens_in=len(message)//4, tokens_out=len(collected)//4, total_latency_ms=...})
    return collected
    finally: cleanup_request(requestId) # shutil.rmtree /tmp/thot/{requestId}
```

**Préparation résultats** (`_prepare_tool_results`) : `tokens=len(result)//4` → `≤8000` brut, `8000-16000` tronqué `[Tronqué]`, `>16000` résumé `create_completion("Résume en 500 mots...")`.

**Outils**

```python
# _execute_vision
with manager.use_model("LFM2.5-VL-3B") as vlm:  # 32K, dense, SigLIP2
    vlm.create_chat_completion(
        messages=[{"role":"system","content":SYSTEM_VISION.format(query)}, 
                  {"role":"user","content":[{"type":"image_url","image_url":{"url":f"file://{img}"}}]}],
        temperature=0.2, max_tokens=2048)

# _execute_doc_ocr
if ext in (png,jpg): avec GLM-OCR une page
else: fitz.open(pdf) → for page in doc[:50]: pix=page.get_pixmap(dpi=200); pix.save(/tmp/thot/{requestId}/page_{i}.png)
      avec GLM-OCR par page → join("\n\n--- Page suivante ---\n\n")
```

**Gestion mémoire** (`manager.py:66`) : `use_model(name)` → si `current != name` → `_unload_current()` (`del + gc.collect() + torch.cuda.empty_cache()`) → `_load_model` (`Llama(use_mmap=True, flash_attn=True, n_ctx=resolve_n_ctx(free_vram), n_keep=512, type_k=8)`) → fallback `flash_attn=False` si erreur flash, fallback `estimate_layers` si OOM. `resolve_n_ctx` : `131072` si `free_vram≥12GB` sinon `min(32768, max(4096, est+1024))`. Borne `<7.5GB` séquentiel.

**Double historique** (`conversations.py:188` `get_orchestrator_context`) : `history = [{"role":m.role,"content":m.content} for m in msgs if role in (user,assistant)]` → `thinking` et `tool` jamais inclus. `should_compact>85% (109K/128K)` → `prune_old_tool_results` → `compact_history` (split 15K récents, résumé `SYSTEM_COMPACTION` `max 2048 temp0.3` → `[Résumé]:... + recent`).

---

## 4. Schéma séquence complet (avec image)

```
Utilisateur tape "Décrit moi cette image." + drop halo.png
    │
    ├─ Composer: processFiles → preview 72x72, droppedFiles=[halo.png]
    ├─ ChatConversation.handleSend → saveUpload(requestId, halo.png) → Rust → /tmp/thot/{requestId}/bea62abc.png (hash sha256)
    ├─ chatSend({requestId, message, mode:"chat", imagePaths:["/tmp/...png"], convId})
    │
Rust lib.rs: chat_send → payload {"id":requestId,"method":"chat.send","params":{message,mode,image_paths,conv_id}} → stdin Python
    │
Python main.py: for line in stdin → json.loads → get_orchestrator_context(conv_id) → history_shared
                → run_agent_loop(manager, conv_id, requestId, message, mode, image_paths, history_shared)
    │
    ├─ emit message_started
    ├─ [Orchestrateur it1] emit thinking "[Orchestrateur] Analyse..."
    │   with LFM2.5-8B → tool_calls=[{name:vision, arguments:{query:"Décrit moi cette image.", image_path:"/tmp/...png"}}]
    │   emit tool_call
    │   _execute_vision → unload 8B → load LFM2.5-VL-3B → vision → result="Figure encapuchonnée halo rouge..."
    │   emit tool_result {ok:true, result}
    │   messages_orch += [assistant tool_calls, tool result]
    │
    ├─ [Orchestrateur it2] emit thinking
    │   with LFM2.5-8B (reload) → pas de tool_calls → brief="Décrire la figure halo rouge, ambiance sombre..."
    │
    ├─ emit thinking "[Générateur] Synthèse..."
    │   with LFM2.5-8B → gen_messages = [SYSTEM_GENERATOR] + history + [Brief: ...] + [Résultats vision] + [user]
    │   → collected="Cette image montre une silhouette encapuchonnée..."
    │   for chunk in collected: emit answer {text: chunk}
    │
    ├─ emit message_completed
    └─ cleanup_request → rmtree /tmp/thot/{requestId}
    │
Rust: BufReader::lines() → window.emit(answer) → Frontend onAnswer → append content
Frontend: onToolCall → ToolCallDisplay running → onToolResult → completed ✅ → thinking "[Orchestrateur]...[Générateur]..." dans collapsible → answer streaming → placeholder "Génération en cours…" → "Cette image montre..."
```

---

## 5. Problème observé dans `conv.png` et correction

**Observé :**
- Bloc `Générateur — Réflexion` contient à la fois `[Orchestrateur] Analyse...` et `[Générateur] Synthèse...` + fuite anglaise `The user asks: "Décrit moi..." They want a description... We have not yet called the tool...` (chain-of-thought LFM en anglais, censé rester dans `<think>`).
- Bloc suivant `[vision(image_path='/tmp/...')]` affiché comme **texte brut** dans une bulle `Copier`, pas comme `ToolCallDisplay` (spinner → ✅).
- Carte réponse affiche `[vision(...)]` au lieu de la description.

**Cause :**
- `loop.py` concatène les deux `thinking` dans le même `msg.thinking` (`ChatConversation.tsx:139` append) sans séparation → label unique `Générateur` trompeur.
- `onAnswer` fuite `extractThinkingLeak` ne strip pas toujours le `The user asks...` si pas de `<think>` → `msg.content` reçoit le raisonnement + `[vision(...)]` comme texte.
- Frontend `ChatConversation.tsx:448` placeholder `En attente…` non phase-aware.

**Correction appliquée (31/08) :**
- `loop.py:184,260` prefixe explicite `[Orchestrateur]` / `[Générateur]` + `ChatConversation.tsx:431` label `Orchestrateur — Réflexion` vs `Générateur — Réflexion` selon contenu.
- Placeholder phase-aware `ChatConversation.tsx:448` : `sending ? "Préparation de la demande…" : streaming ? "Génération en cours…" : "En attente…"`.
- `extractThinkingLeak` + `stripThinkingLeak` gère `The user asks...` + `<think>` → `thinking` vs `clean` séparés, `ToolCallDisplay` reste la seule source pour `vision`.

**Attendu après correction :** Un seul collapsible `Orchestrateur — Réflexion` pendant it1, `ToolCallDisplay` `🔍 Analyse image en cours… → ✅ vision terminé`, puis `Générateur — Réflexion` + réponse `Cette image montre une silhouette...` en français, sans fuite.

---

*Fichier généré pour reprise : `Thot-phase-1/conv_act.md` — pseudo-code conforme au code réel au 31/08/2026.*

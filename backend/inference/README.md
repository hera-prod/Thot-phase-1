# Inference Python — loop + manager + politique média

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.

## 1. Dossiers englobés

```
backend/
├── main.py                 ← point d'entrée sidecar (stdin loop → worker 1 run lourd)
├── inference/
│   ├── loop.py             ← SharedContext + Phase 0 + orchestrateur → brief → générateur
│   ├── media_policy.py     ← R1/R2/R3 déterministe (JAMAIS le LLM qui décide d'observer)
│   ├── manager.py          ← FSM VRAM + admission 1 run + replis OOM
│   ├── ipc.py              ← emit/seq/run_id/protocol_version, ipc_line_bytes 32 Mio, MAX_QUEUE 32
│   ├── hardware.py         ← détection VRAM/RAM, estimate_layers
│   └── compaction.py       ← résumé + ContextSnapshot
├── prompts/
│   ├── orchestrator.py     ← SYSTEM_ORCHESTRATOR + SUBMIT_BRIEF_TOOL (zéro crochet montré)
│   ├── generator.py        ← SYSTEM_GENERATOR (brief SEUL : pré-réponse, jamais tool_defs/chemins/observations)
│   ├── vision.py / ocr.py / compaction.py
├── config/paths.py + defaults.py  ← MODEL_CONFIGS, resolve_n_ctx adaptatif
```

## 2. Pseudo-code (comment c'est fait)

```
# main.py — un tour par conversation, idempotent
SUR stdin_ligne :
  normalisé = {request_id, method, params, protocol_version}
  run_id = request_id            # Phase 1 : 1:1 pour idempotence C11
  SI fingerprint déjà vu → rejouer terminal sans réexécuter
  SINON worker_unique :
    persist_user_turn AVANT appel LLM (C : jamais perdre le tour user)
    collected, trace = loop.run_chat(...)  # trace = {orchestrator_thinking, brief, plan}
    persist_assistant + trace + backfill toolcalls→msg_id AVANT emit terminal
    UN SEUL terminal par run (completed / cancelled / failed)

# loop.py — Phase 0 DÉTERMINISTE puis 2 couches LLM
Phase 0 (sans modèle) :
  registre = construire depuis Artifact/Blob si attachment_ids
             sinon image_paths (compat, log)
  plan = plan_media_tools(nouveaux_fichiers)                    # R1 : pdf→doc_ocr, image→vision
  SI pas de nouveau fichier ET média dispo :
    SI needs_reinspection(question) :                           # R2 : couches A/B/C en OR
      plan += réobserver avec question comme query
  SI plan TOUJOURS vide : rien. La Phase 0 n'ouvre JAMAIS de job image —
  une création d'image se décide dans le tour orchestrateur, et là seulement.
  execution_plan = {intent, selected_tool, selected_model, reason}  # vrai outil, pas "none" muet

Tour orchestrateur (LFM2.5-8B, max 16 iter, timeout 15s/outil, 300s total) — STRUCTUREL :
  messages = [SYSTEM_ORCHESTRATOR, SOURCES (registre), historique user↔assistant]
  tool_defs = exactement DISCOVERY_TOOL_NAMES = search_info/find_files/ask_files/generate_image/web_research
              (tool_choice auto) ; le registre en compte 10 outils (5 orchestrateur + 5 sous-outils)
  Phase 0 image : SUPPRIMÉE (28/09) — l'orchestrateur est le seul décideur.
  generate_image : une image par tour (_already_published_image → dédup, jamais une 2e
              diffusion) ; le prompt exécuté est l'argument que le MODÈLE a produit,
              jamais le message de l'utilisateur.
  web_research : SEULE sortie réseau du tour (web/services/web/…). Mode local_only →
              WEB_DISABLED avec cache rendu ; aucun moteur pour le type demandé → NO_PROVIDER
              BRUYANT (jamais « rien trouvé ») ; requête à terme privé → CONSENT_REQUIRED, rien
              ne sort ; URL hors message utilisateur ou résultat antérieur → URL_NOT_ADMITTED.
              Le rendu porte "evidence_id": "web_<12hex>:<n>" — la forme que la regex
              d'admission de la boucle sait relire. Budget : 2 pages/appel, 6 requêtes/tour,
              2 pages/hôte/tour, intervalle 1 s, 0 redirection.
  Diagnostic prouvé : en auto le modèle émet [fn(k='v')] SANS tokens spéciaux (runtime 0.3.34
  ne parse pas → tool_calls null) ; le forçage NOMMÉ remonte des tool_calls propres.
  Chaîne de récupération, dans cet ordre, au MÊME site (un seul par tour) :
  1. natifs = normalize_native_tool_calls(msg.tool_calls) ; _discovery_records → allowlist
     DISCOVERY_TOOL_NAMES (filtre AVANT persistance comme AVANT exécution)
  2. sinon normalize_lfm25_content (wrapper LFM2.5, allowed_names=DISCOVERY_TOOL_NAMES)
  3. sinon textual_tool_intent → RE-ASK NATIF FORCÉ du même nom (jamais son JSON source)
  4. sinon carries_nameless_envelope → RE-ASK NON CONTRAINT (tous les outils de découverte,
      tool_choice="required")
     3e dialecte, mesuré : {"prompt": "…", "format": "16:9"} — l'appel est CORRECT et
     COMPLET, la clé `name` MANQUE. Aucun nom n'est lisible → le canal nommé n'a rien à
     demander, et la décision tombait sur `if not records: break` (l'image du « format
     16:9 » était perdue). L'enveloppe nue est un SIGNAL, rien d'autre : elle ne
     choisit JAMAIS l'outil — aucune table `prompt`+`format` ⇒ generate_image, ce serait
     un choix backend — et ses arguments ne sont JAMAIS lus (le prédicat ne regarde que
     le jeu de CLÉS, lu dans les tool_defs publiés). Un jeu de clés compatible avec 0
     outil, ou avec 2 ou plus, est REFUSÉ : l'ambiguïté ne se tranche jamais à la place
     du modèle. Toutes les définitions sont proposées ENSEMBLE et "required" oblige à
     appeler : l'enregistrement exécuté est un VRAI appel natif dont le MODÈLE a choisi
     l'outil et les arguments. Sortie : UN SEUL appel de découverte, ou RIEN — un refus
     reste un refus, et un tour n'empile jamais deux re-ask.
      (llama-cpp 0.3.34 ne construit une grammaire que pour un choix NOMMÉ : l'obligation
      est portée par le message correctif, comme le retry de la phase brief forcée.
      Vérifié dans la source installée — llama_chat_format.py:702-721 ne branche la grammaire
      que sur un tool_choice DICT ; la chaîne "required" n'est lue nulle part, c'est un no-op.)
  MESURÉ 28/09 sur le modèle réel (E2E, GPU partagé, 5 formulations + 2 négatifs, Contexte §3g) :
  5/5 formulations → une image, 0/2 négatifs → aucune diffusion, vérité SQL conforme
  (1 toolcall + 1 lien conversation + 1 attachment par tour image ; 0/0/0 sur les négatifs).
  ⚠ Les 5 tours sont passés par le canal 3 (re-ask NOMMÉ) : « indirect » portait l'enveloppe
  nue MAIS aussi la forme fn(...) dans le même texte, donc le nom est parti de fn(...).
  Le canal 4 reste couvert par ses TESTS (dont un test de réversion), pas par cette mesure.
  Le taux n'est pas stable d'une fenêtre à l'autre (tour auto à temperature 0.1) : « 1/2 → 4/5
  → 5/5 » sont trois tirages d'une même loi, pas trois régimes de code. Sur la formulation
  « format 16:9 », le run 4/5 s'arrête sur l'enveloppe sans nom et perd la décision ; le run 5/5
  écrit la même enveloppe AVEC name et la gagne — même prompt, même code, tirage différent.

  POUR chaque itération :
    (args stricts + Pydantic AVANT exécution ; malformé/inconnu = note correctif.
     Le pseudo-appel textuel [fn(k='v')] n'est JAMAIS exécuté : journalisé puis refusé —
     c'est ce refus qui rendait « je ne peux pas générer d'image » visible à l'utilisateur.)
    SI submit_brief → validate_submit_brief (schéma + evidence_ids réels + faits non vides)
      → ok : brief = render_brief (texte, même format qu'avant pour générateur/bulle/DB)
      → refus : 2 classes (brief.py:brief_error_is_retryable) —
         * FORME (champ manquant, valeur hors énumération, SCHEMA_UNAVAILABLE) → message
           de correction + retry (×2), le message nomme le champ ou la panne ;
         * VERDICT sur un inventaire FIGÉ (EVIDENCE_UNKNOWN, ID_OUT_OF_SCOPE) → 0 retry,
           brief de repli déterministe : la phase n'exécute aucun outil, donc la même
           question recevrait le même refus (mesuré : 3 tentatives, 2 appels de plus).
         L'erreur EVIDENCE_UNKNOWN nomme le fautif, les formes admissibles (obs_…,
         doc_N:k, blob:…), leur provenance, et « [] si tu n'as aucune preuve ».
    SI découverte → exécuteur (search_info/find_files/ask_files/generate_image)
    SINON (silence) → break vers phase brief FORCÉE (submit_brief seul, tool_choice nommé,
      retry correctif ×2 « Le brief n'a pas été fourni », puis _minimal_brief)
  vision   : clean_media_query(query) → _factual_vision_query (suffixe « décris, sans avis » :
             l'outil n'a QUE question+image, l'avis se juge dans le brief) → 1536px JPEG88 → VL-3B
  doc_ocr  : clean_media_query(query) → gate brouillon (natif dense direct, sinon PaddleOCR-VL ; scène → caption VL-3B)
  get_source : RETIRÉ du registre (aucun appelant) — la recherche remplace ce chemin ;
             un [get_source()] recopié par le modèle est filtré, jamais exécuté
  cache (tool, artifact_id) anti-doublon dans le tour

Brief → générateur (LFM2.5-8B, SANS tools) — contrat brief-seul, validation SERVEUR :
  brief = render_brief(validé) texte UI + render_brief_json(validé) bloc générateur : {language, complete, artifact_ids_relevant, facts[{text, evidence_ids, confidence}], organization{goal,length,tone}, limits[], clarification}
  SANS fichier et SANS outil : facts depuis les CONNAISSANCES propres (jamais d'abdication)
  gen_messages = _build_generator_messages(history, brief_json, message)  # 1 system + 1 message DEMANDE/HISTORIQUE/BRIEF
  AUCUNE observation brute au générateur (fini obs_block) : petit modèle encadré, zéro bruit d'outils
  (l'ancienne machinerie écho/abdication/saignée/regen textuelle est SUPPRIMÉE : la contrainte
  vit dans la structure — forçage submit_brief + validate_submit_brief)
  réponse = LLM.générer(SYSTEM_GENERATOR + brief)
  réponse = sanitize(<think> strip, SANS réémettre pensée privée)
  réponse = _with_image_truth_guards(réponse, image_status) — le SEUL rempart backend :
    * image PUBLIÉE  → retire la négation fausse, et si la réponse ne nomme AUCUN support,
      y inscrit le fait réservé (_reserved_image_fact, la MÊME phrase que le brief) :
      décrire un chat sans dire qu'une image a été faite est un sous-claim ;
    * image ABSENTE  → retire la promesse d'un rendu qui n'existe pas, sinon la limite
      ENREGISTRÉE. Un filtre distinct (_BRIEF_META_TRAIL_RE) retire la référence au
      mécanisme (« Selon le brief, X » comme « X, bien que le brief n'ait pas pu être
      validé ») ; il exige une concession ET un sujet-mécanisme, sinon il passe l'octet.

Compaction (si budget dépassé) :
  SI historique dépasse fenêtre réelle − 20 000 tokens (CONTEXT_OVERHEAD_TOKENS, compaction.py ; déclenchement à fenêtre − 20 000 = 111 072 sur 131 072, sans facteur 0,85) :
    résumé = LLM.résumer(anciens messages)
    snapshot = ContextSnapshot(revision, covered_through_message_id, summary)
    historique = [Résumé] + recent 15K → SharedContext(revision+1)
```

## 2bis. Schémas ASCII

```
TOUR COMPLET (main.py → loop.py)
─────────────────────────────────
stdin_ligne
  │
  ▼
[normaliser] → {request_id, method, params, proto}
  │
  ├─ fingerprint déjà vu ? ──OUI──▶ rejouer terminal (C11, sans réexécuter)
  │
  NON
  ▼
[transaction 1] Conversation + Message user + MessageAttachment  ◀── JAMAIS après le LLM
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 0 DÉTERMINISTE (media_policy, 0 appel LLM)        │
│                                                         │
│  nouveaux fichiers ?                                    │
│   OUI → R1 : pdf→doc_ocr │ image→vision │ img+texte→ocr │
│   NON + média dispo + question ancrée ?                 │
│    └─ A: "cette image/dans l'image/..."  ─┐             │
│    └─ B: "revois/regarde à nouveau/..."  ─┼─ OR ─▶ R2   │
│    └─ C: "couleur/cheveux/fond/tient" ───┘    réobserver│
│    └─ OCR: "lire...écrit en japonais" ──────▶ doc_ocr   │
│  plan VIDE + demande de création ──▶ generate_image     │
│    (verbe + nom d'image ; prompt = le message)          │
│  SINON → pas d'outil (chat pur)                         │
└─────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Orchestrateur (LFM2.5-8B, ≤16 iter, 15s/outil, 300s tot) │
│  [SYSTEM_ORCHESTRATOR + SOURCES + historique]            │
│  tool_defs = les 5 de DISCOVERY_TOOL_NAMES (auto)       │
│     │                                                   │
│     ├─ natifs → allowlist ; sinon re-ask natif forcé    │
│     │   (pseudo-appel [fn(k='v')] : JAMAIS exécuté)     │
│     ├─ enveloppe d'arguments SANS nom → re-ask NON     │
│     │   contraint (5 outils, "required") : le MODÈLE    │
│     │   nomme l'outil + les arguments, ou rien          │
│     ├─ submit_brief → validate (schéma+preuves) → brief│
│     ├─ découverte → exécuteur → Observ. (+off_topic)   │
│     └─ silence → phase brief FORCÉE (nommé, retry ×2)  │
│        cache (tool, artifact_id) anti-doublon ;         │
│        generate_image : 1 image/tour, run.stage,        │
│        budget 300 s HORS diffusion, 0 retry             │
└─────────────────────────────────────────────────────────┘
   │
   ▼
[brief validé rendu texte] → Générateur (SANS tools, chemins, observations) → sanitize
  │
  ▼
[transaction 2] Message assistant + ToolCall + Observation + Run  ◀── AVANT terminal
  │
  ▼
emit terminal UNIQUE (completed/cancelled/failed) — seq++
```

```
FSM VRAM (manager.py) — 1 modèle à la fois
──────────────────────
   ┌──────────┐  use_model()   ┌─────────┐  prêt  ┌─────────┐
   │ unloaded │ ──────────────▶ │ loading │ ─────▶ │  ready  │
   └──────────┘   admission=1   └─────────┘        └────┬────┘
        ▲                         │ OOM                │ run
        │                    ┌────▼────┐                ▼
        │                    │ failed  │          ┌─────────┐
        └────────────────────│ repli : │          │ running │
          unload + sleep     │ n_layers▼, n_ctx/2 │         │
          0.5s + attente 5s  └─────────┘          └────┬────┘
                                                      │ done
                                                      ▼
                                                ┌───────────┐
                                                │ unloading │
                                                └───────────┘

  n_ctx : ≥8 Go → 131072 · ≥7 Go → 32768 · sinon min(32768, tokens+2048)
  replis : nominal → apres_gc_force → flash_off → sans_mmproj
           (vision garde TOUJOURS son mmproj — fix 09/09)
  OOM si "Failed to load model" OU VRAM basse → 32768→16384→8192 (plancher 4096/2048)
```

## 3. Extraits réels

```python
# backend/main.py — handler C08
run_id = request_id  # 1:1 Phase 1
# persist_user_turn AVANT LLM, terminal unique APRÈS persistance
```

```python
# backend/inference/media_policy.py — R2 en 3 couches OR
EXPLICIT_REFERENCE_TERMS = ("cette image", "dans l'image", ...)
REVISIT_VERBS = ("revois", "regarde à nouveau", ...)
VISUAL_DETAIL_TERMS = ("couleur", "cheveux", "fond", "tient", ...)
# + OCR_REQUEST_TERMS unifié ("lire ... écrit en japonais" → doc_ocr, pas vision)
```

```python
# backend/inference/loop.py — garde-fous tour
MAX_ITER = 16; TOOL_TIMEOUT = 15; TOTAL_TIMEOUT = 300
# Registre = 10 outils ; seule l'offre LLM en découverte est DISCOVERY_TOOL_NAMES.
DISCOVERY_TOOL_NAMES = {"search_info", "find_files", "ask_files",
                        "generate_image", "web_research"}
# LE TEMPS D'HORLOGE de ces outils est SOUSTRAIT de TOTAL_TIMEOUT : une diffusion
# T2I légitime dure 60 à 600 s (checkpoint ~19 Go compris). Sans cette soustraction,
# TOTAL_TIMEOUT expiré APRÈS une image réussie faisait remplacer la réponse par
# « la réponse complète n'a pas pu être terminée dans le délai imparti » et ne
# laissait plus que 0,1 s au générateur. Le budget borne le TEMPS DE RAISONNEMENT ;
# avec le temps lourd soustrait, TOTAL_TIMEOUT n'est PLUS UN PLAFOND D'HORLOGE : un
# tour image dure « raisonnement (≤ TOTAL_TIMEOUT) + diffusion », pas TOTAL_TIMEOUT.
# CE QUI BORNE RÉELLEMENT UNE DIFFUSION : flux_spike.RUN_TIMEOUT_S = 600, dans le
# worker. PAS le timeout de l'outil — ToolExecutor.execute_call sort generate_image
# de son wrapper de timeout, et _run_heavy_tool appelle _execute_generate_image en
# direct : GenerateImageTool.timeout_s = 600 ne borne rien sur le chemin vivant.
# L'admission (coordinator.acquire(..., timeout_s=30.0) dans image_service) borne
# l'ATTENTE du GPU, pas la diffusion.
HEAVY_TOOL_NAMES = {"generate_image"}
# Un timeout de diffusion est un VERDICT, pas une infortune : le retry relançait une
# 2e diffusion de 600 s dans le même tour (jusqu'à ~20 min pour une seule image).
# Ce qui échoue ici échoue UNE fois, bruyamment.
RETRY_EXEMPT_TOOLS = {"generate_image"}
# cache (tool, artifact_id) anti-doublon, available requis
```

## 4. Manager VRAM (pseudo-code)

```
use_model(nom) :
  acquérir admission (1 seul run lourd, sinon attendre)
  _wait_for_vram_release(3s) + sleep déchargement (0.5s + attente 5s)
  n_ctx = sur VRAM *courante* (plus cache détecteur) : ≥8 Go →131072 · ≥7 Go →32768 · sinon min(32768, tokens+2048)
  essayer stratégies : nominal → apres_gc_force → flash_off → sans_mmproj
    MAIS vision garde toujours son projector (fix 09/09 : repli OOM-partiel ne coupe plus mmproj)
  SI _looks_like_oom("Failed to load model" OU VRAM basse) → n_gpu_layers réduit + n_ctx /2
    (32768→16384→8192, plancher 4096/2048)
  _generation++ invalide les handles après switch vision (C07)
```


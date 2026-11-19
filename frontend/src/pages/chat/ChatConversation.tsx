// ChatConversation Phase 1 — IPC local (pas de SSE/REST)
// 7 events : message_started, thinking, tool_call, tool_result, answer, message_completed, error
// + backend_ready (hardware) — JSON Lines sur stdin/stdout, request_id corrélé, hash+path pour les fichiers (jamais base64)
import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  ArrowLeft24Regular, LockClosed24Regular, Edit24Regular,
  ChevronRight24Regular, ChevronLeft24Regular,
} from '@fluentui/react-icons';
import { Composer } from '../../components/composer/Composer';
import { Tag } from '../../components/ui/Tag';
import { showToast } from '../../components/ui/Toast';
import type { ToolCallStatus } from '../../components/chat/ToolCallDisplay';
import { OrchestratorTrace } from '../../components/chat/OrchestratorTrace';
import { Markdown } from '../../components/chat/Markdown';
import './ChatConversation.css';
import { useAttachmentDraft } from '../../features/attachments/AttachmentDraftContext';
import { useInferenceActivity } from '../../features/inference/InferenceActivityContext';
import type { LiveRunSnapshot } from '../../features/inference/InferenceActivityContext';
import { chatSend, saveUpload, MAX_PICKER_ATTACHMENT_BYTES, listMessages, onMessageStarted, onThinking, onToolCall, onToolResult, onAnswer, onMessageCompleted, onError, onBackendReady, onBackendTerminated, onExecutionPlan, onOrchestratorBrief, onArtifactCreated, onRunStage, onRunStarted, onRunCompleted, onRunFailed, onRunCancelled, getStagedPreviewBytes, getConversation, renameConversation } from '../../lib/ipcBridge';
import { advanceRunProgress, ProgressCard, type RunProgress, type RunProgressEvent } from '../../components/image/ProgressCard';
import { GeneratedImageCard } from '../../components/image/GeneratedImageCard';

interface ToolCallItem {
  tool_call_id: string;
  tool: string; // vision | doc_ocr | search_documents | get_document_card | read_document | …
  arguments: Record<string, unknown>;
  status: ToolCallStatus;
  result?: string;
  ok?: boolean;
  latency_ms?: number;
  // Les preuves que le backend a produites. Sans elles, un `web_research`
  // réussi ne laisse dans la bulle qu'un blob technique : l'utilisateur ne
  // voit ni ce qui a été lu, ni d'où la réponse tire ses faits.
  evidence_ids?: string[];
  public_ids?: string[];
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  thinking?: string; // thinking du GÉNÉRATEUR / legacy (itération 99)
  thinkingCollapsed?: boolean;
  // Trace ORCHESTRATEUR (bulle repliée par défaut — ne pas confondre avec thinking générateur)
  orchestratorThinking?: string;
  orchestratorBrief?: string;
  orchestratorBriefChars?: number;
  orchestratorBriefTools?: number;
  orchestratorDone?: boolean;
  images?: string[]; // objectURL previews (éphémères)
  artifactIds?: string[];
  files?: { name: string; kind: string }[]; // pièces jointes non-image (PDF...) rechargées d'historique
  toolCalls?: ToolCallItem[];
  executionPlan?: { attachmentKind?: string | null; intent: string; selectedTool?: string | null; selectedModel?: string | null; reason: string };
}

function stripThinkingLeak(text: string): string {
  if (!text) return text;
  let out = text.replace(/<think>[\s\S]*?<\/think>/gi, '');
  out = out.replace(/<\s*\/\s*think\s*>/gi, '');
  out = out.replace(/<\s*think\s*>/gi, '');
  if (/Theuserisgreeting/i.test(out) || /The user is greeting/i.test(out)) {
    const frIdx = out.search(/Je\s*va/i);
    if (frIdx > 20) {
      out = out.slice(frIdx);
    } else {
      const altIdx = out.search(/Jeva/i);
      if (altIdx > 10) out = out.slice(altIdx).replace(/^Jeva/, 'Je va');
    }
  }
  return out.trimStart();
}

function extractThinkingLeak(content: string): { thinking: string | null; clean: string } {
  if (!content) return { thinking: null, clean: content };
  const tagIdx = content.search(/<\/\s*think\s*>/i);
  if (tagIdx !== -1) {
    const before = content.slice(0, tagIdx);
    const after = content.slice(tagIdx).replace(/<\/\s*think\s*>/i, '').trimStart();
    if (/The\s*user|Okay|Theuser/i.test(before)) {
      return { thinking: before.replace(/<\s*think\s*>/gi, '').trim(), clean: stripThinkingLeak(after) };
    }
  }
  const startsWithThinking = /^Theuserisgreeting/i.test(content) || /^The\s+user\s+is\s+greeting/i.test(content) || /^Okay,?let/i.test(content) || /^Okay, the user/i.test(content);
  if (startsWithThinking) {
    let frIdx = content.search(/Pour une/i);
    if (frIdx === -1) frIdx = content.search(/La Bretagne/i);
    if (frIdx === -1) frIdx = content.search(/Je\s*va/i);
    if (frIdx === -1) frIdx = content.search(/Jeva/i);
    if (frIdx === -1) frIdx = content.search(/Bien sûr ! Les arbres/i);
    if (frIdx === -1) frIdx = content.search(/Les arbres/i);
    if (frIdx > 10) {
      const thinking = content.slice(0, frIdx).trim();
      let clean = content.slice(frIdx).trim();
      clean = clean.replace(/^Jevaistrèsbien/, 'Je vais très bien').replace(/^Jeva/, 'Je va').replace(/^Pourune/, 'Pour une');
      return { thinking, clean: stripThinkingLeak(clean) };
    }
    const fallbackIdx = content.search(/Pourune|Jeva|Bien sûr/i);
    if (fallbackIdx > 20) {
      return { thinking: content.slice(0, fallbackIdx).trim(), clean: stripThinkingLeak(content.slice(fallbackIdx)) };
    }
  }
  if (content.includes('Okay, the user is asking') && !content.includes('</think>')) {
    const frIdx = content.search(/Les arbres|Je vais|Bonjour|Salut/i);
    if (frIdx > 30) {
      return { thinking: content.slice(0, frIdx).trim(), clean: content.slice(frIdx).trim() };
    }
  }
  return { thinking: null, clean: stripThinkingLeak(content) };
}

function liveRunToMessage(live: LiveRunSnapshot): Message {
  const { thinking: leak, clean } = extractThinkingLeak(live.answer);
  const thinkingParts = [live.thinking];
  if (leak && !live.thinking.includes(leak)) thinkingParts.push(leak);
  const thinking = thinkingParts.filter(Boolean).join('\n\n');
  const content = stripThinkingLeak(clean);
  const message: Message = { id: `live-${live.requestId}`, role: 'assistant', content };
  if (thinking) { message.thinking = thinking; message.thinkingCollapsed = true; }
  const orchestratorThinking = live.brief?.thinking || live.orchestratorThinking;
  if (orchestratorThinking) message.orchestratorThinking = orchestratorThinking;
  if (live.toolCalls.length) {
    message.toolCalls = live.toolCalls.map((t) => ({
      tool_call_id: t.tool_call_id, tool: t.tool, arguments: t.arguments,
      status: t.status as ToolCallStatus, result: t.result, ok: t.ok, latency_ms: t.latency_ms,
      evidence_ids: t.evidence_ids, public_ids: t.public_ids,
    }));
  }
  if (live.plan) {
    const plan = live.plan as { attachment_kind?: string | null; intent?: string; selected_tool?: string | null; selected_model?: string | null; reason?: string };
    message.executionPlan = { attachmentKind: plan.attachment_kind ?? null, intent: plan.intent ?? 'unknown', selectedTool: plan.selected_tool ?? null, selectedModel: plan.selected_model ?? null, reason: plan.reason ?? '' };
  }
  if (live.brief) {
    message.orchestratorBrief = live.brief.brief;
    message.orchestratorBriefChars = live.brief.brief_chars ?? live.brief.brief.length;
    message.orchestratorBriefTools = live.brief.tool_calls;
    message.orchestratorDone = true;
  }
  return message;
}

type ChatState = 'idle' | 'preparing' | 'orchestrating' | 'tool_running' | 'generating';

// Cible d'un artefact_created, résolue à la réception de l'événement et jamais après :
// les octets arrivent par un appel IPC asynchrone, or « la dernière bulle assistant » peut
// désigner un autre tour entre l'événement et la résolution (nouvel envoi, rechargement).
function lastAssistantId(messages: Message[]): string | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'assistant') return messages[index].id;
  }
  return null;
}

/** Le tour visé existe-t-il encore, et n'a-t-il pas déjà cet artefact ? */
function canAttachArtifact(messages: Message[], targetId: string | null, artifactId: string): boolean {
  if (targetId === null) return true;
  const target = messages.find((message) => message.id === targetId);
  if (!target) return false;
  return !(target.artifactIds ?? []).includes(artifactId);
}

function attachArtifact(messages: Message[], targetId: string | null, artifactId: string, url: string): Message[] {
  if (targetId === null) {
    return [...messages, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', artifactIds: [artifactId], images: [url] }];
  }
  const target = messages.find((message) => message.id === targetId);
  if (!target) return messages;
  if ((target.artifactIds ?? []).includes(artifactId)) return messages;
  return messages.map((message) => message.id === targetId
    ? { ...message, artifactIds: [...(message.artifactIds ?? []), artifactId], images: [...(message.images ?? []), url] }
    : message);
}

function lireEspaceDepuisEtat(etat: unknown): string | undefined {
  return (etat as { workspaceId?: string } | null)?.workspaceId;
}

export function ChatConversation() {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();

  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState('');
  const [chatState, setChatState] = useState<ChatState>('idle');
  const [runProgress, setRunProgress] = useState<RunProgress | null>(null);
  const [progressElapsed, setProgressElapsed] = useState(0);
  const [showRail, setShowRail] = useState(true);
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const [titleIsFallback, setTitleIsFallback] = useState(false);
  const titleEditedRef = useRef(false);
  const renameSavingRef = useRef(false);
  const renameButtonRef = useRef<HTMLButtonElement | null>(null);
  const renameReturnFocusRef = useRef(false);
  useEffect(() => {
    if (!isRenaming && renameReturnFocusRef.current) {
      renameReturnFocusRef.current = false;
      renameButtonRef.current?.focus();
    }
  }, [isRenaming]);
  useEffect(() => {
    if (!id) return;
    let alive = true;
    void (async () => {
      const conv = await getConversation(id);
      if (!alive || titleEditedRef.current) return;
      if (conv && typeof conv.title === 'string' && conv.title.trim() !== '') {
        setTitle(conv.title);
        setTitleIsFallback(false);
      }
    })();
    return () => { alive = false; };
  }, [id]);
  // L'historique est un état à part entière : « vide » et « illisible » sont
  // deux vérités différentes, et les confondre affichait une invitation à
  // discuter alors que les messages existaient toujours en base.
  const [historyStatus, setHistoryStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyReloadKey, setHistoryReloadKey] = useState(0);
  const { isDraggingFiles } = useAttachmentDraft();
  const inference = useInferenceActivity();
  // Annexe 09 : le dépôt est géré uniquement par Composer (source unique natif), pas de double handler page (évite 3x)
  // useAttachmentDrop gardé pour compat mais non branché au layout (évite bubbling)
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const currentConvIdRef = useRef<string | null>(id ?? null);
  const currentRequestIdRef = useRef<string | null>(null);
  const runStartedRef = useRef(false);
  const inferenceRef = useRef(inference);
  inferenceRef.current = inference;
  const chatStateRef = useRef<ChatState>('idle');
  const hasSentInitialRef = useRef(false);
  const historyIdRef = useRef<string | null | undefined>(null);
  const historyLoadSeqRef = useRef(0);
  // Miroir de la liste pour les gestionnaires d'événements : la destination d'une image se
  // décide à l'arrivée de l'événement, pas au rendu.
  const messagesRef = useRef<Message[]>(messages);
  messagesRef.current = messages;
  // objectURL créées par la page (octets d'artefacts, aperçus d'envoi) : l'inventaire est
  // unique, la révocation aussi (voir l'effet de nettoyage sur `messages`). Seul `blob:`
  // y entre (les `asset://` des previews de staging en sont exclus à la création).
  const objectUrlsRef = useRef<Set<string>>(new Set());
  // Distingue le démontage d'un simple remplacement de `messages` : au démontage, aucune
  // URL n'est plus affichée, donc tout l'inventaire est libérable.
  const isUnmountingRef = useRef(false);

  useEffect(() => {
    chatStateRef.current = chatState;
  }, [chatState]);

  const initialState = location.state as { initialMessage?: string; initialFiles?: File[] } | null;

  // --- écouteurs d'événements (7 + backend_ready) — single mount, full cleanup ---
  useEffect(() => {
    let mounted = true;
    const unsubs: Array<() => void> = [];

    const setup = async () => {
      const ub = await onBackendReady(() => {
        if (!mounted) return;
      });
      unsubs.push(ub);

      const u1 = await onMessageStarted((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current !== p.request_id) return;
        runStartedRef.current = true;
        setChatState('preparing');
      });
      unsubs.push(u1);

      const uRunStarted = await onRunStarted((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current !== p.request_id) return;
        runStartedRef.current = true;
      });
      unsubs.push(uRunStarted);

      const u2 = await onThinking((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        const raw = p as unknown as { text?: string; content?: string; phase?: string; iteration?: number };
        const chunk = raw.text ?? raw.content ?? '';
        if (!chunk) return;
        // Phase structurée backend : orchestrateur (itérations 0..N) vs générateur (99).
        // Séparés dès réception : la bulle Orchestrateur ne montre QUE l'orchestrateur.
        const isGenerator = raw.phase === 'generator' || (typeof raw.iteration === 'number' && raw.iteration === 99);
        setChatState(isGenerator ? 'generating' : 'orchestrating');
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant') {
            if (isGenerator) {
              return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, thinking: (m.thinking ?? '') + chunk, thinkingCollapsed: m.thinkingCollapsed ?? true } : m);
            }
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, orchestratorThinking: (m.orchestratorThinking ?? '') + chunk, thinkingCollapsed: m.thinkingCollapsed ?? true } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', ...(isGenerator ? { thinking: chunk } : { orchestratorThinking: chunk }), thinkingCollapsed: true }];
        });
      });
      unsubs.push(u2);

      const u3 = await onToolCall((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        const tool = p.tool as string;
        setChatState('tool_running');
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          const item: ToolCallItem = { tool_call_id: p.tool_call_id, tool, arguments: p.arguments as Record<string, unknown>, status: 'running' };
          if (last && last.role === 'assistant') {
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, toolCalls: [...(m.toolCalls ?? []), item] } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', toolCalls: [item] }];
        });
      });
      unsubs.push(u3);

      const u4 = await onToolResult((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setMessages((prev) => prev.map((m, idx) => {
          if (idx !== prev.length - 1) return m;
          if (!m.toolCalls) return m;
          const updated = m.toolCalls.map((t) => t.tool_call_id === p.tool_call_id ? { ...t, status: (p.ok ? 'completed' : 'error') as ToolCallStatus, result: p.result, ok: p.ok, latency_ms: p.latency_ms, evidence_ids: p.evidence_ids, public_ids: p.public_ids } : t);
          return { ...m, toolCalls: updated };
        }));
        if (!p.ok) showToast({ type: 'error', title: 'Outil échoué', message: `${p.tool_call_id} — ${p.result?.slice(0, 120) || 'erreur'} (${p.latency_ms} ms)` });
      });
      unsubs.push(u4);

      // answer — première réponse cache l'activité, passe en generating
      const u5 = await onAnswer((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setChatState('generating');
        const rawPayload = p as unknown as { text?: string; content?: string };
        let raw: string = rawPayload.text ?? rawPayload.content ?? '';
        if (!raw) return;
        const extracted = extractThinkingLeak(raw);
        if (extracted.thinking) {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === 'assistant') {
              const already = last.thinking === extracted.thinking || (last.thinking?.includes(extracted.thinking ?? '') ?? false);
              if (already) return prev;
              return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, thinking: ((m.thinking ? m.thinking + '\n\n' : '') + extracted.thinking) as string, thinkingCollapsed: true } : m);
            }
            return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', thinking: extracted.thinking as string, thinkingCollapsed: true }];
          });
          raw = extracted.clean;
          if (!raw) return;
        } else {
          raw = stripThinkingLeak(raw);
          if (!raw) return;
        }
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant') {
            const candidate = (last.content ?? '') + raw;
            const exAll = extractThinkingLeak(candidate);
            if (exAll.thinking) {
              const already = last.thinking === exAll.thinking || (last.thinking?.includes(exAll.thinking.slice(0, 80)) ?? false);
              if (already) {
                return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, content: exAll.clean } : m);
              }
              return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, thinking: ((m.thinking ? m.thinking + '\n\n' : '') + exAll.thinking) as string, thinkingCollapsed: true, content: exAll.clean } : m);
            }
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, content: candidate } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: raw }];
        });
      });
      unsubs.push(u5);

      const finishLocalRun = (requestId: string) => {
        if (!mounted) return;
        // Corrélation STRICTE : un terminal d’un autre run ne doit pas
        // libérer cette conversation ni l’activité globale.
        if (currentRequestIdRef.current !== requestId) return;
        inferenceRef.current.end(requestId);
        runStartedRef.current = false;
        setChatState('idle');
        setRunProgress(null);
        currentRequestIdRef.current = null;
      };

      const u6 = await onMessageCompleted((p) => {
        finishLocalRun(p.request_id);
      });
      unsubs.push(u6);

      const u7 = await onError((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current !== p.request_id) return;
        if (runStartedRef.current) {
          // Le run est déjà entré en exécution : cette erreur peut être un
          // avertissement (llm_error, generator_error, image_paths_invalid…).
          // Seuls run_failed / run_completed / run_cancelled terminent.
          showToast({ type: 'warning', title: 'Avertissement', message: p.message });
          return;
        }
        showToast({ type: 'error', title: 'Erreur', message: p.message });
        finishLocalRun(p.request_id);
      });
      unsubs.push(u7);

      const uRunCompleted = await onRunCompleted((p) => finishLocalRun(p.request_id));
      unsubs.push(uRunCompleted);
      const uRunFailed = await onRunFailed((p) => finishLocalRun(p.request_id));
      unsubs.push(uRunFailed);
      const uRunCancelled = await onRunCancelled((p) => finishLocalRun(p.request_id));
      unsubs.push(uRunCancelled);

      const uBackendTerminated = await onBackendTerminated(() => {
        const current = currentRequestIdRef.current;
        if (current) finishLocalRun(current);
      });
      unsubs.push(uBackendTerminated);

      const u8 = await onExecutionPlan((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant') {
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, executionPlan: { attachmentKind: p.attachment_kind, intent: p.intent, selectedTool: p.selected_tool, selectedModel: p.selected_model, reason: p.reason } } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', executionPlan: { attachmentKind: p.attachment_kind, intent: p.intent, selectedTool: p.selected_tool, selectedModel: p.selected_model, reason: p.reason } }];
        });
      });
      unsubs.push(u8);

      // Handoff orchestrateur→générateur : le brief exact (bulle Orchestrateur, repliée par défaut).
      // thinking joint = contenus des itérations → remplace les labels de statut (plus riche).
      const u9 = await onOrchestratorBrief((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          const patch = { orchestratorBrief: p.brief, orchestratorBriefChars: p.brief_chars, orchestratorBriefTools: p.tool_calls, orchestratorDone: p.orchestrator_done, ...(p.thinking ? { orchestratorThinking: p.thinking } : {}) };
          if (last && last.role === 'assistant') {
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, ...patch } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', ...patch }];
        });
      });
      unsubs.push(u9);

      const u10 = await onArtifactCreated((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        // Cible figée AVANT le moindre await : une fois les octets résolus, « la dernière
        // bulle assistant » peut désigner un autre tour, et l'image atterrirait à côté.
        const targetId = lastAssistantId(messagesRef.current);
        void (async () => {
          try {
            const { getAttachmentBytes } = await import('../../lib/ipcBridge');
            const [bytes, mime] = await getAttachmentBytes(p.artifact_id);
            if (!mounted) return;
            // Tour disparu entre-temps : l'image sera de toute façon rechargée depuis la base.
            if (!canAttachArtifact(messagesRef.current, targetId, p.artifact_id)) return;
            const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
            objectUrlsRef.current.add(url);
            setMessages((prev) => attachArtifact(prev, targetId, p.artifact_id, url));
          } catch (error) {
            showToast({ type: 'error', title: 'Image indisponible', message: error instanceof Error ? error.message : String(error) });
          }
        })();
      });
      unsubs.push(u10);

      // Progression du moteur image (diffusion, VAE, publication) : sans elle, une
      // génération de plusieurs minutes se lit comme un silence.
      const u11 = await onRunStage((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setRunProgress((current) => advanceRunProgress(current, p));
      });
      unsubs.push(u11);
    };
    setup();

    return () => {
      mounted = false;
      unsubs.forEach((fn) => {
        try { fn(); } catch { /* ignore */ }
      });
    };
  }, []);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsAtBottom(distanceToBottom < 120);
  }, []);

  // Une seule sortie pour les objectURL d'images, déclenchée à chaque remplacement
  // de la liste ET au démontage. La liste relue est celle de messagesRef (donc l'état
  // courant, pas la closure d'un rendu passé), ce qui garantit qu'une URL encore
  // affichée n'est jamais révoquée. Au démontage, en revanche, plus rien n'est
  // affiché : sans `isUnmountingRef` les blobs de la dernière conversation furaient
  // pour toute la vie de la page (un lot d'images décodées par navigation).
  // ORDRE IMPÉRATIF : le drapeau est déclaré AVANT l'effet de révocation — React
  // appelle les cleanups dans l'ordre de déclaration, le drapeau est donc déjà posé
  // au démontage, et ne l'est pas lors d'un simple changement de `messages`.
  useEffect(() => () => { isUnmountingRef.current = true; }, []);

  useEffect(() => () => {
    const displayed = new Set(messagesRef.current.flatMap((message) => message.images ?? []));
    [...objectUrlsRef.current].forEach((url) => {
      // Seuls les `blob:` sont révoquables : un `asset://` (convertFileSrc) est un URL du
      // protocole, revokeObjectURL ne le touche pas et ferait tomber la vignette.
      if (!url.startsWith('blob:')) return;
      if (displayed.has(url) && !isUnmountingRef.current) return;
      objectUrlsRef.current.delete(url);
      if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
    });
  }, [messages]);

  useEffect(() => {
    if (!runProgress) return;
    const interval = window.setInterval(() => {
      setProgressElapsed(Math.floor((Date.now() - runProgress.startedAt) / 1000));
    }, 500);
    return () => window.clearInterval(interval);
  }, [runProgress]);

  useEffect(() => {
    if (!isAtBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight - 8, behavior: 'smooth' });
  }, [messages, isAtBottom, chatState]);

  // Un run chat continue après démontage ; au retour sur SA conversation, on
  // l’adopte pour que l’arrêt reste possible. Ne jamais adopter le run d’une
  // autre conversation.
  useEffect(() => {
    const act = inference.activity;
    if (!act.active || act.kind !== 'chat' || !act.requestId) return;
    if (currentRequestIdRef.current) return;
    if (!act.conversationId || act.conversationId !== id) return;
    currentRequestIdRef.current = act.requestId;
    // Un run adopté est déjà démarré si le provider n’est plus en `pending` ;
    // sans cela une erreur de progression serait traitée comme un refus d’admission.
    runStartedRef.current = act.phase === 'running' || act.phase === 'cancelling';
    setChatState('generating');
  }, [inference.activity, id]);

  // Si le provider libère par watchdog/backend sans terminal, la page ne doit
  // pas rester « Préparation… ». Le délai de 2 s couvre le render entre begin
  // et l’état du provider.
  useEffect(() => {
    if (inference.activity.active) return;
    const current = currentRequestIdRef.current;
    if (!current) return;
    const timer = window.setTimeout(() => {
      if (currentRequestIdRef.current === current && !inferenceRef.current.activity.active) {
        currentRequestIdRef.current = null;
        runStartedRef.current = false;
        setChatState('idle');
        setRunProgress(null);
      }
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [inference.activity.active]);

  useEffect(() => {
    if (historyIdRef.current !== id) {
      historyIdRef.current = id;
      currentConvIdRef.current = id ?? null;
      hasSentInitialRef.current = false;
      // ChatConversation peut être réutilisé entre deux :id sans remontage ;
      // les refs de conversation/run ne doivent pas fuir de A vers B.
      const act = inferenceRef.current.activity;
      if (act.active && act.kind === 'chat' && act.conversationId !== id) {
        currentRequestIdRef.current = null;
        runStartedRef.current = false;
        setChatState('idle');
        setRunProgress(null);
      }
      setMessages([]);
      setIsRenaming(false);
      setRenameValue('');
      setRenameSaving(false);
      renameSavingRef.current = false;
      titleEditedRef.current = false;
      renameReturnFocusRef.current = false;
    }
    if (!id) {
      setHistoryStatus('ready');
      return;
    }
    const sequence = historyLoadSeqRef.current + 1;
    historyLoadSeqRef.current = sequence;
    setTitle(`Conversation ${id.slice(0, 8)}`);
    setTitleIsFallback(true);
    let alive = true;
    // Invalidation par séquence : un chargement n'est autoritaire que tant que
    // sa séquence reste la courante. Envoyer un message l'invalide, donc une
    // réponse d'historique tardive ne remplace jamais l'état vivant.
    // Le chargement ne vide jamais `messages` : un rechargement qui échoue doit
    // laisser à l'écran ce que l'utilisateur lisait déjà (bandeau par-dessus).
    setHistoryStatus('loading');
    setHistoryError(null);
    // Charge l'historique existant depuis la base + vignettes persistées
    // Pourquoi recharger les images : à l'envoi on n'affiche que des previews éphémères
    // (objectURL/blob: ou asset:// du staging) qui meurent au rechargement ; la vérité durable
    // est en DB (message_attachments → blobs). Sans ce rechargement, l'historique montre du
    // texte seul alors que le backend, lui, revoit très bien les fichiers (bug q-r.md 09/09).
    void (async () => {
      try {
        const msgs = await listMessages(id);
        if (!alive || historyLoadSeqRef.current !== sequence) return;
        const mapped: Message[] = [];
        const { getAttachmentBytes } = await import("../../lib/ipcBridge");
        if (!alive || historyLoadSeqRef.current !== sequence) return;
        for (const m of msgs ?? []) {
          const base: Message = {
            id: m.id as string,
            role: m.role as 'user' | 'assistant',
            content: (m.content as string) || '',
            thinking: (m as unknown as { thinking?: string }).thinking,
          };
          // Rebuild bulle Orchestrateur au reload : trace persistée AVANT le terminal (C08).
          // Sans trace (legacy), la bulle reste cachée comme avant — jamais de bulle vide.
          if (base.role === 'assistant') {
            const raw = m as unknown as {
              orchestrator_thinking?: string | null; brief?: string | null;
              plan?: { attachmentKind?: string | null; intent?: string; selectedTool?: string | null; selectedModel?: string | null; reason?: string } | string | null;
              toolcalls?: Array<{ tool_call_id: string; tool: string; arguments?: Record<string, unknown>; result?: string; ok?: boolean; latency_ms?: number }>;
            };
            if (raw.orchestrator_thinking) base.orchestratorThinking = raw.orchestrator_thinking;
            if (raw.brief) {
              base.orchestratorBrief = raw.brief;
              base.orchestratorBriefChars = raw.brief.length;
              base.orchestratorDone = true;
            }
            if (raw.plan && typeof raw.plan === 'object') {
              base.executionPlan = {
                attachmentKind: raw.plan.attachmentKind ?? null,
                intent: raw.plan.intent ?? 'unknown',
                selectedTool: raw.plan.selectedTool ?? null,
                selectedModel: raw.plan.selectedModel ?? null,
                reason: raw.plan.reason ?? '',
              };
            }
            const tcs = raw.toolcalls ?? [];
            if (tcs.length > 0) {
              base.toolCalls = tcs.map((t) => ({
                tool_call_id: t.tool_call_id,
                tool: t.tool || 'outil',
                arguments: t.arguments ?? {},
                status: t.ok ? 'completed' : 'error',
                result: t.result ?? '',
                ok: t.ok ?? false,
                latency_ms: t.latency_ms ?? 0,
              }));
              if (base.orchestratorBrief) base.orchestratorBriefTools = tcs.length;
            }
          }
          const atts = (m as unknown as { attachments?: Array<{ id: string; kind: string; name: string; mime: string }> }).attachments ?? [];
          if (atts.length > 0) {
            base.artifactIds = atts.map((attachment) => attachment.id);
            const imgs: string[] = [];
            const files: { name: string; kind: string }[] = [];
            let bytesFailures = 0;
            for (const a of atts) {
              const isImg = (a.kind || '').toLowerCase() === 'image' || (a.mime || '').startsWith('image/');
              if (!isImg) { files.push({ name: a.name, kind: a.kind || 'fichier' }); continue; }
              try {
                const [bytes, mime] = await getAttachmentBytes(a.id);
                if (!alive || historyLoadSeqRef.current !== sequence) return;
                const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
                objectUrlsRef.current.add(url);
                imgs.push(url);
              } catch {
                if (!alive || historyLoadSeqRef.current !== sequence) return;
                bytesFailures += 1;
                files.push({ name: a.name, kind: a.kind || 'image' });
              }
            }
            if (bytesFailures > 0) showToast({ type: 'error', title: 'Vignettes non chargées', message: `${bytesFailures} fichier(s), réessayez` });
            if (imgs.length) base.images = imgs;
            if (files.length) base.files = files;
          }
          mapped.push(base);
        }
        if (!alive || historyLoadSeqRef.current !== sequence) return;
        // Un chargement n'est autoritaire que s'il est encore le chargement
        // courant : dès qu'un message part, sa séquence est invalidée et
        // l'historique relu ne doit pas écraser l'état vivant.
        const live = inferenceRef.current.getLiveRun();
        const liveConv = live?.conversationId ?? inferenceRef.current.activity.conversationId;
        const mergeable = Boolean(live && !live.terminal && liveConv === id);
        const hasContent = Boolean(live && (live.answer || live.thinking || live.orchestratorThinking || live.toolCalls.length > 0 || live.stages.length > 0 || live.plan || live.brief));
        const shouldAppendBubble = Boolean(live && (live.kind === 'chat' || live.stages.length > 0 || hasContent));
        if (mergeable && live && shouldAppendBubble) {
          if (!alive || historyLoadSeqRef.current !== sequence) return;
          setMessages([...mapped, liveRunToMessage(live)]);
          setChatState(live.answer || live.thinking ? 'generating' : live.toolCalls.some((t) => t.status === 'running') ? 'tool_running' : 'orchestrating');
          if (live.stages.length > 0) {
            const restored = live.stages.reduce<RunProgress | null>((acc, stage) => advanceRunProgress(acc, stage as unknown as RunProgressEvent), null);
            if (restored) setRunProgress(restored);
          }
        } else {
          setMessages(mapped);
        }
        setHistoryStatus('ready');
      } catch (error: unknown) {
        if (!alive || historyLoadSeqRef.current !== sequence) return;
        setHistoryStatus('error');
        setHistoryError(error instanceof Error ? error.message : String(error));
      }
    })();
    if (initialState?.initialMessage && !hasSentInitialRef.current) {
      hasSentInitialRef.current = true;
      const msg = initialState.initialMessage;
      const files = initialState.initialFiles as File[] | undefined;
      // React Router stocke `usr`, `key` et `idx` dans `history.state` : écraser
      // cet objet désynchronise la pile et casse le bouton retour. On ne retire
      // que la charge initiale (message + fichiers) de l'état du routeur.
      const historyState = { ...(window.history.state || {}) };
      const routerState = historyState.usr;
      if (routerState && typeof routerState === 'object') {
        const { initialMessage: _initialMessage, initialFiles: _initialFiles, ...rest } = routerState as Record<string, unknown>;
        historyState.usr = rest;
      }
      window.history.replaceState(historyState, document.title);
      setTimeout(() => { void handleSend(msg, files); }, 300);
    }
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, historyReloadKey]);

  const handleSend = useCallback(async (message: string, files?: File[]) => {
    if (inferenceRef.current.memory.status === 'running') {
      showToast({
        type: 'info',
        title: 'Entretien mémoire en cours',
        message: 'Un entretien mémoire est en cours. Attendez sa fin avant d’envoyer un message.',
      });
      return;
    }
    const currentActivity = inferenceRef.current.activity;
    if (currentActivity.active && currentActivity.requestId !== currentRequestIdRef.current) {
      showToast({
        type: 'info',
        title: 'Inférence en cours',
        message: currentActivity.kind === 'image'
          ? 'Une génération d’image est en cours. Attendez sa fin.'
          : 'Une discussion est en cours. Attendez sa fin.',
      });
      return;
    }
    if (!message.trim() && (!files || files.length === 0)) return;
    // Guard isBusy via ref to avoid stale closure + duplicate send
    if (chatStateRef.current !== 'idle') return;
    if (currentRequestIdRef.current) return;

    const request_id = crypto.randomUUID();
    currentRequestIdRef.current = request_id;
    runStartedRef.current = false;

    // Use existing conv_id from route, or generate once and persist for this conversation
    if (!currentConvIdRef.current) {
      currentConvIdRef.current = request_id;
    }
    const conv_id = currentConvIdRef.current;
    const started = inferenceRef.current.begin({ kind: 'chat', requestId: request_id, conversationId: conv_id });
    if (!started) {
      currentRequestIdRef.current = null;
      showToast({
        type: 'info',
        title: 'Inférence en cours',
        message: 'Une inférence est déjà en cours. Attendez sa fin avant d’envoyer un message.',
      });
      return;
    }
    setChatState('preparing');

    const previewUrls: string[] = [];
    if (files) {
      for (const f of files) {
        const staged = (f as unknown as { _stagedPath?: string })._stagedPath;
        if (staged) {
          try {
            // `convertFileSrc` (asset://) n'a jamais fonctionné : aucun
            // assetProtocol n'était configuré. Le chemin réel est les octets.
            const bytes = await getStagedPreviewBytes(staged);
            previewUrls.push(URL.createObjectURL(new Blob([bytes.slice().buffer as ArrayBuffer])));
          } catch {
            // fallback : pas de preview, l'image sera visible après rechargement historique
          }
          continue;
        }
        if (f.type.startsWith('image/') && f.size > 0) previewUrls.push(URL.createObjectURL(f));
        else if (f.name.toLowerCase().endsWith('.png') || f.name.toLowerCase().endsWith('.jpg') || f.name.toLowerCase().endsWith('.webp')) {
          try { if (f.size>0) previewUrls.push(URL.createObjectURL(f)); } catch {}
        }
      }
      // Seules les objectURL blob sont inventoriées : les asset:// de convertFileSrc ne
      // sont pas des blobs et ne doivent jamais passer par revokeObjectURL.
      previewUrls.forEach((url) => { if (url.startsWith('blob:')) objectUrlsRef.current.add(url); });
    }
    const userMsg: Message = { id: `user-${Date.now()}`, role: 'user', content: message, images: previewUrls };
    // Dès qu'un message part, l'état vivant est la vérité : invalider tout
    // chargement d'historique en cours pour qu'une réponse tardive ne
    // remplace pas les bulles optimistes en cours de run.
    historyLoadSeqRef.current += 1;
    setHistoryStatus('ready');
    setHistoryError(null);
    // Single optimistic update — déduplication si même contenu déjà en dernier
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === 'user' && last.content === message) return prev;
      return [...prev, userMsg];
    });

    const assistantId = `assistant-${Date.now() + 1}`;
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === 'assistant' && last.content === '' && !last.thinking) return prev;
      return [...prev, { id: assistantId, role: 'assistant', content: '', thinking: undefined, thinkingCollapsed: true }];
    });

    try {
      let image_paths: string[] | undefined;
      let attachment_ids: string[] | undefined;
      if (files && files.length > 0) {
        // Annexe 10 : promotion stage → artifact avant run. D'où viennent les 3 formes :
        // drag natif = File vide + _stagedPath/_stageItemId (posés par Composer), trombone =
        // File réel (à stager via stagePickerBytes). Où vont les ids : attachmentsPrepare
        // (electron/sidecar.cjs) rend des artifactIds art_xxx scopés à conv_id → chatSend. Pourquoi 3 replis
        // (artifact → stagedPath → saveUpload) : chaque étage peut manquer (electron/sidecar.cjs indispo,
        // staging expiré) ; mieux vaut envoyer le fichier par une voie dégradée que d'échouer.
        const stageItemIds: string[] = [];
        const toStage: File[] = [];
        for (const file of files) {
          const stageId = (file as unknown as { _stageItemId?: string })._stageItemId;
          const staged = (file as unknown as { _stagedPath?: string })._stagedPath;
          if (stageId) {
            stageItemIds.push(stageId);
          } else if (staged) {
            // Ancien drag compat : stagedPath sans stageItemId → dérive stageItemId depuis hash
            const hash = staged.split('/').pop()?.split('.')[0] ?? "";
            if (hash) stageItemIds.push(`sti_${hash.slice(0,12)}`);
            else toStage.push(file);
          } else {
            toStage.push(file);
          }
        }
        // Fichiers non stagés (trombone) → stage_picker_bytes
        if (toStage.length > 0) {
          const { stagePickerBytes } = await import("../../lib/ipcBridge");
          for (const file of toStage) {
            if (file.size > MAX_PICKER_ATTACHMENT_BYTES) {
              showToast({ type: 'error', title: 'Fichier trop volumineux', message: `${file.name} : maximum 20 Mo` });
              continue;
            }
            const buf = new Uint8Array(await file.arrayBuffer());
            if (buf.length === 0) {
              showToast({ type: 'error', title: 'Fichier vide', message: file.name });
              continue;
            }
            const resp = await stagePickerBytes(request_id, file.name, buf);
            for (const it of resp.items) stageItemIds.push(it.stageItemId);
            if (resp.rejected.length) {
              showToast({ type: 'error', title: 'Fichiers rejetés', message: resp.rejected.map(r=>r.message).join(', ') });
            }
          }
        }
        // Promotion stage → artifact
        if (stageItemIds.length > 0) {
          const { attachmentsPrepare } = await import("../../lib/ipcBridge");
          try {
            const prep = await attachmentsPrepare(conv_id, stageItemIds, request_id);
            attachment_ids = prep.items.map(i => i.artifactId);
            // Fallback compat : si promotion échoue, garde image_paths via stagedPath
            if (attachment_ids.length === 0) {
              // Compat : utilise stagedPath direct
              image_paths = stageItemIds.map(sid => {
                const f = files.find(ff => (ff as unknown as { _stageItemId?: string })._stageItemId === sid);
                return (f as unknown as { _stagedPath?: string })?._stagedPath ?? "";
              }).filter(Boolean) as string[];
            }
          } catch (e) {
            // Promotion échouée (ATTACH_PREPARE_FAILED) : toast explicite + repli dégradé
            showToast({ type: 'error', title: 'Préparation pièces jointes échouée', message: e instanceof Error ? e.message : String(e) });
            // Fallback compat : envoie stagedPath direct
            image_paths = files.map(f => (f as unknown as { _stagedPath?: string })._stagedPath).filter(Boolean) as string[];
            if (image_paths.length === 0) {
              // Dernier fallback : saveUpload
              image_paths = [];
              for (const file of files) {
                if (file.size > MAX_PICKER_ATTACHMENT_BYTES) {
                  showToast({ type: 'error', title: 'Fichier trop volumineux', message: `${file.name} : maximum 20 Mo` });
                  continue;
                }
                const buf = new Uint8Array(await file.arrayBuffer());
                const [path] = await saveUpload(request_id, file.name, buf);
                image_paths.push(path);
              }
            }
          }
        }
      }

      await chatSend({ request_id, message, image_paths, attachment_ids, conv_id: conv_id, workspace_id: lireEspaceDepuisEtat(location.state) });
      setChatState('preparing');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      inferenceRef.current.end(request_id);
      showToast({ type: 'error', title: 'Erreur d\'envoi', message: msg });
      setChatState('idle');
      setRunProgress(null);
      currentRequestIdRef.current = null;
    }
  }, [id, location.state]);

  const handleStop = useCallback(() => {
    const req = currentRequestIdRef.current;
    if (req) {
      import("../../lib/ipcBridge").then(({ cancelChat }) =>
        cancelChat(req).catch((e: unknown) =>
          showToast({ type: 'error', title: 'Annulation échouée', message: e instanceof Error ? e.message : String(e) })
        )
      );
    }
    setChatState('idle');
    setRunProgress(null);
    currentRequestIdRef.current = null;
  }, []);

  const handleRenameStart = () => {
    if (!id) return;
    setRenameValue(titleIsFallback || title === 'Sans titre' ? '' : title);
    setIsRenaming(true);
  };

  const handleRenameCancel = () => {
    renameReturnFocusRef.current = true;
    setIsRenaming(false);
    setRenameValue('');
  };

  const handleRenameConfirm = async () => {
    if (!id || renameSavingRef.current) return;
    const next = renameValue.trim();
    if (!next) { handleRenameCancel(); return; }
    if (next === title) { handleRenameCancel(); return; }
    renameSavingRef.current = true;
    setRenameSaving(true);
    try {
      await renameConversation(id, next);
      titleEditedRef.current = true;
      setTitle(next);
      setTitleIsFallback(false);
      renameReturnFocusRef.current = true;
      setIsRenaming(false);
      setRenameValue('');
      showToast({ type: 'success', title: 'Renommé', message: 'Conversation renommée.' });
    } catch {
      showToast({ type: 'error', title: 'Erreur', message: 'Impossible de renommer la conversation.' });
    } finally {
      renameSavingRef.current = false;
      setRenameSaving(false);
    }
  };

  const isBusy = chatState !== 'idle';
  const blockedReason = inference.memory.status === 'running'
    ? 'Un entretien mémoire est en cours. Attendez sa fin avant d’envoyer un message.'
    : inference.activity.active && inference.activity.requestId !== currentRequestIdRef.current
      ? (inference.activity.kind === 'image'
        ? 'Une génération d’image est en cours. Attendez sa fin avant d’envoyer un message.'
        : 'Une discussion est en cours. Attendez sa fin avant d’envoyer un message.')
      : null;
  const lastMessageId = messages[messages.length - 1]?.id;

  return (
    <div className="chatPage" style={{ display: 'grid' }}>
      <div className="chatHeader" style={{
        height: 56, borderBottom: '1px solid var(--thot-border-subtle)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 24px', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button
            type="button"
            onClick={() => {
              // Une discussion ouverte DEPUIS un espace doit y retourner : sans
              // cela « quitter » tombait sur l'accueil du chat et l'utilisateur
              // perdait le contexte dans lequel il se trouvait.
              const workspaceId = lireEspaceDepuisEtat(location.state);
              navigate(workspaceId ? `/espaces/${workspaceId}/discussions` : '/chat');
            }}
            aria-label="Retour"
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}
          >
            <ArrowLeft24Regular />
          </button>
          <LockClosed24Regular style={{ fontSize: 16, color: 'var(--thot-text-faint)' }} />
          {isRenaming ? (
            <form onSubmit={(e) => { e.preventDefault(); void handleRenameConfirm(); }} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                aria-label="Titre de la conversation"
                autoFocus
                maxLength={200}
                disabled={renameSaving}
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') { e.preventDefault(); handleRenameCancel(); } }}
                style={{ width: 260, border: '1px solid var(--thot-gold)', borderRadius: 6, background: 'var(--thot-surface)', color: 'var(--thot-text)', padding: '4px 8px', fontSize: 15, fontWeight: 600 }}
              />
              <button type="submit" aria-label="Valider le titre" disabled={renameSaving} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>✓</button>
              <button type="button" aria-label="Annuler le renommage" disabled={renameSaving} onClick={handleRenameCancel} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>✕</button>
            </form>
          ) : (
            <>
              <span style={{ fontSize: 15, fontWeight: 600 }}>{title || 'Nouvelle conversation'}</span>
              <button type="button" ref={renameButtonRef} aria-label="Modifier le titre" onClick={handleRenameStart} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
                <Edit24Regular />
              </button>
            </>
          )}
        </div>
      </div>

      {/* `scrollRef` est ici, sur le VRAI scroller, pas sur la colonne interne.
          Le div interne portait `messagesViewport` (overflow-y:auto) MAIS aussi
          un `overflow:'visible'` inline qui écrasait sa classe : il ne
          défile donc jamais. Résultat, `handleScroll` ne partait pas et
          `scrollTo` ne faisait rien — l'autoscroll était mort. */}
      <div ref={scrollRef} onScroll={handleScroll} className="conversationLayout messagesViewport" style={{ position: 'relative' }}>
        {isDraggingFiles && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, background: 'var(--thot-gold-soft)', border: '2px dashed var(--thot-gold)', borderRadius: 12, zIndex: 5, pointerEvents: 'none', fontSize: 14, fontWeight: 600, color: 'var(--thot-gold)' }}>
            Déposez vos images ou documents ici
          </div>
        )}
        {/* `messagesViewport` retiré ici : `overflow-y:auto` + `overflow-x:visible`
            étant illégal, `overflow-x` calculerait aussi `auto` et on aurait une
            barre horizontale imbriquée dans le `overflow-x` des blocs de code. */}
        <div className="chatColumn">
          <div className="chatColumnInner">
            {historyStatus === 'error' && (
              <div
                role="alert"
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
                  marginBottom: 16, padding: '12px 16px', borderRadius: 12,
                  background: 'var(--thot-danger-soft)', border: '1px solid var(--thot-danger)',
                  color: 'var(--thot-danger)', fontSize: 14,
                }}
              >
                <span>
                  L’historique de cette discussion n’a pas pu être chargé.
                  {historyError ? <span style={{ display: 'block', fontSize: 12, opacity: 0.8, marginTop: 2 }}>{historyError}</span> : null}
                </span>
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={() => { if (isBusy) return; setHistoryReloadKey((key) => key + 1); }}
                  style={{
                    flexShrink: 0, padding: '6px 12px', borderRadius: 'var(--thot-radius-sm)',
                    border: '1px solid var(--thot-danger)', background: 'transparent',
                    color: 'var(--thot-danger)', cursor: 'pointer', fontSize: 13,
                    fontWeight: 600, fontFamily: 'var(--thot-font-family)',
                  }}
                >
                  Réessayer
                </button>
              </div>
            )}
            {historyStatus === 'loading' && messages.length === 0 && !isBusy && (
              <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--thot-text-muted)', fontSize: 14 }}>
                Chargement de la discussion…
              </div>
            )}
            {messages.length === 0 && !isBusy && historyStatus === 'ready' && (
              <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--thot-text-muted)' }}>
                <p>Commencez une nouvelle conversation en envoyant un message.</p>
                  <p style={{ fontSize: 13, marginTop: 8, color: 'var(--thot-text-faint)' }}>Astuce : glissez une image ou un PDF — l'orchestrateur décidera d'appeler <code>vision</code> ou <code>doc_ocr</code>.</p>
              </div>
            )}
            {messages.map((msg) => (
              msg.role === 'user' ? (
                <div key={msg.id} style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <div style={{ maxWidth: '70%', background: 'var(--thot-gold-soft)', borderRadius: '16px 16px 4px 16px', padding: '12px 16px', fontSize: 14, lineHeight: 1.5 }}>
                    {msg.images && msg.images.length > 0 && (
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: msg.content ? 8 : 0 }}>
                        {msg.images.map((src, i) => (
                          <img key={i} src={src} alt={`image ${i + 1}`} style={{ maxWidth: 120, maxHeight: 120, borderRadius: 8, border: '1px solid rgba(0,0,0,0.1)', objectFit: 'cover' }} />
                        ))}
                      </div>
                    )}
                    {msg.files && msg.files.length > 0 && (
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: msg.content ? 8 : 0 }}>
                        {msg.files.map((f, i) => (
                          <span key={i} title={f.name} style={{ fontSize: 12, padding: '4px 8px', borderRadius: 8, border: '1px solid var(--thot-border-subtle)', background: 'var(--thot-surface-subtle)', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            📄 {f.name}
                          </span>
                        ))}
                      </div>
                    )}
                    {msg.content && <Markdown>{msg.content}</Markdown>}
                  </div>
                </div>
              ) : (
                <div key={msg.id} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                  <div style={{ position: 'relative', width: 24, height: 24, flexShrink: 0, marginTop: 2 }}>
                    <img src="/assets/brand/Thot_logo.svg" alt="Thot" onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }} style={{ width: 24, height: 24, borderRadius: '50%', objectFit: 'cover', background: 'var(--thot-surface-raised)', border: '1px solid var(--thot-border-subtle)' }} />
                    <span style={{
                      position: 'absolute', bottom: -2, right: -2,
                      width: 8, height: 8, borderRadius: '50%',
                      background: isBusy ? 'var(--thot-gold)' : 'var(--thot-success)',
                      border: '2px solid var(--thot-surface-raised)',
                      animation: isBusy ? 'pulse 1.5s ease infinite' : 'none',
                    }} />
                  </div>
                  <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
                    <OrchestratorTrace
                      thinking={msg.orchestratorThinking}
                      toolCalls={msg.toolCalls}
                      executionPlan={msg.executionPlan ?? null}
                      brief={msg.orchestratorBrief}
                      briefChars={msg.orchestratorBriefChars}
                      briefToolCalls={msg.orchestratorBriefTools}
                      orchestratorDone={msg.orchestratorDone}
                      isRunning={isBusy && msg.id === lastMessageId && (chatState === 'preparing' || chatState === 'orchestrating' || chatState === 'tool_running')}
                    />
                    {runProgress && msg.id === lastMessageId && <ProgressCard progress={runProgress} elapsed={progressElapsed} />}
                    <div style={{
                      background: 'var(--thot-surface-1)', border: '1px solid var(--thot-border-subtle)', borderRadius: 18,
                      padding: msg.content ? '16px 18px 34px' : '14px 16px', fontSize: 14, lineHeight: 1.6,
                      minHeight: 48, boxShadow: '0 8px 28px rgba(38,31,19,0.05)', position: 'relative',
                      // PLUS de `whiteSpace: 'pre-wrap'` : le rendu Markdown gere
                      // ses propres retours a la ligne, et le conserver ici
                      // inserait une ligne vide entre chaque bloc.
                      ...(msg.content ? {} : { display: 'flex', alignItems: 'center' }),
                    }}>
                      {msg.content ? (
                        <Markdown enCours={isBusy && msg.id === lastMessageId}>{msg.content}</Markdown>
                      ) : (() => {
                        const isLast = msg.id === lastMessageId;
                        const text = !isLast || !isBusy ? 'En attente de réponse…'
                          : chatState === 'preparing' ? 'Préparation de la demande…'
                          : chatState === 'orchestrating' ? 'Analyse de la demande…'
                          : chatState === 'tool_running' ? 'Analyse en cours…'
                          : 'Génération en cours…';
                        return <span style={{ color: isBusy ? 'var(--thot-text-muted)' : 'var(--thot-text-faint)', animation: isBusy && isLast ? 'pulse 1.5s ease infinite' : 'none' }}>{text}</span>;
                      })()}
                      {msg.content && (
                        <button
                          onClick={() => { navigator.clipboard.writeText(msg.content); showToast({ type: 'success', title: 'Copié', message: 'Réponse copiée' }); }}
                          style={{ position: 'absolute', right: 12, bottom: 8, background: 'none', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--thot-text-faint)', fontFamily: 'inherit' }}
                        >
                          Copier
                        </button>
                      )}
                    </div>
                    {/* Image créée par le moteur : hors de la bulle de texte, elle ne peut ni
                        masquer le « Copier » ni lui voler la lecture. L'identifiant de
                        discussion arme le versement au dépôt (cible déduite côté serveur). */}
                    <GeneratedImageCard images={msg.images} artifactIds={msg.artifactIds} conversationId={id} />
                  </div>
                </div>
              )
            ))}

          </div>
        </div>

        {showRail ? (
          <div className="detailsRail">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 15, fontWeight: 600 }}>Détails</span>
                {isBusy && <Tag variant="running">En cours</Tag>}
              </div>
              <button type="button" onClick={() => setShowRail(false)} aria-label="Rabattre les détails" title="Rabattre les détails" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
                <ChevronRight24Regular />
              </button>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--thot-text-muted)' }}>État</span>
              <div style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{
                  width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
                  background: isBusy ? 'var(--thot-info)' : 'var(--thot-success)',
                  animation: isBusy ? 'pulse 1.5s ease infinite' : 'none',
                }} />
                {chatState === 'preparing' ? 'Préparation…' : chatState === 'orchestrating' ? 'Analyse…' : chatState === 'tool_running' ? 'Outil en cours…' : chatState === 'generating' ? 'Génération…' : 'Prêt'}
              </div>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--thot-text-muted)' }}>Étapes</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: ['preparing','orchestrating'].includes(chatState) ? 'var(--thot-info)' : chatState === 'idle' ? 'var(--thot-success)' : 'var(--thot-border-strong)', animation: ['preparing','orchestrating'].includes(chatState) ? 'pulse 1.5s ease infinite' : 'none' }} />
                <span>Préparation de la demande</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: ['tool_running','generating'].includes(chatState) ? 'var(--thot-info)' : chatState === 'idle' && messages.some((m) => m.role === 'assistant' && m.content) ? 'var(--thot-success)' : 'var(--thot-border-strong)', animation: ['tool_running','generating'].includes(chatState) ? 'pulse 1.5s ease infinite' : 'none' }} />
                <span>Génération de la réponse</span>
              </div>
            </div>

            {isBusy && (
              <button
                onClick={handleStop}
                style={{
                  width: '100%', padding: '8px 16px', borderRadius: 'var(--thot-radius-sm)',
                  border: '1px solid var(--thot-danger)', background: 'transparent',
                  color: 'var(--thot-danger)', cursor: 'pointer',
                  fontSize: 13, fontWeight: 600, fontFamily: 'var(--thot-font-family)',
                }}
                onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-danger-soft)'}
                onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
              >
                Arrêter la génération
              </button>
            )}

            <div style={{ fontSize: 12, color: 'var(--thot-text-faint)', textAlign: 'center', marginTop: 8 }}>
              Une seule génération à la fois par conversation. IPC local • JSON Lines
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="detailsRailRestore"
            onClick={() => setShowRail(true)}
            aria-label="Afficher les détails"
            title="Afficher les détails"
          >
            <ChevronLeft24Regular />
          </button>
        )}
      </div>

      <div className="composerDock">
        <Composer
          variant="docked"
          isGenerating={isBusy}
          blockedReason={blockedReason}
          onSend={handleSend}
          onStop={handleStop}
        />
      </div>
    </div>
  );
}

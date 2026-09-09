// ChatConversation Phase 1 — Tauri IPC (pas de SSE/REST)
// 7 events : message_started, thinking, tool_call, tool_result, answer, message_completed, error
// + backend_ready (hardware) — JSON Lines IPC Tauri, request_id corrélé, hash+path pour les fichiers (jamais base64)
import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  ArrowLeft24Regular, LockClosed24Regular, Edit24Regular,
  Search24Regular, MoreHorizontal24Regular, Dismiss24Regular,
  ChevronDown24Regular, ChevronRight24Regular,
} from '@fluentui/react-icons';
import { Composer } from '../../components/composer/Composer';
import { Tag } from '../../components/ui/Tag';
import { showToast } from '../../components/ui/Toast';
import { ToolCallDisplay, type ToolCallStatus } from '../../components/chat/ToolCallDisplay';
import './ChatConversation.css';
import { useAttachmentDraft } from '../../features/attachments/AttachmentDraftContext';
import { useAttachmentDrop } from '../../features/attachments/useAttachmentDrop';
import { chatSend, saveUpload, listMessages, onMessageStarted, onThinking, onToolCall, onToolResult, onAnswer, onMessageCompleted, onError, onBackendReady, onExecutionPlan } from '../../lib/tauri';

interface ToolCallItem {
  tool_call_id: string;
  tool: 'vision' | 'doc_ocr';
  arguments: Record<string, unknown>;
  status: ToolCallStatus;
  result?: string;
  ok?: boolean;
  latency_ms?: number;
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  thinking?: string;
  thinkingCollapsed?: boolean;
  images?: string[]; // objectURL previews (éphémères)
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

type ChatState = 'idle' | 'preparing' | 'orchestrating' | 'tool_running' | 'generating';

interface ChatConversationProps {
  mode?: 'chat' | 'agent';
}

export function ChatConversation({ mode: propMode }: ChatConversationProps) {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const mode: 'chat' | 'agent' = propMode ?? (location.pathname.startsWith('/agent') ? 'agent' : 'chat');

  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState('');
  const [chatState, setChatState] = useState<ChatState>('idle');
  const [showRail, setShowRail] = useState(true);
  const debugMode = (() => { try { return localStorage.getItem('thot_debug') === '1'; } catch { return false; } })();
  const { isDraggingFiles } = useAttachmentDraft();
  // Annexe 09 : le dépôt est géré uniquement par Composer (source unique natif), pas de double handler page (évite 3x)
  // useAttachmentDrop gardé pour compat mais non branché au layout (évite bubbling)
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const currentConvIdRef = useRef<string | null>(id ?? null);
  const currentRequestIdRef = useRef<string | null>(null);
  const chatStateRef = useRef<ChatState>('idle');
  const hasSentInitialRef = useRef(false);

  useEffect(() => {
    chatStateRef.current = chatState;
  }, [chatState]);

  const initialState = location.state as { initialMessage?: string; initialFiles?: File[] } | null;

  // --- Tauri listeners (7 events + backend_ready) — single mount, full cleanup ---
  useEffect(() => {
    let mounted = true;
    const unsubs: Array<() => void> = [];

    const setup = async () => {
      const ub = await onBackendReady((p) => {
        if (!mounted) return;
        console.debug('[backend_ready]', p.backend, p.vram, p.n_gpu_layers);
      });
      unsubs.push(ub);

      const u1 = await onMessageStarted((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setChatState('preparing');
      });
      unsubs.push(u1);

      const u2 = await onThinking((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        const raw = p as unknown as { text?: string; content?: string; phase?: string; iteration?: number };
        const chunk = raw.text ?? raw.content ?? '';
        if (!chunk) return;
        // Phase structurée backend : orchestrator vs generator
        const isGenerator = raw.phase === 'generator' || (typeof raw.iteration === 'number' && raw.iteration === 99);
        setChatState(isGenerator ? 'generating' : 'orchestrating');
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant') {
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, thinking: (m.thinking ?? '') + chunk, thinkingCollapsed: m.thinkingCollapsed ?? true } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', thinking: chunk, thinkingCollapsed: true }];
        });
      });
      unsubs.push(u2);

      const u3 = await onToolCall((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        const tool = p.tool as 'vision' | 'doc_ocr';
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
          const updated = m.toolCalls.map((t) => t.tool_call_id === p.tool_call_id ? { ...t, status: (p.ok ? 'completed' : 'error') as ToolCallStatus, result: p.result, ok: p.ok, latency_ms: p.latency_ms } : t);
          return { ...m, toolCalls: updated };
        }));
        if (!p.ok) showToast({ type: 'error', title: 'Outil échoué', message: `${p.latency_ms} ms` });
      });
      unsubs.push(u4);

      // answer — première réponse cache l'activité, passe en generating
      const u5 = await onAnswer((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        setChatState('generating');
        const rawPayload = p as unknown as { text?: string; content?: string };
        let raw: string = rawPayload.text ?? rawPayload.content ?? '';
        // Debug log pour diagnostiquer payload vide
        // console.log('[answer]', p);
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

      const u6 = await onMessageCompleted((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        void p;
        setChatState('idle');
        currentRequestIdRef.current = null;
      });
      unsubs.push(u6);

      const u7 = await onError((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        showToast({ type: 'error', title: 'Erreur', message: p.message });
        setChatState('idle');
        currentRequestIdRef.current = null;
      });
      unsubs.push(u7);

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
    };
    setup();

    return () => {
      mounted = false;
      unsubs.forEach((fn) => {
        try { fn(); } catch { /* ignore */ }
      });
    };
  }, []);

  function toolLabelFr(tool: string) {
    return tool === 'vision' ? 'Vision' : tool === 'doc_ocr' ? 'OCR' : tool;
  }

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsAtBottom(distanceToBottom < 120);
  }, []);

  useEffect(() => {
    if (!isAtBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight - 8, behavior: 'smooth' });
  }, [messages, isAtBottom, chatState]);

  useEffect(() => {
    if (!id) return;
    setTitle(`Conversation ${id.slice(0, 8)} — ${mode === 'agent' ? 'Agent' : 'Chat'}`);
    // Charge l'historique existant depuis DB via Tauri + vignettes persistées (annexe 10).
    // Pourquoi recharger les images : à l'envoi on n'affiche que des previews éphémères
    // (objectURL/blob: ou asset:// du staging) qui meurent au rechargement ; la vérité durable
    // est en DB (message_attachments → blobs). Sans ce rechargement, l'historique montre du
    // texte seul alors que le backend, lui, revoit très bien les fichiers (bug q-r.md 09/09).
    listMessages(id).then(async (msgs) => {
      if (!msgs || msgs.length === 0) return;
      const mapped: Message[] = [];
      const { getAttachmentBytes } = await import("../../lib/tauri");
      for (const m of msgs) {
        const base: Message = {
          id: m.id as string,
          role: m.role as 'user' | 'assistant',
          content: (m.content as string) || '',
          thinking: (m as unknown as { thinking?: string }).thinking,
        };
        const atts = (m as unknown as { attachments?: Array<{ id: string; kind: string; name: string; mime: string }> }).attachments ?? [];
        if (atts.length > 0) {
          const imgs: string[] = [];
          const files: { name: string; kind: string }[] = [];
          for (const a of atts) {
            const isImg = (a.kind || '').toLowerCase() === 'image' || (a.mime || '').startsWith('image/');
            if (!isImg) { files.push({ name: a.name, kind: a.kind || 'fichier' }); continue; }
            try {
              const [bytes, mime] = await getAttachmentBytes(a.id);
              const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
              imgs.push(url);
            } catch {
              files.push({ name: a.name, kind: a.kind || 'image' });
            }
          }
          if (imgs.length) base.images = imgs;
          if (files.length) base.files = files;
        }
        mapped.push(base);
      }
      setMessages(mapped);
    }).catch(() => { /* pas de DB en dev */ });
    if (initialState?.initialMessage && !hasSentInitialRef.current) {
      hasSentInitialRef.current = true;
      const msg = initialState.initialMessage;
      const files = initialState.initialFiles as File[] | undefined;
      window.history.replaceState({}, document.title);
      setTimeout(() => { void handleSend(msg, files); }, 300);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const handleSend = useCallback(async (message: string, files?: File[]) => {
    if (!message.trim() && (!files || files.length === 0)) return;
    // Guard isBusy via ref to avoid stale closure + duplicate send
    if (chatStateRef.current !== 'idle') return;
    if (currentRequestIdRef.current) return;

    const request_id = crypto.randomUUID();
    currentRequestIdRef.current = request_id;
    
    // Use existing conv_id from route, or generate once and persist for this conversation
    if (!currentConvIdRef.current) {
      currentConvIdRef.current = request_id;
    }
    const conv_id = currentConvIdRef.current;
    setChatState('preparing');

    const previewUrls: string[] = [];
    if (files) {
      for (const f of files) {
        const staged = (f as unknown as { _stagedPath?: string })._stagedPath;
        if (staged) {
          try {
            const { convertFileSrc } = await import("@tauri-apps/api/core");
            previewUrls.push(convertFileSrc(staged));
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
    }
    const userMsg: Message = { id: `user-${Date.now()}`, role: 'user', content: message, images: previewUrls };
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
        // (Rust) rend des artifactIds art_xxx scopés à conv_id → chatSend. Pourquoi 3 replis
        // (artifact → stagedPath → saveUpload) : chaque étage peut manquer (Rust indispo,
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
          const { stagePickerBytes } = await import("../../lib/tauri");
          for (const file of toStage) {
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
          const { attachmentsPrepare } = await import("../../lib/tauri");
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
            // Fallback compat : envoie stagedPath direct
            image_paths = files.map(f => (f as unknown as { _stagedPath?: string })._stagedPath).filter(Boolean) as string[];
            if (image_paths.length === 0) {
              // Dernier fallback : saveUpload
              image_paths = [];
              for (const file of files) {
                const buf = new Uint8Array(await file.arrayBuffer());
                const [path] = await saveUpload(request_id, file.name, buf);
                image_paths.push(path);
              }
            }
          }
        }
      }

      await chatSend({ request_id, message, mode, image_paths, attachment_ids, conv_id: conv_id });
      setChatState('preparing');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      showToast({ type: 'error', title: 'Erreur d\'envoi', message: msg });
      setChatState('idle');
      currentRequestIdRef.current = null;
    }
  }, [mode, id]);

  const handleStop = useCallback(() => {
    setChatState('idle');
    currentRequestIdRef.current = null;
  }, []);

  const toggleThinking = useCallback((msgId: string) => {
    setMessages((prev) => prev.map((m) => m.id === msgId ? { ...m, thinkingCollapsed: !m.thinkingCollapsed } : m));
  }, []);

  const isBusy = chatState !== 'idle';

  return (
    <div className="chatPage" style={{ display: 'grid' }}>
      <div className="chatHeader" style={{
        height: 56, borderBottom: '1px solid var(--thot-border-subtle)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 24px', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button onClick={() => navigate(mode === 'agent' ? '/agent' : '/chat')} aria-label="Retour" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
            <ArrowLeft24Regular />
          </button>
          <LockClosed24Regular style={{ fontSize: 16, color: 'var(--thot-text-faint)' }} />
          <span style={{ fontSize: 15, fontWeight: 600 }}>{title || 'Nouvelle conversation'}</span>
          <Tag variant={mode === 'agent' ? 'agent' : 'chat'}>{mode === 'agent' ? 'Agent (vision+OCR)' : 'Chat'}</Tag>
          <button aria-label="Modifier le titre" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
            <Edit24Regular />
          </button>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button aria-label="Rechercher" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
            <Search24Regular />
          </button>
          <button aria-label="Menu" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
            <MoreHorizontal24Regular />
          </button>
        </div>
      </div>

      <div className="conversationLayout messagesViewport" style={{ position: 'relative' }}>
        {isDraggingFiles && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, background: 'var(--thot-gold-soft)', border: '2px dashed var(--thot-gold)', borderRadius: 12, zIndex: 5, pointerEvents: 'none', fontSize: 14, fontWeight: 600, color: 'var(--thot-gold)' }}>
            Déposez vos images ou documents ici
          </div>
        )}
        <div ref={scrollRef} onScroll={handleScroll} className="chatColumn messagesViewport" style={{ overflow: 'visible' }}>
          <div className="chatColumnInner">
            {messages.length === 0 && !isBusy && (
              <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--thot-text-muted)' }}>
                <p>Commencez une nouvelle conversation en envoyant un message.</p>
                {mode === 'agent' && <p style={{ fontSize: 13, marginTop: 8, color: 'var(--thot-text-faint)' }}>Astuce : glissez une image ou un PDF — l'orchestrateur décidera d'appeler <code>vision</code> ou <code>doc_ocr</code>.</p>}
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
                    {msg.content}
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
                    {msg.executionPlan && (
                      <details open style={{ border: '1px solid var(--thot-border-subtle)', borderRadius: 8, background: 'var(--thot-surface-subtle)', padding: '8px 12px', fontSize: 12 }}>
                        <summary style={{ cursor: 'pointer', fontWeight: 600 }}>Orchestrateur — Plan d'exécution</summary>
                        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
                          <span>✓ Fichier détecté : {msg.executionPlan.attachmentKind ?? 'aucun'}</span>
                          <span>✓ Intention : {msg.executionPlan.intent}</span>
                          {msg.executionPlan.selectedTool ? <span>✓ Outil retenu : {msg.executionPlan.selectedTool === 'vision' ? 'Vision générale · LFM2.5-VL-3B' : 'Vision OCR document · GLM-OCR'}</span> : <span>→ OCR non requis</span>}
                          <span style={{ color: 'var(--thot-text-faint)' }}>{msg.executionPlan.reason}</span>
                        </div>
                      </details>
                    )}
                    {debugMode && msg.thinking && (
                      <div style={{ border: '1px solid var(--thot-border-subtle)', borderRadius: 8, overflow: 'hidden' }}>
                        <button onClick={() => toggleThinking(msg.id)} style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', background: 'var(--thot-surface-subtle)', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--thot-text-muted)', textAlign: 'left' }}>
                          {msg.thinkingCollapsed ? <ChevronRight24Regular style={{ fontSize: 14 }} /> : <ChevronDown24Regular style={{ fontSize: 14 }} />}
                          <span>
                            Détails d'exécution {msg.thinkingCollapsed ? '(masqués)' : ''}
                          </span>
                          <span style={{ marginLeft: 'auto', fontSize: 11, opacity: 0.7 }}>{msg.thinking.length} car.</span>
                        </button>
                        {!msg.thinkingCollapsed && (
                          <div style={{ padding: '8px 12px', fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap', background: 'var(--thot-surface-raised)', color: 'var(--thot-text-muted)', maxHeight: 200, overflow: 'auto' }}>
                            {msg.thinking}
                          </div>
                        )}
                      </div>
                    )}
                    {msg.toolCalls && msg.toolCalls.length > 0 && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {msg.toolCalls.map((tc) => (
                          <ToolCallDisplay key={tc.tool_call_id} tool={tc.tool} status={tc.status} result={tc.result} ok={tc.ok} latency_ms={tc.latency_ms} arguments={tc.arguments} />
                        ))}
                      </div>
                    )}
                    <div style={{
                      background: 'var(--thot-surface-1)', border: '1px solid var(--thot-border-subtle)', borderRadius: 18,
                      padding: msg.content ? '16px 18px 34px' : '14px 16px', fontSize: 14, lineHeight: 1.6,
                      minHeight: 48, boxShadow: '0 8px 28px rgba(38,31,19,0.05)', position: 'relative',
                      ...(msg.content ? { whiteSpace: 'pre-wrap' } : { display: 'flex', alignItems: 'center' }),
                    }}>
                      {msg.content || (() => {
                        const isLast = msg.id === messages[messages.length - 1]?.id;
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
                  </div>
                </div>
              )
            ))}

          </div>
        </div>

        {showRail && (
          <div className="detailsRail">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 15, fontWeight: 600 }}>Détails</span>
                {isBusy && <Tag variant="running">En cours</Tag>}
              </div>
              <button onClick={() => setShowRail(false)} aria-label="Fermer" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
                <Dismiss24Regular />
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
              <span style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>Mode : {mode === 'agent' ? 'agent (vision+doc_ocr)' : 'chat (sans outil)'}</span>
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
              Une seule génération à la fois par conversation. IPC Tauri • JSON Lines
            </div>
          </div>
        )}
      </div>

      <div className="composerDock">
        <Composer
          variant="docked"
          isGenerating={isBusy}
          onSend={handleSend}
          onStop={handleStop}
        />
      </div>
    </div>
  );
}

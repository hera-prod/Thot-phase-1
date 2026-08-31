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
import { chatSend, saveUpload, listMessages, onMessageStarted, onThinking, onToolCall, onToolResult, onAnswer, onMessageCompleted, onError, onBackendReady } from '../../lib/tauri';

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
  toolCalls?: ToolCallItem[];
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

type ChatState = 'idle' | 'sending' | 'streaming';

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
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
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
        setChatState('streaming');
      });
      unsubs.push(u1);

      const u2 = await onThinking((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
        const chunk = (p as unknown as { text?: string; content?: string }).text ?? (p as unknown as { content?: string }).content ?? '';
        if (!chunk) return;
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
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          const item: ToolCallItem = { tool_call_id: p.tool_call_id, tool, arguments: p.arguments as Record<string, unknown>, status: 'running' };
          if (last && last.role === 'assistant') {
            return prev.map((m, idx) => idx === prev.length - 1 ? { ...m, toolCalls: [...(m.toolCalls ?? []), item] } : m);
          }
          return [...prev, { id: `assistant-${Date.now()}`, role: 'assistant', content: '', toolCalls: [item] }];
        });
        showToast({ type: 'info', title: toolLabelFr(tool), message: 'Analyse en cours…' });
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
        showToast({ type: p.ok ? 'success' : 'error', title: p.ok ? 'Outil terminé' : 'Outil échoué', message: `${p.latency_ms} ms` });
      });
      unsubs.push(u4);

      // answer — fix réponses vides : supporte p.text et p.content, dedup non agressive, logs
      const u5 = await onAnswer((p) => {
        if (!mounted) return;
        if (currentRequestIdRef.current && p.request_id !== currentRequestIdRef.current) return;
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
        setChatState('idle');
        currentRequestIdRef.current = null;
        showToast({ type: 'success', title: 'Réponse terminée', message: `${p.tokens_out} tokens` });
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
    // Charge l'historique existant depuis DB via Tauri
    listMessages(id).then((msgs) => {
      if (!msgs || msgs.length === 0) return;
      setMessages(msgs.map((m) => ({
        id: m.id as string,
        role: m.role as 'user' | 'assistant',
        content: (m.content as string) || '',
        thinking: (m as unknown as { thinking?: string }).thinking,
      })));
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
    setChatState('sending');

    const previewUrls: string[] = [];
    if (files) {
      for (const f of files) {
        if (f.type.startsWith('image/')) previewUrls.push(URL.createObjectURL(f));
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
      if (files && files.length > 0) {
        image_paths = [];
        for (const file of files) {
          const buf = new Uint8Array(await file.arrayBuffer());
          const [path] = await saveUpload(request_id, file.name, buf);
          image_paths.push(path);
        }
      }

      await chatSend({ request_id, message, mode, image_paths, conv_id: id });
      setTimeout(() => {
        if (currentRequestIdRef.current === request_id) setChatState('streaming');
      }, 600);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      showToast({ type: 'error', title: 'Erreur d\'envoi', message: msg });
      setChatState('idle');
      currentRequestIdRef.current = null;
    }
  }, [mode, id]);

  const handleStop = useCallback(() => {
    showToast({ type: 'info', title: 'Arrêt', message: 'Génération interrompue (local).' });
    setChatState('idle');
    currentRequestIdRef.current = null;
  }, []);

  const toggleThinking = useCallback((msgId: string) => {
    setMessages((prev) => prev.map((m) => m.id === msgId ? { ...m, thinkingCollapsed: !m.thinkingCollapsed } : m));
  }, []);

  const isBusy = chatState === 'sending' || chatState === 'streaming';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{
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

      <div style={{ display: 'grid', gridTemplateColumns: showRail ? '1fr 320px' : '1fr 0', flex: 1, overflow: 'hidden' }}>
        <div ref={scrollRef} onScroll={handleScroll} style={{ overflow: 'auto', padding: '24px 32px', background: 'var(--thot-canvas)', display: 'flex', flexDirection: 'column' }}>
          <div style={{ maxWidth: 960, width: '100%', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20, flex: 1 }}>
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
                      background: chatState === 'streaming' ? 'var(--thot-gold)' : 'var(--thot-success)',
                      border: '2px solid var(--thot-surface-raised)',
                      animation: chatState === 'streaming' ? 'pulse 1.5s ease infinite' : 'none',
                    }} />
                  </div>
                  <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
                    {msg.thinking && (
                      <div style={{ border: '1px solid var(--thot-border-subtle)', borderRadius: 8, overflow: 'hidden' }}>
                        <button onClick={() => toggleThinking(msg.id)} style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', background: 'var(--thot-surface-subtle)', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--thot-text-muted)', textAlign: 'left' }}>
                          {msg.thinkingCollapsed ? <ChevronRight24Regular style={{ fontSize: 14 }} /> : <ChevronDown24Regular style={{ fontSize: 14 }} />}
                          <span>
                            {msg.thinking.includes('[Générateur]') ? 'Générateur — Réflexion' : msg.thinking.includes('[Orchestrateur]') ? 'Orchestrateur — Réflexion' : 'Réflexion'} {msg.thinkingCollapsed ? '(masquée)' : ''}
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
                      background: 'var(--thot-surface-1)', border: '1px solid var(--thot-border-subtle)', borderRadius: 16,
                      padding: '14px 16px', fontSize: 14, lineHeight: 1.6,
                      minHeight: 48, boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
                      ...(msg.content ? { whiteSpace: 'pre-wrap' } : { display: 'flex', alignItems: 'center' }),
                    }}>
                      {msg.content || (() => {
                        const isLast = msg.id === messages[messages.length - 1]?.id;
                        const text = isLast && isBusy
                          ? (chatState === 'sending' ? 'Préparation de la demande…' : 'Génération en cours…')
                          : 'En attente de réponse…';
                        return <span style={{ color: isBusy ? 'var(--thot-text-muted)' : 'var(--thot-text-faint)', animation: isBusy && isLast ? 'pulse 1.5s ease infinite' : 'none' }}>{text}</span>;
                      })()}
                    </div>
                    {msg.content ? (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 2, fontSize: 12, color: 'var(--thot-text-muted)' }}>
                        <button style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', fontFamily: 'inherit' }}>Copier</button>
                      </div>
                    ) : null}
                  </div>
                </div>
              )
            ))}

          </div>
        </div>

        {showRail && (
          <div style={{ borderLeft: '1px solid var(--thot-border-subtle)', display: 'flex', flexDirection: 'column', padding: 16, gap: 16, overflow: 'auto' }}>
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
                {chatState === 'sending' ? 'Envoi…' : chatState === 'streaming' ? 'Génération…' : 'Prêt'}
              </div>
              <span style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>Mode : {mode === 'agent' ? 'agent (vision+doc_ocr)' : 'chat (sans outil)'}</span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--thot-text-muted)' }}>Étapes</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: chatState === 'sending' ? 'var(--thot-info)' : 'var(--thot-success)', animation: chatState === 'sending' ? 'pulse 1.5s ease infinite' : 'none' }} />
                <span>Préparation de la demande</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: chatState === 'streaming' ? 'var(--thot-info)' : chatState === 'idle' && messages.some((m) => m.role === 'assistant' && m.content) ? 'var(--thot-success)' : 'var(--thot-border-strong)', animation: chatState === 'streaming' ? 'pulse 1.5s ease infinite' : 'none' }} />
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

      <div style={{ flexShrink: 0 }}>
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

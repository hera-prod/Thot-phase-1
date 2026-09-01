// AssistantTurn — un seul tour assistant par requête (Réponses.md § Structure)
// Remplace la logique dispersée (sending/streaming/content/thinking/toolCalls) par phase centralisée
import { ToolCallDisplay, type ToolCallStatus } from './ToolCallDisplay';

export type RunPhase = 'idle' | 'preparing' | 'orchestrating' | 'tool_running' | 'generating' | 'completed' | 'failed';

export interface ToolCallItem {
  tool_call_id: string;
  tool: 'vision' | 'doc_ocr';
  arguments: Record<string, unknown>;
  status: ToolCallStatus;
  result?: string;
  ok?: boolean;
  latency_ms?: number;
}

export interface AssistantMessage {
  id: string;
  role: 'assistant';
  content: string;
  phase?: RunPhase;
  activity?: string;
  thinking?: string;
  thinkingCollapsed?: boolean;
  toolCalls?: ToolCallItem[];
  error?: string;
}

interface Props {
  message: AssistantMessage;
  isLatest: boolean;
  isBusy: boolean;
  phase: RunPhase;
  debugMode?: boolean;
  onToggleThinking: (id: string) => void;
}

function phaseLabel(phase: RunPhase): string {
  if (phase === 'preparing') return 'Préparation de la demande…';
  if (phase === 'orchestrating') return 'Analyse de la demande…';
  if (phase === 'tool_running') return 'Analyse en cours…';
  if (phase === 'generating') return 'Génération en cours…';
  return 'En attente de réponse…';
}

export function AssistantTurn({ message, isLatest, isBusy, phase, debugMode, onToggleThinking }: Props) {
  const hasContent = message.content.trim().length > 0;
  const isWorking = isLatest && isBusy && !hasContent;

  return (
    <article className="assistantTurn enterSoft" style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
      <div style={{ position: 'relative', width: 24, height: 24, flexShrink: 0, marginTop: 2 }}>
        <img
          src="/assets/brand/Thot_logo.svg"
          alt="Thot"
          onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
          style={{ width: 24, height: 24, borderRadius: '50%', objectFit: 'cover', background: 'var(--thot-surface-raised)', border: '1px solid var(--thot-border-subtle)' }}
        />
        <span
          style={{
            position: 'absolute', bottom: -2, right: -2,
            width: 8, height: 8, borderRadius: '50%',
            background: isBusy && isLatest ? 'var(--thot-gold)' : 'var(--thot-success)',
            border: '2px solid var(--thot-surface-raised)',
            animation: isBusy && isLatest ? 'pulse 1.5s ease infinite' : 'none',
          }}
        />
      </div>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
        {isWorking && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--thot-text-muted)' }}>
            <span className="spinner" />
            <span>{message.activity ?? phaseLabel(phase)}</span>
          </div>
        )}

        {message.toolCalls && message.toolCalls.length > 0 && (
          <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {message.toolCalls.map((tc) => (
              <ToolCallDisplay key={tc.tool_call_id} tool={tc.tool} status={tc.status} result={tc.result} ok={tc.ok} latency_ms={tc.latency_ms} arguments={tc.arguments} />
            ))}
          </section>
        )}

        {debugMode && message.thinking && (
          <div style={{ border: '1px solid var(--thot-border-subtle)', borderRadius: 8, overflow: 'hidden' }}>
            <button
              onClick={() => onToggleThinking(message.id)}
              style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', background: 'var(--thot-surface-subtle)', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--thot-text-muted)', textAlign: 'left' }}
            >
              <span>{message.thinkingCollapsed ? '▶' : '▼'}</span>
              <span>Détails d'exécution</span>
              <span style={{ marginLeft: 'auto', fontSize: 11, opacity: 0.7 }}>{message.thinking.length} car.</span>
            </button>
            {!message.thinkingCollapsed && (
              <div style={{ padding: '8px 12px', fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap', background: 'var(--thot-surface-raised)', color: 'var(--thot-text-muted)', maxHeight: 200, overflow: 'auto' }}>
                {message.thinking}
              </div>
            )}
          </div>
        )}

        {hasContent && (
          <div
            style={{
              background: 'var(--thot-surface-1)', border: '1px solid var(--thot-border-subtle)', borderRadius: 16,
              padding: '14px 16px', fontSize: 14, lineHeight: 1.6, whiteSpace: 'pre-wrap', boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
            }}
          >
            {message.content}
          </div>
        )}

        {!hasContent && !isWorking && (
          <div
            style={{
              background: 'var(--thot-surface-1)', border: '1px solid var(--thot-border-subtle)', borderRadius: 16,
              padding: '14px 16px', fontSize: 14, lineHeight: 1.6, minHeight: 48, display: 'flex', alignItems: 'center', color: 'var(--thot-text-faint)',
            }}
          >
            En attente de réponse…
          </div>
        )}
      </div>
    </article>
  );
}

// OrchestratorTrace — bulle repliée par défaut montrant le travail de l'ORCHESTRATEUR.
// Ne pas confondre avec le générateur (bulle de réponse principale ci-dessous).
// Contenu : 1. réflexion (thinking itérations 0..N, jamais 99=generateur),
// 2. échanges d'outils (tool_call/result), 3. brief EXACT transmis au générateur.
// Fermée par défaut (bruit visuel nul), dépliable au clic, état local par tour.
import { useState } from 'react';
import { ToolCallDisplay, type ToolCallStatus } from './ToolCallDisplay';

export interface OrchestratorToolCall {
  tool_call_id: string;
  tool: string;
  arguments?: Record<string, unknown>;
  status: ToolCallStatus;
  result?: string;
  ok?: boolean;
  latency_ms?: number;
  evidence_ids?: string[];
  public_ids?: string[];
}

export interface OrchestratorPlan {
  attachmentKind?: string | null;
  intent: string;
  selectedTool?: string | null;
  selectedModel?: string | null;
  reason: string;
}

interface Props {
  thinking?: string;
  toolCalls?: OrchestratorToolCall[];
  executionPlan?: OrchestratorPlan | null;
  brief?: string;
  briefChars?: number;
  briefToolCalls?: number;
  orchestratorDone?: boolean;
  isRunning: boolean;
}

export function OrchestratorTrace({ thinking, toolCalls, executionPlan, brief, briefChars, briefToolCalls, orchestratorDone, isRunning }: Props) {
  const [open, setOpen] = useState(false);
  const tools = toolCalls ?? [];
  const doneCount = tools.filter((t) => t.status !== 'running').length;
  // Seul "Fichier détecté" compte comme plan affichable ; le reste (intention, outil retenu)
  // était du jargon interne. Sans fichier, pas de section plan.
  const hasFile = Boolean(executionPlan?.attachmentKind);
  const hasContent = Boolean((thinking && thinking.trim()) || tools.length > 0 || hasFile || brief);

  if (!hasContent && !isRunning) return null;

  const parts: string[] = [];
  if (tools.length > 0) parts.push(`${doneCount}/${tools.length} outil${tools.length > 1 ? 's' : ''}`);
  if (brief) parts.push(`brief ${briefChars ?? brief.length} car.`);
  else if (isRunning) parts.push('travaille…');
  const summary = parts.length > 0 ? parts.join(' · ') : 'aucune activité';

  const panelId = `orch-trace-${Math.random().toString(36).slice(2, 8)}`;

  return (
    <div style={{ border: '1px solid var(--thot-border-subtle)', borderRadius: 8, overflow: 'hidden', background: 'var(--thot-surface-subtle)' }}>
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={panelId}
        title="Voir le travail de l'orchestrateur (réflexion, outils, brief transmis au générateur)"
        style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', background: 'transparent', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--thot-text-muted)', textAlign: 'left', fontFamily: 'inherit' }}
      >
        <span aria-hidden style={{ fontSize: 10 }}>{open ? '▼' : '▶'}</span>
        <span style={{ fontWeight: 600 }}>🔍 Orchestrateur</span>
        <span style={{ opacity: 0.85 }}>{summary}</span>
        {isRunning && (
          <span className="spinner" aria-hidden style={{ width: 12, height: 12 }} />
        )}
        {orchestratorDone === false && !isRunning && (
          <span style={{ fontSize: 11, color: 'var(--thot-text-faint)' }}>(incomplet)</span>
        )}
      </button>

      {open && (
        <div id={panelId} role="region" aria-label="Trace de l'orchestrateur" style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '4px 12px 12px' }}>
          {executionPlan?.attachmentKind && (
            <section aria-label="Plan d'exécution" style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
              <span>✓ Fichier détecté : {executionPlan.attachmentKind}</span>
            </section>
          )}

          {thinking && thinking.trim() && (
            <section aria-label="Réflexion de l'orchestrateur" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text-muted)' }}>
                Réflexion <span style={{ fontWeight: 400, opacity: 0.7 }}>({thinking.length} car.)</span>
              </span>
              <div style={{ padding: '8px 10px', fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap', background: 'var(--thot-surface-raised)', color: 'var(--thot-text-muted)', borderRadius: 8, maxHeight: 200, overflow: 'auto' }}>
                {thinking}
              </div>
            </section>
          )}

          {tools.length > 0 && (
            <section aria-label="Échanges d'outils" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text-muted)' }}>
                Échanges d'outils <span style={{ fontWeight: 400, opacity: 0.7 }}>({doneCount}/{tools.length})</span>
              </span>
              {tools.map((tc) => (
                <ToolCallDisplay key={tc.tool_call_id} tool={tc.tool} status={tc.status} result={tc.result} ok={tc.ok} latency_ms={tc.latency_ms} arguments={tc.arguments} evidence_ids={tc.evidence_ids} public_ids={tc.public_ids} />
              ))}
            </section>
          )}

          {brief ? (
            <section aria-label="Brief transmis au générateur" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text-muted)' }}>
                Brief → générateur <span style={{ fontWeight: 400, opacity: 0.7 }}>
                  ({briefChars ?? brief.length} car.{typeof briefToolCalls === 'number' ? ` · ${briefToolCalls} appel${briefToolCalls > 1 ? 's' : ''}` : ''})
                </span>
              </span>
              <pre style={{ fontFamily: 'var(--thot-font-mono)', fontSize: 12, background: 'var(--thot-canvas)', padding: 10, borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0, maxHeight: 300, overflow: 'auto', border: '1px solid var(--thot-border-subtle)' }}>
                {brief}
              </pre>
            </section>
          ) : (
            isRunning && (
              <span style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>Brief pas encore transmis…</span>
            )
          )}
        </div>
      )}
    </div>
  );
}

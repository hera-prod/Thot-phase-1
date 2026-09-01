// ToolCallDisplay — affichage inline des toolcalls VLM (vision / doc_ocr)
// Spec Phase 1 : running → spinner + message, completed → details collapsible 500 chars, error → rouge
import { useState } from 'react';

export type ToolCallStatus = 'running' | 'completed' | 'error';

interface ToolCallDisplayProps {
  tool: 'vision' | 'doc_ocr';
  status: ToolCallStatus;
  result?: string;
  ok?: boolean;
  latency_ms?: number;
  arguments?: Record<string, unknown>;
}

function toolMeta(tool: string): { label: string; model: string } {
  if (tool === 'vision') return { label: 'Vision générale', model: 'LFM2.5-VL-3B' };
  if (tool === 'doc_ocr') return { label: 'Vision OCR document', model: 'GLM-OCR' };
  return { label: tool, model: '' };
}

function statusIcon(status: ToolCallStatus, ok?: boolean) {
  if (status === 'running') return <span className="spinner" style={{ width: 14, height: 14 }} />;
  if (status === 'error' || ok === false) return <span style={{ color: 'var(--thot-danger)' }}>❌</span>;
  return <span style={{ color: 'var(--thot-success)' }}>✓</span>;
}

export function ToolCallDisplay({ tool, status, result, ok, latency_ms, arguments: args }: ToolCallDisplayProps) {
  const [open, setOpen] = useState(status !== 'running');
  const meta = toolMeta(tool);

  if (status === 'running') {
    return (
      <div
        className="enterSoft"
        style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '10px 14px', borderRadius: 10,
          background: 'var(--thot-surface-2)', border: '1px solid var(--thot-border)',
          fontSize: 13, color: 'var(--thot-text-muted)',
        }}
      >
        {statusIcon(status)}
        <span>{meta.label} · {meta.model} · en cours…</span>
      </div>
    );
  }

  const isError = status === 'error' || ok === false;
  const preview = result ? (result.length > 500 ? result.slice(0, 500) + '…' : result) : '';
  const query = args?.query as string | undefined;
  const hash = (args as Record<string, unknown> | undefined)?.image_hash as string | undefined;
  const hashShort = hash ? String(hash).slice(0, 12) : undefined;
  const latencySec = latency_ms != null ? (latency_ms / 1000).toFixed(2) + ' s' : undefined;

  return (
    <div
      className="enterSoft"
      style={{
        borderRadius: 10,
        border: `1px solid ${isError ? 'var(--thot-danger-soft)' : 'var(--thot-border)'}`,
        background: isError ? 'var(--thot-danger-soft)' : 'var(--thot-surface-raised)',
        overflow: 'hidden',
      }}
    >
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', gap: 8,
          padding: '10px 14px', background: 'transparent', border: 'none',
          cursor: 'pointer', textAlign: 'left', fontFamily: 'var(--thot-font-family)',
        }}
      >
        {statusIcon(isError ? 'error' : 'completed', ok)}
        <span style={{ fontSize: 13, fontWeight: 600, color: isError ? 'var(--thot-danger)' : 'var(--thot-text)' }}>
          {meta.label}
        </span>
        <span style={{ fontSize: 12, color: 'var(--thot-text-muted)' }}>
          {meta.model} · {result ? `${result.length} car.` : '0 car.'}{latencySec ? ` · ${latencySec}` : ''}
        </span>
        <span style={{ marginLeft: 'auto', color: 'var(--thot-text-faint)', fontSize: 12 }}>{open ? '▼' : '▶'}</span>
      </button>

      {open && (
        <div style={{ padding: '0 14px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {query && (
            <div style={{ fontSize: 12, color: 'var(--thot-text-muted)' }}>
              <span style={{ fontWeight: 600 }}>Requête</span> · {query}
            </div>
          )}
          {hashShort && (
            <div style={{ fontSize: 11, color: 'var(--thot-text-faint)', fontFamily: 'var(--thot-font-mono)' }}>
              Source · {hashShort}…
            </div>
          )}
          {isError ? (
            <div style={{ fontSize: 13, color: 'var(--thot-danger)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {result || 'Erreur indéterminée'}
            </div>
          ) : (
            <>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text-muted)' }}>Résultat technique</div>
              <pre
                style={{
                  fontFamily: 'var(--thot-font-mono)', fontSize: 12,
                  background: 'var(--thot-canvas)', padding: 10, borderRadius: 8,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
                  maxHeight: 300, overflow: 'auto', border: '1px solid var(--thot-border-subtle)',
                }}
              >
                {preview || '(vide)'}
              </pre>
              {result && result.length > 500 && (
                <span style={{ fontSize: 11, color: 'var(--thot-text-faint)' }}>Aperçu 500 chars — {result.length} total</span>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

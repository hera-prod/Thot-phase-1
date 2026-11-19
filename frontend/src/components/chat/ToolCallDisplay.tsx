// ToolCallDisplay — affichage inline des toolcalls VLM (vision / doc_ocr)
// Spec Phase 1 : running → spinner + message, completed → details collapsible 500 chars, error → rouge
import { useState } from 'react';
import { modelByRole } from '../../features/models/modelCatalog';import { parseWebSources, type WebSource } from './webSources';

export type ToolCallStatus = 'running' | 'completed' | 'error';

interface ToolCallDisplayProps {
  tool: string; // vision | doc_ocr | search_documents | get_document_card | read_document | …
  status: ToolCallStatus;
  result?: string;
  ok?: boolean;
  latency_ms?: number;
  arguments?: Record<string, unknown>;
  evidence_ids?: string[];
  public_ids?: string[];
}

function toolMeta(tool: string): { label: string; model: string } {
  if (tool === 'vision') return { label: 'Vision générale', model: '' };
  if (tool === 'doc_ocr') return { label: 'Vision OCR document', model: '' };
  if (tool === 'get_source') return { label: 'Récupération source', model: '' };
  if (tool === 'read_text') return { label: 'Lecture texte', model: '' };
  // Le seul nom d'outil brut qui reaches l'utilisateur serait `web_research` :
  // les six autres sont duvocabulaire interne, et un nom de fonction n'est pas
  // une réponse à « qu'as-tu fait ? ».
  if (tool === 'web_research') return { label: 'Recherche web', model: '' };
  if (tool === 'generate_image') return { label: 'Génération d’image', model: '' };
  if (tool === 'search_info') return { label: 'Recherche dans vos pièces', model: '' };
  if (tool === 'find_files') return { label: 'Repérage de fichiers', model: '' };
  if (tool === 'ask_files') return { label: 'Lecture de fichiers', model: '' };
  if (tool === 'submit_brief') return { label: 'Brief de transfert', model: '' };
  return { label: tool, model: '' };
}

// Les arguments d'un web_research ne sont ni `query` ni `image_hash`, donc la
// ligne générique en bas les ignorerait tous : une recherche affichait « 0
// paramètre » alors que le modèle en a choisi trois.
const TYPE_LIBELLE: Record<string, string> = {
  general: 'Web général',
  news: 'Actualités',
  academic: 'Recherche académique',
};

function WebArguments({ args }: { args: Record<string, unknown> }) {
  const queries = Array.isArray(args.queries) ? args.queries.map(String) : [];
  const types = Array.isArray(args.type) ? args.type.map(String) : [];
  if (queries.length === 0 && types.length === 0) return null;
  return (
    <>
      {queries.length > 0 && (
        <div style={{ fontSize: 12, color: 'var(--thot-text-muted)' }}>
          <span style={{ fontWeight: 600 }}>Requêtes</span> · {queries.join(' · ')}
        </div>
      )}
      {types.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--thot-text-faint)' }}>
          Type · {types.map((t) => TYPE_LIBELLE[t] ?? t).join(' · ')}
        </div>
      )}
    </>
  );
}

function WebSources({ sources }: { sources: WebSource[] }) {
  if (sources.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text-muted)' }}>
        Sources lues ({sources.length})
      </div>
      {sources.map((s) => (
        <div
          key={s.url}
          style={{
            display: 'flex', flexDirection: 'column', gap: 2,
            padding: '7px 10px', borderRadius: 8,
            background: 'var(--thot-surface-2)', border: '1px solid var(--thot-border-subtle)',
          }}
        >
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text)', wordBreak: 'break-all' }}>
            {s.host}
          </span>
          <a
            href={s.url}
            target="_blank"
            rel="noreferrer noopener"
            style={{ fontSize: 11, color: 'var(--thot-accent)', wordBreak: 'break-all' }}
          >
            {s.url}
          </a>
          {s.fetchedAt && (
            <span style={{ fontSize: 11, color: 'var(--thot-text-faint)' }}>
              Récupéré le {s.fetchedAt} · {s.ageDays} j · statut {s.status}
            </span>
          )}
          {s.evidenceIds.length > 0 && (
            <span style={{ fontSize: 10, fontFamily: 'var(--thot-font-mono)', color: 'var(--thot-text-faint)' }}>
              {s.evidenceIds.length} extrait{s.evidenceIds.length > 1 ? 's' : ''} citable{s.evidenceIds.length > 1 ? 's' : ''}
              {s.publicId ? ` · ${s.publicId}` : ''}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

function statusIcon(status: ToolCallStatus, ok?: boolean) {
  if (status === 'running') return <span className="spinner" style={{ width: 14, height: 14 }} />;
  if (status === 'error' || ok === false) return <span style={{ color: 'var(--thot-danger)' }}>❌</span>;
  return <span style={{ color: 'var(--thot-success)' }}>✓</span>;
}

export function ToolCallDisplay({ tool, status, result, ok, latency_ms, arguments: args }: ToolCallDisplayProps) {
  const [open, setOpen] = useState(status !== 'running');
  const meta = toolMeta(tool);
  const modele = tool === 'vision' ? modelByRole('vision')?.name ?? '' : tool === 'doc_ocr' ? modelByRole('ocr')?.name ?? '' : '';
  const isError = status === 'error' || ok === false;
  // Les sources ne se lisent que sur un appel RÉUSSI : un refus réseau n'a pas
  // de provenance à afficher, et afficher celles du résultat d'un échec
  // ferait croire qu'une recherche a eu lieu.
  const sources = tool === 'web_research' && !isError ? parseWebSources(result) : [];

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
        <span>{meta.label} · {modele} · en cours…</span>
      </div>
    );
  }

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
          {modele ? `${modele} · ` : ''}{result ? `${result.length} car.` : '0 car.'}{latencySec ? ` · ${latencySec}` : ''}
          {sources.length > 0 ? ` · ${sources.length} source${sources.length > 1 ? 's' : ''}` : ''}
        </span>
        <span style={{ marginLeft: 'auto', color: 'var(--thot-text-faint)', fontSize: 12 }}>{open ? '▼' : '▶'}</span>
      </button>

      {open && (
        <div style={{ padding: '0 14px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {tool === 'web_research'
            ? <WebArguments args={(args ?? {}) as Record<string, unknown>} />
            : query && (
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
              <WebSources sources={sources} />
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

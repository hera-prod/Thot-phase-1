// AgentHome Phase 1 — même structure que ChatHome mais mode agent (vision+OCR)
import { useNavigate } from 'react-router-dom';
import { Bot24Regular, LockClosed24Regular, ChevronRight24Regular } from '@fluentui/react-icons';
import { Composer } from '../../components/composer/Composer';
import { showToast } from '../../components/ui/Toast';

export function AgentHome() {
  const navigate = useNavigate();

  const handleSend = async (message: string, files?: File[]) => {
    if (!message.trim() && (!files || files.length === 0)) return;
    const convId = crypto.randomUUID();
    if (files && files.length > 0) {
      showToast({ type: 'info', title: 'Fichiers joints', message: `${files.length} fichier(s) — upload lors de l'envoi.` });
    }
    navigate(`/agent/${convId}`, { state: { initialMessage: message, initialFiles: files } });
  };

  return (
    <div style={{ position: 'relative', maxWidth: 920, margin: '0 auto', padding: '64px 32px' }}>
      <img src="/assets/brand/Thot_logo.svg" alt="" aria-hidden="true" style={{ position: 'absolute', top: 0, right: 0, opacity: 0.06, width: 420, pointerEvents: 'none' }} />
      <div style={{ fontSize: 'var(--thot-text-hero)', fontWeight: 600, color: 'var(--thot-violet)' }}>
        Agent
      </div>
      <div style={{ fontSize: 'var(--thot-text-hero)', fontWeight: 700, color: 'var(--thot-text)', marginTop: 4 }}>
        Vision et OCR à la demande
      </div>
      <p style={{ fontSize: 16, color: 'var(--thot-text-muted)', marginTop: 16, maxWidth: 560, lineHeight: 1.6 }}>
        Mode agent : l'orchestrateur peut appeler <code style={{ background: 'var(--thot-violet-soft)', padding: '2px 6px', borderRadius: 6 }}>vision</code> (LFM2.5-VL-3B) et <code style={{ background: 'var(--thot-violet-soft)', padding: '2px 6px', borderRadius: 6 }}>doc_ocr</code> (GLM-OCR) en séquentiel (max 5 itérations, 15s/tool).
      </p>

      <Composer variant="hero" onSend={handleSend} />

      <section style={{ marginTop: 48 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h2 style={{ fontSize: 'var(--thot-text-section)', fontWeight: 600 }}>Conversations agent récentes</h2>
          <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 14, color: 'var(--thot-text-faint)' }}>
            Historique local <ChevronRight24Regular style={{ fontSize: 14 }} />
          </span>
        </div>
        <div style={{ display: 'flex', gap: 16, padding: 24, borderRadius: 16, background: 'var(--thot-surface-raised)', border: '1px dashed var(--thot-border)', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', textAlign: 'center' }}>
          <span style={{ color: 'var(--thot-violet)', display: 'flex' }}><Bot24Regular /></span>
          <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', maxWidth: 420, lineHeight: 1.5 }}>
            Aucune conversation agent. Glissez une image ou un PDF et posez votre question — le brief sera généré pour le générateur.
          </p>
        </div>
      </section>

      <footer style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, marginTop: 48, fontSize: 13, color: 'var(--thot-text-faint)' }}>
        <LockClosed24Regular style={{ fontSize: 14 }} />
        Agent local — orchestrateur → brief → générateur (pas d'outil côté générateur)
      </footer>
    </div>
  );
}

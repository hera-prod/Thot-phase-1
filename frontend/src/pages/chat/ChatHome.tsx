// ChatHome Phase 1 — hero + Composer + Conversations récentes via Tauri invoke
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Chat24Regular, LockClosed24Regular, ChevronRight24Regular, MoreHorizontal24Regular } from '@fluentui/react-icons';
import { Composer } from '../../components/composer/Composer';
import { showToast } from '../../components/ui/Toast';
import { listConversations, type ConversationSummary } from '../../lib/tauri';

function relativeDate(dateStr?: string): string {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "À l'instant";
  if (mins < 60) return `Il y a ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `Il y a ${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `Il y a ${days}j`;
  return d.toLocaleDateString('fr-FR');
}

export function ChatHome() {
  const navigate = useNavigate();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    listConversations(5, 0)
      .then((convs) => {
        if (!mounted) return;
        setConversations(convs);
      })
      .catch(() => {
        if (!mounted) return;
        setConversations([]);
      })
      .finally(() => {
        if (!mounted) return;
        setLoading(false);
      });
    return () => { mounted = false; };
  }, []);

  const handleSend = async (message: string, files?: File[]) => {
    if (!message.trim() && (!files || files.length === 0)) return;
    const convId = crypto.randomUUID();
    if (files && files.length > 0) {
      showToast({ type: 'info', title: 'Fichiers joints', message: `${files.length} fichier(s) — upload lors de l'envoi en conversation.` });
    }
    navigate(`/chat/${convId}`, { state: { initialMessage: message, initialFiles: files } });
  };

  return (
    <div style={{ position: 'relative', maxWidth: 960, width: '100%', margin: '0 auto', padding: '56px 32px 32px', minHeight: '100%', display: 'flex', flexDirection: 'column' }}>
      <img
        src="/assets/brand/Thot_logo.svg"
        alt=""
        aria-hidden="true"
        onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
        style={{
          position: 'absolute', top: -8, right: -16, opacity: 0.05,
          width: 520, pointerEvents: 'none',
        }}
      />

      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 'var(--thot-text-hero)', fontWeight: 600, color: 'var(--thot-gold)', lineHeight: 1.1, letterSpacing: '-0.02em' }}>
          Bonjour,
        </div>
        <div style={{ fontSize: 'var(--thot-text-hero)', fontWeight: 700, color: 'var(--thot-text)', marginTop: 2, lineHeight: 1.1, letterSpacing: '-0.02em' }}>
          Que souhaitez-vous créer aujourd'hui ?
        </div>
        <p style={{ fontSize: 16, color: 'var(--thot-text-muted)', marginTop: 14, maxWidth: 560, lineHeight: 1.6 }}>
          Discutez avec Thot — tout fonctionne en local. En mode Agent, ajoutez une image (vision LFM2.5-VL-3B) ou un PDF (OCR GLM-OCR).
        </p>

        <Composer variant="hero" onSend={handleSend} />

        <section style={{ marginTop: 40 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
            <h2 style={{ fontSize: 'var(--thot-text-section)', fontWeight: 600 }}>Conversations récentes</h2>
            <button
              onClick={() => navigate('/history')}
              style={{
                display: 'flex', alignItems: 'center', gap: 4, fontSize: 14, color: 'var(--thot-text-faint)',
                background: 'none', border: 'none', cursor: 'pointer', fontFamily: 'var(--thot-font-family)',
              }}
            >
              Historique local <ChevronRight24Regular style={{ fontSize: 14 }} />
            </button>
          </div>

          {loading ? (
            <div style={{ display: 'flex', gap: 16, padding: 20, borderRadius: 16, background: 'var(--thot-surface-raised)', border: '1px solid var(--thot-border-subtle)', justifyContent: 'center' }}>
              <span style={{ fontSize: 14, color: 'var(--thot-text-muted)' }}>Chargement…</span>
            </div>
          ) : conversations.length === 0 ? (
            <div style={{
              display: 'flex', gap: 16, padding: 20, borderRadius: 16,
              background: 'var(--thot-surface-raised)', border: '1px dashed var(--thot-border)',
              alignItems: 'center', justifyContent: 'center', flexDirection: 'column', textAlign: 'center',
            }}>
              <span style={{ color: 'var(--thot-text-muted)', display: 'flex' }}><Chat24Regular /></span>
              <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', maxWidth: 420, lineHeight: 1.5 }}>
                Aucune conversation pour le moment. Écrivez un message ci-dessus pour démarrer — il sera stocké dans <code style={{ background: 'var(--thot-surface-2)', padding: '2px 6px', borderRadius: 6 }}>data/thot-phase-1.db</code>
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 16, overflowX: 'auto', scrollSnapType: 'x mandatory', paddingBottom: 8 }}>
              {conversations.slice(0, 5).map((conv) => (
                <article
                  key={conv.id}
                  onClick={() => navigate(`/chat/${conv.id}`)}
                  style={{
                    minWidth: 280, maxWidth: 320,
                    background: 'var(--thot-surface-raised)',
                    border: '1px solid var(--thot-border)',
                    borderRadius: 16, padding: 16, cursor: 'pointer', flexShrink: 0,
                    scrollSnapAlign: 'start', transition: 'var(--thot-transition-fast)',
                  }}
                  onMouseEnter={(e) => e.currentTarget.style.boxShadow = 'var(--thot-shadow-card)'}
                  onMouseLeave={(e) => e.currentTarget.style.boxShadow = 'none'}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                    <span style={{ color: 'var(--thot-text-muted)', display: 'flex' }}><Chat24Regular /></span>
                    <span style={{ flex: 1, fontSize: 14, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {conv.title || `Conversation ${conv.id.slice(0, 8)}`}
                    </span>
                    <button aria-label="Menu" onClick={(e) => { e.stopPropagation(); }} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
                      <MoreHorizontal24Regular />
                    </button>
                  </div>
                  {conv.last_message_preview && (
                    <p style={{ fontSize: 13, color: 'var(--thot-text-muted)', lineHeight: 1.4, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', marginBottom: 12 }}>
                      {conv.last_message_preview}
                    </p>
                  )}
                  <div style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>
                    {relativeDate(conv.updated_at)} {conv.message_count != null ? `· ${conv.message_count} messages` : ''}
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>

      <footer style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, marginTop: 32, paddingTop: 16, paddingBottom: 8, fontSize: 13, color: 'var(--thot-text-faint)', borderTop: '1px solid var(--thot-border-subtle)' }}>
        <LockClosed24Regular style={{ fontSize: 14 }} />
        Tout fonctionne en local sur votre machine — IPC Tauri, pas de REST
      </footer>
    </div>
  );
}

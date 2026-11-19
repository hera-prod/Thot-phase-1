// ChatHome Phase 1 — hero + Composer + Conversations récentes via le pont IPC
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Chat24Regular, LockClosed24Regular, ChevronRight24Regular, MoreHorizontal24Regular, Edit24Regular, Delete24Regular, ArrowMove24Regular } from '@fluentui/react-icons';
import { Composer } from '../../components/composer/Composer';
import { showToast } from '../../components/ui/Toast';
import { DeplacerDiscussionDialog } from '../../components/espaces/DeplacerDiscussionDialog';
import { listConversations, renameConversation, deleteConversation, type ConversationSummary } from '../../lib/ipcBridge';
import { useAttachmentDraft } from '../../features/attachments/AttachmentDraftContext';
import { useInferenceActivity } from '../../features/inference/InferenceActivityContext';
import './ChatHome.css';
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
  const [loadError, setLoadError] = useState<string | null>(null);
  const loadSeqRef = useRef(0);
  const { isDraggingFiles } = useAttachmentDraft();
  const { activity, memory } = useInferenceActivity();
  const blockedReason = memory.status === 'running'
    ? 'Un entretien mémoire est en cours. Attendez sa fin avant d’envoyer un message.'
    : activity.active
      ? (activity.kind === 'image'
        ? 'Une génération d’image est en cours. Attendez sa fin avant d’envoyer un message.'
        : 'Une discussion est en cours. Attendez sa fin avant d’envoyer un message.')
      : null;
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<ConversationSummary | null>(null);
  const [menuOpen, setMenuOpen] = useState<string | null>(null);

  const loadConversations = useCallback(async () => {
    const sequence = loadSeqRef.current + 1;
    loadSeqRef.current = sequence;
    setLoading(true);
    setLoadError(null);
    try {
      const convs = await listConversations(5, 0);
      if (loadSeqRef.current !== sequence) return;
      setConversations(convs);
    } catch (error: unknown) {
      if (loadSeqRef.current !== sequence) return;
      // Ne jamais vider la liste sur échec : une lecture ratée n'est pas une
      // absence de conversations. L'ancienne liste reste lisible sous le bandeau.
      setLoadError(error instanceof Error ? error.message : 'Les conversations n’ont pas pu être chargées.');
    } finally {
      if (loadSeqRef.current === sequence) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  const handleSend = async (message: string, files?: File[]) => {
    if (blockedReason) {
      showToast({ type: 'info', title: 'Inférence en cours', message: blockedReason });
      return;
    }
    if (!message.trim() && (!files || files.length === 0)) return;
    const convId = crypto.randomUUID();
    if (files && files.length > 0) {
      showToast({ type: 'info', title: 'Fichiers joints', message: `${files.length} fichier(s) — upload lors de l'envoi en conversation.` });
    }
    navigate(`/chat/${convId}`, { state: { initialMessage: message, initialFiles: files } });
  };

  const handleRename = async (id: string, currentTitle: string) => {
    setRenameTarget(id);
    setRenameValue(currentTitle);
    setMenuOpen(null);
  };

  const handleRenameConfirm = async (id: string) => {
    if (!renameValue.trim()) return;
    try {
      await renameConversation(id, renameValue.trim());
      setConversations((prev) => prev.map((c) => c.id === id ? { ...c, title: renameValue.trim() } : c));
      showToast({ type: 'success', title: 'Renommé', message: 'Conversation renommée avec succès.' });
    } catch {
      showToast({ type: 'error', title: 'Erreur', message: 'Impossible de renommer la conversation.' });
    }
    setRenameTarget(null);
    setRenameValue('');
  };

  const handleDelete = async (id: string) => {
    setDeleteTarget(id);
    setMenuOpen(null);
  };

  const handleMove = (conv: ConversationSummary) => {
    setMenuOpen(null);
    setMoveTarget(conv);
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    try {
      await deleteConversation(deleteTarget);
      setConversations((prev) => prev.filter((c) => c.id !== deleteTarget));
      showToast({ type: 'success', title: 'Supprimé', message: 'Conversation supprimée.' });
    } catch {
      showToast({ type: 'error', title: 'Erreur', message: 'Impossible de supprimer la conversation.' });
    }
    setDeleteTarget(null);
  };

  const handleContextMenu = (e: React.MouseEvent, convId: string) => {
    e.preventDefault();
    e.stopPropagation();
    setMenuOpen(menuOpen === convId ? null : convId);
  };

  return (
      <div
        style={{ position: 'relative', maxWidth: 820, width: '100%', margin: '0 auto', padding: '56px 32px 32px', minHeight: '100%', display: 'flex', flexDirection: 'column', background: isDraggingFiles ? 'var(--thot-gold-soft)' : 'transparent', border: isDraggingFiles ? '2px dashed var(--thot-gold)' : '2px dashed transparent', borderRadius: 16, transition: 'var(--thot-transition-fast)' }}
      >
        {isDraggingFiles && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'rgba(247,245,240,0.9)', borderRadius: 16, zIndex: 10, pointerEvents: 'none' }}>
            <span style={{ fontSize: 32 }}>⇩</span>
            <span style={{ fontSize: 16, fontWeight: 600, marginTop: 8 }}>Déposez une image ou un PDF</span>
            <span style={{ fontSize: 13, color: 'var(--thot-text-muted)', marginTop: 4 }}>Il sera joint à votre prochaine question.</span>
          </div>
        )}
      <div style={{ position: 'relative', zIndex: 1 }}>
        <div style={{ fontSize: 'var(--thot-text-hero)', fontWeight: 600, color: 'var(--thot-gold)', lineHeight: 1.1, letterSpacing: '-0.02em' }}>
          Bonjour,
        </div>
        <div style={{ fontSize: 'var(--thot-text-hero)', fontWeight: 700, color: 'var(--thot-text)', marginTop: 2, lineHeight: 1.1, letterSpacing: '-0.02em' }}>
          Que souhaitez-vous créer aujourd'hui ?
        </div>
        <p style={{ fontSize: 16, color: 'var(--thot-text-muted)', marginTop: 14, maxWidth: 560, lineHeight: 1.6 }}>
          Discutez avec Thot. Posez une question, joignez une image ou un PDF : il choisit l’outil qui convient.
        </p>

        <Composer variant="hero" blockedReason={blockedReason} onSend={handleSend} />

        {loadError && (
          <div
            role="alert"
            style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
              marginTop: 24, padding: '12px 16px', borderRadius: 12,
              background: 'var(--thot-danger-soft)', border: '1px solid var(--thot-danger)',
              color: 'var(--thot-danger)', fontSize: 14,
            }}
          >
            <span>
              Les conversations récentes n’ont pas pu être chargées.
              <span style={{ display: 'block', fontSize: 12, opacity: 0.8, marginTop: 2 }}>{loadError}</span>
            </span>
            <button
              type="button"
              onClick={() => void loadConversations()}
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
          ) : conversations.length === 0 && !loadError ? (
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
                   onContextMenu={(e) => handleContextMenu(e, conv.id)}
                   style={{
                     minWidth: 280, maxWidth: 320,
                     background: 'var(--thot-surface-raised)',
                     border: '1px solid var(--thot-border)',
                     borderRadius: 16, padding: 16, cursor: 'pointer', flexShrink: 0,
                     scrollSnapAlign: 'start', transition: 'var(--thot-transition-fast)',
                     position: 'relative',
                   }}
                   onMouseEnter={(e) => e.currentTarget.style.boxShadow = 'var(--thot-shadow-card)'}
                   onMouseLeave={(e) => e.currentTarget.style.boxShadow = 'none'}
                 >
                   <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                     <span style={{ color: 'var(--thot-text-muted)', display: 'flex' }}><Chat24Regular /></span>
                     <span style={{ flex: 1, fontSize: 14, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                       {conv.title || `Conversation ${conv.id.slice(0, 8)}`}
                     </span>
                     <div style={{ position: 'relative' }}>
                       <button aria-label="Menu" onClick={(e) => { e.stopPropagation(); setMenuOpen(menuOpen === conv.id ? null : conv.id); }} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}>
                         <MoreHorizontal24Regular />
                       </button>
                       {menuOpen === conv.id && (
                         <div style={{
                           position: 'absolute', right: 0, top: '100%', marginTop: 4,
                           background: 'var(--thot-surface-raised)', border: '1px solid var(--thot-border)',
                           borderRadius: 10, boxShadow: 'var(--thot-shadow-float)',
                           zIndex: 20, minWidth: 140, overflow: 'hidden',
                         }}>
                           <button
                             onClick={(e) => { e.stopPropagation(); handleRename(conv.id, conv.title || ''); }}
                             style={{
                               display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                               padding: '8px 12px', border: 'none', background: 'transparent',
                               cursor: 'pointer', fontSize: 13, color: 'var(--thot-text)', fontFamily: 'var(--thot-font-family)',
                             }}
                             onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-surface-2)'}
                             onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
                           >
                             <Edit24Regular style={{ fontSize: 14 }} /> Renommer
                           </button>
                           <button
                             onClick={(e) => { e.stopPropagation(); handleMove(conv); }}
                             style={{
                               display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                               padding: '8px 12px', border: 'none', borderTop: '1px solid var(--thot-border-subtle)',
                               background: 'transparent', cursor: 'pointer', fontSize: 13,
                               color: 'var(--thot-text)', fontFamily: 'var(--thot-font-family)',
                             }}
                             onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-surface-2)'}
                             onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
                           >
                             <ArrowMove24Regular style={{ fontSize: 14 }} /> Déplacer vers un espace…
                           </button>
                           <button
                             onClick={(e) => { e.stopPropagation(); handleDelete(conv.id); }}
                             style={{
                               display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                               padding: '8px 12px', border: 'none', borderTop: '1px solid var(--thot-border-subtle)',
                               background: 'transparent', cursor: 'pointer', fontSize: 13,
                               color: 'var(--thot-danger)', fontFamily: 'var(--thot-font-family)',
                             }}
                             onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-danger-soft)'}
                             onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
                           >
                             <Delete24Regular style={{ fontSize: 14 }} /> Supprimer
                           </button>
                         </div>
                       )}
                     </div>
                   </div>
                   {renameTarget === conv.id ? (
                     <div style={{ display: 'flex', gap: 4, marginBottom: 8 }} onClick={(e) => e.stopPropagation()}>
                       <input
                         value={renameValue}
                         onChange={(e) => setRenameValue(e.target.value)}
                         onKeyDown={(e) => { if (e.key === 'Enter') handleRenameConfirm(conv.id); if (e.key === 'Escape') { setRenameTarget(null); setRenameValue(''); } }}
                         autoFocus
                         style={{
                           flex: 1, padding: '4px 8px', fontSize: 13, border: '1px solid var(--thot-gold)',
                           borderRadius: 6, outline: 'none', fontFamily: 'var(--thot-font-family)',
                         }}
                       />
                       <button onClick={() => handleRenameConfirm(conv.id)} style={{ padding: '4px 8px', fontSize: 12, border: 'none', background: 'var(--thot-gold)', color: '#fff', borderRadius: 6, cursor: 'pointer', fontFamily: 'var(--thot-font-family)' }}>✓</button>
                       <button onClick={() => { setRenameTarget(null); setRenameValue(''); }} style={{ padding: '4px 8px', fontSize: 12, border: 'none', background: 'var(--thot-surface-2)', color: 'var(--thot-text-muted)', borderRadius: 6, cursor: 'pointer', fontFamily: 'var(--thot-font-family)' }}>✕</button>
                     </div>
                   ) : (
                     <>
                       {conv.last_message_preview && (
                         <p style={{ fontSize: 13, color: 'var(--thot-text-muted)', lineHeight: 1.4, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', marginBottom: 12 }}>
                           {conv.last_message_preview}
                         </p>
                       )}
                       <div style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>
                         {relativeDate(conv.updated_at)} {conv.message_count != null ? `· ${conv.message_count} messages` : ''}
                       </div>
                     </>
                   )}
                 </article>
               ))}
            </div>
          )}
        </section>
      </div>

      <div className="homeWatermarkSlot" aria-hidden="true">
        <img
          className="homeWatermark"
          src="/assets/brand/Thot_filigrane.svg"
          alt=""
          onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
        />
      </div>

      <footer style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, marginTop: 32, paddingTop: 16, paddingBottom: 8, fontSize: 13, color: 'var(--thot-text-faint)', borderTop: '1px solid var(--thot-border-subtle)', position: 'relative', zIndex: 1 }}>
        <LockClosed24Regular style={{ fontSize: 14 }} />
        Tout fonctionne en local sur votre machine — aucun envoi sur Internet
      </footer>

      {deleteTarget && (
        <div style={{
          position: 'fixed', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,0.4)', zIndex: 100,
        }} onClick={() => setDeleteTarget(null)}>
          <div style={{
            background: 'var(--thot-surface-raised)', borderRadius: 16, padding: 24,
            boxShadow: 'var(--thot-shadow-float)', maxWidth: 360, width: '90%',
          }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>Supprimer cette conversation ?</h3>
            <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', marginBottom: 20 }}>
              Cette action est irréversible. Toutes les données associées seront supprimées.
            </p>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                onClick={() => setDeleteTarget(null)}
                style={{
                  padding: '8px 16px', borderRadius: 'var(--thot-radius-sm)',
                  border: '1px solid var(--thot-border)', background: 'transparent',
                  cursor: 'pointer', fontSize: 14, fontFamily: 'var(--thot-font-family)',
                  color: 'var(--thot-text)',
                }}
              >
                Annuler
              </button>
              <button
                onClick={handleDeleteConfirm}
                style={{
                  padding: '8px 16px', borderRadius: 'var(--thot-radius-sm)',
                  border: 'none', background: 'var(--thot-danger)',
                  cursor: 'pointer', fontSize: 14, fontFamily: 'var(--thot-font-family)',
                  color: '#fff',
                }}
              >
                Supprimer
              </button>
            </div>
          </div>
        </div>
      )}

      {moveTarget && (
        <DeplacerDiscussionDialog
          conversationId={moveTarget.id}
          conversationTitle={moveTarget.title || `Conversation ${moveTarget.id.slice(0, 8)}`}
          sourceWorkspaceId={moveTarget.workspace_id ?? null}
          sourceWorkspaceName={moveTarget.workspace_name ?? null}
          onClose={() => setMoveTarget(null)}
          onMoved={(targetWorkspaceId, targetWorkspaceName) => {
            // Mise à jour locale seulement : la carte garde sa place et son
            // apparence, seul son rattachement change sous le capot.
            setConversations((prev) =>
              prev.map((c) =>
                c.id === moveTarget.id
                  ? {
                      ...c,
                      workspace_id: targetWorkspaceId,
                      workspace_name: targetWorkspaceName,
                    }
                  : c,
              ),
            );
          }}
        />
      )}
    </div>
  );
}

import { useCallback, useEffect, useState, useRef } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { SearchInput } from '../../components/ui/SearchInput';
import { Chat24Regular, Dismiss24Regular, Edit24Regular, ArrowMove24Regular, Delete24Regular } from '@fluentui/react-icons';
import { listConversations, renameConversation, deleteConversation } from '../../lib/ipcBridge';
import { showToast } from '../../components/ui/Toast';
import { DeplacerDiscussionDialog } from '../../components/espaces/DeplacerDiscussionDialog';
import { teinteEspace } from '../espaces/format';

interface HistoryItem {
  id: string;
  title: string;
  preview: string;
  time: string;
  updated_at: string;
  /** Espace de rattachement ; absent pour un chat hors espace. */
  workspaceId: string | null;
  workspaceName: string | null;
}

const HORS_ESPACE = '__sans_espace__';

type Onglet = 'tout' | 'par_espace';

const ONGLETS: Array<{ id: Onglet; label: string }> = [
  { id: 'tout', label: 'Tout' },
  { id: 'par_espace', label: 'Par espace' },
];

/** Hauteur de la zone défilante d'une case : environ cinq discussions. */
const HAUTEUR_LISTE_CASE = 196;

interface Espace {
  id: string;
  label: string;
}

/**
 * Espaces présents dans l'historique, triés de façon STABLE entre deux rendus.
 * Un `localeCompare` sans locale dépend de la machine : la même liste change
 * d'ordre selon la locale du poste, ce qui donne l'impression d'un bug.
 * « Sans espace » passe en tête : c'est le cas le plus courant, et l'enfouir
 * sous les espaces nommés le rendrait introuvable.
 */
function espacesDe(items: HistoryItem[]): Espace[] {
  const vus = new Map<string, string>();
  for (const item of items) {
    const id = item.workspaceId ?? HORS_ESPACE;
    if (!vus.has(id)) vus.set(id, item.workspaceName?.trim() || 'Sans espace');
  }
  return [...vus.entries()]
    .map(([id, label]) => ({ id, label }))
    .sort((a, b) => {
      const aHors = a.id === HORS_ESPACE;
      const bHors = b.id === HORS_ESPACE;
      if (aHors !== bHors) return aHors ? -1 : 1;
      return a.label.localeCompare(b.label, 'fr');
    });
}

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return debounced;
}

export function HistoryPage() {
  const navigate = useNavigate();
  const [onglet, setOnglet] = useState<Onglet>('tout');
  const [selectedItem, setSelectedItem] = useState<HistoryItem | null>(null);
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<HistoryItem | null>(null);
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const loadSeqRef = useRef(0);
  const debouncedSearch = useDebounce(searchQuery, 300);

  const loadConversations = useCallback(async () => {
    const sequence = loadSeqRef.current + 1;
    loadSeqRef.current = sequence;
    setLoading(true);
    setLoadError(null);
    try {
      const convs = await listConversations(50, 0);
      if (loadSeqRef.current !== sequence) return;
      setItems(
        convs.map((c) => ({
          id: c.id,
          title: c.title || 'Sans titre',
          preview: c.last_message_preview || '',
          time: c.updated_at
            ? new Date(c.updated_at).toLocaleDateString('fr-FR')
            : '',
          updated_at: c.updated_at || '',
          workspaceId: c.workspace_id ?? null,
          workspaceName: c.workspace_name ?? null,
        }))
      );
    } catch (error: unknown) {
      if (loadSeqRef.current !== sequence) return;
      // Conserver `items` : un historique illisible n'efface pas ce qui était
      // déjà affiché, il ajoute un bandeau au-dessus.
      setLoadError(error instanceof Error ? error.message : 'L’historique n’a pas pu être chargé.');
    } finally {
      if (loadSeqRef.current === sequence) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  // Close menu on click outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(null);
      }
    };
    document.addEventListener('click', handler);
    return () => document.removeEventListener('click', handler);
  }, []);

  const handleRename = async (id: string, currentTitle: string) => {
    setRenameTarget(id);
    setRenameValue(currentTitle);
    setMenuOpen(null);
  };

  const handleRenameConfirm = async (id: string) => {
    if (!renameValue.trim()) return;
    try {
      await renameConversation(id, renameValue.trim());
      setItems((prev) => prev.map((i) => i.id === id ? { ...i, title: renameValue.trim() } : i));
      showToast({ type: 'success', title: 'Renommé', message: 'Conversation renommée.' });
    } catch {
      showToast({ type: 'error', title: 'Erreur', message: 'Impossible de renommer.' });
    }
    setRenameTarget(null);
  };

  const handleDelete = async (id: string) => {
    setDeleteTarget(id);
    setMenuOpen(null);
  };

  const handleMove = (item: HistoryItem) => {
    setMenuOpen(null);
    setMoveTarget(item);
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    try {
      await deleteConversation(deleteTarget);
      setItems((prev) => prev.filter((i) => i.id !== deleteTarget));
      setSelectedItem(null);
      showToast({ type: 'success', title: 'Supprimé', message: 'Conversation supprimée.' });
    } catch {
      showToast({ type: 'error', title: 'Erreur', message: 'Impossible de supprimer.' });
    }
    setDeleteTarget(null);
  };

  const espaces = espacesDe(items);
  const filtered = items.filter((i) => {
    if (debouncedSearch) {
      const q = debouncedSearch.toLowerCase();
      if (!i.title.toLowerCase().includes(q) && !i.preview.toLowerCase().includes(q)) return false;
    }
    return true;
  });

  // L'onglet est une vue, pas un filtre : le seul filtre qui reste est la
  // recherche. `resetFilters` n'a donc rien d'autre à annuler qu'elle.
  const hasActiveFilters = debouncedSearch.length > 0;
  // Une seule liste, dans l'ordre de la derniere modification : c'est le seul
  // ordre qui aide a retrouver une conversation. « Tout » et chaque espace ne
  // changent que le perimetre, jamais l'ordre.
  const visibles = [...filtered].sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
  );

  const casesParEspace = espaces
    .map((espace) => ({
      espace,
      discussions: visibles.filter((i) => (i.workspaceId ?? HORS_ESPACE) === espace.id),
    }))
    .filter((carte) => carte.discussions.length > 0);

  const resetFilters = () => {
    setSearchQuery('');
  };

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0, overflow: 'hidden' }}>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'auto', minHeight: 0, padding: '32px' }}>
        <div style={{ width: '100%', maxWidth: 960, margin: '0 auto', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
            <h1 style={{ fontSize: 'var(--thot-text-page-title)', fontWeight: 700 }}>Historique</h1>
            {items.length > 0 && (
              <span style={{ fontSize: 13, color: 'var(--thot-text-faint)' }}>
                {items.length} conversation{items.length > 1 ? 's' : ''}
              </span>
            )}
          </div>
          <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', marginBottom: 24 }}>
            Retrouvez l'ensemble de vos échanges passés — stockage local <code style={{ background: 'var(--thot-surface-2)', padding: '2px 6px', borderRadius: 6 }}>data/thot-phase-1.db</code>
          </p>

          <div style={{ display: 'flex', gap: 8, marginBottom: 32, alignItems: 'center', flexWrap: 'wrap' }}>
            <SearchInput
              placeholder="Rechercher dans l'historique…"
              width={400}
              value={searchQuery}
              onChange={setSearchQuery}
            />
            <div style={{ display: 'flex', gap: 4 }}>
              {ONGLETS.map((ongletCourant) => (
                <button
                  key={ongletCourant.id}
                  onClick={() => setOnglet(ongletCourant.id)}
                  style={{
                    padding: '6px 14px', borderRadius: 'var(--thot-radius-pill)',
                    border: 'none', fontSize: 13, fontWeight: 500, cursor: 'pointer',
                    background: onglet === ongletCourant.id ? 'var(--thot-surface-raised)' : 'transparent',
                    color: onglet === ongletCourant.id ? 'var(--thot-text)' : 'var(--thot-text-muted)',
                    fontFamily: 'var(--thot-font-family)',
                    boxShadow: onglet === ongletCourant.id ? '0 1px 2px rgba(0,0,0,0.05)' : 'none',
                  }}
                >
                  {ongletCourant.label}
                </button>
              ))}
            </div>
            {hasActiveFilters && (
              <button
                onClick={resetFilters}
                style={{
                  padding: '6px 14px', borderRadius: 'var(--thot-radius-pill)',
                  border: '1px solid var(--thot-border)', fontSize: 13, fontWeight: 500, cursor: 'pointer',
                  background: 'transparent', color: 'var(--thot-text-muted)',
                  fontFamily: 'var(--thot-font-family)',
                }}
              >
                Réinitialiser
              </button>
            )}
          </div>

          {loadError && items.length > 0 && (
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
                L’historique n’a pas pu être rechargé ; la liste affichée est la dernière connue.
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

          {loading ? (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px 0', color: 'var(--thot-text-muted)', minHeight: 360 }}>
              Chargement…
            </div>
          ) : loadError && items.length === 0 ? (
            <div role="alert" style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: '40px 20px', minHeight: 360 }}>
              <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8, color: 'var(--thot-danger)' }}>L’historique n’a pas pu être chargé.</h2>
              <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', maxWidth: 420, marginBottom: 16 }}>{loadError}</p>
              <button
                type="button"
                onClick={() => void loadConversations()}
                style={{
                  padding: '8px 20px', borderRadius: 'var(--thot-radius-sm)',
                  border: '1px solid var(--thot-border)', background: 'var(--thot-surface-raised)',
                  cursor: 'pointer', fontSize: 14, fontFamily: 'var(--thot-font-family)',
                  color: 'var(--thot-text)',
                }}
              >
                Réessayer
              </button>
            </div>
          ) : items.length === 0 ? (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: '40px 20px', minHeight: 360 }}>
              <img src="/assets/empty-states/empty-history.png" alt="" style={{ width: 180, height: 180, objectFit: 'contain', marginBottom: 24 }} onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }} />
              <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>Aucun historique</h2>
              <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', maxWidth: 420 }}>
                Vos échanges apparaîtront ici une fois que vous aurez commencé à discuter.
              </p>
            </div>
          ) : filtered.length === 0 ? (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: '40px 20px', minHeight: 360 }}>
              <img src="/assets/empty-states/empty-history.png" alt="" style={{ width: 180, height: 180, objectFit: 'contain', marginBottom: 24, opacity: 0.4 }} onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }} />
              <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>Aucun résultat</h2>
              <p style={{ fontSize: 14, color: 'var(--thot-text-muted)' }}>
                Aucune conversation ne correspond à ces filtres.
              </p>
              <button
                onClick={resetFilters}
                style={{
                  marginTop: 16, padding: '8px 20px', borderRadius: 'var(--thot-radius-sm)',
                  border: '1px solid var(--thot-border)', background: 'var(--thot-surface-raised)',
                  cursor: 'pointer', fontSize: 14, fontFamily: 'var(--thot-font-family)',
                  color: 'var(--thot-text)',
                }}
              >
                Réinitialiser les filtres
              </button>
            </div>
          ) : onglet === 'tout' ? (
            <div style={{
              flex: 1, minHeight: 0, overflowY: 'auto',
              display: 'flex', flexDirection: 'column',
            }}>
              {visibles.map((item) => (

                       <div
                         key={item.id}
                         ref={menuOpen === item.id ? menuRef : undefined}
                         onClick={() => {
                           setSelectedItem(item);
                           navigate(`/chat/${item.id}`);
                         }}
                         style={{
                           display: 'flex', alignItems: 'center', gap: 12,
                           padding: '12px 14px', borderRadius: 10, cursor: 'pointer',
                           background: selectedItem?.id === item.id ? 'var(--thot-gold-soft)' : 'transparent',
                           transition: 'var(--thot-transition-fast)',
                           position: 'relative',
                         }}
                         onMouseEnter={(e) => {
                           if (selectedItem?.id !== item.id) e.currentTarget.style.background = 'var(--thot-surface-2)';
                         }}
                         onMouseLeave={(e) => {
                           if (selectedItem?.id !== item.id) e.currentTarget.style.background = 'transparent';
                         }}
                       >
                         <div style={{
                           width: 36, height: 36, borderRadius: 'var(--thot-radius-pill)',
                           display: 'flex', alignItems: 'center', justifyContent: 'center',
                           background: 'var(--thot-gold-soft)',
                           color: 'var(--thot-gold)',
                           flexShrink: 0,
                         }}>
                           <Chat24Regular />
                         </div>
                         <div style={{ flex: 1, minWidth: 0 }}>
                           {renameTarget === item.id ? (
                             <div onClick={(e) => e.stopPropagation()} style={{ display: 'flex', gap: 4 }}>
                               <input
                                 value={renameValue}
                                 onChange={(e) => setRenameValue(e.target.value)}
                                 onKeyDown={(e) => { if (e.key === 'Enter') handleRenameConfirm(item.id); if (e.key === 'Escape') setRenameTarget(null); }}
                                 autoFocus
                                 style={{
                                   flex: 1, padding: '2px 6px', fontSize: 13, border: '1px solid var(--thot-gold)',
                                   borderRadius: 6, outline: 'none', fontFamily: 'var(--thot-font-family)',
                                 }}
                               />
                               <button onClick={() => handleRenameConfirm(item.id)} style={{ padding: '2px 6px', fontSize: 12, border: 'none', background: 'var(--thot-gold)', color: '#fff', borderRadius: 6, cursor: 'pointer', fontFamily: 'var(--thot-font-family)' }}>✓</button>
                               <button onClick={() => setRenameTarget(null)} style={{ padding: '2px 6px', fontSize: 12, border: 'none', background: 'var(--thot-surface-2)', color: 'var(--thot-text-muted)', borderRadius: 6, cursor: 'pointer', fontFamily: 'var(--thot-font-family)' }}>✕</button>
                             </div>
                           ) : (
                             <>
                               <div style={{ fontSize: 14, fontWeight: 600 }}>{item.title}</div>
                               <div style={{ fontSize: 13, color: 'var(--thot-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                 {item.preview}
                               </div>
                             </>
                           )}
                         </div>
                         <span style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>{item.time}</span>
                         <div style={{ position: 'relative' }}>
                           <button onClick={(e) => { e.stopPropagation(); setMenuOpen(menuOpen === item.id ? null : item.id); }} aria-label="Actions" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-faint)', padding: 4 }}>
                             <Edit24Regular style={{ fontSize: 14 }} />
                           </button>
                           {menuOpen === item.id && (
                             <div style={{
                               position: 'absolute', right: 0, top: '100%', marginTop: 4,
                               background: 'var(--thot-surface-raised)', border: '1px solid var(--thot-border)',
                               borderRadius: 10, boxShadow: 'var(--thot-shadow-float)',
                               zIndex: 20, minWidth: 140, overflow: 'hidden',
                             }}>
                               <button
                                 onClick={(e) => { e.stopPropagation(); handleRename(item.id, item.title); }}
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
                                 onClick={(e) => { e.stopPropagation(); handleMove(item); }}
                                 style={{
                                   display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                                   padding: '8px 12px', border: 'none', borderTop: '1px solid var(--thot-border-subtle)',
                                   background: 'transparent', cursor: 'pointer', fontSize: 13, color: 'var(--thot-text)', fontFamily: 'var(--thot-font-family)',
                                 }}
                                 onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-surface-2)'}
                                 onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
                               >
                                 <ArrowMove24Regular style={{ fontSize: 14 }} /> Déplacer vers un espace…
                               </button>
                               <button
                                 onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }}
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
              ))}
            </div>
          ) : (
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
              <div style={{
                display: 'grid', gap: 16,
                gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
              }}>
                {casesParEspace.map(({ espace, discussions }) => (
                  <CaseEspace
                    key={espace.id}
                    espace={espace}
                    discussions={discussions}
                    onOuvrir={(id) => navigate(`/chat/${id}`)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

       {selectedItem && (
         <div style={{
           width: 340, borderLeft: '1px solid var(--thot-border)',
           padding: 24, overflow: 'auto', flexShrink: 0,
         }}>
           <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
             <h3 style={{ fontSize: 15, fontWeight: 600 }}>Détails</h3>
             <button
               onClick={() => setSelectedItem(null)}
               aria-label="Fermer"
               style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--thot-text-muted)' }}
             >
               <Dismiss24Regular />
             </button>
           </div>
           <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
             <div style={{
               width: 40, height: 40, borderRadius: 'var(--thot-radius-pill)',
               display: 'flex', alignItems: 'center', justifyContent: 'center',
               background: 'var(--thot-gold-soft)',
               color: 'var(--thot-gold)',
             }}>
               <Chat24Regular />
             </div>
             <div>
               <div style={{ fontWeight: 600 }}>{selectedItem.title}</div>
             </div>
           </div>
           <div style={{ fontSize: 13, color: 'var(--thot-text-muted)', marginBottom: 16 }}>
             {selectedItem.preview}
           </div>
           <div style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>
             {selectedItem.time}
           </div>
         </div>
       )}

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
            conversationTitle={moveTarget.title}
            sourceWorkspaceId={moveTarget.workspaceId}
            sourceWorkspaceName={moveTarget.workspaceName}
            onClose={() => setMoveTarget(null)}
            onMoved={() => {
              // Le regroupement « Par espace » suit le nouveau rattachement :
              // sans relecture, la case d'origine garderait une discussion
              // qu'elle ne contient plus.
              void loadConversations();
            }}
          />
        )}
      </div>
    );
  }

function CaseEspace({ espace, discussions, onOuvrir }: {
  espace: Espace;
  discussions: HistoryItem[];
  onOuvrir: (id: string) => void;
}) {
  const teinte = teinteEspace(espace.id === HORS_ESPACE ? '' : espace.id);
  const nombre = discussions.length;

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 12, padding: 18,
      borderRadius: 16,
      border: '1px solid var(--thot-border)',
      background: `linear-gradient(160deg, ${teinte}, transparent 70%)`,
      boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span
          aria-hidden="true"
          style={{
            width: 40, height: 40, borderRadius: '50%', display: 'inline-flex',
            alignItems: 'center', justifyContent: 'center', flexShrink: 0,
            background: teinte, color: 'var(--thot-text)',
          }}
        >
          <Chat24Regular style={{ fontSize: 20 }} />
        </span>
        <div style={{ minWidth: 0 }}>
          {espace.id === HORS_ESPACE ? (
            <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--thot-text)' }}>
              {espace.label}
            </div>
          ) : (
            <Link
              to={`/espaces/${espace.id}`}
              aria-label={`Ouvrir l’espace ${espace.label}`}
              style={{
                display: 'block', minWidth: 0,
                fontSize: 15, fontWeight: 700, color: 'var(--thot-text)',
                textDecoration: 'none', cursor: 'pointer',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.textDecoration = 'underline'; }}
              onMouseLeave={(e) => { e.currentTarget.style.textDecoration = 'none'; }}
            >
              {espace.label}
            </Link>
          )}
          <div style={{ fontSize: 12, color: 'var(--thot-text-muted)' }}>
            {nombre} discussion{nombre > 1 ? 's' : ''}
          </div>
        </div>
      </div>

      <ul
        aria-label={`Discussions de ${espace.label}`}
        style={{
          display: 'flex', flexDirection: 'column', gap: 2,
          listStyle: 'none', margin: 0, padding: 0,
          // La case est bornée : c'est le contenu qui défile, pas la grille.
          // `maxHeight` interdit à une liste longue de déformer la rangée ;
          // `overflowY: auto` remet déjà la taille minimale automatique à zéro,
          // donc `minHeight: 0` est superflu ici. `flex: 0 1 auto` garde une
          // liste courte collée à son contenu : le vide restant est sous la
          // liste, pas dedans.
          flex: '0 1 auto', minHeight: 0, maxHeight: HAUTEUR_LISTE_CASE,
          overflowY: 'auto',
          // PAS de `overscroll-behavior: contain` : sur une liste courte —
          // le cas le plus fréquent ici — il absorbe la molette sans rien
          // faire défiler, et la grille devient une zone morte.
          // `scrollbarGutter` évite que le texte saute quand une recherche
          // fait apparaître la barre de défilement. Motif de `.messagesViewport`.
          scrollbarGutter: 'stable',
        }}
      >
        {discussions.map((item) => (
          <li key={item.id}>
          <button
            type="button"
            onClick={() => onOuvrir(item.id)}
            style={{
              display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, width: '100%',
              padding: '8px 10px', borderRadius: 10, cursor: 'pointer',
              border: 'none', background: 'transparent', textAlign: 'left',
              color: 'var(--thot-text)', fontFamily: 'var(--thot-font-family)', fontSize: 13,
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--thot-surface-2)'; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
          >
            <Chat24Regular style={{ fontSize: 14, flexShrink: 0, color: 'var(--thot-text-faint)' }} />
            <span style={{
              minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
              whiteSpace: 'nowrap', fontWeight: 500,
            }}>
              {item.title || 'Sans titre'}
            </span>
          </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

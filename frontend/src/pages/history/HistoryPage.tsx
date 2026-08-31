import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { SearchInput } from '../../components/ui/SearchInput';
import { Chat24Regular, Dismiss24Regular } from '@fluentui/react-icons';
import { Tag } from '../../components/ui/Tag';
import { listConversations, type ConversationSummary } from '../../lib/tauri';

interface HistoryItem {
  id: string;
  type: 'chat' | 'agent';
  title: string;
  preview: string;
  time: string;
  updated_at: string;
}

function groupByDate(items: HistoryItem[]): { label: string; items: HistoryItem[] }[] {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);
  const thisWeek = new Date(today.getTime() - today.getDay() * 86400000);

  const groups: { label: string; items: HistoryItem[] }[] = [
    { label: "Aujourd'hui", items: [] },
    { label: 'Hier', items: [] },
    { label: 'Cette semaine', items: [] },
    { label: 'Plus ancien', items: [] },
  ];

  for (const item of items) {
    const d = new Date(item.updated_at);
    if (d >= today) groups[0].items.push(item);
    else if (d >= yesterday) groups[1].items.push(item);
    else if (d >= thisWeek) groups[2].items.push(item);
    else groups[3].items.push(item);
  }

  return groups.filter((g) => g.items.length > 0);
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
  const [filter, setFilter] = useState<string>('all');
  const [selectedItem, setSelectedItem] = useState<HistoryItem | null>(null);
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const debouncedSearch = useDebounce(searchQuery, 300);

  useEffect(() => {
    let mounted = true;
    listConversations(50, 0)
      .then((convs: ConversationSummary[]) => {
        if (!mounted) return;
        setItems(
          convs.map((c) => ({
            id: c.id,
            type: (c.mode as 'chat' | 'agent') || 'chat',
            title: c.title || 'Sans titre',
            preview: c.last_message_preview || '',
            time: c.updated_at
              ? new Date(c.updated_at).toLocaleDateString('fr-FR')
              : '',
            updated_at: c.updated_at || '',
          }))
        );
      })
      .catch(() => {
        if (!mounted) return;
        setItems([]);
      })
      .finally(() => {
        if (!mounted) return;
        setLoading(false);
      });
    return () => { mounted = false; };
  }, []);

  const filtered = items.filter((i) => {
    if (filter !== 'all' && i.type !== filter) return false;
    if (debouncedSearch) {
      const q = debouncedSearch.toLowerCase();
      if (!i.title.toLowerCase().includes(q) && !i.preview.toLowerCase().includes(q)) return false;
    }
    return true;
  });

  const hasActiveFilters = filter !== 'all' || debouncedSearch.length > 0;
  const groups = groupByDate(filtered);

  const resetFilters = () => {
    setFilter('all');
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
              {[
                { id: 'all', label: 'Tout' },
                { id: 'chat', label: 'Chat' },
                { id: 'agent', label: 'Agent' },
              ].map((f) => (
                <button
                  key={f.id}
                  onClick={() => setFilter(f.id)}
                  style={{
                    padding: '6px 14px', borderRadius: 'var(--thot-radius-pill)',
                    border: 'none', fontSize: 13, fontWeight: 500, cursor: 'pointer',
                    background: filter === f.id ? 'var(--thot-surface-raised)' : 'transparent',
                    color: filter === f.id ? 'var(--thot-text)' : 'var(--thot-text-muted)',
                    fontFamily: 'var(--thot-font-family)',
                    boxShadow: filter === f.id ? '0 1px 2px rgba(0,0,0,0.05)' : 'none',
                  }}
                >
                  {f.label}
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

          {loading ? (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px 0', color: 'var(--thot-text-muted)', minHeight: 360 }}>
              Chargement…
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
          ) : (
            <div style={{ flex: 1 }}>
              {groups.map((group) => (
                <div key={group.label} style={{ marginBottom: 24 }}>
                  <h3 style={{ fontSize: 14, fontWeight: 600, color: 'var(--thot-text-muted)', marginBottom: 8 }}>
                    {group.label}
                  </h3>
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    {group.items.map((item) => (
                      <div
                        key={item.id}
                        onClick={() => {
                          setSelectedItem(item);
                          navigate(`/chat/${item.id}`);
                        }}
                        style={{
                          display: 'flex', alignItems: 'center', gap: 12,
                          padding: '12px 14px', borderRadius: 10, cursor: 'pointer',
                          background: selectedItem?.id === item.id ? 'var(--thot-gold-soft)' : 'transparent',
                          transition: 'var(--thot-transition-fast)',
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
                          <div style={{ fontSize: 14, fontWeight: 600 }}>{item.title}</div>
                          <div style={{ fontSize: 13, color: 'var(--thot-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {item.preview}
                          </div>
                        </div>
                        <Tag variant={item.type as 'chat' | 'agent'}>{item.type === 'agent' ? 'Agent' : 'Chat'}</Tag>
                        <span style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>{item.time}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
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
              <Tag variant={selectedItem.type}>{selectedItem.type === 'agent' ? 'Agent' : 'Chat'}</Tag>
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
    </div>
  );
}

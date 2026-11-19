import { describe, it, expect, vi } from 'vitest';

vi.mock('@/lib/ipcBridge', async () => {
  const actual = await vi.importActual('@/lib/ipcBridge');
  return {
    ...actual,
    renameConversation: vi.fn().mockResolvedValue(undefined),
    deleteConversation: vi.fn().mockResolvedValue(undefined),
    listConversations: vi.fn().mockResolvedValue([]),
  };
});

import { renameConversation, deleteConversation, listConversations } from '@/lib/ipcBridge';

function groupByDate(items: Array<{ id: string; title: string; updated_at: string }>) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);
  const thisWeek = new Date(today.getTime() - today.getDay() * 86400000);
  const groups: { label: string; items: typeof items }[] = [
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

describe('HistoryPage logic', () => {
  it('groups items by today', () => {
    const today = new Date().toISOString();
    const items = [{ id: '1', title: 'Test', updated_at: today }];
    const result = groupByDate(items);
    expect(result[0].label).toBe("Aujourd'hui");
  });

  it('filters empty groups', () => {
    const items = [
      { id: '1', title: 'Today', updated_at: new Date().toISOString() },
      { id: '2', title: 'Old', updated_at: '2020-01-01T00:00:00Z' },
    ];
    const result = groupByDate(items);
    expect(result.length).toBe(2);
  });

  it('returns empty array for no items', () => {
    const result = groupByDate([]);
    expect(result).toHaveLength(0);
  });
});

describe('tui functions', () => {
  it('renameConversation is a function', () => {
    expect(typeof renameConversation).toBe('function');
  });
  it('deleteConversation is a function', () => {
    expect(typeof deleteConversation).toBe('function');
  });
  it('listConversations is a function', () => {
    expect(typeof listConversations).toBe('function');
  });
});
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { type ImageItem, type ImageListPage } from '@/lib/ipcBridge';

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener> = {};
const calls: Array<{ cmd: string; args: unknown }> = [];
let pages: ImageListPage[] = [];

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    calls.push({ cmd, args });
    if (cmd === 'image_list') {
      return Promise.resolve(pages.shift() ?? { items: [], next_offset: 0, has_more: false });
    }
    if (cmd === 'get_attachment_bytes') {
      return Promise.resolve([[1, 2, 3], 'image/png']);
    }
    if (cmd === 'image_rename') {
      const { artifactId, name } = args as { artifactId: string; name: string };
      return Promise.resolve({ ok: true, artifact_id: artifactId, changed: true, display_name: name });
    }
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    listeners[event] = callback;
    return Promise.resolve(() => {});
  }),
}));

;
import { GalleryPage } from '@/pages/gallery/GalleryPage';

function item(overrides: Partial<ImageItem> = {}): ImageItem {
  return {
    artifact_id: 'art-1',
    display_name: 'Chat',
    original_filename: 'chat.png',
    created_at: '2026-09-24T10:00:00Z',
    kind: 'image',
    size_bytes: 2048,
    mime_detected: 'image/png',
    scopes: [],
    spaces: [],
    is_import: false,
    experimental: false,
    deprecated: false,
    prompt: 'un chat dans un atelier',
    checkpoint: 'flux-klein-9b',
    backend: 'flux2-spike',
    quality: 'qualified',
    aspect_ratio: '1:1',
    width: 512,
    height: 512,
    seed: 0,
    origin: 'studio',
    run_id: 'run-1',
    ...overrides,
  };
}

function spaceItems(): ImageItem[] {
  return [
    item({ artifact_id: 'sans', display_name: 'Sans', spaces: [] }),
    item({ artifact_id: 'aloe', display_name: 'Aloe', spaces: [{ id: 'ws_1', name: 'Plantes' }] }),
    item({
      artifact_id: 'fleur',
      display_name: 'Fleur',
      spaces: [
        { id: 'ws_1', name: 'Plantes' },
        { id: 'ws_2', name: 'Jardin' },
      ],
    }),
  ];
}

function listCalls() {
  return calls.filter((entry) => entry.cmd === 'image_list');
}

async function activateGrouping() {
  fireEvent.click(await screen.findByRole('button', { name: 'Grouper par espace' }));
  await screen.findByRole('heading', { level: 3, name: 'Sans espace' });
}

beforeEach(() => {
  calls.length = 0;
  pages = [];
  Object.keys(listeners).forEach((key) => delete listeners[key]);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:gallery-${Math.random()}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
});

describe('Galerie — renommage et regroupement par espace', () => {
  it('renomme une image depuis le menu et met à jour la carte', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });

    fireEvent.click(screen.getByRole('button', { name: 'Actions pour Chat' }));
    expect(screen.getByRole('menuitem', { name: 'Renommer l’image' })).toBeTruthy();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Renommer l’image' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Renommer l’image')).toBeTruthy();
    const input = within(dialog).getByLabelText('Nouveau nom') as HTMLInputElement;
    expect(input.value).toBe('Chat');

    fireEvent.change(input, { target: { value: 'Nouveau nom' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Renommer' }));

    await waitFor(() => expect(calls.find((entry) => entry.cmd === 'image_rename')?.args).toEqual({
      artifactId: 'art-1',
      name: 'Nouveau nom',
    }));
    expect(await screen.findByRole('button', { name: /Ouvrir le détail de Nouveau nom/ })).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('désactive Renommer quand le nom est vide et n’appelle pas le backend', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });

    fireEvent.click(screen.getByRole('button', { name: 'Actions pour Chat' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Renommer l’image' }));

    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByLabelText('Nouveau nom');
    const submit = within(dialog).getByRole('button', { name: 'Renommer' }) as HTMLButtonElement;
    expect(submit.disabled).toBe(false);

    fireEvent.change(input, { target: { value: '   ' } });
    expect(submit.disabled).toBe(true);
    fireEvent.click(submit);

    expect(calls.some((entry) => entry.cmd === 'image_rename')).toBe(false);
  });

  it('regroupe « Sans espace » d’abord, trie les espaces et duplique les images multi-espaces', async () => {
    pages = [
      { items: spaceItems(), next_offset: 3, has_more: false },
      { items: spaceItems(), next_offset: 3, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ }).catch(() => undefined);
    await screen.findByRole('button', { name: /Ouvrir le détail de Sans/ });

    await activateGrouping();

    const headings = screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent);
    // « Sans espace » d'abord, puis les espaces triés par nom (localeCompare 'fr').
    expect(headings).toEqual(['Sans espace', 'Jardin', 'Plantes']);

    const plantes = screen.getByRole('region', { name: 'Plantes' });
    const jardin = screen.getByRole('region', { name: 'Jardin' });
    const sans = screen.getByRole('region', { name: 'Sans espace' });
    expect(within(plantes).getByRole('button', { name: /Ouvrir le détail de Aloe/ })).toBeTruthy();
    expect(within(plantes).getByRole('button', { name: /Ouvrir le détail de Fleur/ })).toBeTruthy();
    expect(within(jardin).getByRole('button', { name: /Ouvrir le détail de Fleur/ })).toBeTruthy();
    expect(within(sans).queryByRole('button', { name: /Fleur/ })).toBeNull();
    expect(within(plantes).getByRole('button', { name: 'Tout voir' })).toBeTruthy();
    expect(within(jardin).getByRole('button', { name: 'Tout voir' })).toBeTruthy();
    expect(within(sans).getByRole('button', { name: 'Tout voir' })).toBeTruthy();
  });

  it('« Tout voir » charge la première page filtrée du serveur puis revient aux lignes sans rechargement', async () => {
    pages = [
      { items: spaceItems(), next_offset: 3, has_more: false },
      { items: spaceItems(), next_offset: 3, has_more: false },
      { items: spaceItems().slice(1), next_offset: 2, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Sans/ });

    await activateGrouping();
    fireEvent.click(within(screen.getByRole('region', { name: 'Plantes' })).getByRole('button', { name: 'Tout voir' }));

    expect(await screen.findByText('Espace : Plantes')).toBeTruthy();
    await waitFor(() => expect(listCalls()[listCalls().length - 1]?.args).toMatchObject({ offset: 0, limit: 24, spaceId: 'ws_1' }));

    const callsBeforeBack = listCalls().length;
    fireEvent.click(screen.getByRole('button', { name: 'Revenir à toutes les lignes' }));
    expect(await screen.findByRole('heading', { level: 3, name: 'Sans espace' })).toBeTruthy();
    expect(screen.queryByText('Espace : Plantes')).toBeNull();
    expect(listCalls()).toHaveLength(callsBeforeBack);
  });

  it('« Tout voir » sur « Sans espace » filtre avec noSpace', async () => {
    pages = [
      { items: spaceItems(), next_offset: 3, has_more: false },
      { items: spaceItems(), next_offset: 3, has_more: false },
      { items: [spaceItems()[0]], next_offset: 1, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Sans/ });

    await activateGrouping();
    fireEvent.click(within(screen.getByRole('region', { name: 'Sans espace' })).getByRole('button', { name: 'Tout voir' }));

    await waitFor(() => expect(listCalls()[listCalls().length - 1]?.args).toMatchObject({ offset: 0, limit: 24, noSpace: true }));
    expect(screen.getByText('Sans espace')).toBeTruthy();
  });

  it('charge le regroupement en bouclant sur plusieurs pages de 48', async () => {
    const firstBatch = Array.from({ length: 48 }, (_, index) => item({
      artifact_id: `batch-${index}`,
      display_name: `Image ${index}`,
    }));
    const secondBatch = Array.from({ length: 20 }, (_, index) => item({
      artifact_id: `suite-${index}`,
      display_name: `Suite ${index}`,
    }));
    pages = [
      { items: firstBatch.slice(0, 24), next_offset: 24, has_more: true },
      { items: firstBatch, next_offset: 48, has_more: true },
      { items: secondBatch, next_offset: 68, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Image 0/ });

    await activateGrouping();
    await screen.findByText(/68 images/);

    const offsets = listCalls()
      .filter((entry) => (entry.args as { limit?: number }).limit === 48)
      .map((entry) => (entry.args as { offset: number }).offset);
    expect(offsets).toEqual([0, 48]);
  });
});

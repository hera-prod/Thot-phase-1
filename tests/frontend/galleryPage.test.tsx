import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { type ImageItem, type ImageListPage } from '@/lib/ipcBridge';

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener> = {};
const calls: Array<{ cmd: string; args: unknown }> = [];
let pages: ImageListPage[] = [];
let listError: Error | null = null;
let bytesError = false;

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    calls.push({ cmd, args });
    if (cmd === 'image_list') {
      if (listError) {
        const error = listError;
        listError = null;
        return Promise.reject(error);
      }
      return Promise.resolve(pages.shift() ?? { items: [], next_offset: 0, has_more: false });
    }
    if (cmd === 'get_attachment_bytes') {
      return bytesError ? Promise.reject(new Error('aperçu indisponible')) : Promise.resolve([[1, 2, 3], 'image/png']);
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
    is_import: false,
    experimental: true,
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

beforeEach(() => {
  calls.length = 0;
  pages = [];
  listError = null;
  bytesError = false;
  Object.keys(listeners).forEach((key) => delete listeners[key]);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:gallery-${Math.random()}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
});

describe('Galerie — page, filtres et détail', () => {
  it('charge une page de 24 et affiche les images sans charger conversation', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    expect(await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ })).toBeTruthy();
    expect(screen.getByText('1')).toBeTruthy();
    const call = calls.find((entry) => entry.cmd === 'image_list');
    expect(call?.args).toEqual({ offset: 0, limit: 24, includeImports: false, scopeId: undefined });
    expect(screen.queryByText(/Rattacher|conversation/i)).toBeNull();
  });

  it('affiche les actions de carte et supprime après confirmation', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });

    fireEvent.click(screen.getByRole('button', { name: 'Actions pour Chat' }));
    expect(screen.getByRole('menuitem', { name: 'Modifier l’image' })).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: 'Supprimer' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Modifier l’image/ })).toBeNull();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Supprimer' }));

    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toHaveTextContent('Retirer cette image ?');
    fireEvent.click(screen.getByRole('button', { name: 'Supprimer l’image' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: /Ouvrir le détail de Chat/ })).toBeNull());
    expect(calls.find((entry) => entry.cmd === 'image_delete')?.args).toEqual({ artifactId: 'art-1' });
  });

  it('ouvre l’éditeur depuis le menu ⋯', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    function LocationProbe() {
      const location = useLocation();
      return <span data-testid="location">{location.pathname}{location.search}</span>;
    }
    render(<MemoryRouter><GalleryPage /><LocationProbe /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });

    fireEvent.click(screen.getByRole('button', { name: 'Actions pour Chat' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Modifier l’image' }));

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/images?edit=art-1'));
    expect(screen.queryByRole('menuitem', { name: 'Modifier l’image' })).toBeNull();
  });
  it('ouvre l’éditeur depuis le détail', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    function LocationProbe() {
      const location = useLocation();
      return <span data-testid="location">{location.pathname}{location.search}</span>;
    }
    render(<MemoryRouter><GalleryPage /><LocationProbe /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ }));

    const dialog = await screen.findByRole('dialog');
    const editButton = within(dialog).getByRole('button', { name: 'Modifier l’image' });
    expect(dialog.querySelector('.gallery-modal-head')?.contains(editButton)).toBe(true);

    fireEvent.click(editButton);

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/images?edit=art-1'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
  it('distingue une erreur de la liste de son état vide et propose un retry', async () => {
    listError = new Error('base indisponible');
    pages = [{ items: [item({ display_name: 'Après retry' })], next_offset: 1, has_more: false }];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('base indisponible');
    expect(screen.queryByText('Ouvrir le studio')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Réessayer' }));
    expect(await screen.findByRole('button', { name: /Ouvrir le détail de Après retry/ })).toBeTruthy();
  });

  it('bascule les imports sans inventer de conversion I2I', async () => {
    pages = [
      { items: [item()], next_offset: 1, has_more: false },
      { items: [item({ artifact_id: 'art-import', display_name: 'Import', is_import: true })], next_offset: 2, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Afficher les imports' }));
    expect(await screen.findByRole('button', { name: /Ouvrir le détail de Import/ })).toBeTruthy();
    expect(calls.filter((entry) => entry.cmd === 'image_list')[1]?.args).toMatchObject({ includeImports: true, offset: 0, limit: 24 });
    expect(screen.getByText('Import image')).toBeTruthy();
  });

  it('filtre et trie côté client puis pagine avec next_offset', async () => {
    pages = [
      { items: [item({ artifact_id: 'a', display_name: 'Zèbre', created_at: '2026-09-20T10:00:00Z' })], next_offset: 1, has_more: true },
      { items: [item({ artifact_id: 'b', display_name: 'Abeille', created_at: '2026-09-21T10:00:00Z' })], next_offset: 2, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Zèbre/ });
    fireEvent.change(screen.getByRole('searchbox', { name: 'Rechercher dans la galerie' }), { target: { value: 'abeille' } });
    expect(screen.queryByRole('button', { name: /Ouvrir le détail de Zèbre/ })).toBeNull();
    expect(screen.getByText(/Recherche sans résultat/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Effacer la recherche' }));
    fireEvent.click(screen.getByRole('button', { name: 'Charger 24 images de plus' }));
    expect(await screen.findByRole('button', { name: /Ouvrir le détail de Abeille/ })).toBeTruthy();
    expect(calls.filter((entry) => entry.cmd === 'image_list')[1]?.args).toMatchObject({ offset: 1, limit: 24 });
  });

  it('ouvre une modale accessible, réutilise l’URL et la révoque au démontage', async () => {
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    const view = render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('button', { name: 'Fermer le détail' })).toHaveFocus();
    expect(await screen.findByRole('link', { name: 'Télécharger PNG' })).toHaveAttribute('href', expect.stringContaining('blob:'));
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it('rafraîchit la première page sur artifact_created', async () => {
    pages = [
      { items: [item()], next_offset: 1, has_more: false },
      { items: [item({ artifact_id: 'new', display_name: 'Nouvelle' })], next_offset: 1, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });
    const callback = listeners['artifact_created'];
    expect(callback).toBeTruthy();
    callback({ payload: { request_id: 'run', run_id: 'run', artifact_id: 'new' } });
    expect(await screen.findByRole('button', { name: /Ouvrir le détail de Nouvelle/ })).toBeTruthy();
    expect(calls.filter((entry) => entry.cmd === 'image_list')).toHaveLength(2);
  });

  it('affiche un retry par carte quand les octets échouent', async () => {
    bytesError = true;
    pages = [{ items: [item()], next_offset: 1, has_more: false }];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Aperçu indisponible');
    expect(screen.getByRole('button', { name: 'Réessayer' })).toBeTruthy();
  });

  it('ne garde pas les images d’un autre filtre après un échec d’ouverture de groupe', async () => {
    const espaceItem = item({ spaces: [{ id: 'esp-1', name: 'Atelier' }] });
    pages = [
      { items: [espaceItem], next_offset: 1, has_more: false },
      { items: [espaceItem], next_offset: 1, has_more: false },
    ];
    render(<MemoryRouter><GalleryPage /></MemoryRouter>);
    await screen.findByRole('button', { name: /Ouvrir le détail de Chat/ });

    fireEvent.click(screen.getByRole('button', { name: 'Grouper par espace' }));
    const seeAll = await screen.findByRole('button', { name: 'Tout voir' });
    expect(screen.getByRole('region', { name: 'Atelier' })).toBeTruthy();

    listError = new Error('filtre indisponible');
    fireEvent.click(seeAll);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('filtre indisponible');
    expect(screen.queryByRole('button', { name: /Ouvrir le détail de Chat/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Réessayer' })).toBeTruthy();
  });
});

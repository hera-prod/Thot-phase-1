import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener> = {};
const invoked: Array<{ cmd: string; args: unknown }> = [];

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    invoked.push({ cmd, args });
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_status') return Promise.resolve(undefined);
    if (cmd === 'image_generate' || cmd === 'image_set_backend') return Promise.resolve(undefined);
    if (cmd === 'cancel_run') return Promise.resolve();
    if (cmd === 'get_attachment_bytes') return Promise.resolve([[137, 80, 78, 71], 'image/png']);
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    listeners[event] = callback;
    return Promise.resolve(() => {});
  }),
}));

import { ImagesPage } from '@/pages/images/ImagesPage';

class FakeImage {
  naturalWidth = 100;
  naturalHeight = 200;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(_value: string) {
    setTimeout(() => this.onload?.(), 0);
  }
}

function fire(event: string, payload: Record<string, unknown>) {
  const callback = listeners[event];
  if (!callback) throw new Error(`Aucun listener pour ${event}`);
  act(() => callback({ payload }));
}

async function startRun(prompt = 'un phare breton') {
  render(<ImagesPage />);
  const input = await screen.findByRole('textbox', { name: 'Instruction' });
  fireEvent.change(input, { target: { value: prompt } });
  fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
  const generation = invoked.find((entry) => entry.cmd === 'image_generate');
  expect(generation).toBeTruthy();
  const requestId = String((generation as { args: { requestId: string } }).args.requestId);
  await waitFor(() => expect(screen.getAllByText('Envoi…').length).toBeGreaterThan(0));
  return requestId;
}

beforeEach(() => {
  invoked.length = 0;
  Object.keys(listeners).forEach((key) => { delete listeners[key]; });
  window.history.replaceState({}, '', '/');
  vi.stubGlobal('Image', FakeImage as any);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:image-${Math.random()}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Images — entrée éditeur', () => {
  it('scénario 1 : après génération, l’éditeur s’ouvre automatiquement', async () => {
    const requestId = await startRun();
    fire('artifact_created', { request_id: requestId, run_id: requestId, artifact_id: 'art_test123' });
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_test123' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
    expect(screen.getAllByRole('button', { name: 'Retour à la page Image' })).toHaveLength(1);
    expect(screen.getByLabelText('Canevas de modification')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Modifier l’image' })).toBeNull();
    expect(document.querySelectorAll('.img-page')).toHaveLength(1);
  });

  it('scénario 2 : Retour à la page Image après création revient à la page de création', async () => {
    const requestId = await startRun();
    fire('artifact_created', { request_id: requestId, run_id: requestId, artifact_id: 'art_test123' });
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_test123' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Retour à la page Image' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Instruction' })).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'Retour à la page Image' })).toBeNull();
    expect(screen.queryByText('Évolution')).toBeNull();
    expect(document.querySelectorAll('.img-page')).toHaveLength(1);
  });

  it('scénario 3 : galerie ?edit= ouvre l’éditeur une fois et retour nettoie l’URL', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    render(<ImagesPage />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
    expect(screen.getAllByRole('button', { name: 'Retour à la page Image' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Retour à la page Image' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Instruction' })).toBeTruthy());
    expect(window.location.search).not.toContain('edit=');
    expect(document.querySelectorAll('.img-page')).toHaveLength(1);
  });
});
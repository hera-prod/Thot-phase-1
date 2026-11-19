import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener> = {};
const invoked: Array<{ cmd: string; args: Record<string, unknown> }> = [];

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: Record<string, unknown>) => {
    invoked.push({ cmd, args });
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_status') return Promise.resolve(undefined);
    if (cmd === 'image_generate' || cmd === 'image_set_backend') return Promise.resolve(undefined);
    if (cmd === 'cancel_run') return Promise.resolve();
    if (cmd === 'image_stage_work') return Promise.resolve({ token: `token-${invoked.length}` });
    if (cmd === 'get_attachment_bytes') return Promise.resolve([[137, 80, 78, 71], 'image/png']);
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    listeners[event] = callback;
    return Promise.resolve(() => {});
  }),
}));

vi.mock('@/pages/images/imageEncoding', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/images/imageEncoding')>();
  return { ...actual, sha256Bytes: vi.fn(async () => 'sha') };
});

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

function makeFile(name: string, type = 'image/png'): File {
  const file = new File([new Uint8Array([1, 2, 3, 4])], name, { type });
  Object.defineProperty(file, 'arrayBuffer', {
    configurable: true,
    value: async () => new Uint8Array([1, 2, 3, 4]).buffer,
  });
  return file;
}

function generationArgs(): Record<string, unknown> | undefined {
  return invoked.find((entry) => entry.cmd === 'image_generate')?.args;
}

function fire(event: string, payload: Record<string, unknown>) {
  const callback = listeners[event];
  if (!callback) throw new Error(`Aucun listener pour ${event}`);
  act(() => callback({ payload }));
}

async function addFiles(files: File[]) {
  const input = screen.getByLabelText('Ajouter des références', { selector: 'input' });
  await act(async () => {
    fireEvent.change(input, { target: { files } });
    await Promise.resolve();
  });
}

async function fillInstruction(value = 'un phare breton') {
  const input = await screen.findByRole('textbox', { name: 'Instruction' });
  fireEvent.change(input, { target: { value } });
}

beforeEach(() => {
  invoked.length = 0;
  Object.keys(listeners).forEach((key) => { delete listeners[key]; });
  vi.stubGlobal('Image', FakeImage as any);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:image-${Math.random()}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Images — graine automatique et références de création', () => {
  it('affiche une graine aléatoire en placeholder et l’envoie sans saisie', async () => {
    render(<ImagesPage />);
    await fillInstruction();
    const seedField = screen.getByRole('spinbutton', { name: /Graine/ }) as HTMLInputElement;
    expect(seedField.value).toBe('');
    const autoSeed = Number(seedField.placeholder);
    expect(Number.isInteger(autoSeed)).toBe(true);

    const createButton = screen.getByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(generationArgs()).toBeTruthy());
    expect(generationArgs()?.seed).toBe(autoSeed);
  });

  it('passe en graine fixe et envoie la valeur saisie', async () => {
    render(<ImagesPage />);
    await fillInstruction();
    fireEvent.change(screen.getByRole('spinbutton', { name: /Graine/ }), { target: { value: '42' } });
    expect(screen.getByText('fixe')).toBeTruthy();

    const createButton = screen.getByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(generationArgs()).toBeTruthy());
    expect(generationArgs()?.seed).toBe(42);
  });

  it('le bouton Aléatoire vide le champ et régénère une graine', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });
    const seedField = screen.getByRole('spinbutton', { name: /Graine/ }) as HTMLInputElement;

    fireEvent.change(seedField, { target: { value: '42' } });
    expect(seedField.value).toBe('42');
    const previousPlaceholder = seedField.placeholder;

    fireEvent.click(screen.getByRole('button', { name: 'Revenir à une graine aléatoire' }));

    expect(seedField.value).toBe('');
    expect(screen.getByText('aléatoire')).toBeTruthy();
    expect(Number.isInteger(Number(seedField.placeholder))).toBe(true);
    expect(seedField.placeholder).not.toBe('');
    expect(previousPlaceholder.length).toBeGreaterThan(0);
  });

  it('accepte quatre références puis refuse les suivantes', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });

    await addFiles([makeFile('a.png'), makeFile('b.png'), makeFile('c.png'), makeFile('d.png')]);
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(4));
    expect(screen.getByText('4/4')).toBeTruthy();

    await addFiles([makeFile('e.png')]);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('4 références maximum'));
    expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(4);
  });

  it('accepte quatre références glissées sur la zone de création', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });

    const zone = document.querySelector('.img-drop-zone');
    expect(zone).toBeTruthy();
    await act(async () => {
      fireEvent.drop(zone as HTMLElement, {
        dataTransfer: { files: [makeFile('a.png'), makeFile('b.png'), makeFile('c.png'), makeFile('d.png')] },
      });
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(4));
    expect(screen.getByText('4/4')).toBeTruthy();
  });

  it('fait tourner la graine automatique après un run terminal', async () => {
    render(<ImagesPage />);
    await fillInstruction('rotation de graine');
    const seedField = screen.getByRole('spinbutton', { name: /Graine/ }) as HTMLInputElement;
    const previousSeed = Number(seedField.placeholder);
    expect(Number.isInteger(previousSeed)).toBe(true);

    const createButton = screen.getByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(generationArgs()).toBeTruthy());
    expect(generationArgs()?.seed).toBe(previousSeed);
    const requestId = String(generationArgs()?.requestId);

    fire('artifact_created', { request_id: requestId, run_id: 'run-1', artifact_id: 'art_rotation' });
    fire('run_completed', { request_id: requestId, run_id: 'run-1', artifact_id: 'art_rotation', status: 'completed' });

    await waitFor(() => expect(Number(seedField.placeholder)).not.toBe(previousSeed));
    expect(Number.isInteger(Number(seedField.placeholder))).toBe(true);
  });

  it('refuse les fichiers en trop lors d’une sélection multiple', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });

    await addFiles([makeFile('a.png'), makeFile('b.png'), makeFile('c.png'), makeFile('d.png'), makeFile('e.png')]);
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(4));
    expect(screen.getByRole('alert').textContent).toContain('4 références maximum');
  });

  it('ajoute l’image valide d’une sélection mixte et affiche l’erreur de validation', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });

    await addFiles([makeFile('valide.png'), makeFile('notes.txt', 'text/plain')]);

    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('notes.txt'));
  });

  it('révoque l’URL de la référence écartée au-delà de quatre', async () => {
    const revokeObjectUrl = URL.revokeObjectURL as unknown as ReturnType<typeof vi.fn>;
    revokeObjectUrl.mockClear();
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });

    await addFiles([makeFile('a.png'), makeFile('b.png'), makeFile('c.png'), makeFile('d.png'), makeFile('e.png')]);

    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(4));
    expect(revokeObjectUrl.mock.calls.length).toBeGreaterThan(0);
  });

  it('génère en mode référence avec les deux images fournies', async () => {
    render(<ImagesPage />);
    await fillInstruction('deux références');
    await addFiles([makeFile('a.png'), makeFile('b.png')]);
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(2));

    const createButton = screen.getByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(generationArgs()).toBeTruthy());
    const args = generationArgs() as Record<string, unknown>;
    expect(args.mode).toBe('reference');
    expect(args.referenceTokens).toHaveLength(2);
    expect(args.referenceSha256).toHaveLength(2);
  });
});

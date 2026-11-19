import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener> = {};
const invoked: Array<{ cmd: string; args: unknown }> = [];
let attachmentAttempts = 0;
let attachmentShouldFail = false;
let attachmentFailureCount = 0;
let attachmentResolvers: Array<(value: [number[], string]) => void> | null = null;
let cancelNeverResolves = false;
let stageShouldFail = false;
let stageResolvers: Array<(value: { token: string }) => void> | null = null;

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    invoked.push({ cmd, args });
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_status') return Promise.resolve(undefined);
    if (cmd === 'image_generate' || cmd === 'image_set_backend') return Promise.resolve(undefined);
    if (cmd === 'image_stage_work') {
      if (stageResolvers !== null) {
        return new Promise<{ token: string }>((resolve) => stageResolvers?.push(resolve));
      }
      if (stageShouldFail) return Promise.reject(new Error('staging impossible'));
      return Promise.resolve({ token: `token-${invoked.length}` });
    }
    if (cmd === 'cancel_run') return cancelNeverResolves ? new Promise<void>(() => undefined) : Promise.resolve();
    if (cmd === 'get_attachment_bytes') {
      attachmentAttempts += 1;
      if (attachmentResolvers !== null) {
        return new Promise<[number[], string]>((resolve) => attachmentResolvers?.push(resolve));
      }
      if (attachmentFailureCount > 0) {
        attachmentFailureCount -= 1;
        return Promise.reject(new Error('octets illisibles'));
      }
      return attachmentShouldFail && attachmentAttempts === 1
        ? Promise.reject(new Error('octets illisibles'))
        : Promise.resolve([[137, 80, 78, 71], 'image/png']);
    }
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    listeners[event] = callback;
    return Promise.resolve(() => {});
  }),
}));

;

import { ImagesPage } from '@/pages/images/ImagesPage';

function fire(event: string, payload: Record<string, unknown>) {
  const callback = listeners[event];
  if (!callback) throw new Error(`Aucun listener pour ${event}`);
  act(() => callback({ payload }));
}

class FakeImage {
  naturalWidth = 100;
  naturalHeight = 200;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(_value: string) {
    setTimeout(() => this.onload?.(), 0);
  }
}

async function startRun(prompt = 'un phare breton') {
  render(<ImagesPage />);
  const input = await screen.findByRole('textbox', { name: 'Instruction' });
  fireEvent.change(input, { target: { value: prompt } });
  fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
  const generation = invoked.find((entry) => entry.cmd === 'image_generate');
  expect(generation).toBeTruthy();
  const requestId = String((generation?.args as { requestId: string }).requestId);
  await waitFor(() => expect(screen.getAllByText('Envoi…').length).toBeGreaterThan(0));
  return requestId;
}

beforeEach(() => {
  invoked.length = 0;
  attachmentAttempts = 0;
  attachmentShouldFail = false;
  attachmentFailureCount = 0;
  attachmentResolvers = null;
  cancelNeverResolves = false;
  stageShouldFail = false;
  stageResolvers = null;
  Object.keys(listeners).forEach((key) => delete listeners[key]);
  vi.stubGlobal('Image', FakeImage as any);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:image-${Math.random()}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Images — machine à états et événements du run', () => {
  it('écoute tous les événements du contrat', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });
    for (const name of ['run_started', 'run_stage', 'run_failed', 'run_completed', 'run_cancelling', 'run_cancelled', 'artifact_created', 'image_status', 'backend_terminated']) {
      expect(Object.keys(listeners)).toContain(name);
    }
  });

  it('expose les onze ratios officiels proches d’un megapixel', async () => {
    render(<ImagesPage />);
    await screen.findByRole('textbox', { name: 'Instruction' });
    const formats = screen.getAllByRole('button', { name: /^\d+:\d+ \d+x\d+$/ });
    expect(formats).toHaveLength(11);
    expect(screen.getByRole('button', { name: '1:1 1024x1024' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '16:9 1280x720' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '9:21 672x1568' })).toBeTruthy();
  });

  it('échoue visiblement si le sidecar n’accuse pas la demande', async () => {
    render(<ImagesPage />);
    const input = await screen.findByRole('textbox', { name: 'Instruction' });
    cancelNeverResolves = true;
    vi.useFakeTimers();
    fireEvent.change(input, { target: { value: 'un phare' } });
    fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      vi.advanceTimersByTime(8_000);
      await Promise.resolve();
    });
    expect(screen.getAllByText(/n’a pas accusé la demande/).length).toBeGreaterThan(0);
    expect(invoked.some((entry) => entry.cmd === 'cancel_run')).toBe(true);
    expect(screen.getByText('Échec')).toBeTruthy();
  });

  it('échoue proprement si le staging d’une référence échoue', async () => {
    stageShouldFail = true;
    render(<ImagesPage />);
    const input = await screen.findByRole('textbox', { name: 'Instruction' });
    fireEvent.change(input, { target: { value: 'une image' } });

    const file = new File([new Uint8Array([1, 2, 3, 4])], 'ref.png', { type: 'image/png' });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: async () => new Uint8Array([1, 2, 3, 4]).buffer,
    });
    const referenceInput = screen.getByLabelText('Ajouter des références', { selector: 'input' });
    await act(async () => {
      fireEvent.change(referenceInput, { target: { files: [file] } });
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(1));

    const createButton = screen.getByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(screen.getByText('Échec')).toBeTruthy());
    expect(invoked.some((entry) => entry.cmd === 'image_generate')).toBe(false);
    await waitFor(() => expect(screen.getByRole('button', { name: /Créer l’image/ })).toHaveProperty('disabled', false));
  });

  it('affiche l’encodeur, les quatre pas de diffusion, le VAE et le transfert', async () => {
    const requestId = await startRun();
    fire('run_started', { request_id: requestId, run_id: requestId, conversation_id: null });
    await waitFor(() => expect(screen.getAllByText(/Run reçu/).length).toBeGreaterThan(0));

    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'texte-inference', status: 'Encodage du prompt' });
    await waitFor(() => expect(screen.getAllByText('Encodage du prompt').length).toBeGreaterThan(0));

    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'diffusion', status: 'Diffusion — pas 1/4', step: 1, total: 4 });
    await waitFor(() => expect(screen.getAllByText('Diffusion — pas 1/4').length).toBeGreaterThan(0));
    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'diffusion', status: 'Diffusion — pas 4/4', step: 4, total: 4 });
    await waitFor(() => expect(screen.getAllByText('Diffusion — pas 4/4').length).toBeGreaterThan(0));

    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'vae-inference', status: 'Décodage de l’image (VAE)' });
    await waitFor(() => expect(screen.getAllByText('Décodage de l’image (VAE)').length).toBeGreaterThan(0));
    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'publication', status: 'Enregistrement local de l’image' });
    await waitFor(() => expect(screen.getAllByText('Enregistrement local de l’image').length).toBeGreaterThan(0));
    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'transfert', status: 'Transfert de l’image vers l’atelier' });
    await waitFor(() => expect(screen.getAllByText('Transfert de l’image vers l’atelier').length).toBeGreaterThan(0));
  });

  it('fait progresser le run puis charge et valide après artifact_created et run_completed', async () => {
    const requestId = await startRun();
    fire('run_stage', { request_id: requestId, run_id: requestId, stage: 'texte-chargement', status: 'Chargement de l’encodeur' });
    await waitFor(() => expect(screen.getAllByText(/Chargement de l’encodeur/).length).toBeGreaterThan(0));
    fire('artifact_created', { request_id: requestId, run_id: requestId, artifact_id: 'art_test123' });
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_test123' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
    expect(screen.queryByText('Envoi…')).toBeNull();
  });

  it('accepte artifact_id uniquement dans run_completed', async () => {
    const requestId = await startRun();
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_completed_only' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
  });

  it('échoue distinctement et rend un run annulé distinct', async () => {
    const failedRequest = await startRun('une image');
    fire('run_failed', { request_id: failedRequest, run_id: failedRequest, code: 'image_failed', message: 'Spike exit=3' });
    await waitFor(() => expect(screen.getAllByText(/image_failed/).length).toBeGreaterThan(0));
    expect(screen.getByText('Échec')).toBeTruthy();

    cleanup();
    invoked.length = 0;
    const cancelledRequest = await startRun('une autre image');
    fire('run_cancelling', { request_id: cancelledRequest, run_id: cancelledRequest, status: 'cancelling' });
    await waitFor(() => expect(screen.getAllByText('Arrêt demandé…').length).toBeGreaterThan(0));
    fire('run_cancelled', { request_id: cancelledRequest, run_id: cancelledRequest, status: 'cancelled' });
    await waitFor(() => expect(screen.getByText('Annulé')).toBeTruthy());
    expect(screen.queryByText(/image_failed/)).toBeNull();
  });

  it('backend_terminated termine le run avec une erreur claire', async () => {
    const requestId = await startRun();
    fire('backend_terminated', { code: 9, message: 'Sidecar terminé avec le code 9' });
    await waitFor(() => expect(screen.getAllByText(/Sidecar arrêté/).length).toBeGreaterThan(0));
    expect(requestId).toBeTruthy();
    expect(screen.getByText('Échec')).toBeTruthy();
  });

  it('ignore les événements d’un autre request_id', async () => {
    const requestId = await startRun();
    fire('run_stage', { request_id: 'autre-run', run_id: 'autre-run', stage: 'diffusion', status: 'Diffusion' });
    fire('artifact_created', { request_id: 'autre-run', run_id: 'autre-run', artifact_id: 'art_x' });
    await waitFor(() => expect(screen.getAllByText('Envoi…').length).toBeGreaterThan(0));
    expect(screen.queryByText('Diffusion')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
    expect(requestId).toBeTruthy();
  });

  it('propose un retry après un échec de chargement des octets', async () => {
    attachmentShouldFail = true;
    const requestId = await startRun();
    fire('artifact_created', { request_id: requestId, run_id: requestId, artifact_id: 'art_retry' });
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_retry' });
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: 'Retour à la page Image' })).toBeNull();
  });

  it('empêche un ancien chargement de terminer un nouveau run actif', async () => {
    attachmentFailureCount = 1;
    const firstRequest = await startRun('première image');
    fire('artifact_created', { request_id: firstRequest, run_id: firstRequest, artifact_id: 'art_old_load' });
    fire('run_completed', { request_id: firstRequest, run_id: firstRequest, status: 'completed', artifact_id: 'art_old_load' });
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));

    attachmentResolvers = [];
    fireEvent.click(screen.getByRole('button', { name: 'Réessayer le chargement' }));
    await waitFor(() => expect(attachmentResolvers).toHaveLength(1));
    fireEvent.change(await screen.findByRole('textbox', { name: 'Instruction' }), { target: { value: 'nouvelle image' } });
    fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
    const [resolve] = attachmentResolvers;
    await act(async () => {
      resolve?.([[137, 80, 78, 71], 'image/png']);
      await Promise.resolve();
    });
    attachmentResolvers = null;
    expect(screen.getByText('En attente du moteur')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Génération…' })).toHaveProperty('disabled', true);
  });

  it('révoque les object URLs au démontage', async () => {
    const view = render(<ImagesPage />);
    const requestId = await startRunFromExisting(view);
    fire('artifact_created', { request_id: requestId, run_id: requestId, artifact_id: 'art_unmount' });
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_unmount' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
    expect(invoked.some((entry) => entry.cmd === 'cancel_run')).toBe(false);
  });


  it('annule le run actif au démontage', async () => {
    const view = render(<ImagesPage />);
    const requestId = await startRunFromExisting(view);
    view.unmount();
    await waitFor(() => {
      expect(invoked.some((entry) =>
        entry.cmd === 'cancel_run' && (entry.args as { requestId?: string }).requestId === requestId,
      )).toBe(true);
    });
  });


  it('n’annule pas un run encore en staging au démontage', async () => {
    stageResolvers = [];
    const view = render(<ImagesPage />);
    const input = await screen.findByRole('textbox', { name: 'Instruction' });
    fireEvent.change(input, { target: { value: 'une image' } });

    const file = new File([new Uint8Array([1, 2, 3, 4])], 'ref.png', { type: 'image/png' });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: async () => new Uint8Array([1, 2, 3, 4]).buffer,
    });
    const referenceInput = screen.getByLabelText('Ajouter des références', { selector: 'input' });
    await act(async () => {
      fireEvent.change(referenceInput, { target: { files: [file] } });
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Retirer / })).toHaveLength(1));

    const createButton = screen.getByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);
    await waitFor(() => expect(stageResolvers).toHaveLength(1));
    expect(invoked.some((entry) => entry.cmd === 'image_generate')).toBe(false);

    view.unmount();
    await act(async () => {
      stageResolvers?.[0]?.({ token: 'token-tardif' });
      await Promise.resolve();
    });
    expect(invoked.some((entry) => entry.cmd === 'cancel_run')).toBe(false);
  });


  it('envoie la graine automatique affichée, puis la graine saisie', async () => {
    render(<ImagesPage />);
    const input = await screen.findByRole('textbox', { name: 'Instruction' });
    fireEvent.change(input, { target: { value: 'une image' } });
    const seedField = screen.getByRole('spinbutton', { name: /Graine/ }) as HTMLInputElement;
    const autoSeed = Number(seedField.placeholder);
    expect(Number.isInteger(autoSeed)).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
    let generation = invoked.find((entry) => entry.cmd === 'image_generate');
    expect((generation?.args as { seed?: number }).seed).toBe(autoSeed);
    cleanup();

    invoked.length = 0;
    render(<ImagesPage />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'Instruction' }), { target: { value: 'une image' } });
    fireEvent.change(screen.getByRole('spinbutton', { name: /Graine/ }), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
    generation = invoked.find((entry) => entry.cmd === 'image_generate');
    expect((generation?.args as { seed?: number }).seed).toBe(0);
  });
});

async function startRunFromExisting(view: ReturnType<typeof render>) {
  const input = await screen.findByRole('textbox', { name: 'Instruction' });
  fireEvent.change(input, { target: { value: 'une image' } });
  fireEvent.click(screen.getByRole('button', { name: /Créer l’image/ }));
  const generation = invoked.find((entry) => entry.cmd === 'image_generate');
  return String((generation?.args as { requestId: string }).requestId);
}

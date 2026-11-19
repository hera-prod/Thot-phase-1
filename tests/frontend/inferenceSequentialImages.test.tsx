import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { InferenceActivityProvider, useInferenceActivity } from '@/features/inference/InferenceActivityContext';
import { ImagesPage } from '@/pages/images/ImagesPage';

type Listener = (event: { payload: Record<string, unknown> }) => void;

const listeners: Record<string, Listener[]> = {};
const invoked: Array<{ cmd: string; args: unknown }> = [];
let stagePending = false;

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    invoked.push({ cmd, args });
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_status') return Promise.resolve(undefined);
    if (cmd === 'image_generate' || cmd === 'image_set_backend') return Promise.resolve(undefined);
    if (cmd === 'image_stage_work') {
      if (stagePending) return new Promise(() => undefined);
      return Promise.resolve({ token: 'token-test' });
    }
    if (cmd === 'cancel_run') return Promise.resolve();
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    listeners[event] = [...(listeners[event] ?? []), callback];
    let active = true;
    return Promise.resolve(() => {
      if (!active) return;
      active = false;
      listeners[event] = (listeners[event] ?? []).filter((entry) => entry !== callback);
    });
  }),
}));

function fire(event: string, payload: Record<string, unknown>): void {
  const handlers = listeners[event] ?? [];
  if (handlers.length === 0) throw new Error(`Aucun listener pour ${event}`);
  act(() => { handlers.forEach((handler) => handler({ payload })); });
}

function ActivityProbe() {
  const { activity } = useInferenceActivity();
  return <div data-testid="activity">{activity.active ? activity.kind : 'none'}</div>;
}

function Harness({ showImages }: { showImages: boolean }) {
  return (
    <InferenceActivityProvider>
      <ActivityProbe />
      {showImages ? <ImagesPage /> : null}
    </InferenceActivityProvider>
  );
}

function imageFile(name: string): File {
  const file = new File([new Uint8Array([1, 2, 3, 4])], name, { type: 'image/png' });
  Object.defineProperty(file, 'arrayBuffer', {
    configurable: true,
    value: async () => new Uint8Array([1, 2, 3, 4]).buffer,
  });
  return file;
}

beforeEach(() => {
  invoked.length = 0;
  stagePending = false;
  Object.keys(listeners).forEach((key) => { delete listeners[key]; });
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:test') });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
});

describe('Images — sérialisation des inférences', () => {
  it('désactive la création pendant une discussion et la libère à la fin', async () => {
    const { container } = render(
      <InferenceActivityProvider>
        <ImagesPage />
      </InferenceActivityProvider>,
    );

    const promptInput = await screen.findByRole('textbox', { name: 'Instruction' });
    fireEvent.change(promptInput, { target: { value: 'un phare breton' } });

    const createButton = await screen.findByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));

    fire('run_started', { request_id: 'run-chat', run_id: 'run-chat', conversation_id: 'conv-1' });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Moteur occupé…' })).toHaveProperty('disabled', true);
    });

    const form = container.querySelector('form');
    expect(form).toBeTruthy();
    fireEvent.submit(form as HTMLFormElement);

    const alerts = await screen.findAllByText('Une discussion est en cours. Attendez sa fin avant de lancer une génération d’image.');
    expect(alerts.length).toBeGreaterThan(0);
    expect(invoked.some((entry) => entry.cmd === 'image_generate')).toBe(false);

    fire('run_completed', { request_id: 'run-chat', run_id: 'run-chat' });

    const readyButton = await screen.findByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((readyButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.submit(form as HTMLFormElement);
    await waitFor(() => expect(invoked.some((entry) => entry.cmd === 'image_generate')).toBe(true));
  });

  it('libère l’activité globale quand la page est démontée pendant le staging', async () => {
    stagePending = true;
    const { rerender } = render(<Harness showImages />);

    const referenceInput = await screen.findByLabelText('Ajouter des références', { selector: 'input' });
    await act(async () => {
      fireEvent.change(referenceInput, { target: { files: [imageFile('ref.png')] } });
      await Promise.resolve();
    });
    await screen.findByText('ref.png');

    fireEvent.change(screen.getByRole('textbox', { name: 'Instruction' }), { target: { value: 'un phare breton' } });
    const createButton = await screen.findByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(screen.getByTestId('activity').textContent).toBe('image'));
    expect(invoked.some((entry) => entry.cmd === 'image_stage_work')).toBe(true);

    rerender(<Harness showImages={false} />);
    await waitFor(() => expect(screen.getByTestId('activity').textContent).toBe('none'));
  });

  it('désactive la création pendant un entretien mémoire et la libère à la fin', async () => {
    render(
      <InferenceActivityProvider>
        <ImagesPage />
      </InferenceActivityProvider>,
    );

    const promptInput = await screen.findByRole('textbox', { name: 'Instruction' });
    fireEvent.change(promptInput, { target: { value: 'un phare breton' } });
    await screen.findByRole('button', { name: /Créer l’image/ });

    fire('memory_started', { workspace_id: 'ws_1', kind: 'memory', mode: 'auto' });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Entretien mémoire…' })).toHaveProperty('disabled', true);
    });

    fire('memory_finished', { workspace_id: 'ws_1', statut: 'effectue' });

    const readyButton = await screen.findByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((readyButton as HTMLButtonElement).disabled).toBe(false));
  });

  it('garde l’arrêt d’un run image visible malgré un entretien différé', async () => {
    stagePending = true;
    render(
      <InferenceActivityProvider>
        <ImagesPage />
      </InferenceActivityProvider>,
    );

    fireEvent.change(await screen.findByRole('textbox', { name: 'Instruction' }), { target: { value: 'un phare breton' } });
    const createButton = await screen.findByRole('button', { name: /Créer l’image/ });
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(createButton);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Arrêter' })).toBeTruthy());

    fire('memory_deferred', { workspace_id: 'ws_1', statut: 'differe', raison: 'inférence en cours' });

    const arret = screen.getByRole('button', { name: 'Arrêter' });
    expect((arret as HTMLButtonElement).disabled).toBe(false);
  });
});

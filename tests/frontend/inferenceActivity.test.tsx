import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrictMode, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { InferenceActivityProvider, useInferenceActivity } from '@/features/inference/InferenceActivityContext';
import type { LiveRunSnapshot } from '@/features/inference/InferenceActivityContext';
import { Sidebar } from '@/components/shell/Sidebar';

type Listener = (event: { payload: Record<string, unknown> }) => void;

const listeners: Record<string, Listener[]> = {};

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn(() => Promise.resolve(undefined)),
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

const EVENTS = ['run_started', 'message_started', 'run_cancelling', 'run_completed', 'run_failed', 'run_cancelled', 'error', 'backend_terminated', 'memory_started', 'memory_deferred', 'memory_finished'];

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

async function renderSidebar(): Promise<void> {
  render(
    <MemoryRouter initialEntries={['/gallery']}>
      <InferenceActivityProvider>
        <Sidebar />
        <LocationProbe />
      </InferenceActivityProvider>
    </MemoryRouter>,
  );
  await waitFor(() => {
    const missing = EVENTS.filter((event) => (listeners[event]?.length ?? 0) === 0);
    if (missing.length > 0) throw new Error(`Listeners absents : ${missing.join(', ')}`);
  });
}

function BeginProbe() {
  const { activity, begin } = useInferenceActivity();
  const [result, setResult] = useState('none');
  return (
    <div>
      <span data-testid="request">{activity.requestId ?? 'none'}</span>
      <span data-testid="result">{result}</span>
      <button
        type="button"
        onClick={() => setResult(String(begin({ kind: 'chat', requestId: 'req-1', conversationId: 'conv-1' })))}
      >
        Démarrer premier
      </button>
      <button
        type="button"
        onClick={() => setResult(String(begin({ kind: 'chat', requestId: 'req-2', conversationId: 'conv-1' })))}
      >
        Démarrer second
      </button>
    </div>
  );
}

function LiveCapture({ onReady }: { onReady: (fn: () => LiveRunSnapshot | null) => void }) {
  const { getLiveRun } = useInferenceActivity();
  onReady(getLiveRun);
  return null;
}

beforeEach(() => {
  Object.keys(listeners).forEach((key) => { delete listeners[key]; });
});

afterEach(() => {
  cleanup();
});

describe('Indicateur global d’inférence', () => {
  it('refuse un second begin concurrent et ne remplace pas l’activité', () => {
    render(
      <MemoryRouter>
        <InferenceActivityProvider>
          <BeginProbe />
        </InferenceActivityProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Démarrer premier' }));
    expect(screen.getByTestId('result')).toHaveTextContent('true');
    expect(screen.getByTestId('request')).toHaveTextContent('req-1');

    fireEvent.click(screen.getByRole('button', { name: 'Démarrer second' }));
    expect(screen.getByTestId('result')).toHaveTextContent('false');
    expect(screen.getByTestId('request')).toHaveTextContent('req-1');
  });

  it('libère une activité pending restée sans terminal après le watchdog', () => {
    vi.useFakeTimers();
    try {
      render(
        <MemoryRouter>
          <InferenceActivityProvider>
            <BeginProbe />
          </InferenceActivityProvider>
        </MemoryRouter>,
      );

      fireEvent.click(screen.getByRole('button', { name: 'Démarrer premier' }));
      expect(screen.getByTestId('request')).toHaveTextContent('req-1');

      act(() => { vi.advanceTimersByTime(90_000); });
      expect(screen.getByTestId('request')).toHaveTextContent('none');
    } finally {
      vi.useRealTimers();
    }
  });

  it('libère une activité running après le watchdog de 30 minutes', async () => {
    vi.useFakeTimers();
    try {
      render(
        <MemoryRouter>
          <InferenceActivityProvider>
            <BeginProbe />
          </InferenceActivityProvider>
        </MemoryRouter>,
      );

      // Laisse le Promise.all d'abonnements résoudre ses microtâches avant de
      // simuler le run_started (les timers factices ne les retardent pas).
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      fire('run_started', { request_id: 'req-run', run_id: 'req-run', conversation_id: 'conv-1' });
      expect(screen.getByTestId('request')).toHaveTextContent('req-run');

      act(() => { vi.advanceTimersByTime(30 * 60_000 - 1000); });
      expect(screen.getByTestId('request')).toHaveTextContent('req-run');

      act(() => { vi.advanceTimersByTime(2000); });
      expect(screen.getByTestId('request')).toHaveTextContent('none');
    } finally {
      vi.useRealTimers();
    }
  });

  it('affiche une génération d’image puis la retire à la fin du run', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-image', run_id: 'run-image', kind: 'production', conversation_id: null });
    const indicator = await screen.findByRole('button', { name: 'Génération d’image en cours — ouvrir' });
    expect(indicator).toBeTruthy();

    fire('run_completed', { request_id: 'run-image', run_id: 'run-image' });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Génération d’image en cours — ouvrir' })).toBeNull();
    });
  });

  it('affiche une discussion puis son arrêt en cours', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-chat', run_id: 'run-chat', conversation_id: 'conv-42' });
    await screen.findByRole('button', { name: 'Discussion en cours — ouvrir' });

    fire('run_cancelling', { request_id: 'run-chat', run_id: 'run-chat' });
    await screen.findByRole('button', { name: 'Arrêt en cours… — ouvrir' });

    fire('run_cancelled', { request_id: 'run-chat', run_id: 'run-chat' });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Arrêt en cours… — ouvrir' })).toBeNull();
    });
  });

  it('ignore une erreur quelconque pendant un run déjà en exécution', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-chat', run_id: 'run-chat', conversation_id: 'conv-42' });
    await screen.findByRole('button', { name: 'Discussion en cours — ouvrir' });

    fire('error', { request_id: 'run-chat', code: 'generator_error', message: 'générateur instable, reprise' });
    expect(screen.getByRole('button', { name: 'Discussion en cours — ouvrir' })).toBeTruthy();
  });

  it('ne recrée pas une activité sur un message_started tardif après run_completed', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-chat', run_id: 'run-chat', conversation_id: 'conv-42' });
    await screen.findByRole('button', { name: 'Discussion en cours — ouvrir' });

    fire('run_completed', { request_id: 'run-chat', run_id: 'run-chat' });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Discussion en cours — ouvrir' })).toBeNull();
    });

    fire('message_started', { request_id: 'run-chat', run_id: 'run-chat', model: 'LFM2.5-8B' });
    expect(screen.queryByRole('button', { name: 'Discussion en cours — ouvrir' })).toBeNull();
  });

  it('accepte une annulation corrélée par run_id malgré un request_id de commande différent', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-chat', run_id: 'run-42', conversation_id: 'conv-42' });
    await screen.findByRole('button', { name: 'Discussion en cours — ouvrir' });

    fire('run_cancelling', { request_id: 'cancel-cmd', run_id: 'run-42' });
    await screen.findByRole('button', { name: 'Arrêt en cours… — ouvrir' });
  });

  it('ignore le terminal d’un autre run et libère sur le bon run_failed', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-chat', run_id: 'run-chat', conversation_id: 'conv-42' });
    await screen.findByRole('button', { name: 'Discussion en cours — ouvrir' });

    fire('run_completed', { request_id: 'autre-request', run_id: 'autre-run' });
    expect(screen.getByRole('button', { name: 'Discussion en cours — ouvrir' })).toBeTruthy();

    fire('run_failed', { request_id: 'run-chat', run_id: 'run-chat', code: 'empty_response' });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Discussion en cours — ouvrir' })).toBeNull();
    });
  });

  it('retire l’indicateur quand le sidecar se termine', async () => {
    await renderSidebar();

    fire('run_started', { request_id: 'run-image', run_id: 'run-image', kind: 'production' });
    await screen.findByRole('button', { name: 'Génération d’image en cours — ouvrir' });

    fire('backend_terminated', { code: null, message: 'arrêt', retryable: false });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Génération d’image en cours — ouvrir' })).toBeNull();
    });
  });

  describe('Entretien mémoire', () => {
    it('affiche l’entretien en cours et ouvre la fiche mémoire', async () => {
      await renderSidebar();

      fire('memory_started', {
        request_id: 'mem-1',
        workspace_id: 'ws_1',
        workspace_name: 'Espace 1',
        kind: 'memory',
        mode: 'auto',
        raison: 'seuil',
        tours: 3,
      });

      const indicator = await screen.findByRole('button', { name: 'Entretien mémoire en cours — ouvrir' });
      expect(indicator).toBeTruthy();

      fireEvent.click(indicator);
      expect(screen.getByTestId('location')).toHaveTextContent('/espaces/ws_1/memoire');
    });

    it('affiche un entretien en attente', async () => {
      await renderSidebar();

      fire('memory_deferred', {
        workspace_id: 'ws_1',
        workspace_name: 'Espace 1',
        statut: 'en_attente',
        raison: 'run_actif',
        bloque_par: 'chat',
        retry_after_s: 30,
      });

      expect(await screen.findByRole('button', { name: 'Entretien mémoire en attente — ouvrir' })).toBeTruthy();
    });

    it('affiche un entretien différé', async () => {
      await renderSidebar();

      fire('memory_deferred', {
        workspace_id: 'ws_1',
        statut: 'differe',
        raison: 'quota',
      });

      expect(await screen.findByRole('button', { name: 'Entretien mémoire différé — ouvrir' })).toBeTruthy();
    });

    it('ne rétrograde pas un entretien en cours sur un différé, même d’un autre espace', async () => {
      await renderSidebar();

      fire('memory_started', { workspace_id: 'ws_A', workspace_name: 'Espace A', kind: 'memory', mode: 'auto' });
      await screen.findByRole('button', { name: 'Entretien mémoire en cours — ouvrir' });

      // Le service peut réémettre un heartbeat différé alors que SON entretien
      // tourne, et un différé d'un autre espace ne doit pas voler l'indicateur.
      fire('memory_deferred', { workspace_id: 'ws_A', statut: 'differe', raison: 'verrou_occupe' });
      fire('memory_deferred', { workspace_id: 'ws_B', statut: 'differe', raison: 'verrou_occupe' });

      expect(screen.getByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Entretien mémoire différé — ouvrir' })).toBeNull();

      fire('memory_finished', { workspace_id: 'ws_A', statut: 'effectue' });
      await waitFor(() => {
        expect(screen.queryByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toBeNull();
      });
    });

    it('retire l’indicateur à la fin de l’entretien', async () => {
      await renderSidebar();

      fire('memory_started', { workspace_id: 'ws_1', kind: 'memory', mode: 'force' });
      await screen.findByRole('button', { name: 'Entretien mémoire en cours — ouvrir' });

      fire('memory_finished', { workspace_id: 'ws_1', statut: 'succes', messages: 4, faits: 2 });
      await waitFor(() => {
        expect(screen.queryByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toBeNull();
      });
    });

    it('retire l’indicateur quand le sidecar se termine', async () => {
      await renderSidebar();

      fire('memory_started', { workspace_id: 'ws_1', kind: 'memory', mode: 'auto' });
      await screen.findByRole('button', { name: 'Entretien mémoire en cours — ouvrir' });

      fire('backend_terminated', { code: null, message: 'arrêt', retryable: false });
      await waitFor(() => {
        expect(screen.queryByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toBeNull();
      });
    });

    it('laisse coexister l’inférence chat et un entretien différé', async () => {
      await renderSidebar();

      fire('run_started', { request_id: 'run-chat', run_id: 'run-chat', conversation_id: 'conv-42' });
      await screen.findByRole('button', { name: 'Discussion en cours — ouvrir' });

      fire('memory_deferred', { workspace_id: 'ws_1', statut: 'differe' });

      expect(await screen.findByRole('button', { name: 'Entretien mémoire différé — ouvrir' })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Discussion en cours — ouvrir' })).toBeTruthy();
    });

    it('retire un entretien en cours après le watchdog de 5 minutes', async () => {
      vi.useFakeTimers();
      try {
        render(
          <MemoryRouter initialEntries={['/gallery']}>
            <InferenceActivityProvider>
              <Sidebar />
            </InferenceActivityProvider>
          </MemoryRouter>,
        );

        await act(async () => {
          await Promise.resolve();
          await Promise.resolve();
        });

        fire('memory_started', { workspace_id: 'ws_1', kind: 'memory', mode: 'auto' });
        expect(screen.getByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toBeTruthy();

        act(() => { vi.advanceTimersByTime(5 * 60_000 + 1000); });
        expect(screen.queryByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it('ne produit qu’un bouton mémoire sous StrictMode', async () => {
      render(
        <StrictMode>
          <MemoryRouter initialEntries={['/gallery']}>
            <InferenceActivityProvider>
              <Sidebar />
            </InferenceActivityProvider>
          </MemoryRouter>
        </StrictMode>,
      );

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      fire('memory_started', { workspace_id: 'ws_1', kind: 'memory', mode: 'auto' });
      expect(screen.getAllByRole('button', { name: 'Entretien mémoire en cours — ouvrir' })).toHaveLength(1);
    });
  });

describe('Snapshot live du run', () => {
  async function renderLive(): Promise<{ getLiveRun: () => LiveRunSnapshot | null }> {
    let getter: () => LiveRunSnapshot | null = () => null;
    render(
      <MemoryRouter>
        <InferenceActivityProvider>
          <LiveCapture onReady={(fn) => { getter = fn; }} />
        </InferenceActivityProvider>
      </MemoryRouter>,
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return { getLiveRun: () => getter() };
  }

  it('accumule thinking, answer, outils et étapes pendant un run', async () => {
    const { getLiveRun } = await renderLive();

    fire('run_started', { request_id: 'run-live', run_id: 'run-live', kind: 'chat', conversation_id: 'conv-a' });
    fire('thinking', { request_id: 'run-live', text: 'orch-it1', iteration: 1 });
    fire('thinking', { request_id: 'run-live', text: 'gen-think', iteration: 99 });
    fire('answer', { request_id: 'run-live', text: 'Hello' });
    fire('answer', { request_id: 'run-live', text: ' world' });
    fire('tool_call', { request_id: 'run-live', tool_call_id: 'tc1', tool: 'find', arguments: { q: 'x' } });
    fire('tool_result', { request_id: 'run-live', tool_call_id: 'tc1', result: 'ok-res', ok: true, latency_ms: 42 });
    fire('run_stage', { request_id: 'run-live', run_id: 'run-live', stage: 'search', status: 'done' });

    const snap = getLiveRun();
    expect(snap).not.toBeNull();
    expect(snap!.thinking).toBe('gen-think');
    expect(snap!.orchestratorThinking).toBe('orch-it1');
    expect(snap!.answer).toBe('Hello world');
    expect(snap!.toolCalls).toHaveLength(1);
    expect(snap!.toolCalls[0].status).toBe('completed');
    expect(snap!.stages).toHaveLength(1);
    expect(snap!.conversationId).toBe('conv-a');
  });

  it('ignore les événements d’un autre run', async () => {
    const { getLiveRun } = await renderLive();

    fire('run_started', { request_id: 'run-live', run_id: 'run-live', kind: 'chat', conversation_id: 'conv-a' });
    fire('answer', { request_id: 'run-live', text: 'first' });

    fire('answer', { request_id: 'other-run', text: 'second' });

    const snap = getLiveRun();
    expect(snap!.answer).toBe('first');
  });

  it('marque terminal sans effacer, puis efface après le délai', async () => {
    vi.useFakeTimers();
    try {
      const { getLiveRun } = await renderLive();

      fire('run_started', { request_id: 'run-live', run_id: 'run-live', kind: 'chat', conversation_id: 'conv-a' });
      fire('run_completed', { request_id: 'run-live', run_id: 'run-live' });

      const snap = getLiveRun();
      expect(snap).not.toBeNull();
      expect(snap!.terminal).toBe(true);

      act(() => { vi.advanceTimersByTime(60_000); });
      expect(getLiveRun()).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('un nouveau run remplace l’ancien snapshot', async () => {
    const { getLiveRun } = await renderLive();

    fire('run_started', { request_id: 'run-one', run_id: 'run-one', kind: 'chat', conversation_id: 'conv-a' });
    fire('answer', { request_id: 'run-one', text: 'stale' });
    fire('run_completed', { request_id: 'run-one', run_id: 'run-one' });

    fire('run_started', { request_id: 'run-two', run_id: 'run-two', kind: 'chat', conversation_id: 'conv-b' });

    const snap = getLiveRun();
    expect(snap).not.toBeNull();
    expect(snap!.requestId).toBe('run-two');
    expect(snap!.answer).toBe('');
  });
});

});

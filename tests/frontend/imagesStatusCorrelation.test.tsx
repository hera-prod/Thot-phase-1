import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Régression 25/09 : l'UI restait sur « Interrogation… » parce que le diagnostic était
// demandé AVANT que l'écouteur `image_status` soit actif (réponse perdue), et parce que
// la réponse n'était corrélée à aucun request_id (une relance de l'opt-in ou une autre
// page écrasait l'état affiché avec une valeur périmée).
// Ces tests verrouillent les trois liens : écouteur actif d'abord, request_id
// corrélé, réponses dépassées ignorées.

type Listener = (event: { payload: Record<string, unknown> }) => void;
type StatusRequest = { requestId: string; resolve: () => void; reject: (error: unknown) => void };

const listeners: Record<string, Listener> = {};
const timeline: string[] = [];
const statusRequests: StatusRequest[] = [];

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_set_backend') return Promise.resolve(true);
    if (cmd === 'image_status') {
      timeline.push('invoke:image_status');
      const requestId = String((args as { requestId: string }).requestId);
      return new Promise<void>((resolve, reject) => {
        statusRequests.push({ requestId, resolve: () => resolve(), reject });
      });
    }
    return Promise.resolve(undefined);
  }),
  // Un écouteur n'est actif qu'après l'accusé du backend : asynchrone, jamais immédiat.
  listen: vi.fn((event: string, callback: Listener) => new Promise<() => void>((resolve) => {
    setTimeout(() => {
      listeners[event] = callback;
      timeline.push(`listen:${event}`);
      resolve(() => {});
    }, 0);
  }))
}));

import { ImagesPage } from '@/pages/images/ImagesPage';

function fire(event: string, payload: Record<string, unknown>) {
  const callback = listeners[event];
  if (!callback) throw new Error(`Aucun listener pour ${event}`);
  act(() => callback({ payload }));
}

function statusEvent(requestId: string, overrides: Record<string, unknown> = {}) {
  return { request_id: requestId, available: true, enabled: true, reason: '', ...overrides };
}

async function renderPage(): Promise<StatusRequest> {
  render(<ImagesPage />);
  await waitFor(() => expect(statusRequests).toHaveLength(1));
  return statusRequests[0];
}

beforeEach(() => {
  timeline.length = 0;
  statusRequests.length = 0;
  Object.keys(listeners).forEach((key) => delete listeners[key]);
});

afterEach(() => {
  cleanup();
});

describe('Images — corrélation du diagnostic moteur', () => {
  it('n’interroge le moteur qu’une fois l’écouteur image_status actif', async () => {
    await renderPage();
    const firstListen = timeline.indexOf('listen:image_status');
    const firstInvoke = timeline.indexOf('invoke:image_status');
    expect(firstListen).toBeGreaterThanOrEqual(0);
    expect(firstInvoke).toBeGreaterThan(firstListen);
    // Aucune requête avant le premier écouteur : sinon la réponse part dans le vide.
    expect(timeline[0]).toMatch(/^listen:/);
  });

  it('applique la réponse du request_id courant et ignore celle d’une autre page', async () => {
    const request = await renderPage();
    fire('image_status', statusEvent('status-d-autre-page'));
    expect(screen.getByText('Interrogation')).toBeTruthy();
    fire('image_status', statusEvent(request.requestId));
    expect(screen.getByText('Disponible')).toBeTruthy();
  });

  it('ignore une réponse périmée après une relance de l’opt-in', async () => {
    const first = await renderPage();
    fire('image_status', statusEvent(first.requestId));
    fireEvent.click(screen.getByRole('button', { name: 'Désactiver le moteur image' }));
    await waitFor(() => expect(statusRequests).toHaveLength(2));
    const second = statusRequests[1];
    expect(second.requestId).not.toBe(first.requestId);

    fire('image_status', statusEvent(first.requestId, { available: false, reason: 'Diagnostic périmé' }));
    expect(screen.queryByText('Diagnostic périmé')).toBeNull();
    expect(screen.getByText('Disponible')).toBeTruthy();

    fire('image_status', statusEvent(second.requestId, { available: false, reason: 'Diagnostic courant' }));
    await waitFor(() => expect(screen.getByText('Diagnostic courant')).toBeTruthy());
    expect(screen.getByText('Indisponible')).toBeTruthy();
  });

  it('ignore l’échec d’une interrogation dépassée et signale celui de la courante', async () => {
    const first = await renderPage();
    fire('image_status', statusEvent(first.requestId));
    fireEvent.click(screen.getByRole('button', { name: 'Désactiver le moteur image' }));
    await waitFor(() => expect(statusRequests).toHaveLength(2));

    await act(async () => { first.reject(new Error('sidecar injoignable')); });
    expect(screen.queryAllByRole('alert')).toHaveLength(0);

    await act(async () => { statusRequests[1].reject(new Error('sidecar injoignable')); });
    await waitFor(() => expect(screen.getAllByRole('alert')[0].textContent).toContain('Diagnostic moteur indisponible'));
  });
});

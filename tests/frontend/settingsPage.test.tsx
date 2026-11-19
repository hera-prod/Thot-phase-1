import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Ces tests verrouillent le contrat `web.settings` (backend/inference/web_settings.py)
// des deux côtés : les clés du payload sont écrites ici comme le sidecar les produit,
// parce qu'une clé orthographiée autrement côté interface ne se voit pas — elle
// affiche un réglage faux, silencieusement. Les fixtures ci-dessous sont donc le
// miroir du dictionnaire rendu, pas une écriture de fortune.

type Listener = (event: { payload: Record<string, unknown> }) => void;

const listeners: Record<string, Listener> = {};
const timeline: string[] = [];
const invocations: Array<{ cmd: string; args: Record<string, unknown> }> = [];
vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    timeline.push(`invoke:${cmd}`);
    invocations.push({ cmd, args: (args ?? {}) as Record<string, unknown> });
    // La page interroge aussi les dépôts, et un service réel RÉPOND. Sans cette
    // réponse, la section afficherait un second `role="alert"` et les assertions
    // qui comptent les alertes de l'écran web deviendraient fausses pour une
    // raison étrangère à leur sujet.
    if (cmd === 'espace_depot_verifier') {
      return Promise.resolve({
        ok: true, sain: true, lecture_complete: true, diagnostics: [],
        total: 0, reparable: 0, refuse: 0, espaces: [], mesure: {}, lectures: 5,
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

import { SettingsPage } from '@/pages/settings/SettingsPage';
import { MODEL_CATALOG } from '@/features/models/modelCatalog';
const OPTION_AUTO = {
  name: 'auto',
  label: 'Automatique — encyclopédie puis articles scientifiques',
  covers: [],
  coverage: "laisse le registre choisir dans son ordre de préférence",
  configured: true,
  requires: '',
  selected: true,
};

const OPTION_SEARXNG = {
  name: 'searxng',
  label: 'SearXNG (auto-hébergé) — web général',
  covers: ['web general'],
  coverage: 'web général, y compris hors encyclopédie',
  configured: false,
  requires: 'une instance SearXNG qui répond sur une adresse locale',
  selected: false,
};

const OPTION_WIKIPEDIA = {
  name: 'wikipedia',
  label: 'Wikipédia — encyclopédie générale',
  covers: ['encyclopedie'],
  coverage: 'encyclopédie générale uniquement',
  configured: true,
  requires: '',
  selected: false,
};

/** La charge utile du contrat, clé pour clé. */
function settingsPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    request_id: '',
    enabled: true,
    effective_enabled: true,
    provider: 'auto',
    provider_options: [{ ...OPTION_AUTO }],
    searxng_url: '',
    source: 'defaut',
    mode: 'web_allowed',
    writable: true,
    written: true,
    error: '',
    ...overrides,
  };
}

function fireSettings(payload: Record<string, unknown>) {
  const callback = listeners.web_settings;
  if (!callback) throw new Error('Aucun listener pour web_settings');
  act(() => callback({ payload }));
}

async function renderPage(): Promise<string> {
  render(<SettingsPage />);
  await waitFor(() => expect(invocations.some((entry) => entry.cmd === 'web_settings_request')).toBe(true));
  return String(invocations.find((entry) => entry.cmd === 'web_settings_request')?.args.requestId ?? '');
}

function webSwitch(): HTMLButtonElement {
  return screen.getByRole('switch', { name: 'Autoriser la recherche web' }) as HTMLButtonElement;
}

function providerSelect(): HTMLSelectElement {
  return screen.getByLabelText('Moteur de recherche') as HTMLSelectElement;
}

beforeEach(() => {
  timeline.length = 0;
  invocations.length = 0;
  Object.keys(listeners).forEach((key) => delete listeners[key]);
});

afterEach(() => {
  cleanup();
});

describe('Réglages — recherche web', () => {
  it('interroge le moteur après avoir branché l’écouteur, et ignore une réponse d’un autre request_id', async () => {
    const requestId = await renderPage();
    // L'ordre s'affirme SUR LE BLOC WEB, pas sur la page entière : les Réglages
    // interrogent d'autres services (les dépôts), et leur ordre ne dit rien de
    // celui du bloc web — qu'affirmer avant l'arrivée du second.
    const ordre = timeline.filter((evenement) => evenement.includes('web_settings'));
    expect(ordre[0]).toMatch(/^listen:/);
    expect(ordre.indexOf('invoke:web_settings_request')).toBeGreaterThan(ordre.indexOf('listen:web_settings'));

    // Un request_id inconnu ne doit rien appliquer : l'écran reste en attente.
    fireSettings(settingsPayload({ request_id: 'settings-d-autre-page', enabled: true }));
    expect(screen.getByText('Interrogation du moteur…')).toBeTruthy();
    expect(webSwitch().getAttribute('aria-checked')).toBe('false');

    fireSettings(settingsPayload({ request_id: requestId, enabled: true }));
    expect(webSwitch().getAttribute('aria-checked')).toBe('true');
  });

  it('affiche l’état relu par le payload', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({
      request_id: requestId,
      enabled: true,
      effective_enabled: true,
      provider: 'wikipedia',
      provider_options: [
        { ...OPTION_AUTO, selected: false },
        { ...OPTION_WIKIPEDIA, selected: true },
      ],
      searxng_url: 'http://127.0.0.1:8080',
      source: 'fichier',
      mode: 'web_allowed',
    }));

    expect(webSwitch().getAttribute('aria-checked')).toBe('true');
    expect(providerSelect().value).toBe('wikipedia');
    expect(screen.getByText('Origine : fichier de réglages')).toBeTruthy();
    expect(screen.getByText('web_allowed')).toBeTruthy();
    expect(screen.getByText('En vigueur : le moteur peut consulter le web et citer ses sources.')).toBeTruthy();
    expect(screen.getByText('Le moteur a confirmé cet état.')).toBeTruthy();
  });

  it('mode fermé : interrupteur à faux, sélecteur visible mais inactif', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({
      request_id: requestId,
      enabled: false,
      effective_enabled: false,
      mode: 'WEB_DISABLED',
    }));

    expect(webSwitch().getAttribute('aria-checked')).toBe('false');
    expect(providerSelect()).toBeTruthy();
    expect(providerSelect().disabled).toBe(true);
    expect(screen.getByText('En vigueur : le moteur ne consulte pas le web.')).toBeTruthy();
    expect(screen.getByText(/seules les sources déjà indexées dans l’atelier seront citées/)).toBeTruthy();
  });

  it('lecture seule : l’environnement impose le mode et les cases sont désactivées', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({
      request_id: requestId,
      writable: false,
      source: 'env',
    }));

    expect(webSwitch().disabled).toBe(true);
    expect(providerSelect().disabled).toBe(true);
    expect(screen.getByText(/imposé par une variable d’environnement/)).toBeTruthy();
    expect(screen.getByText('Origine : variable d’environnement')).toBeTruthy();
  });

  it('une option non configurée affiche ce qu’elle exige', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({
      request_id: requestId,
      provider_options: [
        { ...OPTION_AUTO, selected: false },
        { ...OPTION_SEARXNG },
      ],
    }));

    expect(screen.getByText(`Nécessite : ${OPTION_SEARXNG.requires}`)).toBeTruthy();
    expect(screen.getByText('Non configuré')).toBeTruthy();
  });

  it('un error non vide s’affiche tel quel', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({ request_id: requestId, error: 'provider doit être un nom de moteur non vide' }));

    expect(screen.getByRole('alert').textContent).toBe('provider doit être un nom de moteur non vide');
  });

  it('un clic sur l’interrupteur écrit puis relit, sans état optimiste', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({ request_id: requestId, enabled: true }));
    invocations.length = 0;

    fireEvent.click(webSwitch());

    await waitFor(() => expect(invocations.map((entry) => entry.cmd)).toEqual(['web_settings_set', 'web_settings_request']));
    expect(invocations[0].args).toEqual({ enabled: false });
    // Tant que le sidecar n'a pas répondu, la case montre l'état relu, pas le voulu.
    expect(webSwitch().getAttribute('aria-checked')).toBe('true');
    expect(webSwitch().disabled).toBe(true);

    const secondRequest = String(invocations[1].args.requestId);
    expect(secondRequest).not.toBe(requestId);
    fireSettings(settingsPayload({
      request_id: secondRequest,
      enabled: false,
      effective_enabled: false,
      mode: 'WEB_DISABLED',
    }));
    expect(webSwitch().getAttribute('aria-checked')).toBe('false');
    expect(webSwitch().disabled).toBe(false);
  });

  it('written: false se voit', async () => {
    const requestId = await renderPage();
    fireSettings(settingsPayload({ request_id: requestId, written: false }));

    expect(screen.getByText('Le moteur n’a pas enregistré la dernière modification.')).toBeTruthy();
    expect(screen.queryByText('Le moteur a confirmé cet état.')).toBeNull();
  });

  it('le champ d’adresse ne lit que la clé searxng_url du contrat', async () => {
    const requestId = await renderPage();
    // Clé bien orthographiée : la valeur s'affiche.
    fireSettings(settingsPayload({
      request_id: requestId,
      provider: 'searxng',
      provider_options: [
        { ...OPTION_AUTO, selected: false },
        { ...OPTION_SEARXNG, selected: true, configured: true },
      ],
      searxng_url: 'http://127.0.0.1:8080',
      searxngUrl: 'http://faux.exemple:8080',
    }));
    const field = screen.getByLabelText('Adresse de l’instance SearXNG') as HTMLInputElement;
    expect(field.value).toBe('http://127.0.0.1:8080');

    // Même événement, clé mal orthographiée : le champ reste vide, il ne devine rien.
    fireSettings(settingsPayload({
      request_id: requestId,
      provider: 'searxng',
      provider_options: [
        { ...OPTION_AUTO, selected: false },
        { ...OPTION_SEARXNG, selected: true, configured: true },
      ],
      searxngUrl: 'http://faux.exemple:8080',
    }));
    expect(field.value).toBe('');
  });

  it('affiche les 7 modèles : names et role_labels visibles', async () => {
    await renderPage();
    await waitFor(() => {
      MODEL_CATALOG.forEach((m) => {
        expect(screen.getByText(m.name)).toBeTruthy();
        expect(screen.getByText(m.roleLabel)).toBeTruthy();
      });
    });
  });

  it('badges : Non commercial sur FLUX, Permissif sur PaddleOCR', async () => {
    await renderPage();
    await waitFor(() => expect(screen.getByText('Non commercial')).toBeTruthy());
    expect(screen.getAllByText('Permissif').length).toBeGreaterThan(0);
  });

  it('commercial_use_allowed false : une seule note is-warn', async () => {
    await renderPage();
    await waitFor(() => expect(document.querySelectorAll('.set-note.is-warn').length).toBe(1));
  });

  it('le titre Accès internet est rendu et l’interrupteur existe', async () => {
    await renderPage();
    expect(screen.getByRole('heading', { name: 'Accès internet' })).toBeTruthy();
    expect(screen.getByRole('switch', { name: 'Autoriser la recherche web' })).toBeTruthy();
  });
});

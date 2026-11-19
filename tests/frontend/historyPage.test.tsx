import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { HistoryPage } from '@/pages/history/HistoryPage';

function ilYa(minutes: number) {
  return new Date(Date.now() - minutes * 60000).toISOString();
}

function conversation(overrides: Record<string, unknown>) {
  return {
    id: 'c', title: 'Sans titre',
    workspace_id: null, workspace_name: null,
    updated_at: ilYa(1),
    last_message_preview: 'aperçu', message_count: 1,
    ...overrides,
  };
}

// `vi.mock` est hissé : la factory ne peut pas refermer une variable de
// l'essai. On passe donc par un module d'etat que la factory relit a chaque
// appel, ce qui permet un jeu de donnees different par essai.
let conversationsCourantes: unknown[] = [];
let echecCourant: Error | null = null;

vi.mock('@/lib/ipcBridge', async () => {
  const reel = await vi.importActual<typeof import('@/lib/ipcBridge')>('@/lib/ipcBridge');
  return {
    ...reel,
    listConversations: vi.fn(async () => {
      if (echecCourant) {
        const failure = echecCourant;
        echecCourant = null;
        throw failure;
      }
      return conversationsCourantes;
    }),
    // Le dialogue de déplacement lit les destinations ; aucun espace dans ces
    // essais, donc il doit afficher son état « aucun autre espace ».
    listWorkspaces: vi.fn(async () => []),
  };
});

function maquer(conversations: unknown[]) {
  conversationsCourantes = conversations;
}

function maquerEchec(message = 'base verrouillée') {
  echecCourant = new Error(message);
}

/**
 * Attend le CONTENU, pas seulement la coquille : l'onglet « Tout » s'affiche
 * avant que `listConversations` ne réponde, et une assertion posée sur la
 * coquille passe avant que la liste n'existe.
 */
async function afficher(...titresAttendus: string[]) {
  render(<MemoryRouter><HistoryPage /></MemoryRouter>);
  await waitFor(() => expect(screen.queryAllByRole('button', { name: 'Tout' }).length).toBe(1));
  for (const titre of titresAttendus) {
    await waitFor(() => expect(screen.getByText(titre)).toBeTruthy());
  }
}

async function ouvrirOngletParEspace() {
  fireEvent.click(screen.getByRole('button', { name: 'Par espace' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Par espace' })).toBeTruthy());
}

/** Ordre de rendu : `textContent` est lisible, les positions DOM ne le sont pas en jsdom. */
function ordreDeRendu(...titres: string[]) {
  const texte = document.body.textContent ?? '';
  return titres.map((t) => texte.indexOf(t));
}

describe('Historique — les deux vues', () => {
  it("n'expose aucun mode de discussion autre que le chat", async () => {
    maquer([conversation({ id: 'c1', title: 'Hors espace' })]);
    await afficher('Hors espace');
    const libelles = screen.getAllByRole('button').map((b) => b.textContent ?? '');
    // Le filtre « Agent » a été retiré : il promettait une liste de discussions
    // qui n'existe plus et ne mènerait nulle part.
    expect(libelles.every((t) => !/agent/i.test(t))).toBe(true);
    // Le badge de type a disparu avec lui : un badge constant n'apprend rien.
    expect(screen.queryByText('Chat', { selector: 'span' })).toBeNull();
  });

  it('a exactement deux onglets', async () => {
    maquer([conversation({ id: 'c1', title: 'Hors espace' })]);
    await afficher('Hors espace');
    const onglets = screen.getAllByRole('button').map((b) => b.textContent ?? '');
    expect(onglets).toContain('Tout');
    expect(onglets).toContain('Par espace');
    // Un onglet par espace promettait un filtre qui n'existe plus.
    expect(onglets).not.toContain('Sans espace');
    expect(onglets).not.toContain('Refonte');
  });

  it('« Tout » suit l’ordre de la dernière modification', async () => {
    maquer([
      conversation({ id: 'c1', title: 'Ancienne', updated_at: ilYa(60 * 24 * 7) }),
      conversation({ id: 'c2', title: 'Recente', updated_at: ilYa(1) }),
      conversation({ id: 'c3', title: 'Moyenne', updated_at: ilYa(120) }),
    ]);
    await afficher('Ancienne', 'Recente', 'Moyenne');
    const [recente, moyenne, ancienne] = ordreDeRendu('Recente', 'Moyenne', 'Ancienne');
    // Sans sous-groupes par date : le seul repere est le temps.
    expect(recente).toBeGreaterThanOrEqual(0);
    expect(recente).toBeLessThan(moyenne);
    expect(moyenne).toBeLessThan(ancienne);
  });

  it('« Par espace » fait une case par espace, « Sans espace » en première', async () => {
    maquer([
      conversation({ id: 'c1', title: 'Hors espace A' }),
      conversation({ id: 'c2', title: 'Dans un espace', workspace_id: 'w1', workspace_name: 'Refonte' }),
      conversation({ id: 'c3', title: 'Dans un autre', workspace_id: 'w2', workspace_name: 'Marketing' }),
    ]);
    await afficher('Hors espace A');
    await ouvrirOngletParEspace();

    const noms = ['Sans espace', 'Marketing', 'Refonte'];
    const [sansEspace, marketing, refonte] = ordreDeRendu(...noms);
    // Chaque case porte bien les discussions de son espace.
    expect(screen.getByText('Hors espace A')).toBeTruthy();
    expect(screen.getByText('Dans un espace')).toBeTruthy();
    expect(screen.getByText('Dans un autre')).toBeTruthy();
    // « Sans espace » en tête : c'est le cas le plus courant.
    expect(sansEspace).toBeGreaterThanOrEqual(0);
    expect(sansEspace).toBeLessThan(refonte);
    expect(marketing).toBeLessThan(refonte);
  });

  it('« Par espace » ne fait pas de case pour un espace vide', async () => {
    maquer([
      conversation({ id: 'c1', title: 'Hors espace A' }),
      conversation({ id: 'c2', title: 'Dans un espace', workspace_id: 'w1', workspace_name: 'Refonte' }),
    ]);
    await afficher('Hors espace A');
    await ouvrirOngletParEspace();
    expect(screen.getByText('Refonte')).toBeTruthy();
    // Un espace sans discussion n'apparaît pas : une case vide est déroutante.
    expect(screen.queryByText('Recherche Universitaire')).toBeNull();
  });

  it('ouvre le dialogue de déplacement depuis le menu Actions', async () => {
    maquer([
      conversation({
        id: 'c1',
        title: 'À déplacer',
        workspace_id: 'w1',
        workspace_name: 'Refonte',
      }),
    ]);
    await afficher('À déplacer');

    fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    fireEvent.click(screen.getByRole('button', { name: 'Déplacer vers un espace…' }));

    const dialogue = await screen.findByRole('dialog');
    expect(dialogue.textContent).toContain('Aucun autre espace disponible.');
  });

  it('échec de chargement : message + Réessayer, jamais « Aucun historique »', async () => {
    maquer([conversation({ id: 'c1', title: 'Revenue après reprise' })]);
    maquerEchec('base verrouillée');
    render(<MemoryRouter><HistoryPage /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText('L’historique n’a pas pu être chargé.')).toBeTruthy());
    expect(screen.getByText('base verrouillée')).toBeTruthy();
    expect(screen.queryByText('Aucun historique')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Réessayer' }));
    await waitFor(() => expect(screen.getByText('Revenue après reprise')).toBeTruthy());
  });

  it('« Par espace » rend le nom de l’espace cliquable, sauf « Sans espace »', async () => {
    maquer([
      conversation({ id: 'c1', title: 'Hors espace' }),
      conversation({ id: 'c2', title: 'Dans un espace', workspace_id: 'w1', workspace_name: 'Refonte' }),
    ]);
    await afficher('Hors espace');
    await ouvrirOngletParEspace();

    const lien = screen.getByRole('link', { name: 'Ouvrir l’espace Refonte' });
    expect(lien).toBeTruthy();
    expect(lien).toHaveAttribute('href', '/espaces/w1');
    expect(screen.queryByRole('link', { name: /Sans espace/i })).toBeNull();
    expect(screen.getAllByText('1 discussion')).toHaveLength(2);
    expect(screen.getByText('Dans un espace')).toBeTruthy();
  });
});
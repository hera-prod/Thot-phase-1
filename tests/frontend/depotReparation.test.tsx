// Thot Phase 1 — La section Dépôts des Réglages.
//
// Ces tests verrouillent une seule chose, mais c'est celle qui coûte le plus cher si
// elle se brise : ce que l'écran AFFIRME quand il n'a pas su regarder.
//
// Un dépôt sain, un dépôt abîmé, une base à moitié lisible et un appel qui échoue
// donnent quatre écrans différents. Le quatrième — « état inconnu » — n'existerait
// dans aucun test écrit à la va-vite, alors qu'il est le seul état honnête quand le
// service ne répond pas. Afficher « tout va bien » sur un dépôt qu'on n'a pas vérifié
// ferait vider un dépôt intact pour rien.
//
// Le dialogue est vérifié sur sa PROMESSE : il ne doit jamais dire qu'une action est
// irréversible. La réparation ne supprime rien, et l'apprendre ici — plutôt que dans
// une phrase de test que personne ne relit — est la seule chose qui empêche une
// future reprise de copie d'importer le vocabulaire de la suppression.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const reponses: Record<string, unknown> = {};
const invokes: Array<{ cmd: string; args: Record<string, unknown> }> = [];

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    invokes.push({ cmd, args: (args ?? {}) as Record<string, unknown> });
    const reponse = reponses[cmd];
    if (reponse instanceof Error) return Promise.reject(reponse);
    return Promise.resolve(reponse);
  }),
  listen: vi.fn(() => Promise.resolve(() => {}))
}));

import { DepotIntegritySection } from '@/components/settings/DepotIntegritySection';

const SAIN = {
  ok: true,
  sain: true,
  lecture_complete: true,
  diagnostics: [],
  total: 0,
  reparable: 0,
  refuse: 0,
  espaces: [],
  mesure: {},
  lectures: 5,
};

const ANOMALIES = {
  ok: true,
  sain: false,
  lecture_complete: true,
  diagnostics: [],
  total: 3,
  reparable: 3,
  refuse: 0,
  espaces: [
    {
      workspace_id: 'ws_1',
      nom: 'Refonte du site',
      nom_connu: true,
      par_type: { index_manquant: 2 },
      refuses_par_type: {},
      total: 2,
      reparable: 2,
      refuse: 0,
      exemples: [
        { type: 'index_manquant', display_name: 'tardif-a.pdf', raison: 'aucune fiche' },
      ],
    },
    {
      workspace_id: 'ws_2',
      nom: 'Stratégie marketing',
      nom_connu: true,
      par_type: { index_manquant: 1 },
      refuses_par_type: {},
      total: 1,
      reparable: 1,
      refuse: 0,
      exemples: [
        { type: 'index_manquant', display_name: 'tardif-b.pdf', raison: 'aucune fiche' },
      ],
    },
  ],
  mesure: {},
  lectures: 6,
};

const LECTURE_INCOMPLETE = {
  ...SAIN,
  sain: false,
  lecture_complete: false,
  total: 0,
  diagnostics: ['blobs illisible (OperationalError) : cette partie n’est pas vérifiée'],
};

const REPARATION = {
  ok: true,
  avant: ANOMALIES,
  apres: { ...SAIN, sain: true },
  repares: { index_manquant: 3, nom_vide: 0 },
  non_repares: [],
};

function arme(cmd: string, reponse: unknown) {
  reponses[cmd] = reponse;
}

// `DepotIntegritySection` contient un `<Link>` SPA : il exige un routeur, même
// quand la navigation n'est pas exercée.
function rendre() {
  return render(
    <MemoryRouter>
      <DepotIntegritySection />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  Object.keys(reponses).forEach((cle) => delete reponses[cle]);
  invokes.length = 0;
  arme('espace_depot_verifier', SAIN);
});

afterEach(cleanup);

describe('Dépôts — ce que l’écran affirme', () => {
  it('un dépôt sain ne montre aucun bouton de réparation', async () => {
    rendre();
    await waitFor(() => expect(screen.getByText(/Tous vos dépôts sont exploitables/)).toBeTruthy());

    expect(screen.queryByRole('button', { name: /Réparer/ })).toBeNull();
    expect(screen.queryByText(/anomalie/)).toBeNull();
  });

  it('après démontage, ne rappelle plus le service et ne laisse aucun minuteur', async () => {
    const espion = vi.spyOn(globalThis, 'setInterval');
    arme('espace_depot_verifier', ANOMALIES);
    const { unmount } = rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });

    const avant = invokes.filter((appel) => appel.cmd === 'espace_depot_verifier').length;
    unmount();
    await new Promise((suite) => setTimeout(suite, 40));

    expect(
      invokes.filter((appel) => appel.cmd === 'espace_depot_verifier').length,
    ).toBe(avant);
    // Aucun minuteur de 60 s ne doit survivre au démontage — et l'écran des
    // dépôts n'en pose aucun, contrairement au repli de l'onglet Dépôt.
    expect(espion.mock.calls.some((appel) => Number(appel[1]) === 60_000)).toBe(false);
    espion.mockRestore();
  });

  it('des anomalies montrent le bouton et le détail par espace', async () => {
    arme('espace_depot_verifier', ANOMALIES);
    rendre();

    const bouton = await screen.findByRole('button', { name: /Réparer les dépôts/ });
    expect(bouton).toBeTruthy();
    expect(screen.getByText('Refonte du site')).toBeTruthy();
    expect(screen.getByText('Stratégie marketing')).toBeTruthy();
    // Le compte est celui du backend, pas recompté par l'écran.
    expect(screen.getByText(/3 anomalies dans 2 espaces/)).toBeTruthy();
  });

  it('un appel qui échoue affiche « inconnu », JAMAIS « sain »', async () => {
    // Le test central du fichier. Le service est mort : dire « tout va bien » ferait
    // croire à un dépôt sain, et l'utilisateur n'aurait plus de bouton pour réparer
    // un dépôt réellement abîmé.
    arme('espace_depot_verifier', new Error('sidecar injoignable'));
    rendre();

    await waitFor(() =>
      expect(screen.getByText(/Le moteur de l’atelier ne répond pas/)).toBeTruthy());
    expect(screen.queryByText(/Tous vos dépôts sont exploitables/)).toBeNull();
    expect(screen.queryByRole('button', { name: /Réparer/ })).toBeNull();

    // Le détail d'ingénieur reste dans la console. L'écran ne montre ni le nom du
    // canal IPC ni « unknown_method » : ce sont des noms que l'utilisateur ne peut
    // pas agir sur, et les afficher le laisse devant un mur au lieu d'une action.
    const alert = screen.getByRole('alert').textContent ?? '';
    expect(alert).not.toContain('thot:cmd');
    expect(alert).not.toContain('Unknown');
    expect(alert).not.toContain('unknown_method');
    // Et l'action, elle, est dite.
    expect(alert).toContain('Rouvrez l’application');
  });

  it('une lecture incomplète ne se lit pas comme « zéro anomalie »', async () => {
    // Base à moitié lisible : zéro anomalie COMME FAIT, mais pas une bonne nouvelle.
    arme('espace_depot_verifier', LECTURE_INCOMPLETE);
    rendre();

    await waitFor(() => expect(screen.getByText(/Vérification incomplète/)).toBeTruthy());
    expect(screen.getByText(/blobs illisible/)).toBeTruthy();
    expect(screen.queryByText(/Tous vos dépôts sont exploitables/)).toBeNull();
  });
});

describe('Dépôts — « à réessayer » n’est pas « réparable »', () => {
  it('affiche les échecs relançables séparément et mène au dépôt de l’espace', async () => {
    arme('espace_depot_verifier', {
      ...ANOMALIES,
      total: 2,
      reparable: 1,
      a_reessayer: 1,
      refuse: 0,
      espaces: [
        {
          ...ANOMALIES.espaces[0],
          total: 2,
          reparable: 1,
          a_reessayer: 1,
          refuse: 0,
          par_type: { index_manquant: 1, extraction_absente: 1 },
          refuses_par_type: {},
          exemples: [
            { type: 'index_manquant', display_name: 'tardif-a.pdf', raison: 'aucune fiche', ingest_state: 'extrait' },
            { type: 'extraction_absente', display_name: 'casse.pdf', raison: 'lecteur cassé', ingest_state: 'echec' },
          ],
        },
      ],
    });
    rendre();

    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    expect(screen.getByText(/1 à réessayer/)).toBeTruthy();
    const lien = screen.getByRole('link', { name: /Ouvrir le dépôt/ });
    expect(lien.getAttribute('href')).toBe('/espaces/ws_1/depot');

    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    const dialogue = await screen.findByRole('dialog');
    expect(dialogue.textContent).toContain('À réessayer depuis son dépôt');
    // La réparation ne doit jamais se présenter comme une relance implicite.
    expect(dialogue.textContent).toContain('ne font pas partie de la réparation');
    expect(dialogue.textContent).not.toContain('relancera');

    // « Sera réparé » ne cite QUE les types réellement réparés. `casse.pdf` est un
    // échec d'extraction : le promettre dans la réparation serait un mensonge.
    const sections = Array.from(
      dialogue.querySelectorAll('.set-modal-sous-titre'),
    ) as HTMLElement[];
    const contenuDe = (titre: string) => {
      const entete = sections.find((element) => element.textContent === titre);
      expect(entete).toBeTruthy();
      return entete?.nextElementSibling?.textContent ?? '';
    };
    expect(contenuDe('Sera réparé')).toContain('tardif-a.pdf');
    expect(contenuDe('Sera réparé')).not.toContain('casse.pdf');
    expect(contenuDe('À réessayer depuis son dépôt')).toContain('casse.pdf');
  });
});

describe('Dépôts — le dialogue ne promet pas ce qu’il ne fait pas', () => {
  it('il ne dit jamais qu’une action est irréversible', async () => {
    // Garde-fou de copie. Le modèle mechanical du dialogue vient du dialogue de
    // SUPPRESSION de la galerie : si quelqu’un en recopie le texte, cette phrase
    // devient fausse — la réparation ne supprime rien — et elle effraierait pour rien.
    arme('espace_depot_verifier', ANOMALIES);
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));

    const dialogue = await screen.findByRole('dialog');
    const texte = dialogue.textContent ?? '';
    for (const interdit of ['irréversible', 'Suppression', 'supprimer vos', 'définitivement']) {
      expect(texte.toLowerCase()).not.toContain(interdit.toLowerCase());
    }
    expect(texte).toContain('Aucun fichier n’est supprimé');
    expect(texte).toContain('pas indexé');
  });

  it('il est un dialogue, pas une alerte', async () => {
    // `alertdialog` ferait sur-appréhender une opération qui répare.
    arme('espace_depot_verifier', ANOMALIES);
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });


  it('beaucoup d’espaces : le dialogue plafonne SA(total) ET le dit', async () => {
    // Le plafond était PAR ESPACE : 40 dépôts abîmés alignaient 160 lignes, et la
    // phrase « aucun fichier n'est supprimé » passait sous la liste. Un test à deux
    // espaces ne voit jamais ça — d'où un test à l'échelle, même s'il est long à lire.
    const beaucoup = Array.from({ length: 40 }, (_, index) => ({
      workspace_id: `ws_${index}`,
      nom: `Espace ${index}`,
      nom_connu: true,
      par_type: { index_manquant: 1 },
      refuses_par_type: {},
      total: 1,
      reparable: 1,
      refuse: 0,
      exemples: [
        { type: 'index_manquant', display_name: `tardif-${index}.pdf`, raison: 'aucune fiche' },
      ],
    }));
    arme('espace_depot_verifier', {
      ...ANOMALIES, total: 40, reparable: 40, refuse: 0, espaces: beaucoup,
    });
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));

    const dialogue = await screen.findByRole('dialog');
    const lignes = dialogue.querySelectorAll('.set-depot-ligne');
    expect(lignes.length).toBeLessThanOrEqual(10);
    // Le reste est DIT : sinon l'utilisateur croit que la liste est tout ce qui va
    // être réparé, et 30 documents partiraient sans qu'il l'ait vu.
    expect(dialogue.textContent).toContain('40 documents au total');
    expect(dialogue.textContent).toContain('tous seront réparés');
    expect(dialogue.textContent).toContain('Aucun fichier n’est supprimé');
  });

  it('il dit ce qui ne sera PAS touché quand il y a des refus', async () => {
    // Un dépôt à moitié réparable ne doit pas laisser croire que tout sera réparé.
    arme('espace_depot_verifier', {
      ...ANOMALIES,
      total: 4,
      reparable: 2,
      refuse: 2,
      espaces: [
        { ...ANOMALIES.espaces[0], total: 2, reparable: 2 },
        {
          ...ANOMALIES.espaces[1], total: 2, reparable: 0, refuse: 2,
          par_type: { fichier_absent: 2 },
          refuses_par_type: { fichier_absent: 2 },
        },
      ],
    });
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));

    const dialogue = await screen.findByRole('dialog');
    expect(dialogue.textContent).toContain('Ne sera PAS touché');
    expect(dialogue.textContent).toContain('fichier disparu du disque');
  });

  it('Annuler ne déclenche aucune écriture', async () => {
    arme('espace_depot_verifier', ANOMALIES);
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));

    fireEvent.click(await screen.findByRole('button', { name: /Annuler/ }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(invokes.some((i) => i.cmd === 'espace_depot_reparer')).toBe(false);
  });
});

describe('Dépôts — la réparation confirmée', () => {
  it('confirme, répare, puis annonce le résultat', async () => {
    arme('espace_depot_verifier', ANOMALIES);
    reponses['espace_depot_reparer'] = REPARATION;
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Réparer$/ }));

    await waitFor(() =>
      expect(screen.getByText(/Dépôts réparés/)).toBeTruthy());
    expect(screen.getByText(/à nouveau trouvable par l’orchestrateur/)).toBeTruthy();
  });

  it('envoie une confirmation VRAIE, et seulement après confirmation', async () => {
    arme('espace_depot_verifier', ANOMALIES);
    reponses['espace_depot_reparer'] = REPARATION;
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Réparer$/ }));

    await waitFor(() => {
      const envoi = invokes.find((i) => i.cmd === 'espace_depot_reparer');
      expect(envoi).toBeTruthy();
      // `true` strict : le backend refuse tout le reste, et il a raison.
      expect(envoi?.args.confirme).toBe(true);
    });
  });

  it('des refus qui subsistent sont annoncés, pasriangleés', async () => {
    // Si la réparation laisse des anomalies non réparables, l'écran doit le dire.
    // Un « tout est réparé » ici serait le mensonge le plus coûteux de l'écran.
    arme('espace_depot_verifier', ANOMALIES);
    reponses['espace_depot_reparer'] = {
      ...REPARATION,
      apres: { ...SAIN, sain: false, total: 1, refuse: 1 },
      non_repares: [{ type: 'fichier_absent', artifact_id: 'art_1' }],
    };
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Réparer$/ }));

    await waitFor(() =>
      expect(screen.getByText(/subsistent et n’ont pas été touchées/)).toBeTruthy());
    expect(screen.queryByText(/Dépôts réparés/)).toBeNull();
  });

  it('un échec d’extraction non réparé est dit relançable, pas « octets disparus »', async () => {
    arme('espace_depot_verifier', ANOMALIES);
    reponses['espace_depot_reparer'] = {
      ...REPARATION,
      apres: { ...SAIN, sain: false, total: 1, refuse: 1 },
      non_repares: [
        { type: 'extraction_absente', ingest_state: 'echec', artifact_id: 'art_1' },
      ],
    };
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Réparer$/ }));

    const bilan = await screen.findByText(/subsistent et n’ont pas été touchées/);
    const texte = bilan.textContent ?? '';
    expect(texte).toContain('se relance');
    // La phrase sur les octets disparus ne décrit pas un échec d'extraction.
    expect(texte).not.toContain('octets disparus');
  });

  it('une réparation refusée laisse l’écran dans son état précédent', async () => {
    // Le canal peut refuser (confirmation absente, base verrouillée). L'écran ne doit
    // pas annoncer un succès : il doit le dire et rester sur les anomalies.
    arme('espace_depot_verifier', ANOMALIES);
    reponses['espace_depot_reparer'] = new Error('[CONFIRMATION_REQUIRED] non confirmée');
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Réparer$/ }));

    // Le marqueur technique reste dans la console ; l'écran dit ce qui s'est passé
    // et ce qui n'a pas bougé — sans exposer `[CONFIRMATION_REQUIRED]` à l'écran.
    await waitFor(() =>
      expect(screen.getByText(/La réparation n’a pas été confirmée/)).toBeTruthy());
    expect(screen.getByRole('alert').textContent).toContain('Aucun dépôt n’a été modifié');
    expect(screen.queryByText(/Dépôts réparés/)).toBeNull();
  });
});

describe('Dépôts — relance après échec et délai', () => {
  it('Réessayer après un échec de vérification relance et passe en sain', async () => {
    arme('espace_depot_verifier', new Error('sidecar injoignable'));
    rendre();
    await waitFor(() => expect(screen.getByText(/Le moteur de l’atelier ne répond pas/)).toBeTruthy());
    expect(screen.getByRole('button', { name: /Réessayer/ })).toBeTruthy();

    const avant = invokes.filter((appel) => appel.cmd === 'espace_depot_verifier').length;
    arme('espace_depot_verifier', SAIN);
    fireEvent.click(screen.getByRole('button', { name: /Réessayer/ }));

    await waitFor(() => expect(screen.getByText(/Tous vos dépôts sont exploitables/)).toBeTruthy());
    expect(screen.queryByRole('button', { name: /Réessayer/ })).toBeNull();
    expect(invokes.filter((appel) => appel.cmd === 'espace_depot_verifier').length).toBe(avant + 1);
    expect(invokes.filter((appel) => appel.cmd === 'espace_depot_verifier').length).toBe(2);
  });

  it('délai dépassé : état inconnu avec message de délai et bouton Réessayer', async () => {
    vi.useFakeTimers();
    try {
      arme('espace_depot_verifier', new Promise(() => {}));
      rendre();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(12_000);
      });
      vi.useRealTimers();
      const alert = await screen.findByRole('alert');
      expect((alert.textContent ?? '')).toContain('Le moteur n’a pas répondu dans le délai imparti');
      expect(screen.getByRole('button', { name: /Réessayer/ })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('échec de réparation : modale disparaît, alerte visible, anomalies conservées', async () => {
    arme('espace_depot_verifier', ANOMALIES);
    reponses['espace_depot_reparer'] = new Error('panne moteur');
    rendre();
    await screen.findByRole('button', { name: /Réparer les dépôts/ });
    fireEvent.click(screen.getByRole('button', { name: /Réparer les dépôts/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Réparer$/ }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.queryByText(/Dépôts réparés/)).toBeNull();
    expect(screen.getByRole('button', { name: /Réparer les dépôts/ })).toBeTruthy();
    expect(screen.getByText(/3 anomalies dans 2 espaces/)).toBeTruthy();
  });
});
// Tests des pages Espaces — ce que l'écran AFFIRME, pas seulement ce qu'il rend.
//
// Le motif de fond : les maquettes 05-1 et 05-3 contiennent trois affirmations
// que le contrat refuse (un statut « Active », un plafond « / 10 Go », une
// arborescence avec un « Chemin » copiable). Un simple test « la page rend »
// passerait avec les trois. Ces tests vérifient donc que l'écran ne les
// affirme pas, et que les compteurs qu'il affiche sont ceux du service.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const listWorkspaces = vi.fn();
const createWorkspace = vi.fn();
const getWorkspace = vi.fn();
const listWorkspaceConversations = vi.fn();
const showToast = vi.fn();

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener[]> = {};

function fire(event: string, payload: Record<string, unknown>): void {
  const handlers = listeners[event] ?? [];
  if (handlers.length === 0) throw new Error(`Aucun listener pour ${event}`);
  act(() => { handlers.forEach((handler) => { handler({ payload }); }); });
}

// Le dépôt passe par le VRAI pont : on observe `invoke` pour prouver que
// l'écran appelle les bons verbes avec les bons arguments, plutôt que de
// substituer des fonctions à la surface et de ne tester que l'écran.
const invokes: Array<{ cmd: string; args: Record<string, unknown> }> = [];
let reponseDepot: DepotReponse;
/** Une promesse qui ne résout que si le test le demande : le retrait reste en vol. */
let retraitEnVol: Promise<unknown> | null = null;
/** Réponse de `artifact_ingestion_retry`, configurable par test. */
let reponseReessai: {
  ok: boolean;
  accepted: boolean;
  artifact_id: string;
  state: string;
  reason?: string | null;
} = { ok: true, accepted: true, artifact_id: "art_1", state: "echec" };

type FicheMemoire = {
  entry_id: string;
  content: string;
  status?: string;
  category?: string | null;
  version?: number;
  origin?: string | null;
  origin_context?: string | null;
  created_at?: string | null;
  sources_count?: number;
  affirmation_status?: string | null;
  freshness?: string | null;
};

type SyntheseMemoire = { content: string; version: number; created_at?: string | null };

type EntretienMemoire = {
  tours: number;
  seuil_tours: number;
  inactivite_s: number | null;
  seuil_inactivite_s: number;
  etat: "en_cours" | "differe" | "en_attente" | "a_jour" | "inconnu";
  raison: string | null;
};

type ReponseMemoire = {
  elements: FicheMemoire[];
  synthese: SyntheseMemoire | null;
  entretien?: EntretienMemoire | null;
};

type RafraichissementMock = {
  statut: string;
  raison?: string;
  fiches?: { crees?: number; mis_a_jour?: number };
  synthese?: { statut?: string };
};

/** Ce que rend `espace_memoire`, et ce que la page relit après chaque mutation. */
let memoireReponse: ReponseMemoire = { elements: [], synthese: null };
/** Ce que rend `espace_memoire_forcer`, configurable par test. */
let reponseForcer: ReponseMemoire & { rafraichissement: RafraichissementMock } = {
  elements: [],
  synthese: null,
  rafraichissement: { statut: "rien_a_promouvoir" },
};
/** Une promesse qui ne résout que si le test le demande : l'entretien reste en vol. */
let forcerEnVol: Promise<unknown> | null = null;
/** Résultat renvoyé par `espace_memoire_corriger`, configurable par test. */
let correctionResultat: { ok: boolean; code?: string } = { ok: true };
/** Résultat renvoyé par `espace_memoire_supprimer`, configurable par test. */
let suppressionResultat: { ok: boolean; statut?: string } = { ok: true };
/** Rang (1-indexé) de la promotion à faire échouer ; -1 = aucune. */
let promotionQuiEchoue = -1;
let promotionsVuees = 0;

vi.mock("@/lib/ipc", () => ({
  invoke: vi.fn((cmd: string, args: Record<string, unknown>) => {
    invokes.push({ cmd, args });
    if (cmd === "espace_depot") return Promise.resolve(reponseDepot);
    if (cmd === "espace_memoire") return Promise.resolve(memoireReponse);
    if (cmd === "espace_memoire_forcer") {
      if (forcerEnVol) return forcerEnVol;
      memoireReponse = {
        elements: reponseForcer.elements,
        synthese: reponseForcer.synthese,
        entretien: reponseForcer.entretien ?? null,
      };
      return Promise.resolve(reponseForcer);
    }
    if (cmd === "espace_memoire_corriger") {
      const contenu = String(args.content ?? "");
      memoireReponse = {
        ...memoireReponse,
        elements: memoireReponse.elements.map((el) =>
          el.entry_id === args.entryId
            ? { ...el, content: contenu, version: Number(el.version ?? 0) + 1 }
            : el,
        ),
      };
      return Promise.resolve({ ...memoireReponse, correction: correctionResultat });
    }
    if (cmd === "espace_memoire_supprimer") {
      if (suppressionResultat.ok === false) {
        // Rien n'est muté : c'est ce que fait le backend quand l'entretien tient
        // le verrou mémoire.
        return Promise.resolve({
          ok: false,
          statut: suppressionResultat.statut ?? "differe",
          raison: "verrou_occupe",
          elements: memoireReponse.elements,
          synthese: memoireReponse.synthese,
        });
      }
      memoireReponse = {
        ...memoireReponse,
        elements: memoireReponse.elements.filter((el) => el.entry_id !== args.entryId),
      };
      return Promise.resolve({ ...memoireReponse, suppression: { ok: true } });
    }
    if (cmd === "espace_depot_retirer") {
      if (retraitEnVol) return retraitEnVol;
      return Promise.resolve({ ok: true, link_id: "lnk_1", already_removed: false, blob_preserved: true });
    }
    if (cmd === "artifact_ingestion_retry") {
      return Promise.resolve(reponseReessai);
    }
    if (cmd === "espace_depot_renommer") {
      const nouveauNom = String(args.nouveauNom ?? "");
      return Promise.resolve({
        ok: true, artifact_id: String(args.artifactId ?? ""),
        changed: nouveauNom !== "", display_name: nouveauNom,
      });
    }
    if (cmd === "stage_picker_bytes") {
      // La VRAIE forme de `attachments.py:249` : `{items, rejected}`, chaque item
      // plate et en snake_case. Les types du pont annoncent autre chose.
      return Promise.resolve({
        items: [{
          stage_item_id: "sti_abc123def456", sha256: "abc123def456",
          name: String(args.filename ?? ""), mime: "application/pdf",
          kind: "pdf", size_bytes: 4, draft_id: "",
        }],
        rejected: [],
      });
    }
    if (cmd === "attachments_prepare") {
      promotionsVuees += 1;
      // Un refus en cours de route : c'est ce qui rend un dépôt PARTIEL, les
      // lots précédents étant déjà versés.
      if (promotionsVuees === promotionQuiEchoue) {
        return Promise.reject(new Error("[QUOTA] Le service a refusé ce lot."));
      }
      const noms = (args.names as string[]) ?? [];
      return Promise.resolve({
        ok: true,
        artifact_ids: noms.map((_, i) => `art_versé_${i}`),
        count: noms.length,
        scope_kind: "workspace",
        items: noms.map((nom, i) => ({
          artifact_id: `art_versé_${i}`, name: nom, kind: "pdf",
          mime: "application/pdf", size_bytes: 4,
        })),
      });
    }
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
  pathForFile: vi.fn(() => null),
}));

vi.mock("@/lib/ipcBridge", async () => {
  const actual = await vi.importActual("@/lib/ipcBridge");
  return {
    ...actual,
    listWorkspaces: (...a: unknown[]) => listWorkspaces(...a),
    createWorkspace: (...a: unknown[]) => createWorkspace(...a),
    getWorkspace: (...a: unknown[]) => getWorkspace(...a),
    listWorkspaceConversations: (...a: unknown[]) => listWorkspaceConversations(...a),
  };
});

vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual("@/components/ui/Toast");
  return { ...actual, showToast: (...a: unknown[]) => showToast(...a) };
});

import { EspacesPage } from "@/pages/espaces/EspacesPage";
import { EspacePage } from "@/pages/espaces/EspacePage";
import { formatOctets, formatNombre, teinteEspace } from "@/pages/espaces/format";
import { InferenceActivityProvider } from "@/features/inference/InferenceActivityContext";
import type { DepotFile } from "@/lib/ipcBridge";

function stats(partiels: Record<string, number> = {}) {
  return {
    workspace_id: "ws_1",
    discussions: 12,
    fichiers: 28,
    octets: 245 * 1024 * 1024,
    memoire: 5,
    memoire_par_statut: {
      candidate: 0,
      accepted: 5,
      stale: 1,
      rejected: 0,
      ...partiels,
    },
  };
}

const ESPACE = {
  id: "ws_1",
  name: "Refonte du site web",
  description: "Design, structure, contenu et développement du nouveau site.",
  state: "active",
  created_at: "2024-05-12T09:00:00Z",
  updated_at: "2024-05-20T09:00:00Z",
  stats: stats(),
};

/** Ce que rend `espace_depot` : une seule catégorie, ce que l'utilisateur a versé. */
const DEPOT = {
  workspace_id: "ws_1",
  files: [
    { artifact_id: "art_1", display_name: "Notes de réunion.pdf", kind: "pdf", size_bytes: 2 * 1024 * 1024, size_known: true, added_at: ESPACE.updated_at },
    { artifact_id: "art_2", display_name: "Schéma d'information.png", kind: "image", size_bytes: 512 * 1024, size_known: true, added_at: ESPACE.updated_at },
    { artifact_id: "art_3", display_name: "Budget 2026.xlsx", kind: "sheet", size_bytes: 64 * 1024, size_known: true, added_at: ESPACE.created_at },
  ] as DepotFile[],
  // `hidden` est ce que le service ÉCARTE. Vide pour un dépôt sain : sans cette
  // clé, un dépôt amputé se lirait comme un dépôt complet.
  hidden: [] as DepotCache[],
  conversation_count: 2,
  is_physical_folder: false,
};

type DepotCache = { artifact_id: string; display_name: string; reason: string };

type DepotReponse = typeof DEPOT;

beforeEach(() => {
  listWorkspaces.mockReset().mockResolvedValue([ESPACE]);
  createWorkspace.mockReset().mockResolvedValue({ ...ESPACE, id: "ws_new", name: "Nouveau" });
  getWorkspace.mockReset().mockResolvedValue({ workspace: ESPACE, stats: stats() });
  listWorkspaceConversations.mockReset().mockResolvedValue([
    { id: "conv_1", title: "Structure de la page d'accueil", created_at: ESPACE.created_at, updated_at: ESPACE.updated_at },
    { id: "conv_2", title: "Générer le prototype Figma", created_at: ESPACE.created_at, updated_at: ESPACE.created_at },
  ]);
  reponseDepot = DEPOT;
  retraitEnVol = null;
  reponseReessai = { ok: true, accepted: true, artifact_id: "art_1", state: "echec" };
  promotionQuiEchoue = -1;
  promotionsVuees = 0;
  memoireReponse = { elements: [], synthese: null };
  reponseForcer = {
    elements: [],
    synthese: null,
    rafraichissement: { statut: "rien_a_promouvoir" },
  };
  forcerEnVol = null;
  correctionResultat = { ok: true };
  suppressionResultat = { ok: true };
  invokes.length = 0;
  showToast.mockReset();
  Object.keys(listeners).forEach((key) => { delete listeners[key]; });
});

afterEach(cleanup);

function rendreAccueil() {
  return render(
    <MemoryRouter initialEntries={["/espaces"]}>
      <Routes>
        <Route path="/espaces" element={<EspacesPage />} />
        <Route path="/espaces/:id" element={<EspacePage />} />
      </Routes>
    </MemoryRouter>
  );
}

function rendreEspace(onglet = "/espaces/ws_1") {
  return render(
    <MemoryRouter initialEntries={[onglet]}>
      <Routes>
        <Route path="/espaces/:id" element={<EspacePage />} />
        <Route path="/espaces/:id/:onglet" element={<EspacePage />} />
      </Routes>
    </MemoryRouter>
  );
}

/** Le même écran, avec le provider qui reçoit les événements d'entretien. */
async function rendreEspaceAvecProvider(onglet = "/espaces/ws_1/memoire") {
  const resultat = render(
    <MemoryRouter initialEntries={[onglet]}>
      <InferenceActivityProvider>
        <Routes>
          <Route path="/espaces/:id" element={<EspacePage />} />
          <Route path="/espaces/:id/:onglet" element={<EspacePage />} />
        </Routes>
      </InferenceActivityProvider>
    </MemoryRouter>
  );
  await waitFor(() => {
    if ((listeners["memory_started"]?.length ?? 0) === 0) {
      throw new Error("Listener memory_started absent");
    }
  });
  return resultat;
}

/** La valeur d'un compteur du panneau de droite, lu par son INTITULÉ. */
function valeurDe(container: HTMLElement, intitule: string): string {
  const terme = [...container.querySelectorAll("dt")].find((el) => el.textContent === intitule);
  return terme?.nextElementSibling?.textContent ?? "";
}

/** Le PDF que tous les tests déposent. */
function pdf(nom = "Cahier des charges.pdf"): File {
  return new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], nom, { type: "application/pdf" });
}

describe("Accueil des espaces", () => {
  it("liste les espaces avec les compteurs du service", async () => {
    rendreAccueil();
    expect(await screen.findByText("Refonte du site web")).toBeTruthy();
    // Les compteurs viennent de `stats()`, pas d'une constante d'écran.
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("28")).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
  });

  it("n'affirme PAS un plafond de dépôt", async () => {
    const { container } = rendreAccueil();
    await screen.findByText("Refonte du site web");
    // La maquette 05-3 écrit « 245 Mo / 10 Go » ; aucun contrat ne définit de quota.
    expect(container.textContent).not.toMatch(/10\s*Go/);
    expect(container.textContent).not.toMatch(/\/\s*10/);
  });

  it("détaillé les statuts de mémoire réels, jamais « Active »", async () => {
    const { container } = rendreAccueil();
    await screen.findByText("Refonte du site web");
    // « Mémoire : Active » des maquettes : un statut qui n'existe pas dans C17.
    expect(container.textContent).not.toMatch(/Mémoire\s*Active/i);
    // Les statuts affichés sont ceux du service, et seulement eux.
    expect(container.textContent).toContain("acceptées");
    expect(container.textContent).toContain("périmées");
  });

  it("distingue une panne d'une absence d'espaces", async () => {
    listWorkspaces.mockRejectedValue(new Error("base injoignable"));
    rendreAccueil();
    const alerte = await screen.findByRole("alert");
    expect(alerte.textContent).toMatch(/Impossible de lire les espaces/);
    // Ce qui distingue la panne de l'absence, c'est l'état VIDE : il ne doit pas
    // s'afficher, sinon l'écran dirait « vous n'avez aucun espace ».
    expect(screen.queryByText("Aucun espace")).toBeNull();
  });

  it("filtre sur le nom et la description", async () => {
    rendreAccueil();
    await screen.findByText("Refonte du site web");
    fireEvent.change(screen.getByPlaceholderText("Rechercher un espace…"), {
      target: { value: "zzz-aucun" },
    });
    await waitFor(() => expect(screen.getByText(/Aucun espace ne correspond/)).toBeTruthy());
  });

  it("crée un espace depuis la modale et appelle la commande", async () => {
    rendreAccueil();
    await screen.findByText("Refonte du site web");
    fireEvent.click(screen.getByRole("button", { name: /Nouvel espace/ }));
    fireEvent.change(await screen.findByLabelText("Nom"), { target: { value: "Mon projet" } });
    fireEvent.click(screen.getByRole("button", { name: "Créer" }));
    await waitFor(() => expect(createWorkspace).toHaveBeenCalledWith("Mon projet", ""));
  });

  it("refuse de créer un espace sans nom, sans appel IPC", async () => {
    rendreAccueil();
    await screen.findByText("Refonte du site web");
    fireEvent.click(screen.getByRole("button", { name: /Nouvel espace/ }));
    const bouton = await screen.findByRole("button", { name: "Créer" });
    expect((bouton as HTMLButtonElement).disabled).toBe(true);
    expect(createWorkspace).not.toHaveBeenCalled();
  });
});

describe("Espace — discussions", () => {
  it("liste les discussions et permet d'ouvrir une conversation", async () => {
    rendreEspace();
    expect(await screen.findByText("Structure de la page d'accueil")).toBeTruthy();
    expect(screen.getByText("Générer le prototype Figma")).toBeTruthy();
    // Le titre d'une conversation SANS titre ne doit pas laisser un trou muet.
    listWorkspaceConversations.mockResolvedValue([
      { id: "c9", title: "", created_at: ESPACE.created_at, updated_at: ESPACE.created_at },
    ]);
  });

  it("montre « Sans titre » plutôt qu'une ligne vide", async () => {
    listWorkspaceConversations.mockResolvedValue([
      { id: "c9", title: "", created_at: ESPACE.created_at, updated_at: ESPACE.created_at },
    ]);
    rendreEspace();
    expect(await screen.findByText("Sans titre")).toBeTruthy();
  });

  it("ne pose aucun minuteur de 60 s sur l'onglet Discussions", async () => {
    const espion = vi.spyOn(globalThis, "setInterval");
    try {
      rendreEspace("/espaces/ws_1/discussions");
      await screen.findByText("Structure de la page d'accueil");
      // Le repli lent appartient à l'onglet Dépôt. Sur Discussions, aucun
      // minuteur ne doit relire un inventaire que l'écran n'affiche pas.
      expect(
        espion.mock.calls.some((appel) => Number(appel[1]) === 60_000),
      ).toBe(false);
    } finally {
      espion.mockRestore();
    }
  });

  it("affiche la taille du dépôt SANS plafond", async () => {
    const { container } = rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    await waitFor(() => expect(valeurDe(container, "Poids du dépôt")).not.toBe("0 o"));
    // Le poids affiché est celui de l'inventaire relu, et il ne s'accompagne
    // d'aucun plafond : la maquette 05-3 écrit « 245 Mo / 10 Go », et le « 10 Go »
    // n'a aucune source.
    expect(container.textContent).not.toMatch(/10\s*Go/);
  });

  it("offre les actions renommer et supprimer sur chaque discussion", async () => {
    rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(
      screen.getByRole("button", {
        name: "Actions pour « Structure de la page d'accueil »",
      }),
    );
    // Le menu est le seul endroit où l'utilisateur peut agir sur une ligne :
    // sans lui, renommer et supprimer n'existent pas dans l'espace.
    expect(screen.getByRole("button", { name: "Renommer" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Supprimer" })).toBeTruthy();
  });

  it("offre le déplacement vers un espace depuis le menu", async () => {
    rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(
      screen.getByRole("button", {
        name: "Actions pour « Structure de la page d'accueil »",
      }),
    );
    const entree = screen.getByRole("button", { name: "Déplacer vers un espace…" });
    expect(entree).toBeTruthy();

    fireEvent.click(entree);
    // `espace_list` n'est pas branché ici : le pont rend une liste vide, et le
    // dialogue doit le dire sans planter plutôt que d'offrir un choix fantôme.
    const dialogue = await screen.findByRole("dialog");
    expect(dialogue.textContent).toContain("Aucun autre espace disponible.");
    expect(screen.getByRole("button", { name: "Fermer" })).toBeTruthy();
  });

  it("renomme une discussion par son identifiant", async () => {
    rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(
      screen.getByRole("button", {
        name: "Actions pour « Structure de la page d'accueil »",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Renommer" }));

    const champ = screen.getByLabelText("Nouveau titre de la discussion");
    // Le champ est pré-rempli du titre courant : on peut corriger, pas réécrire.
    expect((champ as HTMLInputElement).value).toBe("Structure de la page d'accueil");
    fireEvent.change(champ, { target: { value: "Nouveau titre" } });
    fireEvent.keyDown(champ, { key: "Enter" });

    await waitFor(() =>
      expect(invokes).toEqual(
        expect.arrayContaining([
          {
            cmd: "rename_conversation",
            args: { conversationId: "conv_1", title: "Nouveau titre" },
          },
        ]),
      ),
    );
    // L'écran reflète le titre retenu : il ne relit pas une liste que le
    // service n'a pas encore renvoyée.
    expect(await screen.findByText("Nouveau titre")).toBeTruthy();
  });

  it("n'appelle pas le service pour un titre vide", async () => {
    rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(
      screen.getByRole("button", {
        name: "Actions pour « Structure de la page d'accueil »",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Renommer" }));

    const champ = screen.getByLabelText("Nouveau titre de la discussion");
    fireEvent.change(champ, { target: { value: "   " } });
    fireEvent.keyDown(champ, { key: "Enter" });

    // Un titre blanc n'est pas un renommage : ni requête, ni toast d'échec.
    // Sans cette seconde assertion, le refus d'`ipcBridge` suffirait à passer.
    expect(invokes.filter((i) => i.cmd === "rename_conversation")).toHaveLength(0);
    expect(showToast).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Nouveau titre de la discussion")).toBeTruthy();
  });

  it("demande confirmation puis retire la discussion supprimée", async () => {
    rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(
      screen.getByRole("button", {
        name: "Actions pour « Structure de la page d'accueil »",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Supprimer" }));

    const dialogue = await screen.findByRole("dialog");
    // Le texte dit la corbeille, pas une destruction immédiate : c'est ce que
    // fait réellement le backend (`corbeille.py`).
    expect(dialogue.textContent).toContain(
      "Elle sera retirée des vues actives et placée dans la corbeille pendant sept jours.",
    );

    fireEvent.click(screen.getByRole("button", { name: "Supprimer la discussion" }));

    await waitFor(() =>
      expect(invokes).toEqual(
        expect.arrayContaining([
          { cmd: "delete_conversation", args: { conversationId: "conv_1" } },
        ]),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByText("Structure de la page d'accueil")).toBeNull(),
    );
    // Seule la ligne visée disparaît : la liste n'est pas vidée par accident.
    expect(screen.getByText("Générer le prototype Figma")).toBeTruthy();
  });

  it("annule la suppression sans appeler le service", async () => {
    rendreEspace();
    await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(
      screen.getByRole("button", {
        name: "Actions pour « Structure de la page d'accueil »",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Supprimer" }));

    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "Annuler" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(invokes.filter((i) => i.cmd === "delete_conversation")).toHaveLength(0);
    expect(screen.getByText("Structure de la page d'accueil")).toBeTruthy();
  });
});

describe("Espace — démarrer une discussion", () => {
  it("propose le bouton même quand l'espace n'a aucune discussion", async () => {
    listWorkspaceConversations.mockResolvedValue([]);
    rendreEspace();
    expect(await screen.findByRole("button", { name: /Nouvelle discussion/ })).toBeTruthy();
  });

  it("n'expose pas ce bouton dans l'onglet Dépôt", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Vue de portée, pas un dossier");
    expect(screen.queryByRole("button", { name: /Nouvelle discussion/ })).toBeNull();
  });

  it("ouvre une discussion neuve rattachée à l'espace", async () => {
    const vus: Array<{ chemin: string; etat: unknown }> = [];
    function SondeConversation() {
      const location = useLocation();
      vus.push({ chemin: location.pathname, etat: location.state });
      return <span>sonde</span>;
    }
    render(
      <MemoryRouter initialEntries={["/espaces/ws_1/discussions"]}>
        <Routes>
          <Route path="/espaces/:id/:onglet" element={<EspacePage />} />
          <Route path="/chat/:id" element={<SondeConversation />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Nouvelle discussion/ }));
    await waitFor(() => expect(vus.length).toBeGreaterThan(0));
    const identifiant = vus[0].chemin.replace("/chat/", "");
    expect(identifiant).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
    expect(vus[0].etat).toEqual({ workspaceId: "ws_1" });
  });

  it("ne crée aucune conversation avant le premier message", async () => {
    function SondeConversation() {
      return <span>sonde</span>;
    }
    render(
      <MemoryRouter initialEntries={["/espaces/ws_1/discussions"]}>
        <Routes>
          <Route path="/espaces/:id/:onglet" element={<EspacePage />} />
          <Route path="/chat/:id" element={<SondeConversation />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Nouvelle discussion/ }));
    await screen.findByText("sonde");
    expect(getWorkspace).toHaveBeenCalledTimes(1);
    expect(listWorkspaceConversations).toHaveBeenCalledTimes(1);
  });
});

describe("Espace — dépôt", () => {
  it("ne propose ni arborescence ni chemin copiable", async () => {
    const { container } = rendreEspace("/espaces/ws_1/depot");
    // On attend LA PHRASE AFFIRMÉE, pas un onglet qui existe avant le chargement :
    // l'ancienne attente portrait sur un élément déjà là, et la lecture de
    // l'écran pouvait alors tomber sur un dépôt pas encore inventorié.
    await screen.findByText("Vue de portée, pas un dossier");
    // La maquette 05-3 montre une arborescence et un « Chemin » : un espace n'a
    // pas de dossier, donc un chemin affiché serait un chemin faux.
    expect(container.textContent).not.toMatch(/\/docs\//);
    expect(container.textContent).not.toMatch(/Chemin/);
  });

  it("n'offre ni suppression ni déplacement d'un fichier", async () => {
    const { container } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Vue de portée, pas un dossier");
    // « Supprimer » / « Exporter » / « Déplacer » sortent du périmètre : la purge
    // est une décision utilisateur avec dépendances, pas un bouton de ligne.
    // « Renommer » y figure depuis depot.md §5.2 : renommer le NOM AFFICHÉ est
    // exactement la modification prévue par la fiche.
    expect(container.textContent).not.toMatch(/Versions précédentes/);
    const libelles = [...container.querySelectorAll("button")].map((b) => b.textContent ?? "");
    expect(libelles.filter((t) => /Supprimer|Déplacer|Exporter/i.test(t))).toEqual([]);
  });

  it("affiche un dépôt vide plutôt qu'un dossier vide", async () => {
    getWorkspace.mockResolvedValue({
      workspace: { ...ESPACE, stats: stats() },
      stats: { ...stats(), fichiers: 0, octets: 0, discussions: 0 },
    });
    listWorkspaceConversations.mockResolvedValue([]);
    // Le compteur vient du service, l'inventaire aussi : les deux disent « vide ».
    reponseDepot = { ...DEPOT, files: [] };
    const { container } = rendreEspace("/espaces/ws_1/depot");
    expect(await screen.findByText("Dépôt vide")).toBeTruthy();
    expect(container.textContent).not.toMatch(/\/+/);
  });
});

describe("Espace — dépôt : l'inventaire", () => {
  it("liste les documents du service avec nom, poids et type", async () => {
    rendreEspace("/espaces/ws_1/depot");
    const nom = await screen.findByText("Notes de réunion.pdf");
    const ligne = nom.closest("li");
    expect(ligne).toBeTruthy();
    // Le poids vient de `size_bytes` et le type de `kind` : un PDF n'est pas une
    // image, et la ligne ne le prétend pas.
    expect(ligne?.textContent).toContain("PDF");
    expect(ligne?.textContent).toContain("2,0 Mo");
    expect(ligne?.textContent).toContain("2024");
    expect(screen.getByText("Schéma d'information.png").closest("li")?.textContent).toContain("Image");
    expect(screen.getByText("Budget 2026.xlsx").closest("li")?.textContent).toContain("Tableur");
    // Le compte affiché est celui de l'inventaire, pas une constante d'écran.
    expect(screen.getByText(/3 documents/)).toBeTruthy();
  });

  it("dépose un fichier choisi par attachments_prepare, nom d'origine compris", async () => {
    rendreEspace("/espaces/ws_1/depot");
    const bouton = await screen.findByRole("button", { name: "Ajouter des fichiers" });
    expect((bouton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(bouton);
    const fichier = new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], "Cahier des charges.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Fichiers à déposer dans cet espace"), {
      target: { files: [fichier] },
    });

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "attachments_prepare")).toHaveLength(1);
    });
    const promotion = invokes.find((i) => i.cmd === "attachments_prepare");
    // `workspaceId` : la portée est l'espace, pas une discussion. `names` : sans
    // l'étiquette d'origine, l'inventaire afficherait une empreinte.
    expect(promotion?.args.workspaceId).toBe("ws_1");
    expect(promotion?.args.names).toEqual(["Cahier des charges.pdf"]);
    expect(promotion?.args.stageItemIds).toEqual(["sti_abc123def456"]);
    // Le nom vient du fichier choisi, il n'est pas écrit en dur côté écran.
    expect(invokes.find((i) => i.cmd === "stage_picker_bytes")?.args.filename).toBe("Cahier des charges.pdf");
    // L'échec ne resterait pas muet : un dépôt réussi se dit, un échec aussi.
    expect(showToast).toHaveBeenCalledWith(expect.objectContaining({ type: "success" }));
  });

  it("retire un document puis relit l'inventaire", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Retirer Notes de réunion.pdf du dépôt" }));

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_depot_retirer")).toHaveLength(1);
    });
    const retrait = invokes.find((i) => i.cmd === "espace_depot_retirer");
    expect(retrait?.args).toEqual({ workspaceId: "ws_1", artifactId: "art_1" });
    // Le retrait ne SUPPRIME rien : l'interface le dit, et l'inventaire revient
    // du service juste après.
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Retiré du dépôt" }),
    );
    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_depot")).toHaveLength(2);
    });
    const ordre = invokes.map((i) => i.cmd);
    expect(ordre.indexOf("espace_depot_retirer")).toBeGreaterThan(ordre.indexOf("espace_depot"));
    expect(ordre.indexOf("espace_depot_retirer")).toBeLessThan(ordre.lastIndexOf("espace_depot"));
    // Le panneau de droite compte la MÊME chose que l'onglet : il n'a plus son
    // propre appel au service, et c'est l'inventaire relu qui le met à jour.
    expect(getWorkspace).toHaveBeenCalledTimes(1);
  });

  it("propose d'ajouter quand l'inventaire est vide", async () => {
    reponseDepot = { ...DEPOT, files: [] };
    rendreEspace("/espaces/ws_1/depot");
    expect(await screen.findByText("Dépôt vide")).toBeTruthy();
    // L'état vide dit CE QU'EST le dépôt, et ne se contente pas d'un compteur.
    expect(screen.getByText(/mémoire garde ce que vous avez validé/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Déposer un premier document/ })).toBeTruthy();
    expect(screen.queryByText("Notes de réunion.pdf")).toBeNull();
  });

  it("n'ajoute rien dans un espace en corbeille", async () => {
    getWorkspace.mockResolvedValue({
      workspace: { ...ESPACE, state: "trashed" },
      stats: stats(),
    });
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    const bouton = screen.getByRole("button", { name: "Ajouter des fichiers" });
    // Le serveur refuse (`attachments_prepare` exige un espace actif) : un
    // bouton actif n'annoncerait qu'un échec.
    expect((bouton as HTMLButtonElement).disabled).toBe(true);
    const choix = screen.getByLabelText("Fichiers à déposer dans cet espace") as HTMLInputElement;
    expect(choix.disabled).toBe(true);
    fireEvent.change(choix, {
      target: { files: [new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], "Refus.pdf")] },
    });
    // Même une sélection forcée ne franchit pas le garde-fou : rien n'est stagé,
    // donc rien n'est promu, et le dépôt ne bouge pas.
    expect(invokes.filter((i) => i.cmd === "stage_picker_bytes")).toHaveLength(0);
    expect(invokes.filter((i) => i.cmd === "attachments_prepare")).toHaveLength(0);
    expect(showToast).not.toHaveBeenCalled();
  });

  it("distingue le dépôt de la mémoire", async () => {
    const { container } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    // Le dépôt APPORTE des fichiers ; la mémoire RETIENT des validations.
    expect(container.textContent).toMatch(/Un fichier déposé dans une discussion y reste/);
    expect(container.textContent).toMatch(/mémoire retient des informations que vous avez validées/);
  });

  it("n'annonce pas une taille que le chemin de sélection ne tient pas", async () => {
    const { container } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    // « vingt mégaoctets par fichier » était une promesse : la sélection
    // sérialise les octets dans la ligne JSON du protocole, plafonnée à 256 Ko
    // (`inference/ipc.py:26`), donc bien en deçà. Le glisser, lui, ne transmet
    // qu'un chemin. L'écran doit distinguer les deux chemins, pas promettre.
    expect(container.textContent).not.toMatch(/vingt\s*m[ée]gaoctets/i);
    expect(container.textContent).not.toMatch(/\d+\s*m[ée]gaoctets/i);
    expect(container.textContent).toMatch(/choisissez-les/);
    expect(container.textContent).toMatch(/Glissez plutôt les gros fichiers/);
  });

  it("ne dit pas qu'une discussion emporte ses fichiers dans le dépôt", async () => {
    listWorkspaceConversations.mockResolvedValue([]);
    const { container } = rendreEspace();
    expect(await screen.findByText(/Démarrez une discussion/)).toBeTruthy();
    // L'ancien texte disait « et ses fichiers aussi » puis « un espace ne
    // possède aucun fichier » : deux affirmations opposées sur la même
    // fonctionnalité. Le dépôt welcome à part, et le dépôt-tab le dit.
    expect(container.textContent).not.toMatch(/et ses fichiers aussi/);
    expect(container.textContent).toMatch(/onglet Dépôt/);
  });

  it("n'annonce qu'un seul message quand le versement s'arrête en cours de route", async () => {
    // Neuf fichiers : le premier lot de huit passe, le second échoue. C'est la
    // seule façon d'obtenir un dépôt RÉELLEMENT partiel.
    promotionQuiEchoue = 2;
    rendreEspace("/espaces/ws_1/depot");
    fireEvent.change(await screen.findByLabelText("Fichiers à déposer dans cet espace"), {
      target: { files: Array.from({ length: 9 }, (_, i) => pdf(`Piece-${i}.pdf`)) },
    });

    await waitFor(() => expect(showToast).toHaveBeenCalledTimes(1));
    // Un seul toast, et il dit ce qui est VRAIMENT arrivé : une part entrée,
    // une part perdue. « Ajout interrompu » puis « Ajouté au dépôt » se
    // contredisaient, et l'utilisateur ne pouvait pas savoir ce qu'il avait.
    const toast = showToast.mock.calls[0][0] as { title: string; message: string };
    expect(toast.title).toBe("Dépôt partiel");
    expect(toast.message).toMatch(/8 documents ont été versés dans le dépôt, le reste non/);
    expect(toast.message).toMatch(/refusé ce lot/);
    expect(showToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Ajout interrompu" }),
    );
    expect(showToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Ajouté au dépôt" }),
    );
  });
});

describe("Espace — dépôt : ce que le dépôt n'affiche pas", () => {
  it("avertit que des fichiers ne sont pas affichés, et pourquoi", async () => {
    reponseDepot = {
      ...DEPOT,
      hidden: [
        { artifact_id: "art_4", display_name: "", reason: "supprime" },
        { artifact_id: "art_5", display_name: "", reason: "sans_nom" },
      ],
    };
    const { container } = rendreEspace("/espaces/ws_1/depot");
    // Le service sait ce qu'il écarte ; l'écran le dit en UNE ligne, sans dramatiser.
    expect(await screen.findByText(/2 documents ne sont pas affichés dans ce dépôt/)).toBeTruthy();
    expect(container.textContent).toMatch(/supprimés, sans nom affichable/);
    // `artifact_id` est technique : il sert à compter les motifs, pas à les lister.
    expect(container.textContent).not.toMatch(/art_4|art_5/);
    // La ligne énoncée reste celle de l'inventaire : rien n'est ajouté au dépôt.
    expect(screen.getByText(/3 documents · 2,6 Mo/)).toBeTruthy();
  });

  it("ne dit rien quand le dépôt est sain", async () => {
    const { container } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    // Un dépôt sans anomalie n'a rien à dire de son propre silence : une ligne
    // d'avertissement ici apprendrait à l'utilisateur de l'ignorer.
    expect(container.textContent).not.toMatch(/ne sont pas affichés|n'est pas affiché/);
  });

  it("avertit même quand il ne reste aucun fichier à afficher", async () => {
    // Sinon « Dépôt vide » ferait passer l'écart pour une absence : l'utilisateur
    // viderait un dépôt dont un document n'est justement pas visible.
    reponseDepot = { ...DEPOT, files: [], hidden: [{ artifact_id: "art_4", display_name: "", reason: "supprime" }] };
    const { container } = rendreEspace("/espaces/ws_1/depot");
    expect(await screen.findByText("Dépôt vide")).toBeTruthy();
    expect(container.textContent).toMatch(/1 document n'est pas affiché dans ce dépôt : supprimé\./);
  });

  it("écrit « taille inconnue » plutôt que « 0 o »", async () => {
    reponseDepot = {
      ...DEPOT,
      files: [
        { ...DEPOT.files[0], size_bytes: 0, size_known: false },
        ...DEPOT.files.slice(1),
      ],
    };
    const { container } = rendreEspace("/espaces/ws_1/depot");
    const ligne = (await screen.findByText("Notes de réunion.pdf")).closest("li");
    // « 0 o » afficherait un fichier vide qui ne l'est pas : le service dit ne pas
    // connaître le poids, et l'écran doit le dire ainsi.
    expect(ligne?.textContent).toContain("taille inconnue");
    expect(ligne?.textContent).not.toMatch(/\b0 o\b/);
    // La somme devient un plan MINIMAL, et elle s'y dit dans les deux endroits qui
    // pèsent le dépôt : 512 ko + 64 ko, les seuls poids que le service connaît.
    const poids = `${formatOctets(512 * 1024 + 64 * 1024)} au moins`;
    expect(screen.getByText(`3 documents · ${poids}`)).toBeTruthy();
    expect(valeurDe(container, "Poids du dépôt")).toBe(poids);
  });

  it("refuse un inventaire qui ne dit pas ce qu'il a écarté", async () => {
    // Un `hidden` absent n'est pas un dépôt sain : le pont le refuse plutôt que de
    // le supposer vide, sinon l'avertissement serait du décor.
    const { hidden: _absent, ...sansHidden } = DEPOT;
    reponseDepot = sansHidden as DepotReponse;
    rendreEspace("/espaces/ws_1/depot");
    const alerte = await screen.findByRole("alert");
    expect(alerte.textContent).toMatch(/n'a pas dit ce qu'il a écarté/);
  });
});

describe("Espace — dépôt : l'état d'ingestion (depot.md §6)", () => {
  it("distingue disponible, partiel et échec, ligne par ligne", async () => {
    reponseDepot = {
      ...DEPOT,
      files: [
        { ...DEPOT.files[0], ingest_state: "extrait", ingest_reason: "" },
        { ...DEPOT.files[1], ingest_state: "tronque", ingest_reason: "" },
        { ...DEPOT.files[2], ingest_state: "echec", ingest_reason: "Aucun texte extrait de ce PDF." },
      ],
    };
    rendreEspace("/espaces/ws_1/depot");
    expect(await screen.findByText("Prêt")).toBeTruthy();
    expect(screen.getByText("Indexé en partie")).toBeTruthy();
    expect(screen.getByText("Échec d'extraction")).toBeTruthy();
    // La raison du service s'affiche pour un échec : « Échec » seul dit
    // qu'il y a un problème, pas pour quoi l'utilisateur peut y faire.
    expect(screen.getByText("Aucun texte extrait de ce PDF.")).toBeTruthy();
  });

  it("distingue « en attente » de « en cours » et ne prétend jamais un état prêt", async () => {
    reponseDepot = {
      ...DEPOT,
      files: [
        { ...DEPOT.files[0], ingest_state: "depose", ingest_reason: "" },
        { ...DEPOT.files[1], ingest_state: "en_cours", ingest_reason: "" },
        { ...DEPOT.files[2], ingest_state: "extrait", ingest_reason: "" },
      ],
    };
    rendreEspace("/espaces/ws_1/depot");
    expect(await screen.findByText("En attente d'extraction")).toBeTruthy();
    expect(screen.getByText("Extraction en cours")).toBeTruthy();
    expect(screen.getByText("Prêt")).toBeTruthy();
  });

  it("nomme l'inconnu au lieu de le lire comme un prêt", async () => {
    // Un fichier sans champ d'état (service ancien) ne reçoit PAS l'étiquette
    // « Prêt » : l'absence d'information se dit, pas une promesse.
    const { ingest_state: _absent, ...sansEtat } = DEPOT.files[0];
    reponseDepot = {
      ...DEPOT,
      files: [
        sansEtat,
        { ...DEPOT.files[1], ingest_state: "extrait" },
        { ...DEPOT.files[2], ingest_state: "extrait" },
      ],
    };
    rendreEspace("/espaces/ws_1/depot");
    expect(await screen.findByText("État inconnu")).toBeTruthy();
  });
});

describe("Espace — dépôt : observabilité de l'ingestion", () => {
  it("se rafraîchit une seule fois pour un lot d'événements (coalescence)", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    await waitFor(() =>
      expect((listeners["artifact_ingestion_finished"] ?? []).length).toBeGreaterThan(0),
    );

    const avant = invokes.filter((appel) => appel.cmd === "espace_depot").length;
    fire("artifact_ingestion_finished", {
      artifact_id: "art_1", workspace_id: "ws_1", state: "extrait",
    });
    fire("artifact_ingestion_finished", {
      artifact_id: "art_2", workspace_id: "ws_1", state: "extrait",
    });
    fire("artifact_ingestion_failed", {
      artifact_id: "art_3", workspace_id: "ws_1", state: "echec",
    });

    await act(async () => { await new Promise((suite) => setTimeout(suite, 350)); });

    await waitFor(() =>
      expect(invokes.filter((appel) => appel.cmd === "espace_depot").length)
        .toBe(avant + 1),
    );
  });

  it("ignore un événement d'un autre espace", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    await waitFor(() =>
      expect((listeners["artifact_ingestion_finished"] ?? []).length).toBeGreaterThan(0),
    );

    const avant = invokes.filter((appel) => appel.cmd === "espace_depot").length;
    fire("artifact_ingestion_finished", {
      artifact_id: "art_9", workspace_id: "ws_2", state: "extrait",
    });
    await act(async () => { await new Promise((suite) => setTimeout(suite, 350)); });

    expect(invokes.filter((appel) => appel.cmd === "espace_depot").length).toBe(avant);
  });

  it("nettoie l'abonnement et le minuteur au démontage", async () => {
    const { unmount } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    await waitFor(() =>
      expect((listeners["artifact_ingestion_finished"] ?? []).length).toBeGreaterThan(0),
    );

    unmount();

    await waitFor(() =>
      expect((listeners["artifact_ingestion_finished"] ?? []).length).toBe(0),
    );
  });

  it("au démontage, ne relit plus l'inventaire et arrête son minuteur", async () => {
    const arret = vi.spyOn(globalThis, "clearInterval");
    try {
      const { unmount } = rendreEspace("/espaces/ws_1/depot");
      await screen.findByText("Notes de réunion.pdf");
      const avant = invokes.filter((appel) => appel.cmd === "espace_depot").length;

      unmount();
      await act(async () => {
        await new Promise((suite) => setTimeout(suite, 350));
      });

      // Aucun appel ne doit suivre le démontage : un inventaire relu pour un
      // écran disparu serait du travail sans lecteur.
      expect(invokes.filter((appel) => appel.cmd === "espace_depot").length).toBe(avant);
      // Le minuteur de repli est explicitement arrêté par le cleanup.
      expect(arret).toHaveBeenCalled();
    } finally {
      arret.mockRestore();
    }
  });

  it("garde un repli périodique lent pendant que le dépôt est affiché", async () => {
    const espion = vi.spyOn(globalThis, "setInterval");
    try {
      rendreEspace("/espaces/ws_1/depot");
      await screen.findByText("Notes de réunion.pdf");
      expect(
        espion.mock.calls.some(([, delai]) => delai === 60_000),
      ).toBe(true);
    } finally {
      espion.mockRestore();
    }
  });
});

describe("Espace — dépôt : réessayer l'extraction", () => {
  it("appelle la commande et relit l'inventaire quand la relance est acceptée", async () => {
    reponseDepot = {
      ...DEPOT,
      files: [{ ...DEPOT.files[0], ingest_state: "echec", ingest_reason: "lecteur cassé" }],
    };
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Échec d'extraction");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Réessayer l'extraction de Notes de réunion.pdf",
      }),
    );

    await waitFor(() =>
      expect(invokes).toEqual(
        expect.arrayContaining([
          { cmd: "artifact_ingestion_retry", args: { artifactId: "art_1" } },
        ]),
      ),
    );
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({ type: "success", title: "Extraction relancée" }),
      ),
    );
  });

  it("dit « rien n'a été relancé » quand le service répond accepted:false", async () => {
    reponseDepot = {
      ...DEPOT,
      files: [{ ...DEPOT.files[0], ingest_state: "echec", ingest_reason: "lecteur cassé" }],
    };
    reponseReessai = {
      ok: true, accepted: false, artifact_id: "art_1", state: "echec",
      reason: "extraction déjà en file : rien n'a été relancé.",
    };
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Échec d'extraction");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Réessayer l'extraction de Notes de réunion.pdf",
      }),
    );

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "info",
          title: "Rien n'a été relancé",
          message: expect.stringContaining("déjà en file"),
        }),
      ),
    );
    expect(showToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Extraction relancée" }),
    );
  });

  it("désactive le bouton pendant une extraction en cours", async () => {
    reponseDepot = {
      ...DEPOT,
      files: [{ ...DEPOT.files[0], ingest_state: "en_cours", ingest_reason: "" }],
    };
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Extraction en cours");

    const bouton = screen.getByRole("button", {
      name: "Réessayer l'extraction de Notes de réunion.pdf",
    });
    expect((bouton as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("Espace — dépôt : le renommage (depot.md §5.2)", () => {  it("édite le nom en ligne, appelle le verbe et relit l'inventaire", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Renommer Notes de réunion.pdf" }));
    fireEvent.change(screen.getByLabelText("Nouveau nom pour Notes de réunion.pdf"), {
      target: { value: "Notes du 12 mai.pdf" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Enregistrer" }));

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_depot_renommer")).toHaveLength(1);
    });
    // L'appel dit QUI (espace + document) et QUOI (le nom) — rien d'autre :
    // le contenu et les extraits ne dépendent pas de l'écran.
    expect(invokes.find((i) => i.cmd === "espace_depot_renommer")?.args).toEqual({
      workspaceId: "ws_1", artifactId: "art_1", nouveauNom: "Notes du 12 mai.pdf",
    });
    expect(showToast).toHaveBeenCalledWith(expect.objectContaining({ title: "Nom actualisé" }));
    // L'inventaire revient du service : l'écran n'invente pas le nouveau nom.
    await waitFor(() => expect(invokes.filter((i) => i.cmd === "espace_depot")).toHaveLength(2));
  });

  it("annule avec Échap sans écrire", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Renommer Notes de réunion.pdf" }));
    fireEvent.change(screen.getByLabelText("Nouveau nom pour Notes de réunion.pdf"), {
      target: { value: "Jamais écrit.pdf" },
    });
    fireEvent.keyDown(screen.getByLabelText("Nouveau nom pour Notes de réunion.pdf"), {
      key: "Escape",
    });

    expect(screen.queryByRole("button", { name: "Enregistrer" })).toBeNull();
    expect(invokes.filter((i) => i.cmd === "espace_depot_renommer")).toHaveLength(0);
    expect(showToast).not.toHaveBeenCalled();
  });

  it("ne vote pas pour un nom identique : fermeture sans appel", async () => {
    rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Renommer Notes de réunion.pdf" }));
    // Le brouillon est pré-rempli du nom courant : valider sans rien changer
    // est une fermeture, pas une écriture déguisée en révision de fiche.
    fireEvent.click(screen.getByRole("button", { name: "Enregistrer" }));

    expect(invokes.filter((i) => i.cmd === "espace_depot_renommer")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Enregistrer" })).toBeNull();
  });
});

describe("Espace — dépôt : une seule mesure", () => {
  it("fait afficher au panneau la MÊME liste que l'onglet, pas un second appel", async () => {
    // `espace_get` mesure un dépôt VIVANT et rend ici 28 fichiers / 245 Mo ;
    // `espace_depot` en mesure un autre et rend 3 fichiers / 2,6 Mo. Avant la
    // correction, l'onglet annonçait 3 et le panneau 28 : l'écran se contredisait.
    getWorkspace.mockResolvedValue({
      workspace: ESPACE,
      stats: { ...stats(), fichiers: 28, octets: 245 * 1024 * 1024 },
    });
    const { container } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");
    await waitFor(() => expect(valeurDe(container, "Poids du dépôt")).toBe("2,6 Mo"));

    expect(screen.getByText(/3 documents · 2,6 Mo/)).toBeTruthy();
    // Le panneau lit l'inventaire RENDU, jamais `stats` : la mesure n'existe
    // qu'en un seul endroit de l'écran.
    expect(valeurDe(container, "Fichiers")).toBe("3");
    expect(valeurDe(container, "Poids du dépôt")).toBe("2,6 Mo");
    expect(container.textContent).not.toContain("245,0 Mo");
    // Les compteurs qui ne sont PAS le dépôt restent ceux du service.
    expect(valeurDe(container, "Discussions")).toBe("12");
    expect(valeurDe(container, "Mémoire acceptée")).toBe("5");
  });

  it("n'ouvre pas un dépôt pendant un retrait, et ne se contredit pas à l'écran", async () => {
    let finirRetrait: (valeur: unknown) => void = () => {};
    retraitEnVol = new Promise((resoudre) => { finirRetrait = resoudre; });
    const { container } = rendreEspace("/espaces/ws_1/depot");
    await screen.findByText("Notes de réunion.pdf");

    fireEvent.click(screen.getByRole("button", { name: "Retirer Notes de réunion.pdf du dépôt" }));
    await screen.findByText("Retrait…");

    // Le retrait est en vol : un dépôt lancé dessus déclencherait DEUX
    // rafraîchissements, dont l'ordre d'arrivée laisserait l'en-tête et le
    // panneau sur deux états différents du même dépôt.
    const boutonAjout = screen.getByRole("button", { name: "Ajouter des fichiers" }) as HTMLButtonElement;
    fireEvent.drop(boutonAjout.parentElement as HTMLElement, {
      dataTransfer: { files: [pdf()], types: ["Files"] },
    });
    fireEvent.change(screen.getByLabelText("Fichiers à déposer dans cet espace"), {
      target: { files: [pdf()] },
    });
    // Le tour de boucle se vide : un dépôt amorcé aurait DÉJÀ émis son staging.
    await act(async () => { await new Promise((resoudre) => setTimeout(resoudre, 20)); });
    expect(invokes.filter((i) => i.cmd === "stage_picker_bytes")).toHaveLength(0);
    expect(invokes.filter((i) => i.cmd === "attachments_prepare")).toHaveLength(0);
    // Un bouton d'écriture actif pendant un retrait annoncerait un dépôt qui
    // n'irait nulle part.
    expect(boutonAjout.disabled).toBe(true);

    // Le retrait se termine, et l'écart se referme : les deux chiffres
    // redesignent la même valeur.
    finirRetrait({ ok: true, link_id: "lnk_1", already_removed: false, blob_preserved: true });
    retraitEnVol = null;
    await waitFor(() => expect(invokes.filter((i) => i.cmd === "espace_depot")).toHaveLength(2));
    await waitFor(() => expect(valeurDe(container, "Fichiers")).toBe("3"));
    expect(screen.getByText(/3 documents · 2,6 Mo/)).toBeTruthy();
    // Un seul chemin de rafraîchissement : `getWorkspace` n'est pas relu pour
    // mettre le dépôt à jour.
    expect(getWorkspace).toHaveBeenCalledTimes(1);
  });
});

describe("Espace — mémoire", () => {
  it("sépare les statuts et n'affiche aucun statut inexistant", async () => {
    const { container } = rendreEspace("/espaces/ws_1/memoire");
    await screen.findByRole("heading", { name: "Mémoire" });
    expect(container.textContent).toContain("Acceptées");
    expect(container.textContent).toContain("Périmées");
    expect(container.textContent).not.toMatch(/\bActive\b/);
  });

  it("annonce une mémoire alimentée automatiquement, pas une validation préalable", async () => {
    const { container } = rendreEspace("/espaces/ws_1/memoire");
    await screen.findByRole("heading", { name: "Mémoire" });
    expect(container.textContent).toMatch(/alimentée automatiquement à partir de vos échanges/);
    expect(container.textContent).not.toMatch(/Rien n'entre ici sans votre accord/);
  });

  it("affiche le statut de l’entretien mémoire : en attente", async () => {
    memoireReponse = {
      elements: [],
      synthese: null,
      entretien: {
        tours: 2,
        seuil_tours: 5,
        inactivite_s: 600,
        seuil_inactivite_s: 300,
        etat: "en_attente",
        raison: null,
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    const statut = await screen.findByRole("status");
    expect(statut.textContent).toContain("En attente — 2/5 tours.");
    expect(statut.textContent).toContain("Prêt à démarrer.");
  });

  it("signale une activité récente quand l’entretien attend l’inactivité", async () => {
    memoireReponse = {
      elements: [],
      synthese: null,
      entretien: {
        tours: 2,
        seuil_tours: 5,
        inactivite_s: 60,
        seuil_inactivite_s: 300,
        etat: "en_attente",
        raison: "activite_recente",
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    const statut = await screen.findByRole("status");
    expect(statut.textContent).toContain("Activité récente ; l'entretien partira après 5 min d'inactivité.");
  });

  it("affiche l’entretien différé", async () => {
    memoireReponse = {
      elements: [],
      synthese: null,
      entretien: {
        tours: 5,
        seuil_tours: 5,
        inactivite_s: null,
        seuil_inactivite_s: 300,
        etat: "differe",
        raison: "inférence en cours",
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    expect((await screen.findByRole("status")).textContent).toContain(
      "Entretien différé : une inférence est en cours.",
    );
  });

  it("affiche l’entretien en cours", async () => {
    memoireReponse = {
      elements: [],
      synthese: null,
      entretien: {
        tours: 5,
        seuil_tours: 5,
        inactivite_s: null,
        seuil_inactivite_s: 300,
        etat: "en_cours",
        raison: null,
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    expect((await screen.findByRole("status")).textContent).toBe("Entretien mémoire en cours…");
  });

  it("affiche « À jour » quand l’entretien n’a rien à faire", async () => {
    memoireReponse = {
      elements: [],
      synthese: null,
      entretien: {
        tours: 5,
        seuil_tours: 5,
        inactivite_s: null,
        seuil_inactivite_s: 300,
        etat: "a_jour",
        raison: null,
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    expect((await screen.findByRole("status")).textContent).toBe("À jour.");
  });

  it("met à jour la ligne de statut après un forçage réussi", async () => {
    memoireReponse = {
      elements: [],
      synthese: null,
      entretien: {
        tours: 2,
        seuil_tours: 5,
        inactivite_s: 600,
        seuil_inactivite_s: 300,
        etat: "en_attente",
        raison: null,
      },
    };
    reponseForcer = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 1 },
      ],
      synthese: null,
      entretien: {
        tours: 5,
        seuil_tours: 5,
        inactivite_s: null,
        seuil_inactivite_s: 300,
        etat: "a_jour",
        raison: null,
      },
      rafraichissement: { statut: "effectue" },
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByRole("status");

    fireEvent.click(screen.getByRole("button", { name: "Forcer la mémoire" }));

    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("À jour."));
  });

  it("force l'entretien, relit les fiches et dit ce qui a été fait", async () => {
    memoireReponse = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 1 },
      ],
      synthese: null,
    };
    reponseForcer = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 1 },
        { entry_id: "mem_2", content: "La charte graphique est validée.", status: "accepted", version: 1 },
      ],
      synthese: { content: "Lancer en septembre.", version: 2 },
      rafraichissement: {
        statut: "effectue",
        fiches: { crees: 2, mis_a_jour: 1 },
        synthese: { statut: "creee" },
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByText("Lancer le site en septembre.");

    fireEvent.click(screen.getByRole("button", { name: "Forcer la mémoire" }));

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_memoire_forcer")).toHaveLength(1);
    });
    // L'appel nomme l'espace : un entretien sans portée serait un entretien
    // sur le mauvais espace ou sur aucun.
    expect(invokes.find((i) => i.cmd === "espace_memoire_forcer")?.args).toEqual({
      workspaceId: "ws_1",
    });
    // La liste vient de la RELECTURE du service, pas d'une mutation locale.
    expect(await screen.findByText("La charte graphique est validée.")).toBeTruthy();
    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_memoire").length).toBeGreaterThanOrEqual(2);
    });
    const toast = showToast.mock.calls[0][0] as { type: string; title: string; message: string };
    expect(toast.type).toBe("success");
    expect(toast.title).toBe("Mémoire entretenue");
    expect(toast.message).toMatch(/2 fiche\(s\) créée\(s\)/);
    expect(toast.message).toMatch(/1 mise\(s\) à jour/);
    expect(toast.message).toMatch(/synthèse a été mise à jour/);
  });

  it("filtre les fiches sur le texte et le statut, sans nouvel appel", async () => {
    memoireReponse = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", origin: "Discussion" },
        { entry_id: "mem_2", content: "Choisir une police.", status: "candidate", origin: "Discussion" },
        { entry_id: "mem_3", content: "L'ancien logo reste en place.", status: "stale", origin: "Document" },
      ],
      synthese: null,
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByText("Lancer le site en septembre.");
    expect(screen.getByText("3 fiches")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Filtrer par contenu ou origine"), {
      target: { value: "logo" },
    });
    await waitFor(() => {
      expect(screen.queryByText("Lancer le site en septembre.")).toBeNull();
    });
    expect(screen.getByText("L'ancien logo reste en place.")).toBeTruthy();
    expect(screen.getByText("1 fiche")).toBeTruthy();

    // Le statut est un second critère, appliqué en client : le payload contient
    // déjà toutes les fiches, et filtrer ne rappelle pas le service.
    fireEvent.change(screen.getByLabelText("Filtrer par contenu ou origine"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("Filtrer par statut"), {
      target: { value: "candidate" },
    });
    await waitFor(() => {
      expect(screen.queryByText("Lancer le site en septembre.")).toBeNull();
    });
    expect(screen.getByText("Choisir une police.")).toBeTruthy();
    expect(screen.queryByText("L'ancien logo reste en place.")).toBeNull();
    expect(invokes.filter((i) => i.cmd === "espace_memoire")).toHaveLength(1);
  });

  it("corrige une fiche en ligne avec sa version attendue", async () => {
    memoireReponse = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 3 },
      ],
      synthese: null,
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByText("Lancer le site en septembre.");

    fireEvent.click(screen.getByRole("button", { name: "Corriger" }));
    const champ = screen.getByLabelText("Contenu corrigé de la fiche") as HTMLTextAreaElement;
    expect(champ.value).toBe("Lancer le site en septembre.");
    // Le bouton s'ouvre désactivé : un contenu inchangé n'est pas une correction.
    expect((screen.getByRole("button", { name: "Enregistrer" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(champ, { target: { value: "Lancer le site en octobre." } });
    fireEvent.click(screen.getByRole("button", { name: "Enregistrer" }));

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_memoire_corriger")).toHaveLength(1);
    });
    // `expectedVersion` est la version lue : le service peut refuser une
    // correction bâtie sur une fiche qui a changé entre-temps.
    expect(invokes.find((i) => i.cmd === "espace_memoire_corriger")?.args).toEqual({
      workspaceId: "ws_1",
      entryId: "mem_1",
      content: "Lancer le site en octobre.",
      expectedVersion: 3,
    });
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: "success", title: "Fiche corrigée" }),
    );
    expect(await screen.findByText("Lancer le site en octobre.")).toBeTruthy();
    expect(screen.queryByLabelText("Contenu corrigé de la fiche")).toBeNull();
  });

  it("garde l'éditeur ouvert quand la correction est un conflit de révision", async () => {
    correctionResultat = { ok: false, code: "REVISION_CONFLICT" };
    memoireReponse = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 3 },
      ],
      synthese: null,
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByText("Lancer le site en septembre.");

    fireEvent.click(screen.getByRole("button", { name: "Corriger" }));
    const champ = screen.getByLabelText("Contenu corrigé de la fiche") as HTMLTextAreaElement;
    fireEvent.change(champ, { target: { value: "Lancer le site en octobre." } });
    fireEvent.click(screen.getByRole("button", { name: "Enregistrer" }));

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_memoire_corriger")).toHaveLength(1);
    });
    await waitFor(() => {
      const appels = showToast.mock.calls;
      const toast = appels[appels.length - 1]?.[0] as { type: string; title: string };
      expect(toast.type).toBe("error");
      expect(toast.title).toBe("Correction impossible");
    });
    // La saisie n'est pas perdue et aucun succès n'est annoncé.
    expect(screen.getByLabelText("Contenu corrigé de la fiche")).toBeTruthy();
    expect(showToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Fiche corrigée" }),
    );
  });

  it("supprime une fiche seulement après confirmation explicite", async () => {
    memoireReponse = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 1 },
      ],
      synthese: null,
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByText("Lancer le site en septembre.");

    fireEvent.click(screen.getByRole("button", { name: "Supprimer" }));
    expect(
      screen.getByText(/Supprimer définitivement \? Elle ne sera pas recréée automatiquement à partir du même contenu\./),
    ).toBeTruthy();
    // Rien n'est supprimé au premier clic : la confirmation est un état, pas un
    // `window.confirm` que le test ne pourrait pas piloter.
    expect(invokes.filter((i) => i.cmd === "espace_memoire_supprimer")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Confirmer" }));
    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_memoire_supprimer")).toHaveLength(1);
    });
    expect(invokes.find((i) => i.cmd === "espace_memoire_supprimer")?.args).toEqual({
      workspaceId: "ws_1",
      entryId: "mem_1",
    });
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: "success", title: "Fiche supprimée" }),
    );
    await waitFor(() => {
      expect(screen.queryByText("Lancer le site en septembre.")).toBeNull();
    });
  });

  it("affiche la synthèse courante avec sa version et sa date", async () => {
    memoireReponse = {
      elements: [],
      synthese: {
        content: "Lancer le site en septembre, charte validée.",
        version: 2,
        created_at: "2024-05-20T09:00:00Z",
      },
    };
    rendreEspace("/espaces/ws_1/memoire");
    expect(await screen.findByRole("heading", { name: "Synthèse d'espace" })).toBeTruthy();
    expect(screen.getByText("Lancer le site en septembre, charte validée.")).toBeTruthy();
    expect(screen.getByText(/v2/)).toBeTruthy();
    expect(screen.getByText(/20 mai 2024/)).toBeTruthy();
  });

  it("n'invente aucune synthèse quand le service n'en fournit pas", async () => {
    memoireReponse = { elements: [], synthese: null };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByRole("heading", { name: "Mémoire" });
    expect(screen.queryByRole("heading", { name: "Synthèse d'espace" })).toBeNull();
  });

  it("n'envoie qu'un seul entretien sur un double clic", async () => {
    let finir: (valeur: unknown) => void = () => {};
    forcerEnVol = new Promise((resoudre) => { finir = resoudre; });
    rendreEspace("/espaces/ws_1/memoire");
    const bouton = await screen.findByRole("button", { name: "Forcer la mémoire" });

    fireEvent.click(bouton);
    fireEvent.click(bouton);
    await act(async () => { await Promise.resolve(); });
    expect(invokes.filter((i) => i.cmd === "espace_memoire_forcer")).toHaveLength(1);
    expect((bouton as HTMLButtonElement).disabled).toBe(true);

    await act(async () => {
      finir({
        elements: [],
        synthese: null,
        rafraichissement: { statut: "rien_a_promouvoir" },
      });
      await Promise.resolve();
    });
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: "info", message: "Aucun nouveau fait à retenir." }),
    );
  });

  it("n'affiche pas l'entretien d'un autre espace, mais désactive le forçage", async () => {
    memoireReponse = { elements: [], synthese: null, entretien: null };
    await rendreEspaceAvecProvider("/espaces/ws_1/memoire");
    await screen.findByRole("heading", { name: "Mémoire" });

    fire("memory_started", {
      workspace_id: "ws_autre",
      workspace_name: "Autre espace",
      kind: "memory",
      mode: "auto",
    });

    await waitFor(() => {
      const bouton = screen.getByRole("button", { name: "Forcer la mémoire" }) as HTMLButtonElement;
      // Le verrou est global : un entretien sur un autre espace interdit aussi
      // de forcer ici, mais la ligne de statut ne parle que de l'espace courant.
      expect(bouton.disabled).toBe(true);
      expect(bouton.getAttribute("title")).toBe("Entretien mémoire en cours");
    });
    expect(screen.queryByText(/Entretien mémoire en cours/)).toBeNull();
  });

  it("garde la fiche quand la suppression est différée par un entretien", async () => {
    suppressionResultat = { ok: false, statut: "differe" };
    memoireReponse = {
      elements: [
        { entry_id: "mem_1", content: "Lancer le site en septembre.", status: "accepted", version: 1 },
      ],
      synthese: null,
    };
    rendreEspace("/espaces/ws_1/memoire");
    await screen.findByText("Lancer le site en septembre.");

    fireEvent.click(screen.getByRole("button", { name: "Supprimer" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirmer" }));

    await waitFor(() => {
      expect(invokes.filter((i) => i.cmd === "espace_memoire_supprimer")).toHaveLength(1);
    });
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "error",
          message: "Entretien mémoire en cours, réessayez.",
        }),
      );
    });
    // Rien n'a été supprimé : la fiche reste listée et aucun succès n'est annoncé.
    expect(screen.getByText("Lancer le site en septembre.")).toBeTruthy();
    expect(showToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Fiche supprimée" }),
    );
  });
});

describe("Espace — robustesse", () => {
  it("refuse un onglet inconnu au lieu d'afficher un écran vide", async () => {
    rendreEspace("/espaces/ws_1/nimporte-quoi");
    // Retombe sur Discussions, pas sur un onglet muet.
    expect(await screen.findByText("Structure de la page d'accueil")).toBeTruthy();
  });

  it("annonce un espace en corbeille au lieu de laisser croire qu'il est normal", async () => {
    getWorkspace.mockResolvedValue({
      workspace: { ...ESPACE, state: "trashed" },
      stats: stats(),
    });
    const { container } = rendreEspace();
    // Atteignable pour être restauré, donc lisible — mais l'état doit se voir.
    expect(await screen.findByText("En corbeille")).toBeTruthy();
    expect(container.textContent).toContain("Refonte du site web");
    // Le serveur refuse toute création : un bouton actif n'annoncerait qu'un échec.
    const bouton = await screen.findByRole("button", { name: /Nouvelle discussion/ });
    expect((bouton as HTMLButtonElement).disabled).toBe(true);
  });

  it("affiche un refus lisible quand l'espace est introuvable", async () => {
    getWorkspace.mockRejectedValue(new Error("[WORKSPACE_UNKNOWN] Espace introuvable."));
    const { container } = rendreEspace();
    const alerte = await screen.findByRole("alert");
    // Ni un espace vide, ni « aucune discussion » : ce serait lu comme une perte.
    expect(alerte.textContent).toMatch(/introuvable/);
    expect(container.textContent).not.toMatch(/Aucune discussion/);
  });
});

describe("Mise en forme", () => {
  it("formate les octets avec l'unité et la virgule française", () => {
    expect(formatOctets(0)).toBe("0 o");
    expect(formatOctets(512)).toBe("512 o");
    expect(formatOctets(2048)).toBe("2,0 ko");
    expect(formatOctets(245 * 1024 * 1024)).toBe("245,0 Mo");
    expect(formatOctets(3 * 1024 * 1024 * 1024)).toBe("3,0 Go");
  });

  it("ne segmente pas au-delà de Go", () => {
    // Trois unités au plus, donc 4 To s'affichent en Go et non en To : le
    // plafond d'unité est un choix d'affichage, pas un plafond de QUOTA.
    expect(formatOctets(1024 ** 4)).toBe("1024,0 Go");
  });

  it("résiste à une valeur invalide au lieu d'afficher NaN", () => {
    expect(formatOctets(Number.NaN)).toBe("0 o");
    expect(formatOctets(-5)).toBe("0 o");
    expect(formatNombre(Number.NaN)).toBe("0");
  });

  it("donne une teinte stable et dérivée de l'identité", () => {
    expect(teinteEspace("ws_1")).toBe(teinteEspace("ws_1"));
    expect(teinteEspace("ws_1")).not.toBe(teinteEspace("ws_2"));
    expect(teinteEspace("")).toMatch(/^rgba\(/);
  });
});


// Le bouton « Retour » d'une discussion ouverte depuis un espace doit ramener à
// l'ESPACE. Sans l'etat de navigation, l'utilisateur quittait la discussion et
// retombait sur l'accueil du chat, perdant le contexte.
describe("Espace -> discussion : le retour garde le contexte", () => {
  it("transmet l'identifiant de l'espace dans l'etat de navigation", async () => {
    const vus: Array<{ chemin: string; etat: unknown }> = [];
    function SondeConversation() {
      const location = useLocation();
      vus.push({ chemin: location.pathname, etat: location.state });
      return <span>sonde</span>;
    }
    render(
      <MemoryRouter initialEntries={["/espaces/ws_1/discussions"]}>
        <Routes>
          <Route path="/espaces/:id/:onglet" element={<EspacePage />} />
          <Route path="/chat/:id" element={<SondeConversation />} />
        </Routes>
      </MemoryRouter>,
    );
    const titre = await screen.findByText("Structure de la page d'accueil");
    fireEvent.click(titre);
    await waitFor(() => expect(vus.length).toBeGreaterThan(0));
    expect(vus[0].chemin).toBe("/chat/conv_1");
    expect(vus[0].etat).toEqual({ workspaceId: "ws_1" });
  });
});

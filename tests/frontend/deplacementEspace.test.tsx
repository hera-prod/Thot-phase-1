// Le pont du déplacement de conversation — ce que le renderer ENVOIE, dans l'ordre.
//
// Le parcours de `docs/intentions/fonctionnalites/espaces.md` §7.3 est un deux-temps :
// une évaluation (rien ne bouge), puis l'application. Un pont qui inverserait ces
// temps, ou qui écrirait `confirme` au mauvais appel, transformerait une lecture en
// écriture de rattachement sans que l'écran ni le service le feintent. Ces tests
// vérifient donc le canal, pas l'écran : `invoke` est observé, jamais imité.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invokes: Array<{ cmd: string; args: Record<string, unknown> }> = [];

const CONVERSATION = {
  id: "conv_a",
  title: "Projet Alpha",
  mode: "chat",
  workspace_id: "ws_a",
  workspace_name: "Alpha",
  updated_at: "2025-01-01T10:00:00.000Z",
  last_message_preview: "Où en est le projet ?",
  message_count: 3,
};

const ESPACES = [
  { id: "ws_a", name: "Alpha", state: "active", stats: {} },
  { id: "ws_b", name: "Beta", state: "active", stats: {} },
  { id: "ws_c", name: "Corbeille", state: "trashed", stats: {} },
];

const EVALUATION = {
  ok: true,
  allowed: true,
  conversation_id: "conv_a" as string | null,
  source_workspace_id: "ws_a",
  target_workspace_id: "ws_b",
  blocking: [],
  lost_links: [
    { artifact_id: "art_2", display_name: "contrat.pdf", reason: "dépôt de l'origine" },
  ],
  contributions: ["art_1"],
  syntheses_a_invalider: ["mem_1"],
  artifacts_moved: 0,
  changed: true,
  note: "1 contribution(s) suivent la discussion",
};

vi.mock("@/lib/ipc", () => ({
  invoke: vi.fn((cmd: string, args: Record<string, unknown>) => {
    invokes.push({ cmd, args });
    // Le pont lit les espaces et les conversations par les mêmes commandes que
    // l'écran : brancher par commande est la seule façon de tester le parcours
    // complet sans substituer les fonctions du pont.
    if (cmd === "list_conversations") return Promise.resolve([CONVERSATION]);
    if (cmd === "espace_list") return Promise.resolve(ESPACES);
    if (cmd === "espace_conversation_deplacer") {
      if (args.confirme === true) {
        return Promise.resolve({
          ...EVALUATION,
          applied: true,
          note: "rattachement seul ; aucun fichier, id, chunk ou vecteur touché",
        });
      }
      return Promise.resolve(EVALUATION);
    }
    return Promise.resolve(undefined);
  }),
  listen: vi.fn(() => Promise.resolve(() => {})),
  pathForFile: vi.fn(() => null),
}));

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import {
  deplacerConversationEspace,
  evaluerDeplacementConversation,
} from "@/lib/ipcBridge";
import { DeplacerDiscussionDialog } from "@/components/espaces/DeplacerDiscussionDialog";
import { ChatHome } from "@/pages/chat/ChatHome";
import { AttachmentDraftProvider } from "@/features/attachments/AttachmentDraftContext";

beforeEach(() => {
  invokes.length = 0;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Déplacement d'une discussion entre espace — le pont", () => {
  it("l'évaluation appelle le verbe SANS `confirme` : rien ne peut bouger", async () => {
    await evaluerDeplacementConversation("conv_a", "ws_b");
    expect(invokes).toEqual([{
      cmd: "espace_conversation_deplacer",
      args: { conversationId: "conv_a", targetWorkspaceId: "ws_b" },
    }]);
    expect(invokes[0].args.confirme).toBeUndefined();
  });

  it("l'application écrit `confirme: true` DANS l'appel", async () => {
    await deplacerConversationEspace("conv_a", "ws_b");
    expect(invokes).toEqual([{
      cmd: "espace_conversation_deplacer",
      args: { conversationId: "conv_a", targetWorkspaceId: "ws_b", confirme: true },
    }]);
  });

  it("les arguments entrants sont en camelCase (convention du transport)", async () => {
    await evaluerDeplacementConversation("conv_a", "ws_b");
    expect(Object.keys(invokes[0].args)).toContain("conversationId");
    expect(Object.keys(invokes[0].args)).toContain("targetWorkspaceId");
  });
});

describe("Déplacement — le dialogue et l'écran d'accueil", () => {
  it("ne propose que les destinations actives, hors espace d'origine", async () => {
    render(
      <DeplacerDiscussionDialog
        conversationId="conv_a"
        conversationTitle="Projet Alpha"
        sourceWorkspaceId="ws_a"
        sourceWorkspaceName="Alpha"
        onClose={vi.fn()}
        onMoved={vi.fn()}
      />,
    );

    const dialogue = await screen.findByRole("dialog");
    const choix = await screen.findByRole("combobox");
    // L'espace d'origine et la corbeille ne sont pas des destinations.
    expect(within(choix).queryByRole("option", { name: "Alpha" })).toBeNull();
    expect(within(choix).queryByRole("option", { name: "Corbeille" })).toBeNull();
    expect(within(choix).getByRole("option", { name: "Beta" })).toBeTruthy();
    expect(dialogue.textContent).toContain("Projet Alpha");
    // « Continuer » n'est pas une écriture : sans cible, il ne fait rien.
    expect(
      (screen.getByRole("button", { name: "Continuer" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("évalue sans `confirme`, montre les conséquences, puis applique avec `confirme`", async () => {
    const onMoved = vi.fn();
    const onClose = vi.fn();
    render(
      <DeplacerDiscussionDialog
        conversationId="conv_a"
        conversationTitle="Projet Alpha"
        sourceWorkspaceId="ws_a"
        sourceWorkspaceName="Alpha"
        onClose={onClose}
        onMoved={onMoved}
      />,
    );

    const choix = (await screen.findByRole("combobox")) as HTMLSelectElement;
    fireEvent.change(choix, { target: { value: "ws_b" } });
    fireEvent.click(screen.getByRole("button", { name: "Continuer" }));

    // Le rapport d'évaluation, et lui seul : `confirme` n'est pas dans l'appel.
    await waitFor(() => {
      const appels = invokes.filter((i) => i.cmd === "espace_conversation_deplacer");
      expect(appels).toHaveLength(1);
      expect(appels[0].args).toEqual({
        conversationId: "conv_a",
        targetWorkspaceId: "ws_b",
      });
    });

    expect(await screen.findByText("De « Alpha » vers « Beta ».")).toBeTruthy();
    expect(screen.getByText("1 fichier lié suivra la discussion")).toBeTruthy();
    expect(
      screen.getByText("1 fichier du dépôt de l'espace d'origine reste dans cet espace"),
    ).toBeTruthy();
    expect(screen.getByText("« contrat.pdf »")).toBeTruthy();
    expect(
      screen.getByText(
        "1 fiche de mémoire de l'espace d'origine sera marquée à réviser",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Aucun fichier n'est copié ni supprimé.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Déplacer" }));

    await waitFor(() =>
      expect(onMoved).toHaveBeenCalledWith("ws_b", "Beta"),
    );
    const appels = invokes.filter((i) => i.cmd === "espace_conversation_deplacer");
    expect(appels).toHaveLength(2);
    expect(appels[1].args).toEqual({
      conversationId: "conv_a",
      targetWorkspaceId: "ws_b",
      confirme: true,
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("ouvre le dialogue depuis le menu de la carte d'accueil", async () => {
    render(
      <MemoryRouter>
        <AttachmentDraftProvider>
          <ChatHome />
        </AttachmentDraftProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Projet Alpha")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    fireEvent.click(screen.getByRole("button", { name: "Déplacer vers un espace…" }));

    const dialogue = await screen.findByRole("dialog");
    expect(dialogue.textContent).toContain("Projet Alpha");
    const choix = await screen.findByRole("combobox");
    expect(within(choix).getByRole("option", { name: "Beta" })).toBeTruthy();
    expect(within(choix).queryByRole("option", { name: "Alpha" })).toBeNull();
    expect(within(choix).queryByRole("option", { name: "Corbeille" })).toBeNull();
  });
});

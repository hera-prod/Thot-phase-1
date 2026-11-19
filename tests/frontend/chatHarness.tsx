// Harnais de rendu de ChatConversation pour les tests : mocks du pont (ipc + event),
// routeur mémoire et provider d'attachements. Réutilisable par tous les tests qui ont
// besoin de la vraie page (bulles, progression, images générées).
import { vi } from 'vitest';
import { act, render, waitFor, type RenderResult } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { AttachmentDraftProvider } from '@/features/attachments/AttachmentDraftContext';
import { InferenceActivityProvider } from '@/features/inference/InferenceActivityContext';
import { ChatConversation } from '@/pages/chat/ChatConversation';

export type ChatListener = (event: { payload: Record<string, unknown> }) => void;

export interface ChatHistoryAttachment { id: string; kind: string; name: string; mime: string }

export interface ChatHistoryMessage {
  id: string;
  role: string;
  content: string;
  attachments?: ChatHistoryAttachment[];
}

const chatListeners: Record<string, ChatListener[]> = {};
const chatInvocations: Array<{ cmd: string; args: unknown }> = [];
let chatHistory: ChatHistoryMessage[] = [];
let chatHistoryFailure: Error | null = null;
let chatBytesFailure: Error | null = null;
let chatPendingBytes: Array<(value: [number[], string]) => void> | null = null;
let chatPendingHistory: Array<(value: ChatHistoryMessage[]) => void> | null = null;
let chatPendingConversation: Array<(value: unknown) => void> | null = null;
let objectUrlCounter = 0;
/** Réponse (ou refus) du versement au dépôt ; undefined = pont muet. */
let partageReponse: unknown = undefined;
/** Titre réel chargé via get_conversation ; null = pas de titre côté mock. */
let chatConversationTitle: string | null = null;
let chatRenameFailure: Error | null = null;
const toastCalls: Array<{ type: string; title: string; message?: string }> = [];

export function configurerPartage(reponse: unknown): void {
  partageReponse = reponse;
}

export function configurerTitreConversation(title: string | null): void {
  chatConversationTitle = title;
}

export function failConversationRename(message = 'renommage impossible'): void {
  chatRenameFailure = new Error(message);
}

/** Les toasts émis par la page (le conteneur réel n'est pas monté ici). */
export function toastsEmis(): Array<{ type: string; title: string; message?: string }> {
  return toastCalls;
}

vi.mock('@/components/ui/Toast', () => ({
  showToast: (toast: { type: string; title: string; message?: string }) => {
    toastCalls.push(toast);
  },
}));

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    chatInvocations.push({ cmd, args });
    if (cmd === 'list_messages') {
      if (chatPendingHistory !== null) {
        return new Promise<ChatHistoryMessage[]>((resolve) => chatPendingHistory?.push(resolve));
      }
      if (chatHistoryFailure) {
        const failure = chatHistoryFailure;
        // Une seule panne : le test « Réessayer » doit pouvoir réussir ensuite.
        chatHistoryFailure = null;
        return Promise.reject(failure);
      }
      return Promise.resolve(chatHistory);
    }
    if (cmd === 'get_conversation') {
      if (chatPendingConversation !== null) {
        return new Promise<unknown>((resolve) => chatPendingConversation?.push(resolve));
      }
      const conversationId = (args as { conversationId?: string } | undefined)?.conversationId ?? 'conv-thot-1';
      return Promise.resolve(chatConversationTitle === null ? null : { id: conversationId, title: chatConversationTitle, mode: 'chat' });
    }
    if (cmd === 'rename_conversation') {
      if (chatRenameFailure) {
        const failure = chatRenameFailure;
        chatRenameFailure = null;
        return Promise.reject(failure);
      }
      const payload = args as { conversationId?: string; title?: string } | undefined;
      chatConversationTitle = payload?.title ?? null;
      return Promise.resolve({ ok: true, id: payload?.conversationId, title: payload?.title });
    }
    if (cmd === 'espace_conversation_partager') return Promise.resolve(partageReponse);
    if (cmd === 'get_attachment_bytes') {
      if (chatPendingBytes !== null) {
        return new Promise<[number[], string]>((resolve) => chatPendingBytes?.push(resolve));
      }
      if (chatBytesFailure) return Promise.reject(chatBytesFailure);
      return Promise.resolve([[137, 80, 78, 71], 'image/png']);
    }
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: ChatListener) => {
    chatListeners[event] = [...(chatListeners[event] ?? []), callback];
    let active = true;
    return Promise.resolve(() => {
      if (!active) return;
      active = false;
      chatListeners[event] = (chatListeners[event] ?? []).filter((entry) => entry !== callback);
    });
  }),
}));

;

/** Remet le harnais à zéro et installe des objectURL déterministes. */
export function resetChat(): void {
  chatInvocations.length = 0;
  chatHistory = [];
  chatHistoryFailure = null;
  chatBytesFailure = null;
  chatPendingBytes = null;
  chatPendingHistory = null;
  chatPendingConversation = null;
  objectUrlCounter = 0;
  partageReponse = undefined;
  chatConversationTitle = null;
  chatRenameFailure = null;
  toastCalls.length = 0;
  Object.keys(chatListeners).forEach((key) => delete chatListeners[key]);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:thot-${(objectUrlCounter += 1)}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
  // jsdom n'implémente pas Element.scrollTo, appelé par la page à chaque rendu.
  Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, value: vi.fn() });
}

/** Les octets d'artefact ne partiront plus : ils attendent un `resolvePendingBytes`. */
export function deferAttachmentBytes(): void {
  chatPendingBytes = [];
}

/** `list_messages` ne répondra plus : attendre `resolvePendingHistory`. */
export function deferHistory(): void {
  chatPendingHistory = [];
}

export function pendingHistoryRequests(): number {
  return chatPendingHistory?.length ?? 0;
}

/** `get_conversation` ne répondra plus : attendre `resolvePendingConversation`. */
export function deferConversationTitle(): void {
  chatPendingConversation = [];
}

export function pendingConversationRequests(): number {
  return chatPendingConversation?.length ?? 0;
}

export async function resolvePendingConversation(title: string | null): Promise<void> {
  const resolvers = [...(chatPendingConversation ?? [])];
  chatPendingConversation = null;
  await act(async () => {
    resolvers.forEach((resolve) => resolve(title === null ? null : { id: 'conv-thot-1', title, mode: 'chat' }));
    await Promise.resolve();
  });
}

export async function resolvePendingHistory(history: ChatHistoryMessage[] = []): Promise<void> {
  const resolvers = [...(chatPendingHistory ?? [])];
  chatPendingHistory = null;
  await act(async () => {
    resolvers.forEach((resolve) => resolve(history));
    await Promise.resolve();
  });
}

export function failAttachmentBytes(message = 'octets illisibles'): void {
  chatBytesFailure = new Error(message);
}

/** Fait échouer le prochain `list_messages` (une seule fois, pour tester « Réessayer »). */
export function failHistory(message = 'historique illisible'): void {
  chatHistoryFailure = new Error(message);
}

export function pendingByteRequests(): number {
  return chatPendingBytes?.length ?? 0;
}

export async function resolvePendingBytes(): Promise<void> {
  const resolvers = [...(chatPendingBytes ?? [])];
  chatPendingBytes = null;
  await act(async () => {
    resolvers.forEach((resolve) => resolve([[137, 80, 78, 71], 'image/png']));
    await Promise.resolve();
  });
}

export function fireChatEvent(name: string, payload: Record<string, unknown>): void {
  const listeners = chatListeners[name] ?? [];
  if (listeners.length === 0) throw new Error(`Aucun listener pour ${name}`);
  act(() => { listeners.forEach((listener) => listener({ payload })); });
}

/** La page enregistre ses écouteurs en série : on attend ceux dont le test a besoin. */
export async function waitForChatListeners(...names: string[]): Promise<void> {
  await waitFor(() => {
    const missing = names.filter((name) => !chatListeners[name] || chatListeners[name].length === 0);
    if (missing.length > 0) throw new Error(`Listeners absents : ${missing.join(', ')}`);
  });
}

export function invocationsOf(cmd: string): Array<{ cmd: string; args: unknown }> {
  return chatInvocations.filter((entry) => entry.cmd === cmd);
}

export function renderChatConversation(options: {
  conversationId?: string;
  history?: ChatHistoryMessage[];
  locationState?: unknown;
} = {}): RenderResult {
  chatHistory = options.history ?? [];
  const conversationId = options.conversationId ?? 'conv-thot-1';
  // L'état de navigation est celui qu'EspacePage dépose en ouvrant la discussion.
  const initialEntries = [{ pathname: `/chat/${conversationId}`, state: options.locationState ?? null }];
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <AttachmentDraftProvider>
        <InferenceActivityProvider>
          <Routes>
            <Route path="/chat/:id" element={<ChatConversation />} />
            <Route path="/gallery" element={<div>Galerie</div>} />
          </Routes>
        </InferenceActivityProvider>
      </AttachmentDraftProvider>
    </MemoryRouter>,
  );
}

/** Rend la conversation avec deux routes atteignables sans remontage du composant. */
export function renderChatConversationWithNavigation(options: {
  firstId?: string;
  secondId?: string;
} = {}): RenderResult {
  const firstId = options.firstId ?? 'conv-a';
  const secondId = options.secondId ?? 'conv-b';
  function Navigator() {
    const navigate = useNavigate();
    return (
      <div>
        <button type="button" onClick={() => navigate(`/chat/${firstId}`)}>Aller à A</button>
        <button type="button" onClick={() => navigate(`/chat/${secondId}`)}>Aller à B</button>
      </div>
    );
  }
  return render(
    <MemoryRouter initialEntries={[`/chat/${firstId}`]}>
      <AttachmentDraftProvider>
        <InferenceActivityProvider>
          <Navigator />
          <Routes>
            <Route path="/chat/:id" element={<ChatConversation />} />
          </Routes>
        </InferenceActivityProvider>
      </AttachmentDraftProvider>
    </MemoryRouter>,
  );
}

/** Les blocs de tour de la colonne de conversation, dans l'ordre d'affichage. */export function conversationTurns(container: HTMLElement): HTMLElement[] {
  const column = container.querySelector('.chatColumnInner');
  if (!column) throw new Error('Colonne de conversation introuvable');
  return Array.from(column.children) as HTMLElement[];
}

/** Index du tour qui contient l'élément donné (−1 si aucun). */
export function turnIndexOf(container: HTMLElement, element: HTMLElement): number {
  return conversationTurns(container).findIndex((turn) => turn.contains(element));
}

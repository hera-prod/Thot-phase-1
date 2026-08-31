// Wrappers Tauri — invoke + listen typés avec Zod (Phase 1)
// But : cacher les détails Tauri et valider les payloads reçus

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { MessageStarted, Thinking, ToolCall, ToolResult, Answer, MessageCompleted, ErrorEvent, BackendReady } from "../types/ipc";

// === INVOKE : Frontend → Rust → Python (stdin) ===

export async function chatSend(params: {
  request_id: string;      // UUID généré côté frontend pour corréler les events
  message: string;
  mode: "chat" | "agent";  // chat = sans outils, agent = vision+OCR
  image_paths?: string[];   // chemins /tmp/thot/{id}/{hash}.png déjà validés par save_upload
  conv_id?: string;         // id de conversation pour persistance DB
}) {
  // Tauri 2 mappe Rust snake_case -> JS camelCase (request_id -> requestId)
  // cf. erreur Réponses.md : `invalid args requestId missing required key requestId`
  return invoke("chat_send", {
    requestId: params.request_id,
    message: params.message,
    mode: params.mode,
    imagePaths: params.image_paths,
    convId: params.conv_id,
  } as unknown as Record<string, unknown>);
}

export async function saveUpload(request_id: string, filename: string, data: Uint8Array): Promise<[string, string]> {
  // Envoie le binaire au Rust qui valide magic bytes + taille puis écrit dans /tmp/thot/{id}/
  // Retourne [path, hash] ex: ["/tmp/thot/uuid/abc.png", "sha256:abc..."]
  return invoke<[string, string]>("save_upload", {
    requestId: request_id,
    filename,
    data: Array.from(data), // Tauri sérialise en Vec<u8> côté Rust
  } as unknown as Record<string, unknown>);
}

export interface ConversationSummary {
  id: string;
  title: string;
  mode: string;
  created_at?: string;
  updated_at?: string;
  last_message_preview?: string;
  message_count?: number;
}

export async function listConversations(limit = 50, offset = 0): Promise<ConversationSummary[]> {
  try {
    return await invoke<ConversationSummary[]>("list_conversations", { limit, offset } as unknown as Record<string, unknown>);
  } catch {
    return [];
  }
}

export async function getConversation(conversationId: string): Promise<ConversationSummary | null> {
  try {
    return await invoke<ConversationSummary | null>("get_conversation", { conversationId } as unknown as Record<string, unknown>);
  } catch {
    return null;
  }
}

export async function listMessages(conversationId: string): Promise<Array<{ id: string; role: string; content: string }>> {
  try {
    return await invoke("list_messages", { conversationId } as unknown as Record<string, unknown>);
  } catch {
    return [];
  }
}

// === LISTEN : Python → Rust → Frontend (stdout JSON Lines → window.emit) ===
// Chaque listen retourne une fonction unlisten() à appeler au démontage du composant

export function onMessageStarted(cb: (p: MessageStarted) => void) { return listen<MessageStarted>("message_started", e => cb(e.payload)); }
export function onThinking(cb: (p: Thinking) => void) { return listen<Thinking>("thinking", e => cb(e.payload)); }
export function onToolCall(cb: (p: ToolCall) => void) { return listen<ToolCall>("tool_call", e => cb(e.payload)); }
export function onToolResult(cb: (p: ToolResult) => void) { return listen<ToolResult>("tool_result", e => cb(e.payload)); }
export function onAnswer(cb: (p: Answer) => void) { return listen<Answer>("answer", e => cb(e.payload)); }
export function onMessageCompleted(cb: (p: MessageCompleted) => void) { return listen<MessageCompleted>("message_completed", e => cb(e.payload)); }
export function onError(cb: (p: ErrorEvent) => void) { return listen<ErrorEvent>("error", e => cb(e.payload)); }
export function onBackendReady(cb: (p: BackendReady) => void) { return listen<BackendReady>("backend_ready", e => cb(e.payload)); }

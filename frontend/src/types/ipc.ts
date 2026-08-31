// Types IPC — payloads des 8 events (validés Zod côté main, ici types TS simples)
// Chaque event contient toujours request_id pour corréler la requête

export type MessageStarted = { request_id: string; run_id: string; model: string };
export type Thinking = { request_id: string; text: string; iteration: number };
export type ToolCall = { request_id: string; tool_call_id: string; tool: "vision" | "doc_ocr"; arguments: Record<string, unknown> };
export type ToolResult = { request_id: string; tool_call_id: string; result: string; ok: boolean; latency_ms: number };
export type Answer = { request_id: string; text: string; content?: string };
export type MessageCompleted = { request_id: string; run_id: string; tokens_in: number; tokens_out: number; total_latency_ms: number };
export type ErrorEvent = { request_id: string; code: string; message: string };
export type BackendReady = { backend: string; vram: number; n_gpu_layers: number };

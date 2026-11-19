// Types IPC — payloads des 9 events (validés Zod côté main, ici types TS simples)
// Chaque event contient toujours request_id pour corréler la requête

export type MessageStarted = { request_id: string; run_id: string; model: string };
export type Thinking = { request_id: string; text: string; iteration: number };
export type ToolCall = { request_id: string; tool_call_id: string; tool: string; arguments: Record<string, unknown> };
// `evidence_ids` / `public_ids` : les identifiants que le backend calcule déjà
// (loop.py) et que l'interface n'avait aucun canal typé pour recevoir. Ils sont
// optionnels parce que seuls les outils qui produisent des preuves les rendent —
// un `find_files` n'a pas de source à citer.
export type ToolResult = {
  request_id: string;
  tool_call_id: string;
  result: string;
  ok: boolean;
  latency_ms: number;
  evidence_ids?: string[];
  public_ids?: string[];
};
export type Answer = { request_id: string; text: string; content?: string };
export type MessageCompleted = { request_id: string; run_id: string; tokens_in: number; tokens_out: number; total_latency_ms: number };
export type ErrorEvent = { request_id: string; code: string; message: string };
export type BackendReady = { backend: string; vram: number; n_gpu_layers: number };
// Handoff orchestrateur→générateur : le brief exact transmis (bulle Orchestrateur, repliée par défaut)
// thinking = contenus des itérations (remplace les labels de statut accumulés en live)
export type OrchestratorBrief = { request_id: string; brief: string; brief_chars: number; tool_calls: number; orchestrator_done: boolean; thinking?: string };

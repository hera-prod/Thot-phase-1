// Une seule inférence lourde à la fois (discussion ou image). Le backend sérialise
// déjà sur son worker IPC ; ce contexte rend cette règle visible et opposable dans
// le renderer, y compris quand la page du run a été quittée (indicateur Sidebar).
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import {
  onAnswer,
  onArtifactCreated,
  onBackendTerminated,
  onError,
  onExecutionPlan,
  onMemoryDeferred,
  onMemoryFinished,
  onMemoryStarted,
  onMessageStarted,
  onOrchestratorBrief,
  onRunCancelled,
  onRunCancelling,
  onRunCompleted,
  onRunFailed,
  onRunStage,
  onRunStarted,
  onThinking,
  onToolCall,
  onToolResult,
  type BackendTerminatedEvent,
  type MemoryDeferredEvent,
  type MemoryFinishedEvent,
  type MemoryStartedEvent,
  type RunCancelledEvent,
  type RunCancellingEvent,
  type RunCompletedEvent,
  type RunFailedEvent,
  type RunStartedEvent,
} from '../../lib/ipcBridge';
import type { ErrorEvent, MessageStarted, Thinking, ToolCall, ToolResult, Answer, OrchestratorBrief } from '../../types/ipc';
import type { ArtifactCreatedEvent, ExecutionPlan, RunStageEvent } from '../../lib/ipcBridge';

export type InferenceKind = 'chat' | 'image';
export type InferencePhase = 'pending' | 'running' | 'cancelling';
export type InferenceActivity = {
  active: boolean;
  kind: InferenceKind | null;
  phase: InferencePhase | null;
  requestId: string | null;
  runId: string | null;
  conversationId: string | null;
  startedAt: number | null;
};

export type InferenceBeginInput = {
  kind: InferenceKind;
  requestId: string;
  conversationId?: string | null;
};

// L'entretien mémoire est un travail long, mais il n'est pas une inférence au
// sens de `begin/end` : il naît d'un événement du service, pas d'une commande du
// renderer. Il garde donc son propre état, que la Sidebar peut montrer sans
// prétendre qu'une inférence est en cours.
export type MemoryStatus = 'idle' | 'waiting' | 'running' | 'deferred';
export type MemoryActivity = {
  status: MemoryStatus;
  workspaceId: string | null;
  workspaceName: string | null;
  startedAt: number | null;
  tours: number | null;
  raison: string | null;
  lastResult: string | null;
  /** Horodatage du dernier événement : un heartbeat différé doit relancer le TTL. */
  lastEventAt: number | null;
};

export type LiveToolCall = {
  tool_call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  status: 'running' | 'completed' | 'error';
  result?: string;
  ok?: boolean;
  latency_ms?: number;
  evidence_ids?: string[];
  public_ids?: string[];
};

export type LiveRunSnapshot = {
  requestId: string;
  runId: string | null;
  conversationId: string | null;
  kind: 'chat' | 'image';
  startedAt: number | null;
  terminal: boolean;
  /** thinking du générateur (iteration 99) */
  thinking: string;
  /** thinking de l'orchestrateur (toutes les autres itérations) */
  orchestratorThinking: string;
  /** texte brut des événements `answer`, deltas concaténés, sans post-traitement */
  answer: string;
  toolCalls: LiveToolCall[];
  /** payloads bruts des `run_stage`, dans l'ordre, bornés à 500 */
  stages: Array<Record<string, unknown>>;
  /** artifact_id des `artifact_created`, dédupliqués */
  artifacts: string[];
  plan: Record<string, unknown> | null;
  brief: { brief: string; brief_chars?: number; tool_calls?: number; thinking?: string } | null;
};

export type InferenceActivityContextValue = {
  activity: InferenceActivity;
  memory: MemoryActivity;
  begin: (input: InferenceBeginInput) => boolean;
  end: (requestId?: string | null) => void;
  /** Snapshot de l'état vivant du run en cours (ou du dernier run terminal, grâce au délai de grâce). */
  getLiveRun: () => LiveRunSnapshot | null;
};

const EMPTY_ACTIVITY: InferenceActivity = {
  active: false,
  kind: null,
  phase: null,
  requestId: null,
  runId: null,
  conversationId: null,
  startedAt: null,
};

const EMPTY_MEMORY: MemoryActivity = {
  status: 'idle',
  workspaceId: null,
  workspaceName: null,
  startedAt: null,
  tours: null,
  raison: null,
  lastResult: null,
  lastEventAt: null,
};

// Un begin sans terminal (IPC muette, terminal perdu) ne doit pas bloquer
// l’application indéfiniment. L’admission est courte (90 s) ; un run réel peut
// enchaîner des outils lourds, donc sa lease est volontairement large (30 min).
const PENDING_WATCHDOG_MS = 90_000;
const RUNNING_WATCHDOG_MS = 30 * 60_000;

// L'entretien mémoire est plus court qu'un run d'inférence : deux minutes sans
// `memory_finished` suffisent à considérer le signal perdu. Un entretien différé
// ou en attente, lui, n'a plus de terminal à attendre avant 120 s.
// Un entretien de 200 messages peut dépasser deux minutes : cinq minutes
// laissent au `memory_finished` le temps d'arriver avant l'effacement.
const MEMORY_RUNNING_WATCHDOG_MS = 5 * 60_000;
const MEMORY_DEFERRED_TTL_MS = 120_000;

const LIVE_GRACE_MS = 60_000; // le temps qu'une page qui se remonte lise l'état avant de recharger l'historique

function matches(activity: InferenceActivity, payload: { request_id?: string; run_id?: string }): boolean {
  return (
    (activity.requestId !== null && payload.request_id === activity.requestId)
    || (activity.runId !== null && payload.run_id === activity.runId)
  );
}

const noop = () => {};
const beginNoop = () => true;

const DEFAULT_VALUE: InferenceActivityContextValue = {
  activity: EMPTY_ACTIVITY,
  memory: EMPTY_MEMORY,
  begin: beginNoop,
  end: noop,
  getLiveRun: () => null,
};

const InferenceActivityContext = createContext<InferenceActivityContextValue>(DEFAULT_VALUE);

export function InferenceActivityProvider({ children }: { children: ReactNode }) {
  const [activity, setActivity] = useState<InferenceActivity>(EMPTY_ACTIVITY);
  const activityRef = useRef<InferenceActivity>(activity);
  activityRef.current = activity;

  const commit = useCallback((next: InferenceActivity) => {
    activityRef.current = next;
    setActivity(next);
  }, []);

  const [memory, setMemory] = useState<MemoryActivity>(EMPTY_MEMORY);
  const memoryRef = useRef<MemoryActivity>(memory);
  memoryRef.current = memory;

  const commitMemory = useCallback((next: MemoryActivity) => {
    memoryRef.current = next;
    setMemory(next);
  }, []);

  const liveRef = useRef<LiveRunSnapshot | null>(null);
  const graceTimerRef = useRef<number | null>(null);

  const clearGraceTimer = useCallback(() => {
    if (graceTimerRef.current !== null) {
      window.clearTimeout(graceTimerRef.current);
      graceTimerRef.current = null;
    }
  }, []);

  const scheduleGrace = useCallback(() => {
    clearGraceTimer();
    graceTimerRef.current = window.setTimeout(() => {
      liveRef.current = null;
      graceTimerRef.current = null;
    }, LIVE_GRACE_MS);
  }, [clearGraceTimer]);

  const createLive = useCallback((requestId: string, runId: string | null, conversationId: string | null, kind: 'chat' | 'image'): LiveRunSnapshot => ({
    requestId,
    runId,
    conversationId,
    kind,
    startedAt: Date.now(),
    terminal: false,
    thinking: '',
    orchestratorThinking: '',
    answer: '',
    toolCalls: [],
    stages: [],
    artifacts: [],
    plan: null,
    brief: null,
  }), []);

  const ensureLive = useCallback((requestId: string, runId?: string | null, conversationId?: string | null) => {
    const current = liveRef.current;
    if (!current || current.requestId !== requestId) {
      liveRef.current = createLive(requestId, runId ?? null, conversationId ?? null, 'chat');
      return;
    }
    if (runId !== undefined) liveRef.current!.runId = runId;
    if (conversationId !== undefined) liveRef.current!.conversationId = conversationId;
  }, [createLive]);

  const getLiveRun = useCallback(() => {
    const live = liveRef.current;
    if (!live) return null;
    return {
      ...live,
      toolCalls: live.toolCalls.map((t) => ({ ...t })),
      stages: [...live.stages],
      artifacts: [...live.artifacts],
    };
  }, []);

  const begin = useCallback((input: InferenceBeginInput): boolean => {
    const current = activityRef.current;
    if (current.active && current.requestId !== null && current.requestId !== input.requestId) return false;
    const sameRequest = current.requestId === input.requestId;
    clearGraceTimer();
    ensureLive(input.requestId, sameRequest ? current.runId : null, input.conversationId ?? (sameRequest ? current.conversationId : null));
    const live = liveRef.current;
    if (live) {
      live.kind = input.kind;
      live.terminal = false;
      live.startedAt = sameRequest && live.startedAt !== null ? live.startedAt : Date.now();
      if (!sameRequest) {
        live.thinking = '';
        live.orchestratorThinking = '';
        live.answer = '';
        live.toolCalls = [];
        live.stages = [];
        live.artifacts = [];
        live.plan = null;
        live.brief = null;
      }
    }
    commit({
      active: true,
      kind: input.kind,
      phase: 'pending',
      requestId: input.requestId,
      runId: sameRequest ? current.runId : null,
      conversationId: input.conversationId ?? (sameRequest ? current.conversationId : null),
      startedAt: sameRequest && current.startedAt !== null ? current.startedAt : Date.now(),
    });
    return true;
  }, [clearGraceTimer, commit, ensureLive]);

  const end = useCallback((requestId?: string | null) => {
    const current = activityRef.current;
    if (!current.active) return;
    if (requestId && current.requestId !== null && current.requestId !== requestId) return;
    commit(EMPTY_ACTIVITY);
    const live = liveRef.current;
    if (live) {
      live.terminal = true;
      scheduleGrace();
    }
  }, [commit, scheduleGrace]);

  const handleRunStarted = useCallback((payload: RunStartedEvent) => {
    const kind: InferenceKind = payload.kind === 'production' ? 'image' : 'chat';
    const current = activityRef.current;
    if (current.active && current.requestId !== null && current.requestId !== payload.request_id) return;
    const sameRequest = current.requestId === payload.request_id;
    clearGraceTimer();
    ensureLive(payload.request_id, payload.run_id, payload.conversation_id ?? (sameRequest ? current.conversationId : null));
    const live = liveRef.current;
    if (live) {
      live.kind = kind;
      live.terminal = false;
      live.runId = payload.run_id;
      live.conversationId = payload.conversation_id ?? (sameRequest ? current.conversationId : null);
      live.startedAt = sameRequest && live.startedAt !== null ? live.startedAt : Date.now();
      if (!sameRequest) {
        live.thinking = '';
        live.orchestratorThinking = '';
        live.answer = '';
        live.toolCalls = [];
        live.stages = [];
        live.artifacts = [];
        live.plan = null;
        live.brief = null;
      }
    }
    commit({
      active: true,
      kind,
      phase: 'running',
      requestId: payload.request_id,
      runId: payload.run_id,
      conversationId: payload.conversation_id ?? (sameRequest ? current.conversationId : null),
      startedAt: sameRequest && current.startedAt !== null ? current.startedAt : Date.now(),
    });
  }, [clearGraceTimer, commit, ensureLive]);

  const handleMessageStarted = useCallback((payload: MessageStarted) => {
    const current = activityRef.current;
    if (!current.active) return;
    if (current.requestId !== payload.request_id) return;
    if (current.kind === 'image') return;
    commit({ ...current, phase: 'running' });
  }, [commit]);

  const handleRunCancelling = useCallback((payload: RunCancellingEvent) => {
    const current = activityRef.current;
    if (!current.active || !matches(current, payload)) return;
    commit({ ...current, phase: 'cancelling' });
  }, [commit]);

  const handleTerminal = useCallback((payload: RunCompletedEvent | RunFailedEvent | RunCancelledEvent) => {
    const current = activityRef.current;
    if (!current.active || !matches(current, payload)) return;
    commit(EMPTY_ACTIVITY);
    const live = liveRef.current;
    if (live && (payload.request_id === live.requestId || (payload.run_id !== undefined && payload.run_id === live.runId))) {
      live.terminal = true;
      scheduleGrace();
    }
  }, [commit, scheduleGrace]);

  const handleError = useCallback((payload: ErrorEvent) => {
    const current = activityRef.current;
    if (!current.active || !matches(current, payload)) return;
    // Une erreur PENDANT un run est un signal de progression, pas un terminal :
    // seuls run_completed / run_failed / run_cancelled / backend_terminated
    // libèrent une activité admise. Une erreur en phase `pending` est une erreur
    // d’admission (queue_full, handler_error…) : rien ne suivra.
    if (current.phase !== 'pending') return;
    commit(EMPTY_ACTIVITY);
  }, [commit]);

  const handleThinking = useCallback((payload: Thinking) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    if (payload.iteration === 99) live.thinking += payload.text;
    else live.orchestratorThinking += payload.text;
  }, []);

  const handleAnswer = useCallback((payload: Answer) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    live.answer += (payload.text ?? payload.content ?? '');
  }, []);

  const handleToolCall = useCallback((payload: ToolCall) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    if (live.toolCalls.some((entry) => entry.tool_call_id === payload.tool_call_id)) return;
    if (live.toolCalls.length >= 200) return;
    live.toolCalls.push({
      tool_call_id: payload.tool_call_id,
      tool: payload.tool,
      arguments: payload.arguments,
      status: 'running',
    });
  }, []);

  const handleToolResult = useCallback((payload: ToolResult) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    const entry = live.toolCalls.find((item) => item.tool_call_id === payload.tool_call_id);
    if (!entry) return;
    entry.status = payload.ok ? 'completed' : 'error';
    entry.result = payload.result;
    entry.ok = payload.ok;
    entry.latency_ms = payload.latency_ms;
    entry.evidence_ids = payload.evidence_ids;
    entry.public_ids = payload.public_ids;
  }, []);

  const handleRunStage = useCallback((payload: RunStageEvent) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    live.stages.push(payload as Record<string, unknown>);
    if (live.stages.length > 500) {
      live.stages.splice(0, live.stages.length - 500);
    }
  }, []);

  const handleArtifactCreated = useCallback((payload: ArtifactCreatedEvent) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    if (!live.artifacts.includes(payload.artifact_id)) live.artifacts.push(payload.artifact_id);
  }, []);

  const handleExecutionPlan = useCallback((payload: ExecutionPlan) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    live.plan = payload as Record<string, unknown>;
  }, []);

  const handleOrchestratorBrief = useCallback((payload: OrchestratorBrief) => {
    const live = liveRef.current;
    if (!live || payload.request_id !== live.requestId) return;
    live.brief = {
      brief: payload.brief,
      brief_chars: payload.brief_chars,
      tool_calls: payload.tool_calls,
      thinking: payload.thinking,
    };
  }, []);

  const handleMemoryStarted = useCallback((payload: MemoryStartedEvent) => {
    const now = Date.now();
    commitMemory({
      status: 'running',
      workspaceId: payload.workspace_id ?? null,
      workspaceName: payload.workspace_name ?? null,
      startedAt: now,
      tours: payload.tours ?? null,
      raison: payload.raison ?? null,
      lastResult: null,
      lastEventAt: now,
    });
  }, [commitMemory]);

  const handleMemoryDeferred = useCallback((payload: MemoryDeferredEvent) => {
    const current = memoryRef.current;
    // Un « différé » ne rétrograde jamais un entretien qui tourne : le service
    // peut réémettre son heartbeat pendant que son propre `memory_started` est
    // encore l'état courant, et ce heartbeat ne doit pas faire perdre le
    // terminal de l'entretien.
    if (current.status === 'running') return;
    // Le workspace_id est l'identité stable de l'entretien : un différé d'un
    // autre espace ne remplace pas l'indicateur de celui-ci.
    if (current.workspaceId !== null && current.workspaceId !== payload.workspace_id) return;
    commitMemory({
      status: payload.statut === 'en_attente' ? 'waiting' : 'deferred',
      workspaceId: payload.workspace_id ?? null,
      workspaceName: payload.workspace_name ?? null,
      startedAt: null,
      tours: payload.tours ?? null,
      raison: payload.raison ?? null,
      lastResult: null,
      lastEventAt: Date.now(),
    });
  }, [commitMemory]);

  const handleMemoryFinished = useCallback((payload: MemoryFinishedEvent) => {
    // Un terminal ne libère que l'entretien qu'il nomme : le workspace_id est
    // l'identité stable de l'entretien, l'ordre des événements n'en est pas une.
    if (memoryRef.current.workspaceId !== null && memoryRef.current.workspaceId !== payload.workspace_id) return;
    commitMemory(EMPTY_MEMORY);
  }, [commitMemory]);

  const handleBackendTerminated = useCallback((_payload: BackendTerminatedEvent) => {
    end();
    commitMemory(EMPTY_MEMORY);
  }, [commitMemory, end]);

  useEffect(() => {
    let disposed = false;
    const unsubs: Array<() => void> = [];
    const registrations = [
      onRunStarted(handleRunStarted),
      onMessageStarted(handleMessageStarted),
      onRunCancelling(handleRunCancelling),
      onRunCompleted(handleTerminal),
      onRunFailed(handleTerminal),
      onRunCancelled(handleTerminal),
      onError(handleError),
      onBackendTerminated(handleBackendTerminated),
      onMemoryStarted(handleMemoryStarted),
      onMemoryDeferred(handleMemoryDeferred),
      onMemoryFinished(handleMemoryFinished),
      onThinking(handleThinking),
      onToolCall(handleToolCall),
      onToolResult(handleToolResult),
      onAnswer(handleAnswer),
      onRunStage(handleRunStage),
      onArtifactCreated(handleArtifactCreated),
      onExecutionPlan(handleExecutionPlan),
      onOrchestratorBrief(handleOrchestratorBrief),
    ];
    void Promise.all(registrations).then((registered) => {
      if (disposed) {
        registered.forEach((unsubscribe) => { unsubscribe(); });
        return;
      }
      unsubs.push(...registered);
    }).catch(() => {
      // Pont absent : l'activité locale (begin/end) reste utilisable.
    });
    return () => {
      disposed = true;
      unsubs.forEach((unsubscribe) => {
        try { unsubscribe(); } catch { /* ignore */ }
      });
    };
  }, [handleArtifactCreated, handleAnswer, handleBackendTerminated, handleExecutionPlan, handleError, handleMemoryDeferred, handleMemoryFinished, handleMemoryStarted, handleMessageStarted, handleOrchestratorBrief, handleRunCancelling, handleRunStage, handleRunStarted, handleTerminal, handleThinking, handleToolCall, handleToolResult]);

  useEffect(() => {
    if (!activity.active || activity.startedAt === null) return;
    const leaseMs = activity.phase === 'pending' ? PENDING_WATCHDOG_MS : RUNNING_WATCHDOG_MS;
    const remaining = Math.max(0, leaseMs - (Date.now() - activity.startedAt));
    const timer = window.setTimeout(() => {
      const current = activityRef.current;
      if (current.active && current.startedAt === activity.startedAt) {
        commit(EMPTY_ACTIVITY);
      }
    }, remaining);
    return () => window.clearTimeout(timer);
  }, [activity.active, activity.startedAt, activity.phase, commit]);

  useEffect(() => {
    if (memory.status === 'idle') return;

    if (memory.status === 'running') {
      const startedAt = memory.startedAt;
      if (startedAt === null) return;
      const remaining = Math.max(0, MEMORY_RUNNING_WATCHDOG_MS - (Date.now() - startedAt));
      const timer = window.setTimeout(() => {
        const current = memoryRef.current;
        if (current.status === 'running' && current.startedAt === startedAt) {
          commitMemory(EMPTY_MEMORY);
        }
      }, remaining);
      return () => window.clearTimeout(timer);
    }

    // `waiting` et `deferred` n'attendent plus de terminal : leur indicative ne
    // doit pas rester allumée indéfiniment après que le service s'est tu. Le
    // heartbeat réémis par le service change `lastEventAt` et relance donc ce TTL.
    const workspaceId = memory.workspaceId;
    const lastEventAt = memory.lastEventAt;
    const timer = window.setTimeout(() => {
      const current = memoryRef.current;
      if (
        current.status === memory.status
        && current.workspaceId === workspaceId
        && current.lastEventAt === lastEventAt
      ) {
        commitMemory(EMPTY_MEMORY);
      }
    }, MEMORY_DEFERRED_TTL_MS);
    return () => window.clearTimeout(timer);
  }, [memory.status, memory.startedAt, memory.workspaceId, memory.lastEventAt, commitMemory]);

  useEffect(() => {
    return () => {
      if (graceTimerRef.current !== null) window.clearTimeout(graceTimerRef.current);
    };
  }, []);

  return (
    <InferenceActivityContext.Provider value={{ activity, memory, begin, end, getLiveRun }}>
      {children}
    </InferenceActivityContext.Provider>
  );
}

export function useInferenceActivity(): InferenceActivityContextValue {
  return useContext(InferenceActivityContext);
}

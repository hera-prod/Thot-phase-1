import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Image24Regular } from '@fluentui/react-icons';
import './ImagesPage.css';
import * as geom from './imageGeometry';
import * as maskmod from './imageMask';
import * as enc from './imageEncoding';
import * as ie from './imageEditor';
import { snapMoveRect, snapResizeRect, type SnapHandle } from './imageSnap';
import { planModifierRequest } from './imageRequest';
import {
  cancelChat,
  getAttachmentBytes,
  imageStageWork,
  imageGenerate,
  imageList,
  imageSetBackend,
  imageStatus,
  isDeprecatedQuality,
  onArtifactCreated,
  onBackendTerminated,
  onError,
  onImageStatus,
  onRunCancelled,
  onRunCancelling,
  onRunCompleted,
  onRunFailed,
  onRunStage,
  onRunStarted,
  type ArtifactCreatedEvent,
  type BackendTerminatedEvent,
  type ImageStatus,
  type RunCancelledEvent,
  type RunCancellingEvent,
  type RunCompletedEvent,
  type RunFailedEvent,
  type RunStageEvent,
  type RunStartedEvent,
} from '../../lib/ipcBridge';
import { showToast } from '../../components/ui/Toast';
import { validateAttachments } from '../../features/attachments/attachmentValidation';
import { useInferenceActivity } from '../../features/inference/InferenceActivityContext';
import { modelByRole } from '../../features/models/modelCatalog';import { advanceRunProgress, ProgressCard, type RunProgress } from '../../components/image/ProgressCard';
import type { ErrorEvent } from '../../types/ipc';

export type StudioState = 'idle' | 'queued' | 'generating' | 'loading' | 'succeeded' | 'failed' | 'cancelled';

type FormatId = '1:1' | '16:9' | '9:16' | '3:2' | '2:3' | '4:3' | '3:4' | '5:4' | '4:5' | '21:9' | '9:21';
type LoadState = 'idle' | 'loading' | 'ready' | 'error';

type TimelineStep = {
  artifact_id: string;
  prompt: string;
  aspect_ratio: FormatId;
  seed?: number;
  url?: string;
  loadState: LoadState;
  loadError?: string;
};

type EditorReference = { id: string; file: File; bytes: Uint8Array; url: string; name: string; size: number };

type ActiveRun = {
  origin?: 'create' | 'edit';
  sessionId?: string;
  revision?: string;
  activeHistoryId?: string;
  snapshotHash?: string;
  maskHash?: string;
  requestId: string;
  runId?: string;
  prompt: string;
  aspect_ratio: FormatId;
  seed?: number;
  artifactId?: string;
  runCompleted: boolean;
  bytesReady: boolean;
  submitted: boolean;
  terminal: boolean;
  accepted: boolean;
  timeoutId: number | null;
  ackTimeoutId: number | null;
};

const MAX_PROMPT = 2000;
const MAX_SEED = 4294967295;
const RUN_TIMEOUT_MS = 660_000;
const LOAD_TIMEOUT_MS = 20_000;
const ACK_TIMEOUT_MS = 8_000;
const MAX_CREATION_REFERENCES = 4;

const FORMATS: ReadonlyArray<{ id: FormatId; width: number; height: number }> = [
  { id: '1:1', width: 1024, height: 1024 },
  { id: '16:9', width: 1280, height: 720 },
  { id: '9:16', width: 720, height: 1280 },
  { id: '3:2', width: 1248, height: 832 },
  { id: '2:3', width: 832, height: 1248 },
  { id: '4:3', width: 1152, height: 864 },
  { id: '3:4', width: 864, height: 1152 },
  { id: '5:4', width: 1120, height: 896 },
  { id: '4:5', width: 896, height: 1120 },
  { id: '21:9', width: 1568, height: 672 },
  { id: '9:21', width: 672, height: 1568 },
];

const EDITOR_FORMATS = FORMATS.map((format) => ({ id: format.id, width: format.width, height: format.height }));

function createId(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function messageOf(error: unknown, fallback: string): string {
  const objectMessage = typeof error === 'object' && error !== null && 'message' in error
    ? (error as { message?: unknown }).message
    : null;
  if (typeof objectMessage === 'string' && objectMessage) return objectMessage;
  if (error instanceof Error && error.message) return error.message;
  const text = String(error ?? '').trim();
  return text || fallback;
}

function isTerminal(state: StudioState): boolean {
  return state === 'succeeded' || state === 'failed' || state === 'cancelled';
}


function randomSeedValue(): number {
  return Math.floor(Math.random() * (MAX_SEED + 1));
}

function seedFromInput(value: string): { seed?: number; error?: string } {
  const raw = value.trim();
  if (!raw) return {};
  if (!/^\d+$/.test(raw)) {
    return { error: 'La graine doit être un entier positif.' };
  }
  const seed = Number(raw);
  if (!Number.isSafeInteger(seed) || seed < 0 || seed > MAX_SEED) {
    return { error: `La graine doit être comprise entre 0 et ${MAX_SEED}.` };
  }
  return { seed };
}

function VramBar({ freeGb, totalGb }: { freeGb: number; totalGb: number }) {
  const safeTotal = Math.max(totalGb, 0.1);
  const usedGb = Math.max(0, totalGb - freeGb);
  const usedPercent = Math.min(100, Math.max(0, (usedGb / safeTotal) * 100));
  return (
    <div className="img-vram" role="img" aria-label={`VRAM utilisée : ${usedGb.toFixed(1)} Go sur ${totalGb.toFixed(1)} Go`}>
      <div className="img-vram-label">
        <span>VRAM utilisée</span>
        <strong>{usedGb.toFixed(1)} / {totalGb.toFixed(1)} Go</strong>
      </div>
      <div className="img-vram-track" role="img" aria-label={`${usedPercent.toFixed(0)} % de VRAM utilisée`}>
        <span style={{ width: `${usedPercent}%` }} />
      </div>
    </div>
  );
}

function EngineCard({
  engine,
  switching,
  busy,
  onSwitch,
}: {
  engine: ImageStatus | null;
  switching: boolean;
  busy: boolean;
  onSwitch: (enabled: boolean) => void;
}) {
  const diffusion = modelByRole('image_diffusion');  const files = engine?.details?.files ? Object.entries(engine.details.files) : [];
  const missing = files.filter(([, present]) => !present).map(([name]) => name);
  const freeVram = engine?.details?.free_vram_gb;
  const totalVram = engine?.details?.total_vram_gb;
  return (
    <section className="img-engine" aria-labelledby="engine-title">
      <div className="img-engine-heading">
        <div>
          <span className="img-kicker">Diagnostic local</span>
          <h2 id="engine-title">Moteur image</h2>
        </div>
        <span className={`img-engine-state${engine?.available ? ' is-ready' : ''}`}>
          <i aria-hidden="true" />
          {engine ? (engine.available ? 'Disponible' : 'Indisponible') : 'Interrogation'}
        </span>
      </div>
      {engine ? (
        <>
          <dl className="img-engine-facts">
            <div><dt>Opt-in</dt><dd>{engine.enabled ? 'Activé' : 'Désactivé'}</dd></div>
            <div><dt>CUDA</dt><dd>{engine.details?.cuda ? 'Détectée' : 'Non renseignée'}</dd></div>
            <div><dt>Licence</dt><dd>{diffusion ? `${diffusion.name} · ${diffusion.license}` : '—'}</dd></div>
          </dl>
          {engine.details?.cuda && typeof freeVram === 'number' && Number.isFinite(freeVram) && typeof totalVram === 'number' && Number.isFinite(totalVram) && (
            <VramBar freeGb={freeVram} totalGb={totalVram} />
          )}
          {missing.length > 0 && <p className="img-engine-warning">Composants manquants : {missing.join(', ')}</p>}
          {!engine.available && engine.reason && <p className="img-engine-reason">{engine.reason}</p>}
          <div className="img-engine-actions">
            {!engine.enabled ? (
              <button type="button" className="img-button img-button--small" onClick={() => onSwitch(true)} disabled={switching}>
                {switching ? 'Activation…' : 'Activer le moteur local'}
              </button>
            ) : (
              <button type="button" className="img-button img-button--small" onClick={() => onSwitch(false)} disabled={switching || busy}>
                Désactiver le moteur image
              </button>
            )}
          </div>
        </>
      ) : (
        <p className="img-engine-reason">Lecture de l’état CUDA et des composants locaux…</p>
      )}
      <p className="img-engine-footnote">Modifier une image existante se fait depuis son menu, dans la galerie. Désactiver retire l’autorisation d’utilisation ; aucun modèle n’est déchargé.</p>
    </section>
  );
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('decode'));
    image.src = url;
  });
}

function maskCanvasFromMask(mask: Uint8Array, width: number, height: number, rgba: [number, number, number, number]): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Contexte 2D indisponible.');
  const data = ctx.createImageData(width, height);
  for (let i = 0; i < width * height; i += 1) {
    if ((mask[i] ?? 0) >= maskmod.MASK_EDITABLE_THRESHOLD) {
      data.data[i * 4] = rgba[0];
      data.data[i * 4 + 1] = rgba[1];
      data.data[i * 4 + 2] = rgba[2];
      data.data[i * 4 + 3] = rgba[3];
    } else {
      data.data[i * 4 + 3] = 0;
    }
  }
  ctx.putImageData(data, 0, 0);
  return canvas;
}

function cursorForHandle(handle: string): string {
  if (handle === 'nw' || handle === 'se') return 'nwse-resize';
  if (handle === 'ne' || handle === 'sw') return 'nesw-resize';
  if (handle === 'n' || handle === 's') return 'ns-resize';
  if (handle === 'e' || handle === 'w') return 'ew-resize';
  return 'default';
}

type EditorDragState =
  | { type: 'pan'; startX: number; startY: number; startPan: { zoom: number; panX: number; panY: number } }
  | { type: 'paint'; family: 'hole' | 'edit'; lastPaintedDoc?: { x: number; y: number } }
  | { type: 'crop'; start: { x: number; y: number } }
  | { type: 'move'; startTransform: ie.EditorSession['transform']; startDoc: { x: number; y: number } }
  | { type: 'resize'; handle: string; startTransform: ie.EditorSession['transform']; startDoc: { x: number; y: number }; aspect: number }
  | { type: 'view' };

export function ImagesPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const paintingRef = useRef(false);
  const [prompt, setPrompt] = useState('');
  const [format, setFormat] = useState<FormatId>('1:1');
  const [seedInput, setSeedInput] = useState('');
  const [creationAutoSeed, setCreationAutoSeed] = useState<number>(() => randomSeedValue());
  const [studioState, setStudioState] = useState<StudioState>('idle');
  const [lastError, setLastError] = useState<string | null>(null);
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [steps, setSteps] = useState<TimelineStep[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [engine, setEngine] = useState<ImageStatus | null>(null);
  const [switching, setSwitching] = useState(false);
  const [cancelPending, setCancelPending] = useState(false);
  const [seedError, setSeedError] = useState<string | null>(null);
  const [hiddenCount, setHiddenCount] = useState(0);
  const [backgroundLoadCount, setBackgroundLoadCount] = useState(0);
  const [listenersReady, setListenersReady] = useState(false);
  const referenceInputRef = useRef<HTMLInputElement>(null);
  const [references, setReferences] = useState<EditorReference[]>([]);
  const [referenceError, setReferenceError] = useState<string | null>(null);

  const [editorReferences, setEditorReferences] = useState<EditorReference[]>([]);
  const [editorReferenceError, setEditorReferenceError] = useState<string | null>(null);
  const editorReferenceInputRef = useRef<HTMLInputElement>(null);

  const [editorSession, setEditorSession] = useState<ie.EditorSession | null>(null);
  const editorSessionRef = useRef<ie.EditorSession | null>(null);
  useEffect(() => { editorSessionRef.current = editorSession; }, [editorSession]);
  const [editorTool, setEditorTool] = useState<'move' | 'eraser' | 'magic' | 'crop' | 'view'>('move');
  const [brushSize, setBrushSize] = useState(18);
  const [editorAutoSeed, setEditorAutoSeed] = useState<number>(() => randomSeedValue());
  const [view, setView] = useState<{ zoom: number; panX: number; panY: number }>({ zoom: 1, panX: 0, panY: 0 });
  const [snapGuides, setSnapGuides] = useState<{ x: number | null; y: number | null }>({ x: null, y: null });
  const dragRef = useRef<EditorDragState | null>(null);
  const brushPreviewRef = useRef<{ x: number; y: number } | null>(null);
  const isEditingBusyRef = useRef(false);
  const spaceDownRef = useRef(false);

  const mountedRef = useRef(true);
  const stateRef = useRef<StudioState>('idle');
  const stepsRef = useRef<TimelineStep[]>([]);
  const activeRunRef = useRef<ActiveRun | null>(null);
  const inference = useInferenceActivity();
  const inferenceRef = useRef(inference);
  inferenceRef.current = inference;
  const objectUrlsRef = useRef<Set<string>>(new Set());
  const loadTimeoutsRef = useRef<Set<number>>(new Set());
  const loadTokensRef = useRef<Map<string, number>>(new Map());
  const backgroundLoadCountRef = useRef(0);
  const engineRequestIdRef = useRef<string | null>(null);
  const engineRequestSeqRef = useRef(0);

  const beginBackgroundLoad = useCallback(() => {
    backgroundLoadCountRef.current += 1;
    if (mountedRef.current) setBackgroundLoadCount(backgroundLoadCountRef.current);
  }, []);

  const endBackgroundLoad = useCallback(() => {
    backgroundLoadCountRef.current = Math.max(0, backgroundLoadCountRef.current - 1);
    if (mountedRef.current) setBackgroundLoadCount(backgroundLoadCountRef.current);
  }, []);

  const setCurrentState = useCallback((next: StudioState) => {
    stateRef.current = next;
    if (mountedRef.current) setStudioState(next);
  }, []);

  const updateSteps = useCallback((updater: (current: TimelineStep[]) => TimelineStep[]) => {
    const next = updater(stepsRef.current);
    stepsRef.current = next;
    if (mountedRef.current) setSteps(next);
  }, []);

  const revokeUrl = useCallback((url?: string) => {
    if (!url) return;
    objectUrlsRef.current.delete(url);
    if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
  }, []);

  const clearRunTimeout = useCallback((run: ActiveRun) => {
    if (run.timeoutId !== null) {
      window.clearTimeout(run.timeoutId);
      run.timeoutId = null;
    }
  }, []);

  const clearAckTimeout = useCallback((run: ActiveRun) => {
    if (run.ackTimeoutId !== null) {
      window.clearTimeout(run.ackTimeoutId);
      run.ackTimeoutId = null;
    }
  }, []);

  const terminateRun = useCallback((run: ActiveRun, next: Extract<StudioState, 'failed' | 'cancelled'>, message?: string) => {
    if (run.terminal || activeRunRef.current !== run) return false;
    run.terminal = true;
    inferenceRef.current.end(run.requestId);
    clearRunTimeout(run);
    clearAckTimeout(run);
    activeRunRef.current = null;
    setCurrentState(next);
    setCancelPending(false);
    if (run.origin === 'create' && mountedRef.current) setCreationAutoSeed(randomSeedValue());
    if (run.artifactId && message) {
      updateSteps((current) => current.map((step) => step.artifact_id === run.artifactId
        ? { ...step, loadState: 'error', loadError: message }
        : step));
    }
    if (message) setLastError(message);
    if (next === 'cancelled') setProgress(null);
    return true;
  }, [clearAckTimeout, clearRunTimeout, setCurrentState, updateSteps]);

  const openEditorFromArtifact = useCallback(async (artifactId: string) => {
    let url: string | undefined;
    try {
      const [bytes, mime] = await getAttachmentBytes(artifactId);
      url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
      objectUrlsRef.current.add(url);
      const img = await loadImage(url);
      const historyStep = stepsRef.current.find((step) => step.artifact_id === artifactId);
      const baseSeed = typeof historyStep?.seed === 'number' ? historyStep.seed : undefined;
      const session = ie.createEditorSession({
        id: createId('session'),
        image: img,
        url,
        bytes: new Uint8Array(bytes),
        sourceSize: { width: img.naturalWidth || 512, height: img.naturalHeight || 512 },
        formats: EDITOR_FORMATS,
        prompt: '',
        seed: baseSeed,
        sourceArtifactId: artifactId,
        revision: '0',
      });
      const previous = editorSessionRef.current;
      if (previous?.url) revokeUrl(previous.url);
      if (previous?.history) for (const entry of previous.history) revokeUrl(entry.url);
      if (previous?.undoStack) for (const snapshot of previous.undoStack) revokeUrl(snapshot.url);
      setEditorSession(session);
      setEditorAutoSeed(randomSeedValue());
      setView({ zoom: 1, panX: 0, panY: 0 });
      setEditorTool('move');
      setBrushSize(18);
    } catch (error) {
      revokeUrl(url);
      setLastError(messageOf(error, 'Impossible d’ouvrir l’éditeur.'));
    }
  }, [revokeUrl]);

  const openEditorFromReference = useCallback(async (ref: EditorReference) => {
    let url: string | undefined;
    try {
      url = URL.createObjectURL(new Blob([new Uint8Array(ref.bytes)], { type: 'image/png' }));
      objectUrlsRef.current.add(url);
      const img = await loadImage(url);
      const session = ie.createEditorSession({
        id: createId('session'),
        image: img,
        url,
        bytes: new Uint8Array(ref.bytes),
        sourceArtifactId: undefined,
        sourceSize: { width: img.naturalWidth || 1, height: img.naturalHeight || 1 },
        formats: EDITOR_FORMATS,
        prompt: '',
        revision: '0',
      });
      const previous = editorSessionRef.current;
      if (previous?.url) revokeUrl(previous.url);
      if (previous?.history) for (const entry of previous.history) revokeUrl(entry.url);
      if (previous?.undoStack) for (const snapshot of previous.undoStack) revokeUrl(snapshot.url);
      setEditorSession(session);
      setEditorAutoSeed(randomSeedValue());
      setView({ zoom: 1, panX: 0, panY: 0 });
      setEditorTool('move');
      setBrushSize(18);
    } catch (error) {
      revokeUrl(url);
      setLastError(messageOf(error, 'Impossible d’ouvrir l’éditeur.'));
    }
  }, [revokeUrl]);

  const finalizeEditResult = useCallback(async (run: ActiveRun) => {
    if (!run.artifactId || !run.sessionId || run.revision === undefined) return;
    const launchSessionId = run.sessionId;
    const launchRevision = run.revision;
    const launchHistoryId = run.activeHistoryId;
    const currentAtStart = editorSessionRef.current;
    if (!currentAtStart || currentAtStart.id !== launchSessionId || currentAtStart.revision !== launchRevision || (launchHistoryId !== undefined && currentAtStart.activeHistoryId !== launchHistoryId)) {
      showToast({ type: 'info', title: 'Résultat ignoré', message: 'La session a changé pendant la génération.' });
      return;
    }
    let url: string | undefined;
    try {
      const [bytes, mime] = await getAttachmentBytes(run.artifactId);
      if (!mountedRef.current) return;
      url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
      objectUrlsRef.current.add(url);
      const img = await loadImage(url);
      const latest = editorSessionRef.current;
      if (!latest || latest.id !== launchSessionId || latest.revision !== launchRevision || (launchHistoryId !== undefined && latest.activeHistoryId !== launchHistoryId)) {
        showToast({ type: 'info', title: 'Résultat ignoré', message: 'La session a changé pendant la génération.' });
        revokeUrl(url);
        return;
      }
      setEditorSession((current) => {
        if (!current || current.id !== launchSessionId || current.revision !== launchRevision) return current;
        const entry = ie.createHistoryEntry({
          image: img,
          url,
          sourceSize: { width: img.naturalWidth || 1, height: img.naturalHeight || 1 },
          format: current.format,
          kind: 'result',
          artifactId: run.artifactId,
        });
        const withAppend = ie.appendResult(current, entry);
        return ie.selectHistoryBase(withAppend, entry.id);
      });
    } catch (error) {
      revokeUrl(url);
      if (mountedRef.current) setLastError(messageOf(error, 'Impossible de charger le résultat.'));
    }
  }, [revokeUrl]);

  const completeRun = useCallback((run: ActiveRun) => {
    if (run.terminal || activeRunRef.current !== run || !run.runCompleted || !run.bytesReady) return;
    run.terminal = true;
    inferenceRef.current.end(run.requestId);
    clearRunTimeout(run);
    clearAckTimeout(run);
    activeRunRef.current = null;
    setCurrentState('succeeded');
    setCancelPending(false);
    setLastError(null);
    setProgress(null);
    if (run.origin === 'create' && mountedRef.current) setCreationAutoSeed(randomSeedValue());
    if (run.origin === 'edit' && run.artifactId && run.sessionId) {
      void finalizeEditResult(run);
    } else if (run.origin === 'create' && run.artifactId) {
      void openEditorFromArtifact(run.artifactId);
    }
  }, [clearAckTimeout, clearRunTimeout, finalizeEditResult, openEditorFromArtifact, setCurrentState]);

  const cancelBeforeTerminal = useCallback((run: ActiveRun, message: string) => {
    setCancelPending(true);
    setProgress((current) => current ? { ...current, label: 'Annulation de la demande…' } : current);
    void cancelChat(run.requestId).catch(() => undefined);
    terminateRun(run, 'failed', message);
  }, [terminateRun]);

  const armRunTimeout = useCallback((run: ActiveRun) => {
    clearRunTimeout(run);
    run.timeoutId = window.setTimeout(() => {
      if (!mountedRef.current || run.terminal || activeRunRef.current !== run) return;
      run.timeoutId = null;
      cancelBeforeTerminal(run, 'Le moteur n’a pas confirmé le run dans le délai de sécurité. La demande a été annulée.');
    }, RUN_TIMEOUT_MS);
  }, [cancelBeforeTerminal, clearRunTimeout]);

  const markRunAccepted = useCallback((run: ActiveRun, runId: string) => {
    run.runId = runId;
    if (!run.accepted) {
      run.accepted = true;
      clearAckTimeout(run);
      setProgress((current) => ({
        done: current?.done ?? [],
        currentId: 'production',
        label: 'Run reçu · préparation du moteur',
        step: null,
        total: null,
        startedAt: current?.startedAt ?? Date.now(),
      }));
    }
  }, [clearAckTimeout]);

  const touchRun = useCallback((run: ActiveRun) => {
    if (activeRunRef.current === run && !run.terminal) armRunTimeout(run);
  }, [armRunTimeout]);

  const loadArtifact = useCallback(async (artifactId: string, run: ActiveRun | null) => {
    const existing = stepsRef.current.find((step) => step.artifact_id === artifactId);
    if (existing?.loadState === 'ready') {
      if (run) {
        run.bytesReady = true;
        completeRun(run);
      }
      return;
    }
    if (existing?.loadState === 'loading') return;

    const token = (loadTokensRef.current.get(artifactId) ?? 0) + 1;
    loadTokensRef.current.set(artifactId, token);
    updateSteps((current) => current.map((step) => step.artifact_id === artifactId
      ? { ...step, loadState: 'loading', loadError: undefined }
      : step));
    if (run) setCurrentState('loading');
    else beginBackgroundLoad();
    let backgroundLoadEnded = false;

    const timeoutId = window.setTimeout(() => {
      if (!mountedRef.current || token !== (loadTokensRef.current.get(artifactId) ?? 0) || (run && (run.terminal || activeRunRef.current !== run))) return;
      loadTimeoutsRef.current.delete(timeoutId);
      const message = 'Le chargement des octets de l’image a expiré.';
      updateSteps((current) => current.map((step) => step.artifact_id === artifactId
        ? { ...step, loadState: 'error', loadError: message }
        : step));
      setLastError(message);
      if (!run) {
        backgroundLoadEnded = true;
        endBackgroundLoad();
        loadTokensRef.current.set(artifactId, token + 1);
      }
      if (run && activeRunRef.current === run) terminateRun(run, 'failed', message);
      else if (!activeRunRef.current) setCurrentState('failed');
    }, LOAD_TIMEOUT_MS);
    loadTimeoutsRef.current.add(timeoutId);

    try {
      const [bytes, mime] = await getAttachmentBytes(artifactId);
      if (!mountedRef.current || token !== (loadTokensRef.current.get(artifactId) ?? 0) || (run && (run.terminal || activeRunRef.current !== run))) return;
      if (typeof URL.createObjectURL !== 'function') throw new Error('Le navigateur ne peut pas créer d’URL d’image.');
      const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
      if (!url) throw new Error('URL d’image vide.');
      const previous = stepsRef.current.find((step) => step.artifact_id === artifactId)?.url;
      if (previous && previous !== url) revokeUrl(previous);
      objectUrlsRef.current.add(url);
      updateSteps((current) => current.map((step) => step.artifact_id === artifactId
        ? { ...step, url, loadState: 'ready', loadError: undefined }
        : step));
      if (run) {
        run.bytesReady = true;
        completeRun(run);
      } else if (!activeRunRef.current) {
        backgroundLoadEnded = true;
        endBackgroundLoad();
        setCurrentState('succeeded');
        setLastError(null);
      }
    } catch (error) {
      if (!mountedRef.current || token !== (loadTokensRef.current.get(artifactId) ?? 0) || (run && (run.terminal || activeRunRef.current !== run))) return;
      const message = messageOf(error, 'Impossible de charger les octets de l’image.');
      updateSteps((current) => current.map((step) => step.artifact_id === artifactId
        ? { ...step, loadState: 'error', loadError: message }
        : step));
      setLastError(message);
      if (run && activeRunRef.current === run) terminateRun(run, 'failed', message);
      else if (!activeRunRef.current) {
        backgroundLoadEnded = true;
        endBackgroundLoad();
        setCurrentState('failed');
      }
    } finally {
      window.clearTimeout(timeoutId);
      loadTimeoutsRef.current.delete(timeoutId);
      if (!run && !backgroundLoadEnded) endBackgroundLoad();
    }
  }, [beginBackgroundLoad, completeRun, endBackgroundLoad, revokeUrl, setCurrentState, terminateRun, updateSteps]);

  const acceptArtifact = useCallback((run: ActiveRun, artifactId: string) => {
    if (!artifactId) return false;
    if (run.artifactId && run.artifactId !== artifactId) return false;
    run.artifactId = artifactId;
    const existing = stepsRef.current.find((step) => step.artifact_id === artifactId);
    if (!existing) {
      const step: TimelineStep = {
        artifact_id: artifactId,
        prompt: run.prompt,
        aspect_ratio: run.aspect_ratio,
        seed: run.seed,
        loadState: 'idle',
      };
      updateSteps((current) => [...current, step]);
    }
    setSelectedId(artifactId);
    setProgress((current) => ({
      done: current?.done ?? [],
      currentId: 'transfert',
      label: 'Transfert de l’image vers l’atelier',
      step: null,
      total: null,
      startedAt: current?.startedAt ?? Date.now(),
    }));
    setCurrentState('loading');
    void loadArtifact(artifactId, run);
    return true;
  }, [loadArtifact, setCurrentState, updateSteps]);

  const handleRunStarted = useCallback((event: RunStartedEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    markRunAccepted(run, event.run_id ?? run.requestId);
    touchRun(run);
  }, [markRunAccepted, touchRun]);

  const handleRunStage = useCallback((event: RunStageEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    markRunAccepted(run, event.run_id ?? run.requestId);
    touchRun(run);
    setCurrentState(stateRef.current === 'loading' ? 'loading' : 'generating');
    setProgress((current) => advanceRunProgress(current, event));
  }, [markRunAccepted, setCurrentState, touchRun]);

  const handleRunFailed = useCallback((event: RunFailedEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    touchRun(run);
    terminateRun(run, 'failed', `[${event.code}] ${event.message || 'Le run image a échoué.'}`);
  }, [terminateRun, touchRun]);

  const handleRunCompleted = useCallback((event: RunCompletedEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    markRunAccepted(run, event.run_id ?? run.requestId);
    touchRun(run);
    const status = (event.status ?? 'completed').toLowerCase();
    if (status === 'cancelled') {
      terminateRun(run, 'cancelled', 'Run annulé.');
      return;
    }
    if (status === 'failed' || status === 'error') {
      terminateRun(run, 'failed', 'Le run image s’est terminé en échec.');
      return;
    }
    run.runCompleted = true;
    const artifactId = event.artifact_id ?? run.artifactId;
    if (!artifactId) {
      terminateRun(run, 'failed', 'Le run s’est terminé sans identifiant d’image.');
      return;
    }
    if (!acceptArtifact(run, artifactId)) {
      terminateRun(run, 'failed', 'Le run a terminé avec un identifiant d’image incohérent.');
      return;
    }
    if (run.bytesReady) completeRun(run);
  }, [acceptArtifact, completeRun, markRunAccepted, terminateRun, touchRun]);

  const handleRunCancelling = useCallback((event: RunCancellingEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    touchRun(run);
    setCancelPending(true);
    setProgress((current) => current ? { ...current, label: 'Arrêt demandé…' } : current);
  }, [touchRun]);

  const handleRunCancelled = useCallback((event: RunCancelledEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    terminateRun(run, 'cancelled', event.message || 'Run annulé.');
  }, [terminateRun]);

  const handleArtifactCreated = useCallback((event: ArtifactCreatedEvent) => {
    const run = activeRunRef.current;
    if (!run || run.terminal || event.request_id !== run.requestId) return;
    markRunAccepted(run, event.run_id ?? run.requestId);
    touchRun(run);
    if (!acceptArtifact(run, event.artifact_id)) {
      terminateRun(run, 'failed', 'Le run a publié un identifiant d’image incohérent.');
    }
  }, [acceptArtifact, markRunAccepted, terminateRun, touchRun]);

  const handleError = useCallback((event: ErrorEvent) => {
    const run = activeRunRef.current;
    if (run && !run.terminal && event.request_id === run.requestId) {
      touchRun(run);
      terminateRun(run, 'failed', `[${event.code}] ${event.message || 'Erreur du moteur image.'}`);
      return;
    }
    if (!run && event.code === 'IMAGE_UNAVAILABLE') {
      setLastError(event.message || 'Moteur image indisponible.');
    }
  }, [terminateRun, touchRun]);

  const handleBackendTerminated = useCallback((event: BackendTerminatedEvent) => {
    const run = activeRunRef.current;
    if (run && !run.terminal) {
      terminateRun(run, 'failed', `Sidecar arrêté : ${event.message || 'le processus du moteur a pris fin.'}`);
      return;
    }
    if (!run) setLastError(`Sidecar arrêté : ${event.message || 'le processus du moteur a pris fin.'}`);
  }, [terminateRun]);

  const requestEngineStatus = useCallback(() => {
    if (!mountedRef.current) return;
    const requestId = createId('status');
    const sequence = engineRequestSeqRef.current + 1;
    engineRequestSeqRef.current = sequence;
    engineRequestIdRef.current = requestId;
    imageStatus(requestId).catch((error: unknown) => {
      if (!mountedRef.current || engineRequestSeqRef.current !== sequence) return;
      setLastError(`Diagnostic moteur indisponible : ${messageOf(error, 'commande injoignable.')}`);
    });
  }, []);

  // Quitter la page (retour navigateur inclus) abandonne le run : sans cela le
  // worker/VRAM reste occupé et toute la navigation paraît morte. Déclaré AVANT
  // l'effet principal pour que ce cleanup passe en premier, avant que
  // `activeRunRef.current` ne soit remis à null. Un run terminal n'est pas annulé.
  useEffect(() => () => {
    const run = activeRunRef.current;
    if (!run || run.terminal) return;
    // Marquer terminal AVANT l'appel asynchrone : les handlers en vol
    // (staging, imageGenerate) sortiront sur leur garde, et le cleanup
    // principal pourra encore annuler les timers.
    run.terminal = true;
    if (!run.submitted) {
      inferenceRef.current.end(run.requestId);
    } else if (!run.runCompleted) {
      void cancelChat(run.requestId).catch(() => undefined);
    }
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => { if (e.code === 'Space') spaceDownRef.current = true; };
    const onKeyUp = (e: KeyboardEvent) => { if (e.code === 'Space') spaceDownRef.current = false; };
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    mountedRef.current = true;
    const params = new URLSearchParams(window.location.search);
    const editId = params.get('edit');
    if (editId) {
      void openEditorFromArtifact(editId);
      params.delete('edit');
      const next = `${window.location.pathname}${params.toString() ? '?' + params.toString() : ''}${window.location.hash}`;
      // React Router stocke `usr`, `key` et `idx` dans `history.state` : écraser
      // cet objet désynchronise la pile et casse le bouton retour après l'édition.
      window.history.replaceState({ ...(window.history.state || {}) }, '', next);
    }
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      spaceDownRef.current = false;
      mountedRef.current = false;
      const run = activeRunRef.current;
      if (run) {
        clearRunTimeout(run);
        clearAckTimeout(run);
      }
      for (const timeoutId of loadTimeoutsRef.current) window.clearTimeout(timeoutId);
      loadTimeoutsRef.current.clear();
      loadTokensRef.current.clear();
      for (const url of objectUrlsRef.current) {
        if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
      }
      objectUrlsRef.current.clear();
      activeRunRef.current = null;
    };
  }, [clearAckTimeout, clearRunTimeout, openEditorFromArtifact]);

  useEffect(() => {
    let alive = true;
    imageList({ offset: 0, limit: 24 }).then((page) => {
      if (!alive) return;
      setHiddenCount(page.items.filter((item) => item.deprecated || isDeprecatedQuality(item.quality)).length);
    }).catch((error: unknown) => {
      if (alive) setLastError(messageOf(error, 'La galerie ne répond pas.'));
    });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!progress || !['queued', 'generating', 'loading'].includes(studioState)) return;
    const interval = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - progress.startedAt) / 1000));
    }, 500);
    return () => window.clearInterval(interval);
  }, [progress, studioState]);

  useEffect(() => {
    let disposed = false;
    const unlisteners: Array<() => void> = [];
    const registrations = [
      onRunStarted(handleRunStarted),
      onRunStage(handleRunStage),
      onRunFailed(handleRunFailed),
      onRunCompleted(handleRunCompleted),
      onRunCancelling(handleRunCancelling),
      onRunCancelled(handleRunCancelled),
      onArtifactCreated(handleArtifactCreated),
      onBackendTerminated(handleBackendTerminated),
      onError(handleError),
      onImageStatus((status) => {
        if (!mountedRef.current || status.request_id !== engineRequestIdRef.current) return;
        setEngine(status);
      }),
    ];
    void Promise.all(registrations).then((unlistenersFromRegistrations) => {
      if (disposed) {
        for (const unlisten of unlistenersFromRegistrations) unlisten();
        return;
      }
      unlisteners.push(...unlistenersFromRegistrations);
      if (mountedRef.current) setListenersReady(true);
    }).catch((error: unknown) => {
      if (!disposed && mountedRef.current) setLastError(messageOf(error, 'Impossible d’écouter les événements du moteur.'));
    });
    return () => {
      disposed = true;
      for (const unlisten of unlisteners) unlisten();
    };
  }, [handleArtifactCreated, handleBackendTerminated, handleError, handleRunCancelled, handleRunCancelling, handleRunCompleted, handleRunFailed, handleRunStage, handleRunStarted]);

  useEffect(() => {
    if (!listenersReady) return;
    requestEngineStatus();
  }, [listenersReady, requestEngineStatus]);

  const handleReferenceImport = useCallback(async (files: FileList | File[] | null) => {
    if (!files) return;
    const list = Array.from(files);
    setReferenceError(null);
    if (list.length === 0) return;
    if (references.length >= MAX_CREATION_REFERENCES) {
      setReferenceError(`${MAX_CREATION_REFERENCES} références maximum.`);
      return;
    }
    const validation = validateAttachments(list);
    const valid = validation.files.filter((file) => file.type.startsWith('image/') || /\.(png|jpe?g|webp)$/i.test(file.name));
    if (valid.length === 0) {
      setReferenceError('Aucune image valide (PNG/JPG/WEBP).');
      return;
    }
    const added: EditorReference[] = [];
    try {
      for (const file of valid) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const url = URL.createObjectURL(file);
        objectUrlsRef.current.add(url);
        added.push({ id: createId('ref'), file, bytes, url, name: file.name, size: file.size });
      }
      setReferences((current) => {
        const next = [...current, ...added].slice(0, MAX_CREATION_REFERENCES);
        const retained = new Set(next.map((ref) => ref.id));
        for (const ref of added) {
          if (!retained.has(ref.id)) revokeUrl(ref.url);
        }
        return next;
      });
      const refuses = Math.max(0, references.length + added.length - MAX_CREATION_REFERENCES);
      const notices = [...validation.errors];
      if (refuses > 0) notices.push(`${refuses} fichier(s) non ajouté(s) : ${MAX_CREATION_REFERENCES} références maximum.`);
      if (notices.length > 0) setReferenceError(notices.join(' · '));
    } catch {
      for (const ref of added) revokeUrl(ref.url);
      setReferenceError('Impossible de lire une image de référence.');
    }
  }, [references.length, revokeUrl]);

  const handleEditorReferenceImport = useCallback(async (files: FileList | File[] | null) => {
    if (!files) return;
    const list = Array.from(files);
    setEditorReferenceError(null);
    if (list.length === 0) return;
    const room = 3 - editorReferences.length;
    if (room <= 0) {
      setEditorReferenceError('3 références supplémentaires maximum (4 images au total avec l\'image en cours).');
      return;
    }
    const validation = validateAttachments(list);
    if (validation.errors.length > 0) {
      setEditorReferenceError(validation.errors.join(' · '));
      return;
    }
    const candidates = validation.files
      .filter((file) => file.type.startsWith('image/') || /\.(png|jpe?g|webp)$/i.test(file.name));
    const accepted = candidates.slice(0, room);
    if (accepted.length === 0) {
      setEditorReferenceError('Aucune image valide (PNG/JPG/WEBP).');
      return;
    }
    const added: EditorReference[] = [];
    try {
      for (const file of accepted) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const url = URL.createObjectURL(file);
        objectUrlsRef.current.add(url);
        added.push({ id: createId('ref'), file, bytes, url, name: file.name, size: file.size });
      }
      // L'updater garantit la borne atomique face aux imports concurrents ; les URLs écartées y sont révoquées.
      setEditorReferences((current) => {
        const next = [...current, ...added].slice(0, 3);
        const retained = new Set(next.map((ref) => ref.id));
        for (const ref of added) {
          if (!retained.has(ref.id)) revokeUrl(ref.url);
        }
        return next;
      });
      const refuses = candidates.length - accepted.length;
      if (refuses > 0) {
        setEditorReferenceError(`${refuses} fichier(s) non ajouté(s) : 3 références supplémentaires maximum.`);
      }
    } catch {
      for (const ref of added) revokeUrl(ref.url);
      setEditorReferenceError('Impossible de lire une image de référence.');
    }
  }, [editorReferences.length, revokeUrl]);

  const removeEditorReference = useCallback((id: string) => {
    setEditorReferences((current) => {
      const found = current.find((ref) => ref.id === id);
      if (found) revokeUrl(found.url);
      return current.filter((ref) => ref.id !== id);
    });
  }, [revokeUrl]);

  const handleGenerate = useCallback(async () => {
    if (!listenersReady) {
      setLastError('Connexion au moteur en cours. Réessayez dans un instant.');
      return;
    }
    const trimmedPrompt = prompt.trim();
    const parsedSeed = seedFromInput(seedInput);
    if (!trimmedPrompt) {
      setLastError('Décrivez l’image à créer.');
      return;
    }
    if (trimmedPrompt.length > MAX_PROMPT) {
      setLastError(`L’instruction est limitée à ${MAX_PROMPT} caractères.`);
      return;
    }
    if (parsedSeed.error) {
      setSeedError(parsedSeed.error);
      return;
    }
    if (!isTerminal(stateRef.current) && stateRef.current !== 'idle') return;
    if (inferenceRef.current.memory.status === 'running') {
      setLastError('Un entretien mémoire est en cours. Attendez sa fin avant de lancer une génération d’image.');
      return;
    }
    const otherActivity = inferenceRef.current.activity;
    if (otherActivity.active) {
      setLastError(otherActivity.kind === 'image'
        ? 'Une génération d’image est en cours. Attendez sa fin avant d’en lancer une autre.'
        : 'Une discussion est en cours. Attendez sa fin avant de lancer une génération d’image.');
      return;
    }
    setSeedError(null);
    setLastError(null);
    setCancelPending(false);
    setProgress({ done: [], currentId: 'demarrage', label: 'Envoi…', step: null, total: null, startedAt: Date.now() });
    setElapsed(0);
    const run: ActiveRun = {
      origin: 'create',
      requestId: createId('image'),
      prompt: trimmedPrompt,
      aspect_ratio: format,
      seed: parsedSeed.seed ?? creationAutoSeed,
      runCompleted: false,
      bytesReady: false,
      submitted: false,
      terminal: false,
      accepted: false,
      timeoutId: null,
      ackTimeoutId: null,
    };
    activeRunRef.current = run;
    setCurrentState('queued');
    const started = inferenceRef.current.begin({ kind: 'image', requestId: run.requestId });
    if (!started) {
      run.terminal = true;
      activeRunRef.current = null;
      setCurrentState('idle');
      setProgress(null);
      setCancelPending(false);
      const current = inferenceRef.current.activity;
      setLastError(current.kind === 'image'
        ? 'Une génération d’image est en cours. Attendez sa fin avant d’en lancer une autre.'
        : 'Une discussion est en cours. Attendez sa fin avant de lancer une génération d’image.');
      return;
    }
    armRunTimeout(run);
    const referenceTokens: string[] = [];
    const referenceShas: string[] = [];
    for (const ref of references) {
      try {
        const tokenRes = await imageStageWork(ref.bytes, 'image/png');
        referenceTokens.push(tokenRes.token);
        referenceShas.push(await enc.sha256Bytes(ref.bytes));
      } catch (error) {
        terminateRun(run, 'failed', messageOf(error, 'Impossible de préparer la référence.'));
        return;
      }
    }
    if (run.terminal || activeRunRef.current !== run) return;
    run.ackTimeoutId = window.setTimeout(() => {
      if (!mountedRef.current || run.terminal || run.accepted || activeRunRef.current !== run) return;
      run.ackTimeoutId = null;
      cancelBeforeTerminal(run, 'Le sidecar n’a pas accusé la demande. Elle a été annulée avant de lancer un worker.');
    }, ACK_TIMEOUT_MS);
    try {
      run.submitted = true;
      if (referenceTokens.length > 0) {
        await imageGenerate({
          request_id: run.requestId,
          prompt: run.prompt,
          aspect_ratio: run.aspect_ratio,
          mode: 'reference',
          reference_tokens: referenceTokens,
          reference_sha256: referenceShas,
          strength: 0.65,
          ...(run.seed !== undefined ? { seed: run.seed } : {}),
          idempotency_key: createId('idempotency'),
        });
      } else {
        await imageGenerate({
          request_id: run.requestId,
          prompt: run.prompt,
          aspect_ratio: run.aspect_ratio,
          mode: 'create',
          ...(run.seed !== undefined ? { seed: run.seed } : {}),
          idempotency_key: createId('idempotency'),
        });
      }
      if (activeRunRef.current === run && !run.terminal) {
        showToast({ type: 'info', title: 'Génération demandée', message: 'Le run image local a été remis au moteur.' });
      }
    } catch (error) {
      if (activeRunRef.current === run && !run.terminal) {
        terminateRun(run, 'failed', messageOf(error, 'Impossible de lancer la génération.'));
      }
    }
  }, [armRunTimeout, cancelBeforeTerminal, creationAutoSeed, format, imageGenerate, imageStageWork, listenersReady, prompt, references, seedInput, setCurrentState, terminateRun]);

  const handleCancel = useCallback(async () => {
    const run = activeRunRef.current;
    if (!run || run.terminal || run.runCompleted) return;
    setCancelPending(true);
    try {
      await cancelChat(run.requestId);
    } catch (error) {
      if (mountedRef.current) setCancelPending(false);
      setLastError(`Arrêt impossible : ${messageOf(error, 'le moteur ne répond pas.')}`);
    }
  }, []);

  const handleSwitch = useCallback(async (enabled: boolean) => {
    setSwitching(true);
    try {
      await imageSetBackend(enabled);
      requestEngineStatus();
      setLastError(null);
    } catch (error) {
      setLastError(messageOf(error, 'Impossible de modifier l’opt-in du moteur.'));
    } finally {
      if (mountedRef.current) setSwitching(false);
    }
  }, [requestEngineStatus]);

  const retrySelected = useCallback(() => {
    if (activeRunRef.current && !activeRunRef.current.terminal) return;
    const step = stepsRef.current.find((candidate) => candidate.artifact_id === selectedId);
    if (!step || step.loadState === 'loading') return;
    void loadArtifact(step.artifact_id, null);
  }, [loadArtifact, selectedId]);

  const handleModifierGenerate = useCallback(async () => {
    if (!editorSession || !listenersReady) return;
    const sessionSnapshot = editorSession;
    let plan: ReturnType<typeof planModifierRequest>;
    try {
      plan = planModifierRequest(sessionSnapshot);
    } catch (error) {
      showToast({ type: 'error', title: 'Génération impossible', message: messageOf(error, 'Conflit de masques.') });
      return;
    }
    const instruction = (sessionSnapshot.prompt ?? '').trim();
    const promptRequired = plan.mode === 'global' || plan.mode === 'local';
    if (promptRequired && !instruction) {
      showToast({ type: 'error', title: 'Instruction requise', message: 'Décrivez la modification à appliquer dans le champ Instruction.' });
      return;
    }
    const promptToSend = plan.promptToSend;
    const chosenMask = plan.chosenMask;
    const mode = plan.mode;
    const strength = plan.strength;
    const maskKind = plan.maskKind;

    if (activeRunRef.current && !activeRunRef.current.terminal) return;
    if (inferenceRef.current.memory.status === 'running') {
      setLastError('Un entretien mémoire est en cours. Attendez sa fin avant de lancer une génération d’image.');
      return;
    }
    const otherActivity = inferenceRef.current.activity;
    if (otherActivity.active) {
      setLastError(otherActivity.kind === 'image'
        ? 'Une génération d’image est en cours. Attendez sa fin avant d’en lancer une autre.'
        : 'Une discussion est en cours. Attendez sa fin avant de lancer une génération d’image.');
      return;
    }

    const run: ActiveRun = {
      origin: 'edit',
      sessionId: sessionSnapshot.id,
      revision: sessionSnapshot.revision,
      activeHistoryId: sessionSnapshot.activeHistoryId,
      snapshotHash: '',
      maskHash: undefined,
      requestId: createId('image'),
      prompt: promptToSend,
      aspect_ratio: (sessionSnapshot.format.id as FormatId),
      seed: sessionSnapshot.seed ?? editorAutoSeed,
      runCompleted: false,
      bytesReady: false,
      submitted: false,
      terminal: false,
      accepted: false,
      timeoutId: null,
      ackTimeoutId: null,
    };
    activeRunRef.current = run;
    setCurrentState('queued');
    const started = inferenceRef.current.begin({ kind: 'image', requestId: run.requestId });
    if (!started) {
      run.terminal = true;
      activeRunRef.current = null;
      setCurrentState('idle');
      setProgress(null);
      setCancelPending(false);
      const current = inferenceRef.current.activity;
      setLastError(current.kind === 'image'
        ? 'Une génération d’image est en cours. Attendez sa fin avant d’en lancer une autre.'
        : 'Une discussion est en cours. Attendez sa fin avant de lancer une génération d’image.');
      return;
    }
    setProgress({ done: [], currentId: 'demarrage', label: 'Envoi…', step: null, total: null, startedAt: Date.now() });
    setElapsed(0);
    armRunTimeout(run);

    try {
      // Le masque est envoyé séparément ; le moteur doit recevoir le contenu source complet. Effacer la work image avec le masque cuirait une zone grise.
      const workCanvas = enc.drawWorkCanvas(plan.canvasWidth, plan.canvasHeight, sessionSnapshot.image, plan.transform, null, plan.sourceSize);
      const workBlob = await enc.canvasToBlob(workCanvas);
      const { bytes: workBytes, sha256: workSha } = await enc.stageBytes(workBlob);
      const workRes = await imageStageWork(workBytes, 'image/png');
      const workToken = workRes.token;
      let maskToken: string | undefined;
      let maskSha: string | undefined;
      if (chosenMask) {
        const maskCanvas = maskCanvasFromMask(chosenMask, plan.canvasWidth, plan.canvasHeight, [255, 255, 255, 255]);
        const maskBlob = await enc.canvasToBlob(maskCanvas);
        const { bytes: maskBytes, sha256 } = await enc.stageBytes(maskBlob);
        maskSha = sha256;
        const maskRes = await imageStageWork(maskBytes, 'image/png');
        maskToken = maskRes.token;
      }

      const referenceTokens: string[] = [];
      const referenceShas: string[] = [];
      for (const ref of editorReferences) {
        const tokenRes = await imageStageWork(ref.bytes, 'image/png');
        referenceTokens.push(tokenRes.token);
        referenceShas.push(await enc.sha256Bytes(ref.bytes));
      }

      if (run.terminal || activeRunRef.current !== run) return;
      run.snapshotHash = workSha;
      run.maskHash = maskSha;
      run.ackTimeoutId = window.setTimeout(() => {
        if (!mountedRef.current || run.terminal || run.accepted || activeRunRef.current !== run) return;
        run.ackTimeoutId = null;
        cancelBeforeTerminal(run, 'Le sidecar n’a pas accusé la demande.');
      }, ACK_TIMEOUT_MS);

      try {
        run.submitted = true;
        await imageGenerate({
          request_id: run.requestId,
          prompt: run.prompt,
          aspect_ratio: sessionSnapshot.format.id,
          mode,
          source_artifact_id: sessionSnapshot.sourceArtifactId,
          work_token: workToken,
          ...(maskToken ? { mask_token: maskToken } : {}),
          work_sha256: workSha,
          ...(maskSha ? { mask_sha256: maskSha } : {}),
          ...(referenceTokens.length > 0
            ? { reference_tokens: referenceTokens, reference_sha256: referenceShas }
            : {}),
          canvas_width: plan.canvasWidth,
          canvas_height: plan.canvasHeight,
          strength,
          session_id: sessionSnapshot.id,
          revision: sessionSnapshot.revision,
          mask_kind: maskKind,
          source_transform: plan.transform,
          source_size: plan.sourceSize,
          ...(run.seed !== undefined ? { seed: run.seed } : {}),
          idempotency_key: createId('idempotency'),
        });
        if (activeRunRef.current === run && !run.terminal) {
          showToast({ type: 'info', title: 'Modification demandée', message: 'Le run image local a été remis au moteur.' });
        }
      } catch (error) {
        if (activeRunRef.current === run && !run.terminal) terminateRun(run, 'failed', messageOf(error, 'Impossible de lancer la modification.'));
      }
    } catch (error) {
      const message = messageOf(error, 'Impossible de lancer la modification.');
      if (!terminateRun(run, 'failed', message)) setLastError(message);
    }
  }, [armRunTimeout, cancelBeforeTerminal, editorAutoSeed, editorReferences, editorSession, imageGenerate, imageStageWork, listenersReady, setCurrentState, terminateRun]);


  const confirmCrop = useCallback(async () => {
    if (!editorSession?.cropPreview) return;
    const sess = editorSession;
    const crop = sess.cropPreview as geom.Rect | null;
    if (!crop || crop.width <= 1 || crop.height <= 1) return;
    const transform = sess.transform;
    const intersected = {
      x: Math.max(crop.x, transform.x),
      y: Math.max(crop.y, transform.y),
      width: Math.min(crop.x + crop.width, transform.x + transform.width) - Math.max(crop.x, transform.x),
      height: Math.min(crop.y + crop.height, transform.y + transform.height) - Math.max(crop.y, transform.y),
    };
    if (intersected.width <= 1 || intersected.height <= 1) return;
    const sourceRect = geom.cropDocumentRectToSource(intersected, transform, sess.sourceSize.width, sess.sourceSize.height);
    const clamped = {
      x: Math.max(0, sourceRect.x),
      y: Math.max(0, sourceRect.y),
      width: Math.min(sourceRect.width, Math.max(1, sess.sourceSize.width - sourceRect.x)),
      height: Math.min(sourceRect.height, Math.max(1, sess.sourceSize.height - sourceRect.y)),
    };
    if (clamped.width <= 1 || clamped.height <= 1) return;
    const off = document.createElement('canvas');
    off.width = Math.max(1, Math.round(clamped.width));
    off.height = Math.max(1, Math.round(clamped.height));
    const ctx = off.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(sess.image, clamped.x, clamped.y, clamped.width, clamped.height, 0, 0, off.width, off.height);
    const url = off.toDataURL('image/png');
    try {
      const img = await loadImage(url);
      setEditorSession((current) => {
        if (!current || current.id !== sess.id) return current;
        return ie.applyCrop(sess, intersected, img, url);
      });
    } catch (error) {
      setLastError(messageOf(error, 'Impossible de recadrer.'));
    }
  }, [editorSession]);

  // pointer handlers bundled in JSX for clarity
  const getHandles = (transform: ie.EditorSession['transform'], currentView: { zoom: number; panX: number; panY: number }) => {
    const pts: Array<{ name: string; docX: number; docY: number }> = [
      { name: 'nw', docX: transform.x, docY: transform.y },
      { name: 'ne', docX: transform.x + transform.width, docY: transform.y },
      { name: 'sw', docX: transform.x, docY: transform.y + transform.height },
      { name: 'se', docX: transform.x + transform.width, docY: transform.y + transform.height },
      { name: 'n', docX: transform.x + transform.width / 2, docY: transform.y },
      { name: 's', docX: transform.x + transform.width / 2, docY: transform.y + transform.height },
      { name: 'w', docX: transform.x, docY: transform.y + transform.height / 2 },
      { name: 'e', docX: transform.x + transform.width, docY: transform.y + transform.height / 2 },
    ];
    return pts.map((p) => ({ name: p.name, screenX: p.docX * currentView.zoom + currentView.panX, screenY: p.docY * currentView.zoom + currentView.panY }));
  };

  const renderEditorCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !editorSession) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    canvas.width = editorSession.canvasWidth;
    canvas.height = editorSession.canvasHeight;

    ctx.setTransform(view.zoom, 0, 0, view.zoom, view.panX, view.panY);
    // Damier dessiné dans les coordonnées du document : il zoome avec l'image.
    const size = 16;
    for (let y = 0; y < canvas.height; y += size) {
      for (let x = 0; x < canvas.width; x += size) {
        ctx.fillStyle = ((x / size + y / size) % 2 === 0) ? '#d8d2c6' : '#f0ebe1';
        ctx.fillRect(x, y, size, size);
      }
    }
    ctx.strokeStyle = 'rgba(32, 37, 34, .45)';
    ctx.lineWidth = 1.5 / Math.max(view.zoom, .001);
    ctx.strokeRect(0, 0, canvas.width, canvas.height);

    const cssRect = canvas.getBoundingClientRect();
    const cssScale = Math.max(canvas.width / Math.max(1, cssRect.width), canvas.height / Math.max(1, cssRect.height));
    const transform = editorSession.transform;
    if (editorSession.image && transform) {
      ctx.drawImage(editorSession.image, transform.x, transform.y, transform.width, transform.height);
    }

    const out = ie.effectiveOutputMasks(editorSession);
    const holeCanvas = maskCanvasFromMask(out.holeMask, editorSession.canvasWidth, editorSession.canvasHeight, [0, 0, 0, 255]);
    ctx.globalCompositeOperation = 'destination-out';
    ctx.drawImage(holeCanvas, 0, 0, editorSession.canvasWidth, editorSession.canvasHeight);
    ctx.globalCompositeOperation = 'source-over';

    const editCanvas = maskCanvasFromMask(out.editMask, editorSession.canvasWidth, editorSession.canvasHeight, [138, 60, 220, 110]);
    ctx.drawImage(editCanvas, 0, 0, editorSession.canvasWidth, editorSession.canvasHeight);

    if (editorTool === 'move' && transform) {
      ctx.strokeStyle = '#a7653f';
      ctx.lineWidth = (1.5 * cssScale) / Math.max(view.zoom, 0.001);
      ctx.strokeRect(transform.x, transform.y, transform.width, transform.height);
      const handles = getHandles(transform as any, view);
      ctx.fillStyle = '#a7653f';
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = (1.5 * cssScale) / Math.max(view.zoom, 0.001);
      const handleHalf = (6 * cssScale) / Math.max(view.zoom, 0.001);
      handles.forEach((handle) => {
        const x = handle.screenX / view.zoom - view.panX / view.zoom;
        const y = handle.screenY / view.zoom - view.panY / view.zoom;
        ctx.fillRect(x - handleHalf, y - handleHalf, handleHalf * 2, handleHalf * 2);
        ctx.strokeRect(x - handleHalf, y - handleHalf, handleHalf * 2, handleHalf * 2);
      });
    }

    if (snapGuides.x != null || snapGuides.y != null) {
      ctx.save();
      ctx.setLineDash([6 / Math.max(view.zoom, .001), 4 / Math.max(view.zoom, .001)]);
      ctx.strokeStyle = '#a7653f';
      ctx.lineWidth = 1 / Math.max(view.zoom, .001);
      if (snapGuides.x != null) {
        ctx.beginPath();
        ctx.moveTo(snapGuides.x, 0);
        ctx.lineTo(snapGuides.x, editorSession.canvasHeight);
        ctx.stroke();
      }
      if (snapGuides.y != null) {
        ctx.beginPath();
        ctx.moveTo(0, snapGuides.y);
        ctx.lineTo(editorSession.canvasWidth, snapGuides.y);
        ctx.stroke();
      }
      ctx.restore();
    }

    const crop = editorSession.cropPreview as { x: number; y: number; width: number; height: number } | null;
    if (crop) {
      ctx.strokeStyle = '#2a7fff';
      ctx.lineWidth = (1.5 * cssScale) / Math.max(view.zoom, 0.001);
      ctx.strokeRect(crop.x, crop.y, crop.width, crop.height);
    }

    const preview = brushPreviewRef.current;
    if ((editorTool === 'eraser' || editorTool === 'magic') && preview && !isEditingBusyRef.current) {
      ctx.beginPath();
      ctx.arc(preview.x, preview.y, brushSize / 2, 0, Math.PI * 2);
      ctx.strokeStyle = editorTool === 'eraser' ? '#a7653f' : '#8a3cdc';
      ctx.lineWidth = 2 / Math.max(view.zoom, 0.001);
      ctx.stroke();
    }
  }, [editorSession, view, editorTool, brushSize, snapGuides]);

  useEffect(() => {
    renderEditorCanvas();
  }, [renderEditorCanvas, editorSession, view, editorTool, snapGuides, editorSession?.cropPreview, editorSession?.revision]);

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !editorSession) return;
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      if (isEditingBusyRef.current) return;
      const rect = canvas.getBoundingClientRect();
      const rectWidth = rect.width === 0 ? canvas.width : rect.width;
      const rectHeight = rect.height === 0 ? canvas.height : rect.height;
      if (rectWidth === 0 || rectHeight === 0) return;
      const sx = (event.clientX - rect.left) * (canvas.width / rectWidth);
      const sy = (event.clientY - rect.top) * (canvas.height / rectHeight);
      setView((current) => {
        const factor = event.deltaY < 0 ? 1.1 : 0.9;
        const newZoom = Math.min(8, Math.max(0.1, current.zoom * factor));
        const panX = sx - (sx - current.panX) * (newZoom / current.zoom);
        const panY = sy - (sy - current.panY) * (newZoom / current.zoom);
        return { zoom: newZoom, panX, panY };
      });
    };
    canvas.addEventListener('wheel', handleWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', handleWheel);
  }, [editorSession]);

  const formatDecision = editorSession ? ie.canChangeFormat(editorSession) : { allowed: true };
  const transformDecision = editorSession ? ie.canTransform(editorSession) : { allowed: true };
  const editorVram =
    engine?.details?.cuda === true
    && typeof engine.details.free_vram_gb === 'number' && Number.isFinite(engine.details.free_vram_gb)
    && typeof engine.details.total_vram_gb === 'number' && Number.isFinite(engine.details.total_vram_gb)
      ? { freeGb: engine.details.free_vram_gb, totalGb: engine.details.total_vram_gb }
      : null;

  const hasPrompt = prompt.trim().length > 0;
  const busy = ['queued', 'generating', 'loading'].includes(studioState);
  const blockedByOther = (inference.activity.active || inference.memory.status === 'running') && !busy;
  const blockedLabel = blockedByOther && inference.memory.status === 'running' ? 'Entretien mémoire…' : 'Moteur occupé…';
  const isEditingBusy = busy && editorSession !== null;
  isEditingBusyRef.current = isEditingBusy;
  let genMode: 'global' | 'local' | 'inpaint' | 'outpaint' = 'global';
  if (editorSession) {
    try {
      genMode = ie.resolveGeneration(editorSession).mode;
    } catch {
      genMode = 'global';
    }
  }
  const promptNeeded = genMode === 'global' || genMode === 'local';
  const canCancel = busy && activeRunRef.current !== null && !activeRunRef.current.terminal && !activeRunRef.current.runCompleted;
  const selected = steps.find((step) => step.artifact_id === selectedId) ?? steps[steps.length - 1] ?? null;
  const statusLabel: Record<StudioState, string> = {
    idle: 'Prêt à créer',
    queued: 'En attente du moteur',
    generating: 'Génération',
    loading: 'Chargement des octets',
    succeeded: 'Image prête',
    failed: 'Échec',
    cancelled: 'Annulé',
  };
  const visibleStatusLabel = !busy && backgroundLoadCount > 0 ? 'Chargement de l’aperçu' : statusLabel[studioState];

  const formatPills = (
    <div className="img-formats" role="group" aria-label="Format de sortie">
      {FORMATS.map((preset) => (
        <button
          key={preset.id}
          type="button"
          className="img-format"
          aria-label={`${preset.id} ${preset.width}x${preset.height}`}
          data-dimensions={`${preset.width}x${preset.height}`}
          aria-pressed={format === preset.id}
          onClick={() => setFormat(preset.id)}
        >
          <span>{preset.id}</span>
          <small>{preset.width} × {preset.height}</small>
        </button>
      ))}
    </div>
  );

  const controls = (
    <>
      <form
        className="img-prompt-form"
        onSubmit={(event) => {
          event.preventDefault();
          void handleGenerate();
        }}
      >
        <label className="img-field-label" htmlFor="image-prompt">Instruction</label>
        <textarea
          id="image-prompt"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          rows={5}
          maxLength={MAX_PROMPT}
          placeholder="Décrivez ce que vous souhaitez créer…"
          aria-describedby="prompt-count"
        />
        <div className="img-field-meta" id="prompt-count">{prompt.length} / {MAX_PROMPT}</div>
        <div className="img-seed-row">
          <div className="img-seed-field">
            <label className="img-field-label" htmlFor="image-seed">Graine <span>{seedInput.trim() === '' ? 'aléatoire' : 'fixe'}</span></label>
            <input
              id="image-seed"
              type="number"
              inputMode="numeric"
              min={0}
              max={MAX_SEED}
              step={1}
              value={seedInput}
              onChange={(event) => {
                setSeedInput(event.target.value);
                setSeedError(null);
              }}
              placeholder={String(creationAutoSeed)}
              aria-invalid={seedError !== null}
              aria-describedby={seedError ? 'seed-error' : 'seed-help'}
            />
          </div>
          <button
            type="button"
            className="img-button img-button--quiet"
            onClick={() => {
              setSeedInput('');
              setSeedError(null);
              setCreationAutoSeed(randomSeedValue());
            }}
            aria-label="Revenir à une graine aléatoire"
          >
            Aléatoire
          </button>
        </div>
        <p className="img-field-help" id="seed-help">
          {seedInput.trim() === ''
            ? `Graine aléatoire pour la prochaine génération : ${creationAutoSeed}. Saisissez une valeur pour la fixer.`
            : 'Graine fixe : elle sera utilisée telle quelle pour cette génération.'}
        </p>
        {seedError && <p className="img-field-error" id="seed-error" role="alert">{seedError}</p>}
        <section className="img-reference-zone" aria-label="Références visuelles">
          <div className="img-zone-heading"><span className="img-kicker">Références</span><h2>Source visuelle <small>{references.length}/{MAX_CREATION_REFERENCES}</small></h2></div>
          <input
            ref={referenceInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            className="img-file-input"
            aria-label="Ajouter des références"
            onChange={(event) => {
              void handleReferenceImport(event.target.files);
              event.target.value = '';
            }}
          />
          <div
            className="img-drop-zone"
            role="button"
            tabIndex={0}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              void handleReferenceImport(event.dataTransfer.files);
            }}
          >
            <button type="button" className="img-button img-button--quiet" onClick={() => referenceInputRef.current?.click()} aria-label="Ajouter des références">
              + Ajouter une référence
            </button>
            <span>Glisser-déposer ou cliquer pour choisir ({MAX_CREATION_REFERENCES} références maximum)</span>
          </div>
          {references.length > 0 && (
            <div className="img-reference-list">
              {references.map((ref) => (
                <div className="img-reference-card" key={ref.id}>
                  <img src={ref.url} alt={ref.name} />
                  <div>
                    <strong title={ref.name}>{ref.name}</strong>
                    <span>{ref.size} o</span>
                  </div>
                  <div className="img-reference-actions">
                    <button
                      type="button"
                      onClick={() => {
                        revokeUrl(ref.url);
                        setReferences((current) => current.filter((entry) => entry.id !== ref.id));
                      }}
                      aria-label={`Retirer ${ref.name}`}
                    >
                      Retirer
                    </button>
                    <button type="button" onClick={() => void openEditorFromReference(ref)} aria-label={`Éditer l’image ${ref.name}`}>
                      Éditer l’image
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          {referenceError && <p className="img-field-error" role="alert">{referenceError}</p>}
        </section>
        {formatPills}
        <div className="img-form-actions">
          <button type="submit" className="img-button img-button--primary" disabled={!hasPrompt || busy || seedError !== null || !listenersReady || blockedByOther}>
            {!listenersReady ? 'Connexion au moteur…' : blockedByOther ? blockedLabel : busy ? 'Génération…' : 'Créer l’image →'}
          </button>
          {canCancel && (
            <button type="button" className="img-button img-button--stop" onClick={() => void handleCancel()} disabled={cancelPending}>
              {cancelPending ? 'Arrêt…' : 'Arrêter'}
            </button>
          )}
        </div>
      </form>
      <p className="img-tool-note">Presets officiels ≈1 MP · création à partir d’une instruction, avec ou sans images de référence.</p>
      {lastError && <div className="img-alert" role="alert">{lastError}</div>}
      {selected?.loadState === 'error' && (
        <button type="button" className="img-button img-button--quiet" onClick={retrySelected}>Réessayer le chargement</button>
      )}
    </>
  );


  const status = (
    <div className={`img-status img-status--${studioState}`} role="status" aria-live="polite">
      <span className="img-status-dot" aria-hidden="true" />
      <span>{visibleStatusLabel}</span>
      {studioState === 'cancelled' && <span className="img-status-detail">Aucune image ajoutée à la timeline.</span>}
      {studioState === 'succeeded' && <span className="img-status-detail">Les octets sont chargés localement.</span>}
    </div>
  );

  if (!editorSession) {
    return (
      <div className="img-page" aria-busy={busy}>
        <section className="img-hero" aria-labelledby="studio-title">
          <div className="img-hero-copy">
            <span className="img-kicker">Création d’image</span>
            <h1 id="studio-title">Images</h1>
            <p>Créez une image à partir de vos idées et de vos références.</p>
          </div>
          {status}
          {progress && busy && <ProgressCard progress={progress} elapsed={elapsed} />}
          {lastError && <div className="img-alert" role="alert">{lastError}</div>}
          <div className="img-prompt-card">
            {controls}
          </div>
          <EngineCard engine={engine} switching={switching} busy={busy} onSwitch={(enabled) => void handleSwitch(enabled)} />
          {hiddenCount > 0 && <p className="img-footnote">{hiddenCount} essai{hiddenCount > 1 ? 's' : ''} obsolète{hiddenCount > 1 ? 's' : ''} masqué{hiddenCount > 1 ? 's' : ''} · visible{hiddenCount > 1 ? 's' : ''} en Galerie.</p>}
        </section>
      </div>
    );
  }

  return (
    <div className="img-page img-page--workspace" aria-busy={busy}>
      <div className="img-workspace">
        <aside className="img-timeline" aria-label="Historique de la session">
          <div className="img-zone-heading">
            <span className="img-kicker">Session</span>
          </div>
          <div className="img-timeline-list">
            {editorSession ? (
              editorSession.history.map((entry, index) => {
                const resultNumber = editorSession.history
                  .slice(0, index + 1)
                  .filter((item) => item.kind !== 'source').length;
                const label = entry.kind === 'source' ? 'Source' : `Résultat ${resultNumber}`;
                const active = entry.id === editorSession.activeHistoryId;
                return (
                  <div className="img-timeline-item" key={entry.id}>
                    <button
                      type="button"
                      className={`img-timeline-button${active ? ' is-current' : ''}`}
                      disabled={isEditingBusy}
                      onClick={() => {
                        if (isEditingBusy || !editorSession) return;
                        setEditorSession((current) => (current ? ie.selectHistoryBase(current, entry.id) : current));
                        setView({ zoom: 1, panX: 0, panY: 0 });
                      }}
                      aria-label={`Afficher ${label}`}
                      aria-current={active ? 'true' : undefined}
                    >
                      <img src={entry.url} alt="" />
                    </button>
                    <span className="img-timeline-index">{entry.kind === 'source' ? 'S' : String(resultNumber).padStart(2, '0')}</span>
                  </div>
                );
              })
            ) : (
              steps.map((step, index) => (
                <div className="img-timeline-item" key={step.artifact_id}>
                  <button
                    type="button"
                    className={`img-timeline-button${step.artifact_id === selected?.artifact_id ? ' is-current' : ''}`}
                    onClick={() => setSelectedId(step.artifact_id)}
                    aria-label={`Voir l’image ${index + 1}${step.prompt ? ` : ${step.prompt.slice(0, 60)}` : ''}`}
                    aria-current={step.artifact_id === selected?.artifact_id ? 'true' : undefined}
                  >
                    {step.url ? <img src={step.url} alt="" /> : <span className="img-timeline-placeholder">{step.loadState === 'error' ? '!' : '…'}</span>}
                  </button>
                  <span className="img-timeline-index">{String(index + 1).padStart(2, '0')}</span>
                </div>
              ))
            )}
          </div>
        </aside>

        <section className="img-canvas-zone" aria-labelledby="canvas-title">
          <div className="img-zone-heading img-zone-heading--canvas">
            <div>
              <h2 id="canvas-title">Plan de travail</h2>
              {editorSession && (
                <span className="img-canvas-metrics">
                  Image {Math.round(editorSession.transform.width)} × {Math.round(editorSession.transform.height)} px ({Math.round(100 * editorSession.transform.width * editorSession.transform.height / (editorSession.canvasWidth * editorSession.canvasHeight))} % de la toile)
                </span>
              )}
            </div>
            <div className="img-canvas-actions">
              <>
                <span className="img-canvas-format">{editorSession.format.id} · {editorSession.canvasWidth} × {editorSession.canvasHeight}</span>
                <button
                  type="button"
                  className="img-button img-button--quiet"
                  onClick={() => {
                    if (editorSession?.url) revokeUrl(editorSession.url);
                    if (editorSession?.history) for (const entry of editorSession.history) revokeUrl(entry.url);
                    if (editorSession?.undoStack) for (const snapshot of editorSession.undoStack) revokeUrl(snapshot.url);
                    const leavingRun = activeRunRef.current;
                    if (leavingRun && !leavingRun.terminal) void cancelChat(leavingRun.requestId).catch(() => undefined);
                    activeRunRef.current = null;
                    setCancelPending(false);
                    setProgress(null);
                    setCurrentState('idle');
                    setLastError(null);
                    for (const step of stepsRef.current) {
                      if (step.url) revokeUrl(step.url);
                    }
                    for (const ref of editorReferences) revokeUrl(ref.url);
                    setEditorReferences([]);
                    setEditorReferenceError(null);
                    setSteps([]);
                    setSelectedId(null);
                    setEditorSession(null);
                    setEditorTool('move');
                    setBrushSize(18);
                    setView({ zoom: 1, panX: 0, panY: 0 });
                  }}
                >
                  Retour à la page Image
                </button>
              </>
            </div>
          </div>
          {progress && busy && <ProgressCard progress={progress} elapsed={elapsed} />}
          <>
            <div className="img-canvas img-canvas--editing" aria-live="polite" style={{ '--img-canvas-aspect': `${editorSession.canvasWidth} / ${editorSession.canvasHeight}` } as CSSProperties}>
                <div className="img-stage-surface">
            <canvas
              ref={canvasRef}
              width={editorSession?.canvasWidth ?? 1024}
              height={editorSession?.canvasHeight ?? 1024}
              className="img-modifier-canvas-element"
              aria-label="Canevas de modification"
              title="Zone d’édition"
              tabIndex={0}
              onPointerDown={(event) => {
                if (isEditingBusy) return;
                if (!editorSession || !canvasRef.current) return;
                const canvas = canvasRef.current;
                const rect = canvas.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;
                const sx = (event.clientX - rect.left) * (canvas.width / rect.width);
                const sy = (event.clientY - rect.top) * (canvas.height / rect.height);
                const doc = { x: (sx - view.panX) / view.zoom, y: (sy - view.panY) / view.zoom };

                if (event.button === 1 || spaceDownRef.current || editorTool === 'view') {
                  dragRef.current = { type: 'pan', startX: sx, startY: sy, startPan: { ...view } };
                  canvas.setPointerCapture(event.pointerId);
                  return;
                }

                if (editorTool === 'eraser' || editorTool === 'magic') {
                  const allowed = ie.canUseTool(editorSession, editorTool === 'eraser' ? 'eraser' : 'magicEraser');
                  if (!allowed.allowed) {
                    showToast({ type: 'info', title: 'Outil indisponible', message: allowed.reason || 'Outil indisponible.' });
                    return;
                  }
                  const family = editorTool === 'eraser' ? 'hole' : 'edit';
                  const pushed = ie.pushUndo(editorSession);
                  dragRef.current = { type: 'paint', family, lastPaintedDoc: doc };
                  canvas.setPointerCapture(event.pointerId);
                  setEditorSession(() => {
                    return ie.paintMaskSource(pushed, family, doc, brushSize);
                  });
                  return;
                }

                if (editorTool === 'crop') {
                  dragRef.current = { type: 'crop', start: doc };
                  canvas.setPointerCapture(event.pointerId);
                  setEditorSession((current) => (current ? { ...current, cropPreview: { x: doc.x, y: doc.y, width: 0, height: 0 } } : current));
                  return;
                }

                if (editorTool === 'move') {
                  const lock = ie.canTransform(editorSession);
                  if (!lock.allowed) {
                    showToast({ type: 'info', title: 'Transformation bloquée', message: lock.reason || 'Terminez l’édition locale avant de transformer.' });
                    return;
                  }
                  const t = editorSession.transform;
                  const handles = getHandles(t as any, view);
                  const cssToCanvasX = canvas.width / Math.max(1, rect.width);
                  const cssToCanvasY = canvas.height / Math.max(1, rect.height);
                  const handleTolerance = 12 * Math.max(cssToCanvasX, cssToCanvasY);
                  const hitHandle = handles.find((h) => Math.hypot(h.screenX - sx, h.screenY - sy) <= handleTolerance);
                  if (hitHandle || (doc.x >= t.x && doc.x <= t.x + t.width && doc.y >= t.y && doc.y <= t.y + t.height)) {
                    if (hitHandle) {
                      dragRef.current = { type: 'resize', handle: hitHandle.name, startTransform: { ...t }, startDoc: doc, aspect: t.width / t.height };
                    } else {
                      dragRef.current = { type: 'move', startTransform: { ...t }, startDoc: doc };
                    }
                    canvas.setPointerCapture(event.pointerId);
                    setEditorSession(ie.withRevision(ie.pushUndo(editorSession)));
                  }
                }
              }}
              onPointerMove={(event) => {
                if (isEditingBusy) return;
                if (!editorSession || !canvasRef.current) return;
                const canvas = canvasRef.current;
                const rect = canvas.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;
                const sx = (event.clientX - rect.left) * (canvas.width / rect.width);
                const sy = (event.clientY - rect.top) * (canvas.height / rect.height);
                const doc = { x: (sx - view.panX) / view.zoom, y: (sy - view.panY) / view.zoom };

                if (editorTool === 'eraser' || editorTool === 'magic') {
                  brushPreviewRef.current = doc;
                  renderEditorCanvas();
                }

                if (editorTool === 'move') {
                  const activeDrag = dragRef.current as EditorDragState | null;
                  const t = editorSession.transform;
                  const handles = getHandles(t as any, view);
                  const cssToCanvasX = canvas.width / Math.max(1, rect.width);
                  const cssToCanvasY = canvas.height / Math.max(1, rect.height);
                  const handleTolerance = 12 * Math.max(cssToCanvasX, cssToCanvasY);
                  const hoverHandle = handles.find((h) => Math.hypot(h.screenX - sx, h.screenY - sy) <= handleTolerance);
                  if (activeDrag?.type === 'move') {
                    canvas.style.cursor = 'grabbing';
                  } else if (activeDrag?.type === 'resize') {
                    canvas.style.cursor = cursorForHandle(activeDrag.handle);
                  } else if (hoverHandle) {
                    canvas.style.cursor = cursorForHandle(hoverHandle.name);
                  } else if (doc.x >= t.x && doc.x <= t.x + t.width && doc.y >= t.y && doc.y <= t.y + t.height) {
                    canvas.style.cursor = 'grab';
                  } else {
                    canvas.style.cursor = 'default';
                  }
                }

                if (!dragRef.current) return;
                const state = dragRef.current as EditorDragState;

                if (state.type === 'pan') {
                  setView({ zoom: state.startPan.zoom, panX: state.startPan.panX + (sx - state.startX), panY: state.startPan.panY + (sy - state.startY) });
                  return;
                }
                if (state.type === 'paint') {
                  const previous = state.lastPaintedDoc;
                  setEditorSession((current) => {
                    if (!current) return current;
                    return ie.paintMaskSource(current, state.family, doc, brushSize, previous);
                  });
                  state.lastPaintedDoc = doc;
                  return;
                }
                if (state.type === 'crop') {
                  setEditorSession((current) => {
                    if (!current) return current;
                    const start = state.start;
                    const x = Math.min(start.x, doc.x);
                    const y = Math.min(start.y, doc.y);
                    const width = Math.abs(doc.x - start.x);
                    const height = Math.abs(doc.y - start.y);
                    return { ...current, cropPreview: { x, y, width, height } };
                  });
                  return;
                }
                if (state.type === 'move') {
                  const t0 = state.startTransform;
                  const dx = doc.x - state.startDoc.x;
                  const dy = doc.y - state.startDoc.y;
                  const result = snapMoveRect(
                    { x: t0.x + dx, y: t0.y + dy, width: t0.width, height: t0.height },
                    { width: editorSession.canvasWidth, height: editorSession.canvasHeight },
                    8 / Math.max(view.zoom, .001),
                    !event.altKey,
                  );
                  setSnapGuides({ x: result.guideX, y: result.guideY });
                  setEditorSession((current) => {
                    if (!current) return current;
                    return { ...current, transform: result.rect };
                  });
                  return;
                }
                if (state.type === 'resize') {
                  const t0 = state.startTransform;
                  const start = state.startDoc;
                  const dx = doc.x - start.x;
                  const dy = doc.y - start.y;
                  let { x, y, width, height } = t0;
                  const handle = state.handle;
                  const proportional = handle === 'nw' || handle === 'ne' || handle === 'sw' || handle === 'se';
                  if (proportional) {
                    const aspect = state.aspect;
                    if (handle === 'se') { width = Math.max(1, t0.width + dx); height = width / aspect; }
                    if (handle === 'sw') { width = Math.max(1, t0.width - dx); x = t0.x + (t0.width - width); height = width / aspect; y = t0.y; }
                    if (handle === 'ne') { width = Math.max(1, t0.width + dx); height = width / aspect; y = t0.y + (t0.height - height); }
                    if (handle === 'nw') { width = Math.max(1, t0.width - dx); x = t0.x + (t0.width - width); height = width / aspect; y = t0.y + (t0.height - height); }
                  } else {
                    if (handle === 'e') width = Math.max(1, t0.width + dx);
                    if (handle === 'w') { width = Math.max(1, t0.width - dx); x = t0.x + (t0.width - width); }
                    if (handle === 's') height = Math.max(1, t0.height + dy);
                    if (handle === 'n') { height = Math.max(1, t0.height - dy); y = t0.y + (t0.height - height); }
                  }
                  const candidate = { x, y, width, height };
                  const result = snapResizeRect(
                    candidate,
                    handle as SnapHandle,
                    { width: editorSession.canvasWidth, height: editorSession.canvasHeight },
                    8 / Math.max(view.zoom, .001),
                    state.aspect,
                    !event.altKey,
                  );
                  const snapped = {
                    x: result.rect.x,
                    y: result.rect.y,
                    width: Math.max(1, result.rect.width),
                    height: Math.max(1, result.rect.height),
                  };
                  setSnapGuides({ x: result.guideX, y: result.guideY });
                  setEditorSession((current) => {
                    if (!current) return current;
                    return { ...current, transform: snapped };
                  });
                  return;
                }
              }}
              onPointerUp={(event) => {
                setSnapGuides({ x: null, y: null });
                if (canvasRef.current) canvasRef.current.style.cursor = '';
                if (isEditingBusy) {
                  dragRef.current = null;
                  paintingRef.current = false;
                  try { if (canvasRef.current) canvasRef.current.releasePointerCapture(event.pointerId); } catch { /* ignore */ }
                  return;
                }
                const state = dragRef.current;
                if (state?.type === 'crop') {
                  setEditorSession((current) => (current ? ie.withRevision(current) : current));
                }
                dragRef.current = null;
                paintingRef.current = false;
                try {
                  if (canvasRef.current) canvasRef.current.releasePointerCapture(event.pointerId);
                } catch {
                  // ignore
                }
              }}
              onPointerCancel={(event) => {
                setSnapGuides({ x: null, y: null });
                if (canvasRef.current) canvasRef.current.style.cursor = '';
                dragRef.current = null;
                paintingRef.current = false;
                try {
                  if (canvasRef.current) canvasRef.current.releasePointerCapture(event.pointerId);
                } catch {
                  // ignore
                }
              }}
              onPointerEnter={(event) => {
                if (isEditingBusy) return;
                if (editorTool !== 'eraser' && editorTool !== 'magic') return;
                const canvas = canvasRef.current;
                if (!canvas) return;
                const rect = canvas.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;
                const sx = (event.clientX - rect.left) * (canvas.width / rect.width);
                const sy = (event.clientY - rect.top) * (canvas.height / rect.height);
                brushPreviewRef.current = { x: (sx - view.panX) / view.zoom, y: (sy - view.panY) / view.zoom };
                renderEditorCanvas();
              }}
              onPointerLeave={() => {
                brushPreviewRef.current = null;
                renderEditorCanvas();
              }}
            />
            {isEditingBusy && (
              <div
                className="img-generation-overlay"
                data-testid="img-generation-overlay"
                role="status"
                aria-live="polite"
                aria-busy="true"
              >
                <div
                  className={`img-generation-spinner${progress?.step != null && progress?.total != null && progress?.currentId === 'diffusion' ? ' is-determinate' : ' is-indeterminate'}`}
                  aria-hidden="true"
                >
                  <svg viewBox="0 0 48 48" width="88" height="88">
                    <circle className="img-generation-track" cx="24" cy="24" r="20" fill="none" strokeWidth="4" />
                    <circle
                      className="img-generation-indicator"
                      cx="24"
                      cy="24"
                      r="20"
                      fill="none"
                      strokeWidth="4"
                      strokeLinecap="round"
                      style={
                        progress?.step != null &&
                        progress?.total != null &&
                        progress.total > 0 &&
                        progress?.currentId === 'diffusion'
                          ? {
                              strokeDasharray: `${2 * Math.PI * 20}`,
                              strokeDashoffset: `${2 * Math.PI * 20 * (1 - Math.max(0, Math.min(1, (progress.step) / (progress.total))))}`,
                              transform: 'rotate(-90deg)',
                              transformOrigin: '50% 50%',
                            }
                          : undefined
                      }
                    />
                  </svg>
                </div>
                <p className="img-generation-phase">{progress?.label || 'Génération en cours'}</p>
                {progress?.step != null && progress?.total != null && progress?.currentId === 'diffusion' && (
                  <p className="img-generation-progress">{progress.step} / {progress.total}</p>
                )}
              </div>
            )}
            </div>
              </div>
              <div className="img-zoom-control" role="group" aria-label="Vue" title="Zoom de la vue (n’affecte pas l’image)" data-testid="img-zoom-control">
                <span className="img-zoom-label">Vue</span>
                <button
                  type="button"
                  aria-label="Dézoomer la vue"
                  onClick={() => setView((current) => {
                    const next = Math.max(0.1, current.zoom / 1.1);
                    const cx = editorSession.canvasWidth / 2;
                    const cy = editorSession.canvasHeight / 2;
                    const ratio = next / current.zoom;
                    return { zoom: next, panX: cx - (cx - current.panX) * ratio, panY: cy - (cy - current.panY) * ratio };
                  })}
                >−</button>
                <strong data-testid="img-zoom-value">{Math.round(view.zoom * 100)} %</strong>
                <button
                  type="button"
                  aria-label="Zoomer la vue"
                  onClick={() => setView((current) => {
                    const next = Math.min(8, current.zoom * 1.1);
                    const cx = editorSession.canvasWidth / 2;
                    const cy = editorSession.canvasHeight / 2;
                    const ratio = next / current.zoom;
                    return { zoom: next, panX: cx - (cx - current.panX) * ratio, panY: cy - (cy - current.panY) * ratio };
                  })}
                >+</button>
                <button type="button" aria-label="Réinitialiser la vue" onClick={() => setView({ zoom: 1, panX: 0, panY: 0 })}>↺</button>
              </div>
              <div className="img-canvas-footer">
                <div className="img-footer-group" aria-label="Image">
                  <span className="img-footer-label">Image</span>
                  <button type="button" disabled={isEditingBusy} onClick={() => setEditorSession((current) => (current ? ie.undo(current) : current))}>Annuler</button>
                  <button type="button" disabled={isEditingBusy} onClick={() => setEditorSession((current) => current ? { ...current, transform: geom.fitRectToCanvas({ width: current.canvasWidth, height: current.canvasHeight }, current.sourceSize), cropPreview: null, revision: ie.bumpRevision?.(current.revision) ?? current.revision } : current)}>Ajuster l’image à la toile</button>
                  <button type="button" disabled={isEditingBusy} onClick={() => setEditorSession((current) => (current ? ie.resetActiveBase(current) : current))}>Revenir à la version</button>
                </div>
                <span className="img-tool-note">Le damier montre les zones vides à générer.</span>
              </div>
            </>
        </section>

        <aside className="img-controls-zone" aria-label="Contrôles de modification">
            <div className="img-modifier-controls">
            <div className="img-zone-heading">
              <span className="img-kicker">Outils</span>
              <h2>Modifier l’image</h2>
            </div>
            <div className="img-tool-row" role="toolbar" aria-label="Outils">
              {[
                { id: 'move', label: 'Déplacer / redimensionner' },
                { id: 'eraser', label: 'Gomme' },
                { id: 'crop', label: 'Recadrer' },
              ].map((item) => {
                const toolAllowed = !editorSession
                  ? true
                  : item.id === 'eraser'
                  ? ie.canUseTool(editorSession, 'eraser').allowed
                  : ie.canTransform(editorSession).allowed;
                return (
                  <button
                    key={item.id}
                    type="button"
                    aria-pressed={editorTool === item.id}
                    disabled={isEditingBusy || (editorSession !== null && !toolAllowed)}
                    onClick={() => setEditorTool(item.id as any)}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
            {(transformDecision.allowed === false || formatDecision.allowed === false) && (
              <p className="img-field-error" role="alert">
                {transformDecision.reason || formatDecision.reason}
              </p>
            )}
            <div className="img-tool-row">
              <button type="button" disabled={isEditingBusy} onClick={() => setEditorSession((current) => (current ? ie.clearMask(ie.pushUndo(current), 'hole') : current))}>Effacer le masque</button>
            </div>
            <label>Taille du pinceau<input type="range" min={1} max={80} value={brushSize} onChange={(e) => setBrushSize(Number(e.target.value))} /></label>
            <span className="img-field-label">Format de sortie</span>
            <div className="img-formats img-editor-formats" role="group" aria-label="Format de sortie">
              {EDITOR_FORMATS.map((f) => (
                <button
                  type="button"
                  key={f.id}
                  className="img-format"
                  aria-label={`${f.id} ${f.width}x${f.height}`}
                  aria-pressed={editorSession?.format.id === f.id}
                  disabled={isEditingBusy || !formatDecision.allowed}
                  onClick={() => {
                    if (!editorSession || !formatDecision.allowed) return;
                    const preset = EDITOR_FORMATS.find((entry) => entry.id === f.id);
                    if (!preset) return;
                    setEditorSession((current) => (current ? ie.changeFormat(current, preset as ie.FormatPreset) : current));
                    setView({ zoom: 1, panX: 0, panY: 0 });
                  }}
                >
                  <span>{f.id}</span>
                  <small>{f.width} × {f.height}</small>
                </button>
              ))}
            </div>
            <div
              className="img-editor-refs"
              aria-label="Références visuelles"
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                void handleEditorReferenceImport(event.dataTransfer.files);
              }}
            >
              <div className="img-editor-refs-head">
                <span className="img-field-label">Références supplémentaires <small>{editorReferences.length}/3</small></span>
                <button
                  type="button"
                  className="img-button img-button--quiet"
                  disabled={isEditingBusy || editorReferences.length >= 3}
                  onClick={() => editorReferenceInputRef.current?.click()}
                  aria-label="Ajouter des références"
                >
                  + Ajouter
                </button>
              </div>
              <input
                ref={editorReferenceInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                multiple
                hidden
                aria-label="Choisir des images de référence"
                onChange={(event) => {
                  void handleEditorReferenceImport(event.target.files);
                  event.target.value = '';
                }}
              />
              <p className="img-editor-refs-hint">3 références maximum, en plus de l'image en cours (4 images au total).</p>
              {editorReferences.length === 0 ? (
                <p className="img-editor-refs-empty">Ajoutez jusqu’à 3 images supplémentaires pour guider la génération (style, objet, ambiance).</p>
              ) : (
                <div className="img-editor-refs-list">
                  {editorReferences.map((ref) => (
                    <div className="img-reference-card" key={ref.id}>
                      <img src={ref.url} alt={ref.name} />
                      <div>
                        <strong title={ref.name}>{ref.name}</strong>
                        <span>{ref.size} o</span>
                      </div>
                      <div className="img-reference-actions">
                        <button
                          type="button"
                          disabled={isEditingBusy}
                          onClick={() => removeEditorReference(ref.id)}
                          aria-label={`Retirer ${ref.name}`}
                        >
                          Retirer
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {editorReferenceError && <p className="img-field-error" role="alert">{editorReferenceError}</p>}
            </div>
            <label className="img-field-label" htmlFor="editor-instruction">Instruction</label>
            <textarea
              id="editor-instruction"
              className="img-editor-instruction"
              rows={4}
              value={editorSession?.prompt ?? ''}
              onChange={(e) => setEditorSession((c) => (c ? { ...c, prompt: e.target.value } : c))}
              disabled={isEditingBusy}
              placeholder="Optionnel pour l’inpainting/outpainting"
            />
            <p className="img-tool-note">Conseil : l’instruction doit décrire l’image que vous souhaitez générer.</p>
            <label className="img-field-label" htmlFor="editor-seed">Graine <span>aléatoire par défaut</span></label>
            <input
              id="editor-seed"
              className="img-editor-seed"
              type="number"
              min={0}
              max={MAX_SEED}
              step={1}
              disabled={isEditingBusy}
              placeholder={String(editorAutoSeed)}
              value={editorSession?.seed ?? ''}
              onChange={(e) => {
                const value = e.target.value;
                setEditorSession((c) => (c ? { ...c, seed: value.trim() === '' ? undefined : Number(value) } : c));
              }}
            />
            {editorVram && (
              <div className="img-editor-memory" aria-label="Mémoire vidéo">
                <span className="img-field-label">Mémoire vidéo</span>
                <VramBar freeGb={editorVram.freeGb} totalGb={editorVram.totalGb} />
              </div>
            )}
            {editorSession?.cropPreview && (
              <div className="img-tool-row">
                <button type="button" onClick={() => void confirmCrop()}>Confirmer</button>
                <button type="button" onClick={() => setEditorSession((c) => (c ? { ...c, cropPreview: null } : c))}>Abandonner</button>
              </div>
            )}
            <div className="img-form-actions">
              <button type="button" className="img-button img-button--primary" disabled={busy || blockedByOther || !listenersReady || (promptNeeded && !(editorSession?.prompt ?? '').trim())} onClick={() => void handleModifierGenerate()}>{!listenersReady ? 'Connexion au moteur…' : blockedByOther ? blockedLabel : busy ? 'Génération…' : 'Générer'}</button>
              {canCancel && <button type="button" className="img-button img-button--stop" onClick={() => void handleCancel()}>Arrêter</button>}
            </div>
            {lastError && <div className="img-alert" role="alert">{lastError}</div>}
            </div>
          </aside>
      </div>
    </div>
  );
}

export function ImagesPageHeader() {
  return (
    <span className="img-page-header">
      <Image24Regular /> Images
    </span>
  );
}
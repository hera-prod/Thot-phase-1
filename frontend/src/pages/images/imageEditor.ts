import { createMask, paintCircle, createCoverageMask, normalizeMaskByte, isEditableMaskByte, MASK_PROTECT, MASK_EDITABLE, MASK_EDITABLE_THRESHOLD } from './imageMask';
import {
  fitRectToCanvas,
  documentToSource,
  cropDocumentRectToSource,
  isPointInRect,
  isPixelInsideRect,
  type Size,
  type Rect,
  type Point,
} from './imageGeometry';

// -----------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------
export type FormatPreset = { id: string; width: number; height: number };

export type EditorHistoryEntry = {
  id: string;
  url: string;
  image: HTMLImageElement;
  kind: 'source' | 'result';
  artifactId?: string;
  format: FormatPreset;
  sourceSize: Size;
};

export type EditorUndoSnapshot = {
  image: HTMLImageElement;
  url: string;
  sourceSize: Size;
  format: FormatPreset;
  canvasWidth: number;
  canvasHeight: number;
  transform: Rect;
  holeMask: Uint8Array;
  editMask: Uint8Array;
  cropPreview: Rect | null;
};

export type EditorSession = {
  id: string;
  sourceArtifactId?: string;
  image: HTMLImageElement;
  url: string;
  bytes?: Uint8Array;
  sourceSize: Size;
  canvasWidth: number;
  canvasHeight: number;
  format: FormatPreset;
  transform: Rect;
  holeMask: Uint8Array;
  editMask: Uint8Array;
  cropPreview: Rect | null;
  prompt: string;
  seed: number | undefined;
  history: EditorHistoryEntry[];
  activeHistoryId: string;
  revision: string;
  undoStack: EditorUndoSnapshot[];
};

export type MaskState = {
  hasHoles: boolean;
  hasEdit: boolean;
  hasUncovered: boolean;
  fillActive: boolean;
  editActive: boolean;
  conflict: boolean;
};

export type EffectiveOutputMasks = {
  fillMask: Uint8Array;
  editMask: Uint8Array;
  holeMask: Uint8Array;
};

export type GenerationResolution = {
  mode: 'global' | 'local' | 'inpaint' | 'outpaint';
  strength: number;
  maskKind: 'fill' | 'edit' | null;
};

export type ToolDecision = { allowed: boolean; reason?: string };

// -----------------------------------------------------------------------
// Internal utilities
// -----------------------------------------------------------------------
let idCounter = 0;
function makeId(prefix: string): string {
  idCounter += 1;
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    try {
      return crypto.randomUUID();
    } catch {
      // fall through
    }
  }
  return `${prefix}-${Date.now()}-${idCounter}`;
}

function cloneSize(size: Size): Size {
  return { width: size.width, height: size.height };
}

function cloneRect(rect: Rect | null): Rect | null {
  return rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null;
}

function cloneFormat(format: FormatPreset): FormatPreset {
  return { id: format.id, width: format.width, height: format.height };
}

function cloneEntry(entry: EditorHistoryEntry): EditorHistoryEntry {
  return {
    id: entry.id,
    url: entry.url,
    image: entry.image,
    kind: entry.kind,
    artifactId: entry.artifactId,
    format: cloneFormat(entry.format),
    sourceSize: cloneSize(entry.sourceSize),
  };
}

function cloneSnapshot(snapshot: EditorUndoSnapshot): EditorUndoSnapshot {
  return {
    image: snapshot.image,
    url: snapshot.url,
    sourceSize: cloneSize(snapshot.sourceSize),
    format: cloneFormat(snapshot.format),
    canvasWidth: snapshot.canvasWidth,
    canvasHeight: snapshot.canvasHeight,
    transform: cloneRect(snapshot.transform) as Rect,
    holeMask: new Uint8Array(snapshot.holeMask),
    editMask: new Uint8Array(snapshot.editMask),
    cropPreview: cloneRect(snapshot.cropPreview),
  };
}

function cloneSession(session: EditorSession): EditorSession {
  return {
    ...session,
    sourceSize: cloneSize(session.sourceSize),
    format: cloneFormat(session.format),
    transform: cloneRect(session.transform) as Rect,
    holeMask: new Uint8Array(session.holeMask),
    editMask: new Uint8Array(session.editMask),
    cropPreview: cloneRect(session.cropPreview),
    history: session.history.map(cloneEntry),
    undoStack: session.undoStack.map(cloneSnapshot),
    seed: session.seed,
  };
}

function sourceDims(session: EditorSession): { width: number; height: number } {
  const width = Math.max(1, Math.floor(session.sourceSize.width || 1));
  const height = Math.max(1, Math.floor(session.sourceSize.height || 1));
  return { width, height };
}

export function bumpRevision(current: string): string {
  const numeric = Number(current);
  if (current.trim() !== '' && Number.isFinite(numeric)) {
    return String(numeric + 1);
  }
  return `${current}-1`;
}

function resolveRevisionArg(current: string, arg: unknown, fallbackArg?: unknown): string {
  let candidate: unknown = arg;
  if (candidate && typeof candidate === 'object' && !Array.isArray(candidate) && !ArrayBuffer.isView(candidate)) {
    const obj = candidate as Record<string, unknown>;
    candidate =
      obj.newRevision ??
      obj.revision ??
      obj.revisionId ??
      obj.id ??
      obj.idFactory ??
      obj.revisionFactory ??
      obj.factory;
  }
  if (typeof candidate === 'function') {
    try {
      candidate = (candidate as () => unknown)();
    } catch {
      candidate = undefined;
    }
  }
  if (typeof candidate === 'string' && candidate.length > 0) return candidate;
  if (typeof candidate === 'number' && Number.isFinite(candidate)) return String(candidate);
  if (typeof fallbackArg === 'string' && fallbackArg.length > 0) return fallbackArg;
  return bumpRevision(current);
}

function pushSnapshot(session: EditorSession): EditorUndoSnapshot {
  const dims = sourceDims(session);
  // Ensure mask byte length consistency is not altered; keep arrays as-is copies.
  return {
    image: session.image,
    url: session.url,
    sourceSize: cloneSize(session.sourceSize),
    format: cloneFormat(session.format),
    canvasWidth: session.canvasWidth,
    canvasHeight: session.canvasHeight,
    transform: cloneRect(session.transform) as Rect,
    holeMask: new Uint8Array(session.holeMask),
    editMask: new Uint8Array(session.editMask),
    cropPreview: cloneRect(session.cropPreview),
  };
}

function orientationOf(width: number, height: number): 'square' | 'portrait' | 'landscape' {
  if (width === height) return 'square';
  return width > height ? 'landscape' : 'portrait';
}

function safeDim(value: number | undefined, fallback = 1): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return fallback;
  return Math.floor(value);
}

// -----------------------------------------------------------------------
// 1. pickInitialFormat
// -----------------------------------------------------------------------
export function pickInitialFormat(sourceSize: Size, formats: FormatPreset[]): FormatPreset {
  const sourceWidth = safeDim(sourceSize?.width, 1);
  const sourceHeight = safeDim(sourceSize?.height, 1);
  const sourceRatio = sourceWidth / sourceHeight;
  const sourceOrientation = orientationOf(sourceWidth, sourceHeight);
  const list = Array.isArray(formats) && formats.length > 0 ? formats : [];

  // Exact dimension match first.
  const exact = list.find((format) => format.width === sourceWidth && format.height === sourceHeight);
  if (exact) return cloneFormat(exact);

  // Same orientation candidates.
  const sameOrientation = list.filter((format) => orientationOf(format.width, format.height) === sourceOrientation);
  const candidates = sameOrientation.length > 0 ? sameOrientation : list;

  if (candidates.length === 0) {
    return { id: `${sourceWidth}x${sourceHeight}`, width: sourceWidth, height: sourceHeight };
  }

  let best = candidates[0];
  let bestDiff = Math.abs(best.width / best.height - sourceRatio);
  for (let i = 1; i < candidates.length; i += 1) {
    const diff = Math.abs(candidates[i].width / candidates[i].height - sourceRatio);
    if (diff < bestDiff) {
      best = candidates[i];
      bestDiff = diff;
    }
  }
  return cloneFormat(best);
}

// -----------------------------------------------------------------------
// 2. createEditorSession
// -----------------------------------------------------------------------
type CreateInput = {
  id?: string;
  sessionId?: string;
  sourceArtifactId?: string;
  artifactId?: string;
  sourceId?: string;
  image?: HTMLImageElement;
  img?: HTMLImageElement;
  sourceImage?: HTMLImageElement;
  htmlImage?: HTMLImageElement;
  url?: string;
  src?: string;
  objectUrl?: string;
  imageUrl?: string;
  bytes?: Uint8Array;
  sourceSize?: Size;
  size?: Size;
  imageSize?: Size;
  dimensions?: Size;
  formats?: FormatPreset[];
  presets?: FormatPreset[];
  availableFormats?: FormatPreset[];
  format?: FormatPreset;
  prompt?: string;
  seed?: number;
  history?: EditorHistoryEntry[];
  activeHistoryId?: string;
  revision?: string;
};

export function createEditorSession(input: CreateInput | any): EditorSession {
  const resolvedImage: HTMLImageElement =
    input?.image ?? input?.img ?? input?.sourceImage ?? input?.htmlImage ?? ({} as HTMLImageElement);
  const resolvedUrl: string =
    input?.url ?? input?.src ?? input?.objectUrl ?? input?.imageUrl ?? '';

  const sourceSizeRaw: Size | undefined =
    input?.sourceSize ?? input?.size ?? input?.imageSize ?? input?.dimensions;
  const sourceSize: Size = sourceSizeRaw
    ? { width: safeDim(sourceSizeRaw.width, 1), height: safeDim(sourceSizeRaw.height, 1) }
    : {
        width: safeDim(resolvedImage?.naturalWidth ?? resolvedImage?.width, 1),
        height: safeDim(resolvedImage?.naturalHeight ?? resolvedImage?.height, 1),
      };

  const formats: FormatPreset[] =
    input?.formats ?? input?.presets ?? input?.availableFormats ?? [];
  const chosenFormat: FormatPreset = input?.format
    ? cloneFormat(input.format)
    : pickInitialFormat(sourceSize, formats);

  const canvasWidth = safeDim(chosenFormat.width, 1);
  const canvasHeight = safeDim(chosenFormat.height, 1);
  const transform = fitRectToCanvas(
    { width: canvasWidth, height: canvasHeight },
    { width: sourceSize.width, height: sourceSize.height },
  );

  const id = input?.id ?? input?.sessionId ?? makeId('session');
  const sourceArtifactId = input?.sourceArtifactId ?? input?.artifactId ?? input?.sourceId;
  const historyId = makeId('hist');
  const entry: EditorHistoryEntry = {
    id: historyId,
    url: resolvedUrl,
    image: resolvedImage,
    kind: 'source',
    artifactId: sourceArtifactId,
    format: cloneFormat(chosenFormat),
    sourceSize: cloneSize(sourceSize),
  };

  const holeMask = new Uint8Array(Math.max(0, sourceSize.width * sourceSize.height));
  const editMask = new Uint8Array(Math.max(0, sourceSize.width * sourceSize.height));

  return {
    id,
    sourceArtifactId,
    image: resolvedImage,
    url: resolvedUrl,
    bytes: input?.bytes instanceof Uint8Array ? new Uint8Array(input.bytes) : input?.bytes,
    sourceSize: cloneSize(sourceSize),
    canvasWidth,
    canvasHeight,
    format: cloneFormat(chosenFormat),
    transform,
    holeMask,
    editMask,
    cropPreview: null,
    prompt: typeof input?.prompt === 'string' ? input.prompt : '',
    seed: typeof input?.seed === 'number' ? input.seed : undefined,
    history: Array.isArray(input?.history) ? input.history.map(cloneEntry) : [entry],
    activeHistoryId: input?.activeHistoryId ?? historyId,
    revision: typeof input?.revision === 'string' ? input.revision : '0',
    undoStack: [],
  };
}

// -----------------------------------------------------------------------
// History helpers
// -----------------------------------------------------------------------
export function createHistoryEntry(input: any): EditorHistoryEntry {
  const image: HTMLImageElement = input?.image ?? input?.img ?? ({} as HTMLImageElement);
  const url: string = input?.url ?? input?.src ?? '';
  const sourceSizeRaw = input?.sourceSize ?? input?.size ?? input?.imageSize ?? input?.dimensions;
  const sourceSize: Size = sourceSizeRaw
    ? { width: safeDim(sourceSizeRaw.width, 1), height: safeDim(sourceSizeRaw.height, 1) }
    : { width: safeDim(image?.naturalWidth ?? image?.width, 1), height: safeDim(image?.naturalHeight ?? image?.height, 1) };
  const formatInput = input?.format ?? input?.preset;
  const format: FormatPreset = formatInput
    ? cloneFormat(formatInput)
    : { id: `${sourceSize.width}x${sourceSize.height}`, width: sourceSize.width, height: sourceSize.height };
  return {
    id: input?.id ?? makeId('hist'),
    url,
    image,
    kind: input?.kind === 'source' ? 'source' : 'result',
    artifactId: input?.artifactId ?? input?.sourceArtifactId,
    format,
    sourceSize: cloneSize(sourceSize),
  };
}

function normalizeHistoryEntryArg(arg: any, makeKind: 'source' | 'result' = 'result'): EditorHistoryEntry {
  if (arg && typeof arg === 'object' && 'image' in arg && !('id' in arg && 'format' in arg && 'sourceSize' in arg && 'url' in arg)) {
    // Plain payload without entry shape; still create.
  }
  if (arg && typeof arg === 'object' && typeof arg.id === 'string' && typeof arg.url === 'string' && arg.image && arg.format && arg.sourceSize) {
    return cloneEntry(arg as EditorHistoryEntry);
  }
  const payload = { ...arg };
  if (!payload.kind) payload.kind = makeKind;
  return createHistoryEntry(payload);
}

export function appendResult(session: EditorSession, resultOrEntry: any, ...rest: any[]): EditorSession {
  const nextSession = cloneSession(session);

  let entry: EditorHistoryEntry;
  if (typeof resultOrEntry === 'string') {
    // appendResult(session, url, image, options?) legacy positional support
    const url = resultOrEntry;
    const image = rest[0];
    const opts = rest[1] ?? {};
    entry = createHistoryEntry({ url, image, ...opts, kind: 'result' });
  } else if (resultOrEntry instanceof HTMLImageElement) {
    const image = resultOrEntry;
    const url = rest[0];
    const opts = rest[1] ?? {};
    entry = createHistoryEntry({ image, url, ...opts, kind: 'result' });
  } else {
    entry = normalizeHistoryEntryArg(resultOrEntry, 'result');
  }

  nextSession.history = [...nextSession.history, entry];
  return nextSession;
}

export function selectHistoryBase(session: EditorSession, historyId?: string): EditorSession {
  const id = historyId ?? session.activeHistoryId;
  const entry = session.history.find((e) => e.id === id);
  if (!entry) return cloneSession(session);

  const next = cloneSession(session);
  const canvasWidth = safeDim(entry.format.width, next.canvasWidth);
  const canvasHeight = safeDim(entry.format.height, next.canvasHeight);
  const sourceSize = cloneSize(entry.sourceSize);
  const transform = fitRectToCanvas(
    { width: canvasWidth, height: canvasHeight },
    { width: sourceSize.width, height: sourceSize.height },
  );
  const dims = { width: Math.max(1, sourceSize.width), height: Math.max(1, sourceSize.height) };

  next.image = entry.image;
  next.url = entry.url;
  next.sourceSize = sourceSize;
  next.format = cloneFormat(entry.format);
  next.canvasWidth = canvasWidth;
  next.canvasHeight = canvasHeight;
  next.transform = transform;
  next.holeMask = new Uint8Array(Math.max(0, dims.width * dims.height));
  next.editMask = new Uint8Array(Math.max(0, dims.width * dims.height));
  next.cropPreview = null;
  next.activeHistoryId = entry.id;
  next.sourceArtifactId = entry.artifactId;
  next.undoStack = [];
  next.revision = bumpRevision(session.revision);
  return next;
}

export function resetActiveBase(session: EditorSession): EditorSession {
  return selectHistoryBase(session, session.activeHistoryId);
}

// -----------------------------------------------------------------------
// mask helpers
// -----------------------------------------------------------------------
function hasEditableBytes(mask: Uint8Array): boolean {
  for (let i = 0; i < mask.length; i += 1) {
    if (isEditableMaskByte(mask[i])) return true;
  }
  return false;
}

function uncoveredExists(session: EditorSession): boolean {
  const { canvasWidth, canvasHeight } = session;
  const transform = session.transform;
  if (canvasWidth <= 0 || canvasHeight <= 0) return false;
  // If any canvas pixel outside transform rect => uncovered.
  const tx = transform.x;
  const ty = transform.y;
  const tx2 = transform.x + transform.width;
  const ty2 = transform.y + transform.height;
  if (tx > 0 || ty > 0 || tx2 < canvasWidth || ty2 < canvasHeight) {
    // Need ensure partial? Check area more directly.
    return true;
  }
  return false;
}

function transformCoversCanvas(session: EditorSession): boolean {
  const t = session.transform;
  return t.x <= 0 && t.y <= 0 && t.x + t.width >= session.canvasWidth && t.y + t.height >= session.canvasHeight;
}

function hasUncoveredSession(session: EditorSession): boolean {
  return !transformCoversCanvas(session);
}

// -----------------------------------------------------------------------
// 4. paintMaskSource (+ primitive paintDiskIntoMask)
// -----------------------------------------------------------------------
export function paintDiskIntoMask(
  target: Uint8Array,
  dims: { width: number; height: number },
  transform: Rect,
  docPoint: Point,
  brushDiameterDoc: number,
): boolean {
  const sourceCenter = documentToSource(docPoint, transform, dims.width, dims.height);

  // Guard: outside source image => no paint.
  if (sourceCenter.x < 0 || sourceCenter.x >= dims.width || sourceCenter.y < 0 || sourceCenter.y >= dims.height) {
    return false;
  }

  const sourceWidthSafe = Math.max(1, transform.width) || 1;
  const sourceHeightSafe = Math.max(1, transform.height) || 1;
  const scaleX = dims.width / sourceWidthSafe;
  const scaleY = dims.height / sourceHeightSafe;
  const radiusX = Math.max(0.5, (brushDiameterDoc / 2) * scaleX);
  const radiusY = Math.max(0.5, (brushDiameterDoc / 2) * scaleY);

  const minX = Math.max(0, Math.floor(sourceCenter.x - radiusX));
  const maxX = Math.min(dims.width - 1, Math.ceil(sourceCenter.x + radiusX));
  const minY = Math.max(0, Math.floor(sourceCenter.y - radiusY));
  const maxY = Math.min(dims.height - 1, Math.ceil(sourceCenter.y + radiusY));

  let marked = false;
  for (let y = minY; y <= maxY; y += 1) {
    for (let x = minX; x <= maxX; x += 1) {
      const dx = (x + 0.5 - sourceCenter.x) / radiusX;
      const dy = (y + 0.5 - sourceCenter.y) / radiusY;
      const isNearestPixel = x === Math.floor(sourceCenter.x) && y === Math.floor(sourceCenter.y);
      if (isNearestPixel || dx * dx + dy * dy <= 1) {
        target[y * dims.width + x] = MASK_EDITABLE;
        marked = true;
      }
    }
  }
  return marked;
}

export function paintMaskSource(
  session: EditorSession,
  family: 'hole' | 'edit',
  docPoint: Point,
  brushDiameterDoc: number,
  previousDocPoint?: Point
): EditorSession {
  const dims = sourceDims(session);
  const normalizedFamily = String(family ?? '').toLowerCase();
  const holeFamily = normalizedFamily === 'hole' || normalizedFamily === 'holes' || normalizedFamily === 'fill';

  const spacing = brushDiameterDoc > 0 ? brushDiameterDoc / 2 : 1;
  const buildCenters = (): Point[] => {
    if (!previousDocPoint) return [docPoint];
    const dx = docPoint.x - previousDocPoint.x;
    const dy = docPoint.y - previousDocPoint.y;
    const distance = Math.hypot(dx, dy);
    if (distance <= 0 || !Number.isFinite(distance)) return [docPoint];
    const count = Math.min(64, Math.max(2, Math.ceil(distance / spacing) + 1));
    const points: Point[] = [];
    for (let i = 0; i < count; i += 1) {
      const t = i / (count - 1);
      points.push({ x: previousDocPoint.x + dx * t, y: previousDocPoint.y + dy * t });
    }
    // Ensure endpoint is included.
    const last = points[points.length - 1];
    if (!last || last.x !== docPoint.x || last.y !== docPoint.y) {
      if (points.length < 64) points.push({ x: docPoint.x, y: docPoint.y });
      else points[points.length - 1] = { x: docPoint.x, y: docPoint.y };
    }
    return points;
  };

  const centers = buildCenters();

  // Early reject: if no centre maps inside the source image, skip clone entirely.
  let touchesSource = false;
  for (const pt of centers) {
    const sourceCenter = documentToSource(pt, session.transform, dims.width, dims.height);
    if (
      sourceCenter.x >= 0 && sourceCenter.x < dims.width &&
      sourceCenter.y >= 0 && sourceCenter.y < dims.height
    ) {
      touchesSource = true;
      break;
    }
  }
  if (!touchesSource) return session;

  // Always clone once; never mutate the incoming mask.
  const next = cloneSession(session);
  const target = holeFamily ? next.holeMask : next.editMask;

  let anyMarked = false;
  for (const pt of centers) {
    if (paintDiskIntoMask(target, dims, session.transform, pt, brushDiameterDoc)) {
      anyMarked = true;
    }
  }

  if (!anyMarked) {
    // Preserve reference: no revision bump.
    return session;
  }

  if (holeFamily) next.holeMask = target;
  else next.editMask = target;
  next.revision = bumpRevision(session.revision);
  return next;
}

export function paintMaskStroke(session: EditorSession, family: 'hole' | 'edit', from: Point, to: Point, brushDiameterDoc: number): EditorSession {
  return paintMaskSource(session, family, to, brushDiameterDoc, from);
}

// -----------------------------------------------------------------------
// 5. clearMask / clearAllMasks
// -----------------------------------------------------------------------
export function clearMask(session: EditorSession, family: 'hole' | 'edit' | string): EditorSession {
  const next = cloneSession(session);
  const dims = sourceDims(session);
  const size = Math.max(0, dims.width * dims.height);
  const normalized = String(family ?? '').toLowerCase();
  const isHole = normalized === 'hole' || normalized === 'holes' || normalized === 'fill';
  if (isHole) next.holeMask = new Uint8Array(size);
  else next.editMask = new Uint8Array(size);
  next.revision = bumpRevision(session.revision);
  return next;
}

export function clearAllMasks(session: EditorSession): EditorSession {
  const next = cloneSession(session);
  const dims = sourceDims(session);
  const size = Math.max(0, dims.width * dims.height);
  next.holeMask = new Uint8Array(size);
  next.editMask = new Uint8Array(size);
  next.revision = bumpRevision(session.revision);
  return next;
}

// -----------------------------------------------------------------------
// 6. maskState
// -----------------------------------------------------------------------
export function maskState(session: EditorSession): MaskState {
  const hasHoles = hasEditableBytes(session.holeMask);
  const hasEdit = hasEditableBytes(session.editMask);
  const uncovered = hasUncoveredSession(session);
  const hasUncovered = uncovered;
  const fillActive = hasHoles || hasUncovered;
  const editActive = hasEdit;
  const conflict = fillActive && editActive;
  return { hasHoles, hasEdit, hasUncovered, fillActive, editActive, conflict };
}

// -----------------------------------------------------------------------
// 7. effectiveOutputMasks
// -----------------------------------------------------------------------
export function effectiveOutputMasks(session: EditorSession): EffectiveOutputMasks {
  const canvasWidth = Math.max(0, Math.floor(session.canvasWidth));
  const canvasHeight = Math.max(0, Math.floor(session.canvasHeight));
  const sourceDimsResult = sourceDims(session);
  const sw = sourceDimsResult.width;
  const sh = sourceDimsResult.height;
  const holeMask = session.holeMask;
  const editMask = session.editMask;
  const transform = session.transform;

  const fillMask = new Uint8Array(Math.max(0, canvasWidth * canvasHeight));
  const outEditMask = new Uint8Array(Math.max(0, canvasWidth * canvasHeight));
  const outHoleMask = new Uint8Array(Math.max(0, canvasWidth * canvasHeight));

  for (let y = 0; y < canvasHeight; y += 1) {
    for (let x = 0; x < canvasWidth; x += 1) {
      const index = y * canvasWidth + x;
      const insideImage = isPixelInsideRect({ x, y }, transform);
      if (!insideImage) {
        fillMask[index] = MASK_EDITABLE;
        continue;
      }
      const src = documentToSource({ x, y }, transform, sw, sh);
      const sx = Math.max(0, Math.min(sw - 1, Math.round(src.x)));
      const sy = Math.max(0, Math.min(sh - 1, Math.round(src.y)));
      const srcIndex = sy * sw + sx;
      if (srcIndex >= 0 && srcIndex < holeMask.length && isEditableMaskByte(holeMask[srcIndex])) {
        fillMask[index] = MASK_EDITABLE;
        outHoleMask[index] = MASK_EDITABLE;
      }
      if (srcIndex >= 0 && srcIndex < editMask.length && isEditableMaskByte(editMask[srcIndex])) {
        outEditMask[index] = MASK_EDITABLE;
      }
    }
  }
  return { fillMask, editMask: outEditMask, holeMask: outHoleMask };
}

// -----------------------------------------------------------------------
// 8. resolveGeneration
// -----------------------------------------------------------------------
export function resolveGeneration(session: EditorSession): GenerationResolution {
  const state = maskState(session);
  if (state.conflict) {
    throw new Error('Conflit entre zones de remplissage et d’édition locale.');
  }
  const { fillActive, editActive } = state;
  if (editActive) {
    return { mode: 'local', strength: 0.55, maskKind: 'edit' };
  }
  if (state.hasHoles && state.hasUncovered) {
    return { mode: 'inpaint', strength: 1.0, maskKind: 'fill' };
  }
  if (state.hasHoles) {
    return { mode: 'inpaint', strength: 1.0, maskKind: 'fill' };
  }
  if (state.hasUncovered) {
    return { mode: 'outpaint', strength: 1.0, maskKind: 'fill' };
  }
  return { mode: 'global', strength: 0.65, maskKind: null };
}

// -----------------------------------------------------------------------
// 9. lock helpers
// -----------------------------------------------------------------------
export function canUseTool(session: EditorSession, tool: string): ToolDecision {
  const state = maskState(session);
  const normalized = String(tool ?? '').toLowerCase().replace(/[\s_-]/g, '');

  const isMagicEraser = normalized.includes('magic') || normalized.includes('magique');
  const isNormalEraser = !isMagicEraser && (normalized.includes('eraser') || normalized.includes('gomme') || normalized === 'gomme');

  if (isMagicEraser && state.fillActive) {
    return { allowed: false, reason: 'Terminez ou réinitialisez le remplissage avant d’utiliser la gomme magique.' };
  }
  if (isNormalEraser && state.editActive) {
    return { allowed: false, reason: 'Terminez ou réinitialisez l’édition locale avant d’utiliser la gomme.' };
  }
  return { allowed: true };
}

export function canChangeFormat(session: EditorSession): ToolDecision {
  const state = maskState(session);
  if (state.editActive) {
    return { allowed: false, reason: 'Terminez ou réinitialisez l’édition locale avant de changer le format.' };
  }
  return { allowed: true };
}

export function canTransform(session: EditorSession): ToolDecision {
  const state = maskState(session);
  if (state.editActive) {
    return { allowed: false, reason: 'Terminez ou réinitialisez l’édition locale avant de transformer.' };
  }
  return { allowed: true };
}

// -----------------------------------------------------------------------
// 10. changeFormat
// -----------------------------------------------------------------------
export function changeFormat(session: EditorSession, format: FormatPreset): EditorSession {
  const decision = canChangeFormat(session);
  if (!decision.allowed) return cloneSession(session);

  const next = cloneSession(session);
  // push undo snapshot of current
  const undoStack = [...next.undoStack, pushSnapshot(session)];
  next.undoStack = undoStack.length > 30 ? undoStack.slice(undoStack.length - 30) : undoStack;

  const newFormat = cloneFormat(format);
  const canvasWidth = safeDim(newFormat.width, next.canvasWidth);
  const canvasHeight = safeDim(newFormat.height, next.canvasHeight);
  const transform = fitRectToCanvas(
    { width: canvasWidth, height: canvasHeight },
    { width: next.sourceSize.width, height: next.sourceSize.height },
  );

  next.format = newFormat;
  next.canvasWidth = canvasWidth;
  next.canvasHeight = canvasHeight;
  next.transform = transform;
  next.cropPreview = null;
  next.history = next.history.map((entry) =>
    entry.id === next.activeHistoryId
      ? { ...entry, format: cloneFormat(newFormat) }
      : entry
  );
  // keep source-space masks (cloned)
  next.revision = bumpRevision(session.revision);
  return next;
}

// -----------------------------------------------------------------------
// 11. pushUndo / undo
// -----------------------------------------------------------------------
export function pushUndo(session: EditorSession): EditorSession {
  const next = cloneSession(session);
  const undoStack = [...next.undoStack, pushSnapshot(session)];
  next.undoStack = undoStack.length > 30 ? undoStack.slice(undoStack.length - 30) : undoStack;
  return next;
}

export function undo(session: EditorSession): EditorSession {
  if (!session.undoStack || session.undoStack.length === 0) return cloneSession(session);

  const next = cloneSession(session);
  const stack = [...next.undoStack];
  const snapshot = stack.pop() as EditorUndoSnapshot;
  next.undoStack = stack;

  next.image = snapshot.image;
  next.url = snapshot.url;
  next.sourceSize = cloneSize(snapshot.sourceSize);
  next.format = cloneFormat(snapshot.format);
  next.canvasWidth = snapshot.canvasWidth;
  next.canvasHeight = snapshot.canvasHeight;
  next.transform = cloneRect(snapshot.transform) as Rect;
  next.holeMask = new Uint8Array(snapshot.holeMask);
  next.editMask = new Uint8Array(snapshot.editMask);
  next.cropPreview = cloneRect(snapshot.cropPreview);
  next.revision = bumpRevision(session.revision);
  return next;
}

// -----------------------------------------------------------------------
// 12. applyCrop
// -----------------------------------------------------------------------
export function applyCrop(
  session: EditorSession,
  cropDocRect: Rect,
  croppedImage: HTMLImageElement,
  croppedUrl: string,
): EditorSession {
  // push undo
  const withUndo = pushUndo(session);
  const base = session;

  const sourceRect = cropDocumentRectToSource(cropDocRect, base.transform, base.sourceSize.width, base.sourceSize.height);
  const newWidth = safeDim(croppedImage?.naturalWidth ?? croppedImage?.width, Math.max(1, Math.round(sourceRect.width)));
  const newHeight = safeDim(croppedImage?.naturalHeight ?? croppedImage?.height, Math.max(1, Math.round(sourceRect.height)));

  const oldSw = Math.max(1, base.sourceSize.width);
  const oldSh = Math.max(1, base.sourceSize.height);

  const newHole = new Uint8Array(Math.max(0, newWidth * newHeight));
  const newEdit = new Uint8Array(Math.max(0, newWidth * newHeight));

  // nearest source mapping
  for (let ny = 0; ny < newHeight; ny += 1) {
    for (let nx = 0; nx < newWidth; nx += 1) {
      const oldX = sourceRect.x + ((nx + 0.5) / Math.max(1, newWidth)) * sourceRect.width;
      const oldY = sourceRect.y + ((ny + 0.5) / Math.max(1, newHeight)) * sourceRect.height;
      const sx = Math.max(0, Math.min(oldSw - 1, Math.round(oldX)));
      const sy = Math.max(0, Math.min(oldSh - 1, Math.round(oldY)));
      const oldIndex = sy * oldSw + sx;
      if (oldIndex >= 0 && oldIndex < base.holeMask.length && isEditableMaskByte(base.holeMask[oldIndex])) {
        newHole[ny * newWidth + nx] = MASK_EDITABLE;
      }
      if (oldIndex >= 0 && oldIndex < base.editMask.length && isEditableMaskByte(base.editMask[oldIndex])) {
        newEdit[ny * newWidth + nx] = MASK_EDITABLE;
      }
    }
  }

  const next = cloneSession(withUndo);
  next.image = croppedImage;
  next.url = croppedUrl;
  next.sourceSize = { width: newWidth, height: newHeight };
  next.transform = fitRectToCanvas(
    { width: next.canvasWidth, height: next.canvasHeight },
    { width: newWidth, height: newHeight },
  );
  next.holeMask = newHole;
  next.editMask = newEdit;
  next.cropPreview = null;
  next.revision = bumpRevision(base.revision);
  // preserve history
  next.history = base.history.map(cloneEntry);
  next.activeHistoryId = base.activeHistoryId;
  next.undoStack = withUndo.undoStack;
  return next;
}

// -----------------------------------------------------------------------
// 13. setWorkTransform / withRevision / commitSession
// -----------------------------------------------------------------------
export function setWorkTransform(session: EditorSession, transform: Rect): EditorSession {
  const next = cloneSession(session);
  next.transform = { x: transform.x, y: transform.y, width: transform.width, height: transform.height };
  return next;
}

export function withRevision(session: EditorSession, newRevisionOrFactory?: any, maybeFactory?: any): EditorSession {
  const next = cloneSession(session);
  const arg = newRevisionOrFactory !== undefined ? newRevisionOrFactory : maybeFactory;
  next.revision = resolveRevisionArg(session.revision, arg);
  return next;
}

export function commitSession(session: EditorSession, newRevisionOrFactory?: any, maybeFactory?: any): EditorSession {
  return withRevision(session, newRevisionOrFactory, maybeFactory);
}

// -----------------------------------------------------------------------
// 15. collectSessionUrls
// -----------------------------------------------------------------------
export function collectSessionUrls(session: EditorSession): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];
  const add = (url?: string) => {
    if (typeof url === 'string' && url.length > 0 && !seen.has(url)) {
      seen.add(url);
      urls.push(url);
    }
  };
  for (const entry of session.history) add(entry.url);
  add(session.url);
  for (const snapshot of session.undoStack) add(snapshot.url);
  return urls;
}

// Re-export helpers that might be expected.
export { MASK_EDITABLE_THRESHOLD };

// Keep unused alias safe? expose isEditable via mask module already.
void paintCircle;
void createCoverageMask;
void createMask;
void normalizeMaskByte;
void MASK_PROTECT;
void MASK_EDITABLE;
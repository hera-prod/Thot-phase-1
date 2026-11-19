import { effectiveOutputMasks, resolveGeneration } from './imageEditor';
import type { EditorSession } from './imageEditor';
import { documentToSource, isPixelInsideRect, type Rect, type Size } from './imageGeometry';
import { isEditableMaskByte } from './imageMask';

export type MaskCoverage = {
  total: number;
  editable: number;
  top: number;
  bottom: number;
  left: number;
  right: number;
  center: number;
};

export type ModifierRequestPlan = {
  mode: 'global' | 'local' | 'inpaint' | 'outpaint';
  strength: number;
  maskKind: 'fill' | 'edit' | null;
  promptToSend: string;
  canvasWidth: number;
  canvasHeight: number;
  transform: Rect;
  sourceSize: Size;
  chosenMask: Uint8Array | null;
  expectedMask: Uint8Array | null;
  coverage: MaskCoverage;
  differences: { missing: number; extra: number; missingSamples: Array<{ x: number; y: number }> };
  requestFields: Record<string, unknown>;
};

function cloneSize(size: Size): Size {
  return { width: size.width, height: size.height };
}

function cloneRect(rect: Rect): Rect {
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}

function safeDim(value: number | undefined, fallback = 1): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return fallback;
  return Math.floor(value);
}

function toMaskCoverage(
  chosenMask: Uint8Array | null,
  canvasWidth: number,
  canvasHeight: number,
  transform: Rect,
): MaskCoverage {
  const total = canvasWidth * canvasHeight;
  let editable = 0;
  let top = 0;
  let bottom = 0;
  let left = 0;
  let right = 0;
  let center = 0;
  if (chosenMask) {
    for (let i = 0; i < total; i += 1) {
      const x = i % Math.max(1, canvasWidth);
      const y = Math.floor(i / Math.max(1, canvasWidth));
      const isEditable = (chosenMask[i] ?? 0) >= 128 || isEditableMaskByte(chosenMask[i] ?? 0);
      if (!isEditable) continue;
      editable += 1;
      if (y < transform.y) top += 1;
      else if (y >= transform.y + transform.height) bottom += 1;
      else if (x < transform.x) left += 1;
      else if (x >= transform.x + transform.width) right += 1;
      else center += 1;
    }
  }
  return { total, editable, top, bottom, left, right, center };
}

function compareMasks(
  chosenMask: Uint8Array | null,
  expectedMask: Uint8Array | null,
  canvasWidth: number,
  canvasHeight: number,
): { missing: number; extra: number; missingSamples: Array<{ x: number; y: number }> } {
  const total = canvasWidth * canvasHeight;
  let missing = 0;
  let extra = 0;
  const missingSamples: Array<{ x: number; y: number }> = [];
  for (let i = 0; i < total; i += 1) {
    const chosenEditable = !!chosenMask && ((chosenMask[i] ?? 0) >= 128 || isEditableMaskByte(chosenMask[i] ?? 0));
    const expectedEditable = !!expectedMask && ((expectedMask[i] ?? 0) >= 128 || isEditableMaskByte(expectedMask[i] ?? 0));
    if (expectedEditable && !chosenEditable) {
      missing += 1;
      if (missingSamples.length < 5) {
        missingSamples.push({ x: i % Math.max(1, canvasWidth), y: Math.floor(i / Math.max(1, canvasWidth)) });
      }
    } else if (chosenEditable && !expectedEditable) {
      extra += 1;
    }
  }
  return { missing, extra, missingSamples };
}

function buildExpectedMask(
  session: EditorSession,
  maskKind: 'fill' | 'edit' | null,
  includeUncovered: boolean,
  canvasWidth: number,
  canvasHeight: number,
): Uint8Array | null {
  if (maskKind === null) return null;
  const transform = session.transform;
  const sw = safeDim(session.sourceSize?.width, 1);
  const sh = safeDim(session.sourceSize?.height, 1);
  const holeMask = session.holeMask;
  const editMask = session.editMask;
  const total = canvasWidth * canvasHeight;
  const expected = new Uint8Array(Math.max(0, total));
  for (let y = 0; y < canvasHeight; y += 1) {
    for (let x = 0; x < canvasWidth; x += 1) {
      const index = y * canvasWidth + x;
      const inside = isPixelInsideRect({ x, y }, transform);
      if (!inside) {
        expected[index] = includeUncovered ? 255 : 0;
        continue;
      }
      const src = documentToSource({ x, y }, transform, sw, sh);
      const sx = Math.max(0, Math.min(sw - 1, Math.round(src.x)));
      const sy = Math.max(0, Math.min(sh - 1, Math.round(src.y)));
      const srcIndex = sy * sw + sx;
      let editable = false;
      if (srcIndex >= 0 && srcIndex < holeMask.length && isEditableMaskByte(holeMask[srcIndex])) {
        editable = true;
      }
      if (!editable && maskKind === 'edit' && srcIndex >= 0 && srcIndex < editMask.length && isEditableMaskByte(editMask[srcIndex])) {
        editable = true;
      }
      expected[index] = editable ? 255 : 0;
    }
  }
  return expected;
}

export function planModifierRequest(session: EditorSession): ModifierRequestPlan {
  const resolution = resolveGeneration(session);
  const mode = resolution.mode;
  const strength = resolution.strength;
  const maskKind = resolution.maskKind;

  const instruction = (session.prompt ?? '').trim();
  const promptToSend = instruction || (mode === 'inpaint' || mode === 'outpaint'
    ? 'Reconstruct and extend naturally the masked area matching the surrounding scene.'
    : 'Apply the edit to the selected region.');

  const canvasWidth = Math.max(0, Math.floor(session.canvasWidth));
  const canvasHeight = Math.max(0, Math.floor(session.canvasHeight));
  const transform = cloneRect(session.transform);
  const sourceSize = cloneSize(session.sourceSize);

  const effective = effectiveOutputMasks(session);
  // `fillMask` contient les trous ET les marges hors image : réservé à l'outpainting.
  // En inpainting/local, seuls les pixels réellement peints doivent être éditables.
  const chosenMask = maskKind === 'fill'
    ? new Uint8Array(mode === 'outpaint' ? effective.fillMask : effective.holeMask)
    : maskKind === 'edit'
      ? new Uint8Array(effective.editMask)
      : null;

  const expectedMask = buildExpectedMask(session, maskKind, mode === 'outpaint', canvasWidth, canvasHeight);

  const coverage = toMaskCoverage(chosenMask, canvasWidth, canvasHeight, transform);
  const differences = compareMasks(chosenMask, expectedMask, canvasWidth, canvasHeight);

  const requestFields: Record<string, unknown> = {
    mode,
    strength,
    mask_kind: maskKind,
    canvas_width: canvasWidth,
    canvas_height: canvasHeight,
    source_transform: transform,
    source_size: sourceSize,
  };

  const plan: ModifierRequestPlan = {
    mode,
    strength,
    maskKind,
    promptToSend,
    canvasWidth,
    canvasHeight,
    transform,
    sourceSize,
    chosenMask,
    expectedMask,
    coverage,
    differences,
    requestFields,
  };

  if (typeof window !== 'undefined' && (window as { __THOT_IMAGE_DIAG_CAPTURE__?: boolean }).__THOT_IMAGE_DIAG_CAPTURE__ === true) {
    const serializeMask = (mask: Uint8Array | null): { length: number; bytes: number[] } | null => {
      if (!mask) return null;
      return { length: mask.length, bytes: Array.from(mask.slice(0, 32)) };
    };
    const detail = {
      mode: plan.mode,
      strength: plan.strength,
      maskKind: plan.maskKind,
      promptToSend: plan.promptToSend,
      canvasWidth: plan.canvasWidth,
      canvasHeight: plan.canvasHeight,
      transform: plan.transform,
      sourceSize: plan.sourceSize,
      chosenMask: serializeMask(plan.chosenMask),
      expectedMask: serializeMask(plan.expectedMask),
      coverage: plan.coverage,
      differences: plan.differences,
      requestFields: plan.requestFields,
    };
    window.dispatchEvent(new CustomEvent('thot:image-plan', { detail }));
  }

  return plan;
}
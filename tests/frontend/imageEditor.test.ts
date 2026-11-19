import { describe, expect, it } from 'vitest';
import {
  pickInitialFormat,
  createEditorSession,
  createHistoryEntry,
  appendResult,
  selectHistoryBase,
  resetActiveBase,
  paintMaskSource,
  clearMask,
  clearAllMasks,
  maskState,
  effectiveOutputMasks,
  resolveGeneration,
  canUseTool,
  canChangeFormat,
  canTransform,
  changeFormat,
  pushUndo,
  undo,
  applyCrop,
  setWorkTransform,
  withRevision,
  commitSession,
  collectSessionUrls,
} from '@/pages/images/imageEditor';
import { fitRectToCanvas } from '@/pages/images/imageGeometry';
import { MASK_EDITABLE, MASK_PROTECTED, MASK_EDITABLE_THRESHOLD } from '@/pages/images/imageMask';

const FORMATS = [
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

describe('imageEditor pickInitialFormat', () => {
  it('exact dimensions first', () => {
    const f = pickInitialFormat({ width: 1024, height: 1024 }, FORMATS);
    expect(f.id).toBe('1:1');
  });
  it('9:21 source yields 9:21', () => {
    const f = pickInitialFormat({ width: 672, height: 1568 }, FORMATS);
    expect(f.id).toBe('9:21');
  });
  it('21:9 source yields 21:9', () => {
    const f = pickInitialFormat({ width: 1568, height: 672 }, FORMATS);
    expect(f.id).toBe('21:9');
  });
  it('portrait non-exact picks portrait', () => {
    const f = pickInitialFormat({ width: 3, height: 7 }, FORMATS);
    expect(f.id).toBe('9:21');
  });
});

describe('session basics', () => {
  it('create session initial', () => {
    const img = {} as HTMLImageElement;
    const s = createEditorSession({ image: img, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS, prompt: 'p', seed: 7 });
    expect(s.canvasWidth).toBe(1152);
    expect(s.canvasHeight).toBe(864);
    expect(s.holeMask.length).toBe(12);
    expect(s.editMask.length).toBe(12);
    expect(s.revision).toBe('0');
    expect(s.undoStack).toHaveLength(0);
    expect(s.history).toHaveLength(1);
    expect(s.activeHistoryId).toBe(s.history[0].id);
    expect(s.transform).toEqual(fitRectToCanvas({ width: 1152, height: 864 }, { width: 4, height: 3 }));
  });
});

describe('painting', () => {
  it('paints hole at doc coords and bumps revision', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    const docPoint = { x: s.transform.x + s.transform.width / 2, y: s.transform.y + s.transform.height / 2 };
    const next = paintMaskSource(s, 'hole', docPoint, 3);
    expect(next.revision).not.toBe(s.revision);
    expect(hasAny(next.holeMask, MASK_EDITABLE)).toBe(true);
    expect(Array.from(next.editMask)).toEqual(Array.from(s.editMask));
    // source mask outside image? center lands valid source
  });
  it('does not mutate input', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    const before = Array.from(s.holeMask);
    const docPoint = { x: s.transform.x, y: s.transform.y };
    paintMaskSource(s, 'edit', docPoint, 10);
    expect(Array.from(s.holeMask)).toEqual(before);
  });
  it('outside image no bump? returns same ref maybe', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    const next = paintMaskSource(s, 'hole', { x: -1000, y: -1000 }, 4);
    expect(next).toBe(s);
  });
});

function hasAny(mask: Uint8Array, value: number) {
  return Array.from(mask).some((v) => v === value);
}

describe('mask state & effective', () => {
  it('maskState and conflict', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    expect(maskState(s).fillActive).toBe(false);
    expect(maskState(s).editActive).toBe(false);
    const holes = paintMaskSource(s, 'hole', { x: s.transform.x + 1, y: s.transform.y + 1 }, 2);
    expect(maskState(holes).hasHoles).toBe(true);
    expect(maskState(holes).fillActive).toBe(true);
  });
  it('effective output dims', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: [{ id: '1:1', width: 1024, height: 1024 }] });
    const out = effectiveOutputMasks(s);
    expect(out.fillMask.length).toBe(s.canvasWidth * s.canvasHeight);
    expect(out.editMask.length).toBe(s.canvasWidth * s.canvasHeight);
    // uncovered borders likely present for 4:3 source in 1:1? source 4x3 in 1024 square => uncovered top/bottom
    const editable = Array.from(out.fillMask).filter((v) => v >= MASK_EDITABLE_THRESHOLD).length;
    expect(editable).toBeGreaterThan(0);
  });
  it('resolveGeneration mapping', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    expect(resolveGeneration(s).mode).toBe('global');
    const holes = paintMaskSource(s, 'hole', { x: s.transform.x + 1, y: s.transform.y + 1 }, 2);
    // holes only -> inpaint
    expect(resolveGeneration(holes).mode).toBe('inpaint');
    const uncoveredOnly = changeFormat(s, FORMATS[0]); // same format no holes? uncovered? fit fills canvas maybe for 1:1 source no uncovered
    void uncoveredOnly;
  });
});

describe('locks', () => {
  it('edit active blocks format/change transform', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    const doc = { x: s.transform.x + 1, y: s.transform.y + 1 };
    const edited = paintMaskSource(s, 'edit', doc, 2);
    expect(canUseTool(edited, 'eraser').allowed).toBe(false);
    expect(canUseTool(edited, 'magicEraser').allowed).toBe(true);
    expect(canChangeFormat(edited).allowed).toBe(false);
    expect(canTransform(edited).allowed).toBe(false);
    expect(canUseTool(edited, 'normalEraser')).toBeDefined();
  });
  it('fill active blocks magic eraser', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats: FORMATS });
    const doc = { x: s.transform.x + 1, y: s.transform.y + 1 };
    const filled = paintMaskSource(s, 'hole', doc, 2);
    expect(canUseTool(filled, 'magicEraser').allowed).toBe(false);
  });
});

describe('changeFormat', () => {
  it('changes canvas dims and preserves masks', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 4 }, formats: FORMATS });
    const painted = paintMaskSource(s, 'hole', { x: s.transform.x + 1, y: s.transform.y + 1 }, 2);
    const next = changeFormat(painted, FORMATS.find((f) => f.id === '9:16')!);
    expect(next.canvasWidth).toBe(720);
    expect(next.canvasHeight).toBe(1280);
    expect(Array.from(next.holeMask)).toEqual(Array.from(painted.holeMask));
    expect(next.revision).not.toBe(painted.revision);
  });
  it('refuses when edit active (returns same content)', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 4 }, formats: FORMATS });
    const edited = paintMaskSource(s, 'edit', { x: s.transform.x + 1, y: s.transform.y + 1 }, 2);
    const next = changeFormat(edited, FORMATS[1]);
    expect(next.canvasWidth).toBe(edited.canvasWidth);
    expect(next.revision).toBe(edited.revision);
  });
});

describe('undo', () => {
  it('one push per gesture restores', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 4 }, formats: FORMATS });
    const pushed = pushUndo(s);
    const changed = { ...pushed, canvasWidth: 999 } as any;
    // transform change via setWorkTransform then withRevision
    const changed2 = setWorkTransform(changed, { x: 10, y: 10, width: 5, height: 5 });
    const committed = withRevision(changed2, 'rev5');
    const undone = undo(committed);
    // undo should restore snapshot from pushed (original)
    expect(undone.canvasWidth).toBe(s.canvasWidth);
  });
});

describe('applyCrop', () => {
  it('remaps masks', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 4 }, formats: FORMATS });
    const painted = paintMaskSource(s, 'hole', { x: s.transform.x + 1, y: s.transform.y + 1 }, 2);
    const croppedImg = { naturalWidth: 2, naturalHeight: 2 } as unknown as HTMLImageElement;
    const next = applyCrop(painted, { x: s.transform.x, y: s.transform.y, width: s.transform.width / 2, height: s.transform.height / 2 }, croppedImg, 'c');
    expect(next.sourceSize).toEqual({ width: 2, height: 2 });
    expect(next.cropPreview).toBeNull();
    // at least maybe 0/1 mask
    expect(next.holeMask.length).toBe(4);
  });
});

describe('history selection / reset', () => {
  it('reset restores active base not first', () => {
    const img = {} as HTMLImageElement;
    const s = createEditorSession({ image: img, url: 'u0', sourceSize: { width: 4, height: 4 }, formats: FORMATS });
    const entry2 = createHistoryEntry({ image: img, url: 'u1', sourceSize: { width: 8, height: 8 }, format: FORMATS[1], kind: 'result' });
    const s2 = appendResult(s, entry2);
    const s3 = selectHistoryBase(s2, entry2.id);
    expect(s3.sourceSize).toEqual({ width: 8, height: 8 });
    // dirty
    const dirty = paintMaskSource(s3, 'hole', { x: s3.transform.x + 1, y: s3.transform.y + 1 }, 2);
    const reset = resetActiveBase(dirty);
    expect(reset.sourceSize).toEqual({ width: 8, height: 8 });
    expect(hasAnyMaskBit(reset.holeMask, MASK_EDITABLE)).toBe(false);
    expect(reset.history).toHaveLength(2);
  });
});

function hasAnyMaskBit(mask: Uint8Array, value: number) {
  return Array.from(mask).some((v) => v === value);
}

describe('collectSessionUrls', () => {
  it('collects unique urls', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 2, height: 2 }, formats: FORMATS });
    const u = collectSessionUrls(s);
    expect(u).toContain('u');
  });
});

describe('revision helpers', () => {
  it('commitSession uses newRevision', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 2, height: 2 }, formats: FORMATS });
    const c = commitSession(s, 'abc');
    expect(c.revision).toBe('abc');
    const c2 = commitSession(s, { newRevision: 'def' });
    expect(c2.revision).toBe('def');
    const c3 = commitSession(s, () => 'ghi');
    expect(c3.revision).toBe('ghi');
  });
  it('setWorkTransform does not bump revision', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 2, height: 2 }, formats: FORMATS });
    const t = setWorkTransform(s, { x: 5, y: 5, width: 5, height: 5 });
    expect(t.revision).toBe(s.revision);
    expect(t.transform.x).toBe(5);
  });
});

describe('clear masks', () => {
  it('clearMask and clearAllMasks', () => {
    const s = createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 2, height: 2 }, formats: FORMATS });
    const p = paintMaskSource(s, 'hole', { x: s.transform.x + 0.5, y: s.transform.y + 0.5 }, 2);
    const c = clearMask(p, 'hole');
    expect(Array.from(c.holeMask).every((v) => (v as number) === 0)).toBe(true);
    const both = clearAllMasks(p);
    expect(Array.from(both.holeMask).every((v) => (v as number) === 0)).toBe(true);
    expect(Array.from(both.editMask).every((v) => (v as number) === 0)).toBe(true);
  });
});
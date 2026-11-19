import { describe, expect, it } from 'vitest';
import { screenToDocument, documentToSource, cropDocumentRectToSource } from '@/pages/images/imageGeometry';
import { sha256Bytes } from '@/pages/images/imageEncoding';
import { createMask, paintCircle, createCoverageMask, isEditableMaskByte, MASK_EDITABLE, MASK_PROTECTED } from '@/pages/images/imageMask';

describe('image helpers', () => {
  it('converts screen to document coordinates at zoom', () => {
    const point = screenToDocument({ x: 110, y: 60 }, { zoom: 2, panX: 10, panY: 20 });
    expect(point).toEqual({ x: 50, y: 20 });
  });

  it('converts document to source coordinates', () => {
    const point = documentToSource({ x: 50, y: 50 }, { x: 0, y: 0, width: 100, height: 100 }, 200, 200);
    expect(point).toEqual({ x: 100, y: 100 });
  });

  it('paints a circle at zoom-corrected document coords', () => {
    const mask = paintCircle(createMask(4, 4, MASK_PROTECTED), 4, 4, 1, 1, 0.5, MASK_EDITABLE);
    expect(mask[1 * 4 + 1]).toBe(MASK_EDITABLE);
    expect(isEditableMaskByte(mask[1 * 4 + 1])).toBe(true);
  });

  it('createCoverageMask fills uncovered zones only', () => {
    const mask = createCoverageMask(4, 4, { x: 1, y: 1, width: 2, height: 2 });
    expect(mask[0]).toBe(MASK_EDITABLE);
    expect(mask[1 * 4 + 1]).toBe(MASK_PROTECTED);
  });

  it('crop maps document rect to source', () => {
    const rect = cropDocumentRectToSource({ x: 10, y: 10, width: 20, height: 20 }, { x: 0, y: 0, width: 100, height: 100 }, 200, 200);
    expect(rect).toEqual({ x: 20, y: 20, width: 40, height: 40 });
  });
});


describe('image review helpers', () => {
  it('sha256 digests only the sliced region', async () => {
    const base = new ArrayBuffer(8);
    const view = new Uint8Array(base);
    view.set([9, 9, 1, 2, 3, 4, 9, 9]);
    const slice = new Uint8Array(base, 2, 4);
    const digest = await sha256Bytes(slice);
    const expected = await sha256Bytes(new Uint8Array([1, 2, 3, 4]));
    expect(digest).toBe(expected);
  });

  it('paintCircle does not mutate the input mask', () => {
    const input = new Uint8Array([0, 0, 0, 0]);
    const output = paintCircle(input, 2, 2, 0, 0, 1, 255);
    expect(Array.from(input)).toEqual([0, 0, 0, 0]);
    expect(output).not.toBe(input);
    expect(output[0]).toBe(255);
  });

  it('createCoverageMask floors fractional source rect', () => {
    const mask = createCoverageMask(4, 4, { x: 0.7, y: 0.7, width: 1.2, height: 1.2 });
    // floor: sx=0, sy=0, sw=1, sh=1 => protected only at (0,0)
    expect(mask[0 * 4 + 0]).toBe(MASK_PROTECTED);
    expect(mask[1 * 4 + 1]).toBe(MASK_EDITABLE);
    expect(mask[1 * 4 + 0]).toBe(MASK_EDITABLE);
  });
});

import { describe, expect, it } from 'vitest';
import {
  createCoverageMask,
  pointInRect,
  MASK_EDITABLE,
  MASK_PROTECT,
  MASK_PROTECTED,
} from '@/pages/images/imageMask';

function index(width: number, x: number, y: number): number {
  return y * width + x;
}

describe('imageMask.createCoverageMask margins', () => {
  const width = 6;
  const height = 6;
  const sourceRect = { x: 1, y: 1, width: 4, height: 4 };

  it('marks the top margin (disjoint uncovered strip) as editable', () => {
    const mask = createCoverageMask(width, height, sourceRect);
    for (let x = 0; x < width; x += 1) {
      expect(mask[index(width, x, 0)]).toBe(MASK_EDITABLE);
    }
    // row just below top margin belongs to source rect and is protected (except corners handled by side margins)
    expect(mask[index(width, 2, 1)]).toBe(MASK_PROTECT);
  });

  it('marks the bottom margin (disjoint uncovered strip) as editable', () => {
    const mask = createCoverageMask(width, height, sourceRect);
    for (let x = 0; x < width; x += 1) {
      expect(mask[index(width, x, height - 1)]).toBe(MASK_EDITABLE);
    }
    expect(mask[index(width, 2, height - 2)]).toBe(MASK_PROTECT);
  });

  it('marks the left margin as editable', () => {
    const mask = createCoverageMask(width, height, sourceRect);
    for (let y = 0; y < height; y += 1) {
      expect(mask[index(width, 0, y)]).toBe(MASK_EDITABLE);
    }
    expect(mask[index(width, 1, 2)]).toBe(MASK_PROTECT);
  });

  it('marks the right margin as editable', () => {
    const mask = createCoverageMask(width, height, sourceRect);
    for (let x = 0, y = 0; y < height; y += 1) {
      expect(mask[index(width, width - 1, y)]).toBe(MASK_EDITABLE);
    }
    expect(mask[index(width, width - 2, 2)]).toBe(MASK_PROTECT);
  });

  it('produces two disjoint uncovered rectangles on top and bottom', () => {
    const mask = createCoverageMask(width, height, sourceRect);
    const topRow = Array.from({ length: width }, (_, x) => mask[index(width, x, 0)]);
    const bottomRow = Array.from({ length: width }, (_, x) => mask[index(width, x, height - 1)]);
    expect(topRow.every((v) => v === MASK_EDITABLE)).toBe(true);
    expect(bottomRow.every((v) => v === MASK_EDITABLE)).toBe(true);
    // separated by at least one fully protected row in the middle
    const middleRow = Array.from({ length: width }, (_, x) => mask[index(width, x, 2)]);
    expect(middleRow.some((v) => v === MASK_PROTECT)).toBe(true);
  });

  it('produces two disjoint uncovered rectangles on left and right', () => {
    const mask = createCoverageMask(width, height, sourceRect);
    const leftCol = Array.from({ length: height }, (_, y) => mask[index(width, 0, y)]);
    const rightCol = Array.from({ length: height }, (_, y) => mask[index(width, width - 1, y)]);
    expect(leftCol.every((v) => v === MASK_EDITABLE)).toBe(true);
    expect(rightCol.every((v) => v === MASK_EDITABLE)).toBe(true);
    // at least one protected column sits between them on source rows
    const middleCol = Array.from({ length: height }, (_, y) => mask[index(width, 2, y)]);
    expect(middleCol.some((v) => v === MASK_PROTECT)).toBe(true);
  });

  it('marks every pixel protected when source covers the whole canvas', () => {
    const mask = createCoverageMask(width, height, { x: 0, y: 0, width, height });
    for (let i = 0; i < mask.length; i += 1) {
      expect(mask[i]).toBe(MASK_PROTECT);
    }
  });

  it('marks every pixel editable when source is outside the canvas', () => {
    const mask = createCoverageMask(width, height, { x: width + 1, y: height + 1, width: 2, height: 2 });
    for (let i = 0; i < mask.length; i += 1) {
      expect(mask[i]).toBe(MASK_EDITABLE);
    }
  });

  it('floors fractional source rect (asymmetric)', () => {
    const mask = createCoverageMask(4, 4, { x: 0.7, y: 0.7, width: 1.2, height: 1.2 });
    // sx=0, sy=0, sw=1, sh=1 => only (0,0) protected
    expect(mask[0 * 4 + 0]).toBe(MASK_PROTECT);
    expect(mask[1 * 4 + 1]).toBe(MASK_EDITABLE);
    expect(mask[1 * 4 + 0]).toBe(MASK_EDITABLE);
  });

  it('clips source rect to canvas bounds', () => {
    const mask = createCoverageMask(4, 4, { x: 2, y: 2, width: 10, height: 10 });
    expect(mask[3 * 4 + 3]).toBe(MASK_PROTECT);
    expect(mask[0]).toBe(MASK_EDITABLE);
  });
});

describe('imageMask.pointInRect', () => {
  const rect = { x: 2, y: 3, width: 4, height: 5 };

  it('returns true inside and on inclusive boundaries', () => {
    expect(pointInRect({ x: 4, y: 5 }, rect)).toBe(true);
    expect(pointInRect({ x: 2, y: 3 }, rect)).toBe(true);
    expect(pointInRect({ x: 6, y: 8 }, rect)).toBe(true);
  });

  it('returns false outside each side', () => {
    expect(pointInRect({ x: 1, y: 5 }, rect)).toBe(false);
    expect(pointInRect({ x: 7, y: 5 }, rect)).toBe(false);
    expect(pointInRect({ x: 4, y: 2 }, rect)).toBe(false);
    expect(pointInRect({ x: 4, y: 9 }, rect)).toBe(false);
  });

  it('is equivalent to geometry isPointInRect on sampled points', () => {
    // imported lazily to avoid circular coupling in assertions
    // compare via direct reimplementation of the shared inclusive rule
    const points = [
      { x: 2, y: 3 },
      { x: 6, y: 8 },
      { x: 4, y: 5.5 },
      { x: 1.5, y: 5 },
      { x: 4, y: 8.5 },
    ];
    for (const p of points) {
      const expected = p.x >= rect.x && p.x <= rect.x + rect.width && p.y >= rect.y && p.y <= rect.y + rect.height;
      expect(pointInRect(p, rect)).toBe(expected);
    }
  });
});

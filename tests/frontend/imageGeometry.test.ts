import { describe, expect, it } from 'vitest';
import {
  fitRectToCanvas,
  isPointInRect,
  isPixelInsideRect,
  documentToSource,
  type Rect,
} from '@/pages/images/imageGeometry';

describe('imageGeometry.fitRectToCanvas', () => {
  it('fits equal aspect ratio exactly with no letterboxing', () => {
    const result = fitRectToCanvas({ width: 100, height: 100 }, { width: 50, height: 50 });
    expect(result).toEqual({ x: 0, y: 0, width: 100, height: 100 });
  });

  it('letterboxes a landscape source in a portrait canvas (width-limited)', () => {
    const result = fitRectToCanvas({ width: 100, height: 200 }, { width: 200, height: 100 });
    expect(result.x).toBe(0);
    expect(result.width).toBe(100);
    expect(result.height).toBe(50);
    expect(result.y).toBe(75);
  });

  it('pillarboxes a portrait source in a landscape canvas (height-limited)', () => {
    const result = fitRectToCanvas({ width: 200, height: 100 }, { width: 100, height: 200 });
    expect(result.x).toBe(75);
    expect(result.y).toBe(0);
    expect(result.width).toBe(50);
    expect(result.height).toBe(100);
  });

  it('handles an asymmetric non-integer scaling case', () => {
    const result = fitRectToCanvas({ width: 10, height: 20 }, { width: 3, height: 7 });
    const scale = Math.min(10 / 3, 20 / 7);
    expect(result.width).toBeCloseTo(3 * scale, 10);
    expect(result.height).toBeCloseTo(7 * scale, 10);
    expect(result.x).toBeCloseTo((10 - 3 * scale) / 2, 10);
    expect(result.y).toBeCloseTo((20 - 7 * scale) / 2, 10);
  });

  it('returns a degenerate canvas-sized rect when canvas has non-positive dimensions', () => {
    expect(fitRectToCanvas({ width: 0, height: 10 }, { width: 5, height: 5 })).toEqual({
      x: 0,
      y: 0,
      width: 0,
      height: 10,
    });
    expect(fitRectToCanvas({ width: 10, height: 0 }, { width: 5, height: 5 })).toEqual({
      x: 0,
      y: 0,
      width: 10,
      height: 0,
    });
  });

  it('fills the canvas when source has non-positive dimensions', () => {
    expect(fitRectToCanvas({ width: 80, height: 60 }, { width: 0, height: 5 })).toEqual({
      x: 0,
      y: 0,
      width: 80,
      height: 60,
    });
    expect(fitRectToCanvas({ width: 80, height: 60 }, { width: 5, height: 0 })).toEqual({
      x: 0,
      y: 0,
      width: 80,
      height: 60,
    });
  });

  it('never returns a sub-pixel-collapsed rect below 1px', () => {
    const result = fitRectToCanvas({ width: 1, height: 1 }, { width: 1000, height: 1000 });
    expect(result.width).toBe(1);
    expect(result.height).toBe(1);
  });
});

describe('imageGeometry.isPointInRect', () => {
  const rect: Rect = { x: 2, y: 3, width: 4, height: 5 };

  it('returns true for points strictly inside', () => {
    expect(isPointInRect({ x: 4, y: 5 }, rect)).toBe(true);
    expect(isPointInRect({ x: 3, y: 4 }, rect)).toBe(true);
  });

  it('returns false when outside on each side', () => {
    expect(isPointInRect({ x: 1, y: 5 }, rect)).toBe(false);
    expect(isPointInRect({ x: 7, y: 5 }, rect)).toBe(false);
    expect(isPointInRect({ x: 4, y: 2 }, rect)).toBe(false);
    expect(isPointInRect({ x: 4, y: 9 }, rect)).toBe(false);
  });

  it('treats inclusive boundaries as inside', () => {
    expect(isPointInRect({ x: 2, y: 3 }, rect)).toBe(true);
    expect(isPointInRect({ x: 6, y: 3 }, rect)).toBe(true);
    expect(isPointInRect({ x: 2, y: 8 }, rect)).toBe(true);
    expect(isPointInRect({ x: 6, y: 8 }, rect)).toBe(true);
  });

  it('boundary just outside is false', () => {
    expect(isPointInRect({ x: 1.999, y: 5 }, rect)).toBe(false);
    expect(isPointInRect({ x: 6.001, y: 5 }, rect)).toBe(false);
    expect(isPointInRect({ x: 4, y: 2.999 }, rect)).toBe(false);
    expect(isPointInRect({ x: 4, y: 8.001 }, rect)).toBe(false);
  });
});

describe('imageGeometry.documentToSource', () => {
  it('maps document coordinates through a matching rect', () => {
    const point = documentToSource({ x: 50, y: 50 }, { x: 0, y: 0, width: 100, height: 100 }, 200, 200);
    expect(point).toEqual({ x: 100, y: 100 });
  });

  it('accounts for rect offset and asymmetric source dimensions', () => {
    const point = documentToSource({ x: 60, y: 45 }, { x: 10, y: 20, width: 100, height: 50 }, 200, 100);
    expect(point).toEqual({ x: 100, y: 50 });
  });

  it('does not divide by zero when rect dimensions vanish', () => {
    const point = documentToSource({ x: 5, y: 5 }, { x: 0, y: 0, width: 0, height: 0 }, 10, 20);
    expect(Number.isFinite(point.x)).toBe(true);
    expect(Number.isFinite(point.y)).toBe(true);
  });
});

describe('imageGeometry.isPixelInsideRect', () => {
  const rect: Rect = { x: 2, y: 3, width: 4, height: 5 };

  it('returns true for pixels strictly inside', () => {
    expect(isPixelInsideRect({ x: 2, y: 3 }, rect)).toBe(true);
    expect(isPixelInsideRect({ x: 5, y: 7 }, rect)).toBe(true);
  });

  it('treats the right and bottom borders as outside (exclusive)', () => {
    expect(isPixelInsideRect({ x: 6, y: 3 }, rect)).toBe(false);
    expect(isPixelInsideRect({ x: 2, y: 8 }, rect)).toBe(false);
    expect(isPixelInsideRect({ x: 6, y: 8 }, rect)).toBe(false);
  });

  it('returns false for pixels outside', () => {
    expect(isPixelInsideRect({ x: 1, y: 5 }, rect)).toBe(false);
    expect(isPixelInsideRect({ x: 7, y: 5 }, rect)).toBe(false);
  });
});

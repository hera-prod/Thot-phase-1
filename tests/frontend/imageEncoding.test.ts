import { describe, expect, it } from 'vitest';
import { extendEdgesToCanvas } from '@/pages/images/imageEncoding';

describe('extendEdgesToCanvas', () => {
  it('extends inside edges (clamp-to-edge) and leaves inside pixels unchanged', () => {
    const W = 6;
    const H = 4;
    const rect = { x: 2, y: 1, width: 2, height: 2 };
    const data = new Uint8ClampedArray(W * H * 4);
    // outside default
    for (let i = 0; i < data.length; i += 4) {
      data[i] = 7; data[i + 1] = 7; data[i + 2] = 7; data[i + 3] = 7;
    }
    const insideValues: Record<string, [number, number, number, number]> = {
      '2,1': [10, 20, 30, 40],
      '3,1': [50, 60, 70, 80],
      '2,2': [90, 100, 110, 120],
      '3,2': [130, 140, 150, 160],
    };
    for (const [key, v] of Object.entries(insideValues)) {
      const [x, y] = key.split(',').map(Number);
      const i = (y * W + x) * 4;
      data[i] = v[0]; data[i + 1] = v[1]; data[i + 2] = v[2]; data[i + 3] = v[3];
    }

    extendEdgesToCanvas(data, W, H, rect);

    // inside unchanged
    for (const [key, v] of Object.entries(insideValues)) {
      const [x, y] = key.split(',').map(Number);
      const i = (y * W + x) * 4;
      expect([data[i], data[i + 1], data[i + 2], data[i + 3]]).toEqual(v);
    }
    // top-left corner outside copies nearest inside (2,1)
    {
      const i = 0;
      expect([data[i], data[i + 1], data[i + 2], data[i + 3]]).toEqual([10, 20, 30, 40]);
    }
    // bottom-right corner outside (5,3) copies (3,2)
    {
      const i = (3 * W + 5) * 4;
      expect([data[i], data[i + 1], data[i + 2], data[i + 3]]).toEqual([130, 140, 150, 160]);
    }
    // left margin same row (1,1) copies (2,1)
    {
      const i = (1 * W + 1) * 4;
      expect([data[i], data[i + 1], data[i + 2], data[i + 3]]).toEqual([10, 20, 30, 40]);
    }
    // right margin same row (4,1) copies (3,1)
    {
      const i = (1 * W + 4) * 4;
      expect([data[i], data[i + 1], data[i + 2], data[i + 3]]).toEqual([50, 60, 70, 80]);
    }
  });

  it('is a no-op for degenerate rect', () => {
    const W = 4; const H = 3;
    const data = new Uint8ClampedArray(W * H * 4);
    for (let i = 0; i < data.length; i += 1) data[i] = i % 251;
    const before = new Uint8ClampedArray(data);
    extendEdgesToCanvas(data, W, H, { x: 1, y: 1, width: 0, height: 2 });
    expect(Array.from(data)).toEqual(Array.from(before));
    extendEdgesToCanvas(data, W, H, { x: 1, y: 1, width: 2, height: 0 });
    expect(Array.from(data)).toEqual(Array.from(before));
  });
});
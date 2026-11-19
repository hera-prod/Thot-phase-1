import { describe, expect, it } from 'vitest';
import * as ie from '@/pages/images/imageEditor';
import { planModifierRequest } from '@/pages/images/imageRequest';
import * as geom from '@/pages/images/imageGeometry';

function makeSession(sourceWidth: number, sourceHeight: number, canvasWidth: number, canvasHeight: number) {
  return ie.createEditorSession({
    image: {} as HTMLImageElement,
    url: 'u',
    sourceSize: { width: sourceWidth, height: sourceHeight },
    formats: [{ id: `${canvasWidth}x${canvasHeight}`, width: canvasWidth, height: canvasHeight }],
  });
}

describe('imageRequest.planModifierRequest', () => {
  it('handles a 16:9 source in a 9:16 canvas', () => {
    const session = makeSession(1920, 1080, 720, 1280);
    const plan = planModifierRequest(session);
    expect(plan.coverage.center).toBe(0);
    expect(plan.coverage.top).toBeGreaterThan(0);
    expect(plan.coverage.bottom).toBeGreaterThan(0);
    expect(plan.differences.missing).toBe(0);
    expect(plan.differences.extra).toBe(0);
    expect(Array.from(plan.chosenMask ?? [])).toEqual(Array.from(plan.expectedMask ?? []));
  });

  it('handles a 9:16 source in a 16:9 canvas', () => {
    const session = makeSession(1080, 1920, 1280, 720);
    const plan = planModifierRequest(session);
    expect(plan.coverage.center).toBe(0);
    expect(plan.coverage.left).toBeGreaterThan(0);
    expect(plan.coverage.right).toBeGreaterThan(0);
    expect(plan.differences.missing).toBe(0);
    expect(plan.differences.extra).toBe(0);
    expect(Array.from(plan.chosenMask ?? [])).toEqual(Array.from(plan.expectedMask ?? []));
  });

  it('marks outer horizontal bands editable and inner columns protected (P-08)', () => {
    const session = makeSession(4, 4, 8, 8);
    session.transform = { x: 2, y: 2, width: 4, height: 4 };
    const plan = planModifierRequest(session);
    const chosen = plan.chosenMask as Uint8Array;
    const midRow = 3; // inside transform y range
    for (const x of [0, 1, 6, 7]) {
      expect(chosen[midRow * 8 + x]).toBeGreaterThanOrEqual(128);
    }
    for (const x of [2, 3, 4, 5]) {
      expect(chosen[midRow * 8 + x]).toBeLessThan(128);
    }
    // explicitly x=6 editable
    expect(chosen[midRow * 8 + 6]).toBeGreaterThanOrEqual(128);
    expect(plan.differences.missing).toBe(0);
    expect(plan.differences.extra).toBe(0);
    expect(Array.from(plan.chosenMask ?? [])).toEqual(Array.from(plan.expectedMask ?? []));
  });

  it('produces an edit mask when an edit mask is painted', () => {
    const session = makeSession(8, 8, 8, 8);
    const painted = ie.paintMaskSource(session, 'edit', { x: 4, y: 4 }, 2);
    const plan = planModifierRequest(painted);
    expect(plan.maskKind).toBe('edit');
  });

  it('round-trips isPixelInsideRect on the transform border', () => {
    const rect = { x: 2, y: 2, width: 4, height: 4 };
    expect(geom.isPixelInsideRect({ x: 6, y: 3 }, rect)).toBe(false);
    expect(geom.isPixelInsideRect({ x: 5, y: 3 }, rect)).toBe(true);
  });

  it('inpaint avec trous : les marges hors image restent protégées', () => {
    const session = makeSession(4, 4, 8, 8);
    session.transform = { x: 2, y: 2, width: 4, height: 4 };
    const painted = ie.paintMaskSource(session, 'hole', { x: 4, y: 4 }, 1);
    const plan = planModifierRequest(painted);
    expect(plan.mode).toBe('inpaint');
    const chosen = plan.chosenMask as Uint8Array;
    // marges (lignes hors transform) protégées
    for (let x = 0; x < 8; x += 1) {
      expect(chosen[0 * 8 + x]).toBeLessThan(128);
      expect(chosen[7 * 8 + x]).toBeLessThan(128);
    }
    // le trou peint reste éditable
    expect(chosen[4 * 8 + 4]).toBeGreaterThanOrEqual(128);
    expect(plan.differences.missing).toBe(0);
    expect(plan.differences.extra).toBe(0);
    // marges gauche/droite protégées sur une ligne du trou
    expect(chosen[4 * 8 + 0]).toBeLessThan(128);
    expect(chosen[4 * 8 + 7]).toBeLessThan(128);
    expect(Array.from(chosen)).toEqual(Array.from(plan.expectedMask ?? []));
  });

  it('outpaint pur : les marges restent éditables', () => {
    const session = makeSession(4, 4, 8, 8);
    session.transform = { x: 2, y: 2, width: 4, height: 4 };
    const plan = planModifierRequest(session);
    expect(plan.mode).toBe('outpaint');
    const chosen = plan.chosenMask as Uint8Array;
    expect(chosen[0 * 8 + 4]).toBeGreaterThanOrEqual(128); // haut
    expect(chosen[7 * 8 + 4]).toBeGreaterThanOrEqual(128); // bas
    expect(chosen[4 * 8 + 0]).toBeGreaterThanOrEqual(128); // gauche
    expect(chosen[4 * 8 + 7]).toBeGreaterThanOrEqual(128); // droite
    expect(plan.differences.missing).toBe(0);
    expect(plan.differences.extra).toBe(0);
    expect(Array.from(chosen)).toEqual(Array.from(plan.expectedMask ?? []));
  });
});

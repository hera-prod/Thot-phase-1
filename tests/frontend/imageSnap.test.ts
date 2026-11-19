import { describe, expect, it } from 'vitest';
import {
  canvasSnapTargets,
  snapMoveRect,
  snapResizeRect,
  type SnapHandle,
} from '@/pages/images/imageSnap';
import type { Rect } from '@/pages/images/imageGeometry';

const canvas = { width: 100, height: 80 };

describe('imageSnap', () => {
  it('renvoie les cibles de bord et de centre du canevas', () => {
    expect(canvasSnapTargets(canvas)).toEqual({
      x: [0, 50, 100],
      y: [0, 40, 80],
    });
  });

  describe('snapMoveRect', () => {
    it('aimante le bord gauche et le bord haut au canevas', () => {
      const result = snapMoveRect({ x: 2, y: 2, width: 20, height: 10 }, canvas, 4);
      expect(result.rect).toEqual({ x: 0, y: 0, width: 20, height: 10 });
      expect(result.guideX).toBe(0);
      expect(result.guideY).toBe(0);
    });

    it('aimante le centre du rect sur le centre du canevas', () => {
      const result = snapMoveRect({ x: 38, y: 0, width: 24, height: 10 }, canvas, 4);
      expect(result.rect.x).toBe(38);
      expect(result.guideX).toBe(50);
      expect(result.guideY).toBe(0);
    });

    it('ne modifie rien au-delà du seuil', () => {
      const result = snapMoveRect({ x: 20, y: 20, width: 20, height: 10 }, canvas, 4);
      expect(result.rect).toEqual({ x: 20, y: 20, width: 20, height: 10 });
      expect(result.guideX).toBeNull();
      expect(result.guideY).toBeNull();
    });

    it('respecte enabled=false', () => {
      const result = snapMoveRect({ x: 2, y: 2, width: 20, height: 10 }, canvas, 4, false);
      expect(result.rect).toEqual({ x: 2, y: 2, width: 20, height: 10 });
      expect(result.guideX).toBeNull();
      expect(result.guideY).toBeNull();
    });
  });

  describe('snapResizeRect', () => {
    it('aimante le bord droit d’un côté e sans bouger le bord gauche', () => {
      const result = snapResizeRect({ x: 80, y: 20, width: 18, height: 10 }, 'e', canvas, 4, 1.8);
      expect(result.rect).toEqual({ x: 80, y: 20, width: 20, height: 10 });
      expect(result.guideX).toBe(100);
      expect(result.guideY).toBeNull();
    });

    it('aimante le bord gauche d’un côté w en gardant le bord droit fixe', () => {
      const result = snapResizeRect({ x: 3, y: 20, width: 50, height: 10 }, 'w', canvas, 4, 5);
      expect(result.rect.x).toBe(0);
      expect(result.rect.width).toBe(53);
      expect(result.rect.x + result.rect.width).toBe(53);
      expect(result.guideX).toBe(0);
    });

    it('coin se : l’axe X choisi est aimanté et le ratio est conservé', () => {
      const input: Rect = { x: 20, y: 20, width: 28, height: 14 };
      const result = snapResizeRect(input, 'se', canvas, 4, 2);
      expect(result.guideX).toBe(50);
      expect(result.guideY).toBeNull();
      expect(result.rect.x).toBe(20);
      expect(result.rect.y).toBe(20);
      expect(result.rect.x + result.rect.width).toBe(50);
      expect(result.rect.height).toBe(result.rect.width / 2);
    });

    it('coin nw : le coin opposé bas-droite reste fixe', () => {
      const input: Rect = { x: 3, y: 3, width: 40, height: 20 };
      const result = snapResizeRect(input, 'nw', canvas, 4, 2);
      expect(result.guideX).toBe(0);
      expect(result.guideY).toBeNull();
      expect(result.rect.x + result.rect.width).toBe(input.x + input.width);
      expect(result.rect.y + result.rect.height).toBe(input.y + input.height);
    });

    it('respecte un minimum de 1 px', () => {
      const result = snapResizeRect({ x: 2, y: 20, width: 1, height: 10 }, 'e', canvas, 4, 0.1);
      expect(result.rect.width).toBe(1);
    });

    it('respecte enabled=false et ne mute pas les objets d’entrée', () => {
      const input: Rect = { x: 20, y: 20, width: 28, height: 14 };
      const copy = { ...input };
      const disabled = snapResizeRect(input, 'se', canvas, 4, 2, false);
      expect(disabled.rect).toEqual(copy);
      expect(disabled.guideX).toBeNull();
      expect(disabled.guideY).toBeNull();

      const enabled = snapResizeRect(input, 'se' as SnapHandle, canvas, 4, 2);
      expect(input).toEqual(copy);
      expect(enabled.rect).not.toBe(input);
    });
  });
});

export const MASK_EDITABLE_THRESHOLD = 128;
export const MASK_PROTECT = 0;
export const MASK_PROTECTED = 0;
export const MASK_EDITABLE = 255;

export function createMask(width: number, height: number, fill = MASK_PROTECTED): Uint8Array {
  return new Uint8Array(Math.max(0, width * height)).fill(fill & 0xff);
}

export function isEditableMaskByte(value: number): boolean {
  return (value & 0xff) >= MASK_EDITABLE_THRESHOLD;
}

export function paintCircle(
  mask: Uint8Array,
  width: number,
  height: number,
  centerX: number,
  centerY: number,
  radius: number,
  value: number,
): Uint8Array {
  const next = new Uint8Array(mask);
  const r = Math.max(0, radius);
  const r2 = r * r;
  const minX = Math.max(0, Math.floor(centerX - r));
  const maxX = Math.min(width - 1, Math.ceil(centerX + r));
  const minY = Math.max(0, Math.floor(centerY - r));
  const maxY = Math.min(height - 1, Math.ceil(centerY + r));
  for (let y = minY; y <= maxY; y += 1) {
    for (let x = minX; x <= maxX; x += 1) {
      const dx = x - centerX;
      const dy = y - centerY;
      if (dx * dx + dy * dy <= r2) {
        const index = y * width + x;
        if (index >= 0 && index < next.length) next[index] = value & 0xff;
      }
    }
  }
  return next;
}

export function pointInRect(point: { x: number; y: number }, rect: { x: number; y: number; width: number; height: number }): boolean {
  return point.x >= rect.x && point.x <= rect.x + rect.width && point.y >= rect.y && point.y <= rect.y + rect.height;
}

export function createCoverageMask(
  width: number,
  height: number,
  sourceRect: { x: number; y: number; width: number; height: number },
  includeSourceTransparentHoles = false,
): Uint8Array {
  const sx = Math.max(0, Math.floor(sourceRect.x));
  const sy = Math.max(0, Math.floor(sourceRect.y));
  const sw = Math.max(0, Math.floor(sourceRect.width));
  const sh = Math.max(0, Math.floor(sourceRect.height));
  const ex = Math.min(width, sx + sw);
  const ey = Math.min(height, sy + sh);

  const mask = createMask(width, height, 0);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x;
      const covered = x >= sx && x < ex && y >= sy && y < ey;
      if (!covered) mask[index] = MASK_EDITABLE;
      if (includeSourceTransparentHoles && covered) mask[index] = MASK_EDITABLE;
    }
  }
  // Parameter kept explicitly; behavior default preserves imported transparency.
  void includeSourceTransparentHoles;
  return mask;
}

export function normalizeMaskByte(value: number): number {
  return value >= MASK_EDITABLE_THRESHOLD ? MASK_EDITABLE : MASK_PROTECTED;
}

// aliases
export const createOutpaintMask = createCoverageMask;
export const editableThreshold = MASK_EDITABLE_THRESHOLD;

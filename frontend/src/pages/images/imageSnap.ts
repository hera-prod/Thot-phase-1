import type { Rect, Size } from './imageGeometry';

export type SnapHandle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w';
export type SnapResult = { rect: Rect; guideX: number | null; guideY: number | null };
export type SnapTargets = { x: number[]; y: number[] };

type AxisSnap = { value: number; target: number; delta: number };

export function canvasSnapTargets(canvas: Size): SnapTargets {
  return {
    x: [0, canvas.width / 2, canvas.width],
    y: [0, canvas.height / 2, canvas.height],
  };
}

function nearest(value: number, targets: number[], threshold: number): AxisSnap | null {
  let best: AxisSnap | null = null;
  for (const target of targets) {
    const delta = target - value;
    if (Math.abs(delta) > threshold) continue;
    if (best === null || Math.abs(delta) < Math.abs(best.delta)) {
      best = { value: target, target, delta };
    }
  }
  return best;
}

function nearestEdge(edges: number[], targets: number[], threshold: number): AxisSnap | null {
  let best: AxisSnap | null = null;
  for (const edge of edges) {
    const candidate = nearest(edge, targets, threshold);
    if (candidate === null) continue;
    if (best === null || Math.abs(candidate.delta) < Math.abs(best.delta)) {
      best = candidate;
    }
  }
  return best;
}

function unchanged(rect: Rect): SnapResult {
  return { rect: { ...rect }, guideX: null, guideY: null };
}

export function snapMoveRect(rect: Rect, canvas: Size, threshold: number, enabled = true): SnapResult {
  if (!enabled || threshold <= 0) return unchanged(rect);

  const targets = canvasSnapTargets(canvas);
  const xSnap = nearestEdge(
    [rect.x, rect.x + rect.width / 2, rect.x + rect.width],
    targets.x,
    threshold,
  );
  const ySnap = nearestEdge(
    [rect.y, rect.y + rect.height / 2, rect.y + rect.height],
    targets.y,
    threshold,
  );

  return {
    rect: {
      x: rect.x + (xSnap?.delta ?? 0),
      y: rect.y + (ySnap?.delta ?? 0),
      width: rect.width,
      height: rect.height,
    },
    guideX: xSnap?.target ?? null,
    guideY: ySnap?.target ?? null,
  };
}

function snapSide(
  rect: Rect,
  handle: Exclude<SnapHandle, 'nw' | 'ne' | 'sw' | 'se'>,
  targets: SnapTargets,
  threshold: number,
): SnapResult {
  const right = rect.x + rect.width;
  const bottom = rect.y + rect.height;

  if (handle === 'e') {
    const snap = nearest(right, targets.x, threshold);
    if (snap === null) return unchanged(rect);
    return { rect: { ...rect, width: Math.max(1, snap.target - rect.x) }, guideX: snap.target, guideY: null };
  }
  if (handle === 'w') {
    const snap = nearest(rect.x, targets.x, threshold);
    if (snap === null) return unchanged(rect);
    const width = Math.max(1, right - snap.target);
    return { rect: { ...rect, x: right - width, width }, guideX: snap.target, guideY: null };
  }
  if (handle === 's') {
    const snap = nearest(bottom, targets.y, threshold);
    if (snap === null) return unchanged(rect);
    return { rect: { ...rect, height: Math.max(1, snap.target - rect.y) }, guideX: null, guideY: snap.target };
  }
  const snap = nearest(rect.y, targets.y, threshold);
  if (snap === null) return unchanged(rect);
  const height = Math.max(1, bottom - snap.target);
  return { rect: { ...rect, y: bottom - height, height }, guideX: null, guideY: snap.target };
}

function snapCorner(
  rect: Rect,
  handle: 'nw' | 'ne' | 'sw' | 'se',
  targets: SnapTargets,
  threshold: number,
  aspect: number,
): SnapResult {
  const right = rect.x + rect.width;
  const bottom = rect.y + rect.height;
  const mobileX = handle === 'sw' || handle === 'nw' ? rect.x : right;
  const mobileY = handle === 'ne' || handle === 'nw' ? rect.y : bottom;

  const xSnap = nearest(mobileX, targets.x, threshold);
  const ySnap = nearest(mobileY, targets.y, threshold);
  if (xSnap === null && ySnap === null) return unchanged(rect);

  const safeAspect = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
  const chooseX = xSnap !== null && (ySnap === null || Math.abs(xSnap.delta) <= Math.abs(ySnap.delta));

  if (handle === 'se') {
    const width = chooseX
      ? Math.max(1, xSnap!.target - rect.x)
      : Math.max(1, ySnap!.target - rect.y) * safeAspect;
    const height = chooseX ? width / safeAspect : Math.max(1, ySnap!.target - rect.y);
    return {
      rect: { x: rect.x, y: rect.y, width, height },
      guideX: chooseX ? xSnap!.target : null,
      guideY: chooseX ? null : ySnap!.target,
    };
  }
  if (handle === 'sw') {
    const width = chooseX
      ? Math.max(1, right - xSnap!.target)
      : Math.max(1, ySnap!.target - rect.y) * safeAspect;
    const height = chooseX ? width / safeAspect : Math.max(1, ySnap!.target - rect.y);
    return {
      rect: { x: right - width, y: rect.y, width, height },
      guideX: chooseX ? xSnap!.target : null,
      guideY: chooseX ? null : ySnap!.target,
    };
  }
  if (handle === 'ne') {
    const width = chooseX
      ? Math.max(1, xSnap!.target - rect.x)
      : Math.max(1, bottom - ySnap!.target) * safeAspect;
    const height = chooseX ? width / safeAspect : Math.max(1, bottom - ySnap!.target);
    return {
      rect: { x: rect.x, y: bottom - height, width, height },
      guideX: chooseX ? xSnap!.target : null,
      guideY: chooseX ? null : ySnap!.target,
    };
  }
  const width = chooseX
    ? Math.max(1, right - xSnap!.target)
    : Math.max(1, bottom - ySnap!.target) * safeAspect;
  const height = chooseX ? width / safeAspect : Math.max(1, bottom - ySnap!.target);
  return {
    rect: { x: right - width, y: bottom - height, width, height },
    guideX: chooseX ? xSnap!.target : null,
    guideY: chooseX ? null : ySnap!.target,
  };
}

export function snapResizeRect(
  rect: Rect,
  handle: SnapHandle,
  canvas: Size,
  threshold: number,
  aspect: number,
  enabled = true,
): SnapResult {
  if (!enabled || threshold <= 0) return unchanged(rect);

  const targets = canvasSnapTargets(canvas);
  if (handle === 'nw' || handle === 'ne' || handle === 'sw' || handle === 'se') {
    return snapCorner(rect, handle, targets, threshold, aspect);
  }
  return snapSide(rect, handle, targets, threshold);
}

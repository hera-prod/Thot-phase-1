export type Point = { x: number; y: number };
export type Size = { width: number; height: number };
export type Rect = { x: number; y: number; width: number; height: number };
export type Viewport = { zoom: number; panX: number; panY: number };

export function clamp(value: number, min: number, max: number): number {
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

export function screenToDocument(point: Point, viewport: Viewport): Point {
  const zoom = viewport.zoom || 1;
  return { x: (point.x - viewport.panX) / zoom, y: (point.y - viewport.panY) / zoom };
}

export function documentToScreen(point: Point, viewport: Viewport): Point {
  const zoom = viewport.zoom || 1;
  return { x: point.x * zoom + viewport.panX, y: point.y * zoom + viewport.panY };
}

export function documentToSource(point: Point, rect: Rect, sourceWidth: number, sourceHeight: number): Point {
  const safeWidth = rect.width || 1;
  const safeHeight = rect.height || 1;
  return {
    x: ((point.x - rect.x) / safeWidth) * sourceWidth,
    y: ((point.y - rect.y) / safeHeight) * sourceHeight,
  };
}

export function sourceToDocument(point: Point, rect: Rect, sourceWidth: number, sourceHeight: number): Point {
  const safeSourceWidth = sourceWidth || 1;
  const safeSourceHeight = sourceHeight || 1;
  return {
    x: rect.x + (point.x / safeSourceWidth) * rect.width,
    y: rect.y + (point.y / safeSourceHeight) * rect.height,
  };
}

export function fitRectToCanvas(canvas: Size, source: Size): Rect {
  if (canvas.width <= 0 || canvas.height <= 0) return { x: 0, y: 0, width: canvas.width, height: canvas.height };
  if (source.width <= 0 || source.height <= 0) return { x: 0, y: 0, width: canvas.width, height: canvas.height };
  const scale = Math.min(canvas.width / source.width, canvas.height / source.height);
  const width = Math.max(1, source.width * scale);
  const height = Math.max(1, source.height * scale);
  return { x: (canvas.width - width) / 2, y: (canvas.height - height) / 2, width, height };
}

export function isPointInRect(point: Point, rect: Rect): boolean {
  return point.x >= rect.x && point.x <= rect.x + rect.width && point.y >= rect.y && point.y <= rect.y + rect.height;
}

export function isPixelInsideRect(point: Point, rect: Rect): boolean {
  return point.x >= rect.x && point.x < rect.x + rect.width
      && point.y >= rect.y && point.y < rect.y + rect.height;
}

export function cropDocumentRectToSource(crop: Rect, rect: Rect, sourceWidth: number, sourceHeight: number): Rect {
  const topLeft = documentToSource({ x: crop.x, y: crop.y }, rect, sourceWidth, sourceHeight);
  const bottomRight = documentToSource({ x: crop.x + crop.width, y: crop.y + crop.height }, rect, sourceWidth, sourceHeight);
  return {
    x: Math.min(topLeft.x, bottomRight.x),
    y: Math.min(topLeft.y, bottomRight.y),
    width: Math.abs(bottomRight.x - topLeft.x),
    height: Math.abs(bottomRight.y - topLeft.y),
  };
}

export const screenToCanvas = screenToDocument;
export const canvasToScreen = documentToScreen;
export const screenToImage = (point: Point, viewport: Viewport, rect: Rect, sourceWidth: number, sourceHeight: number) =>
  documentToSource(screenToDocument(point, viewport), rect, sourceWidth, sourceHeight);
export const imageToScreen = (point: Point, rect: Rect, sourceWidth: number, sourceHeight: number, viewport: Viewport) =>
  documentToScreen(sourceToDocument(point, rect, sourceWidth, sourceHeight), viewport);
export const canvasToImage = documentToSource;
export const imageToCanvas = sourceToDocument;
export const documentToCanvas = (p: Point) => p;
export const canvasToDocument = (p: Point) => p;
export const toDocumentCoordinates = screenToDocument;
export const toScreenCoordinates = documentToScreen;
export const toSourceCoordinates = documentToSource;
export const toImageCoordinates = documentToSource;

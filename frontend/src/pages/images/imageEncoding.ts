export async function sha256Bytes(bytes: Uint8Array | number[] | ArrayBuffer): Promise<string> {
  let exact: Uint8Array;
  if (bytes instanceof Uint8Array) {
    // byteOffset-safe copy of only the visible bytes
    const sliced = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    exact = new Uint8Array(sliced);
  } else if (bytes instanceof ArrayBuffer) {
    exact = new Uint8Array(bytes.slice(0));
  } else if (ArrayBuffer.isView(bytes)) {
    const view = bytes as ArrayBufferView;
    const sliced = view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength) as ArrayBuffer;
    exact = new Uint8Array(sliced);
  } else {
    exact = new Uint8Array(bytes);
  }
  const source: any =
    typeof Buffer !== 'undefined' ? Buffer.from(exact) : exact.buffer.slice(0, exact.byteLength);
  const digest = await crypto.subtle.digest('SHA-256', source);
  const arr = Array.from(new Uint8Array(digest));
  return arr.map((b) => b.toString(16).padStart(2, '0')).join('');
}

export const sha256Hex = sha256Bytes;
export const computeSha256 = sha256Bytes;
export const hashBytes = sha256Bytes;

export function canvasToBlob(canvas: HTMLCanvasElement, type = 'image/png', quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    if (typeof canvas.toBlob !== 'function') {
      reject(new Error('toBlob indisponible dans cet environnement.'));
      return;
    }
    canvas.toBlob((blob) => {
      if (!blob) reject(new Error('Blob image vide.'));
      else resolve(blob);
    }, type, quality);
  });
}

export function bytesFromBlob(blob: Blob): Promise<Uint8Array> {
  return blob.arrayBuffer().then((b) => new Uint8Array(b));
}

export async function stageBytes(blob: Blob): Promise<{ bytes: Uint8Array; sha256: string }> {
  const bytes = await bytesFromBlob(blob);
  const sha256 = await sha256Bytes(bytes);
  return { bytes, sha256 };
}

export function extendEdgesToCanvas(
  data: Uint8ClampedArray,
  canvasWidth: number,
  canvasHeight: number,
  rect: { x: number; y: number; width: number; height: number },
): void {
  if (!Number.isFinite(canvasWidth) || !Number.isFinite(canvasHeight) || canvasWidth <= 0 || canvasHeight <= 0) return;
  if (!rect || !Number.isFinite(rect.x) || !Number.isFinite(rect.y) || rect.width <= 0 || rect.height <= 0) return;
  const inside = (x: number, y: number): boolean =>
    x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height;
  for (let y = 0; y < canvasHeight; y += 1) {
    for (let x = 0; x < canvasWidth; x += 1) {
      if (inside(x, y)) continue;
      let sx = Math.round(Math.max(rect.x, Math.min(rect.x + rect.width - 1, x)));
      let sy = Math.round(Math.max(rect.y, Math.min(rect.y + rect.height - 1, y)));
      if (sx < 0) sx = 0;
      if (sx >= canvasWidth) sx = canvasWidth - 1;
      if (sy < 0) sy = 0;
      if (sy >= canvasHeight) sy = canvasHeight - 1;
      const di = (y * canvasWidth + x) * 4;
      const si = (sy * canvasWidth + sx) * 4;
      data[di] = data[si];
      data[di + 1] = data[si + 1];
      data[di + 2] = data[si + 2];
      data[di + 3] = data[si + 3];
    }
  }
}

export function drawWorkCanvas(
  canvasWidth: number,
  canvasHeight: number,
  image: HTMLImageElement | null,
  rect: { x: number; y: number; width: number; height: number } | null,
  holeMask?: Uint8Array | null,
  sourceSize?: { width: number; height: number } | null,
  options?: { eraseHoles?: boolean },
): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  canvas.width = canvasWidth;
  canvas.height = canvasHeight;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Contexte 2D indisponible.');
  ctx.clearRect(0, 0, canvasWidth, canvasHeight);
  if (image && rect) ctx.drawImage(image, rect.x, rect.y, rect.width, rect.height);

  // Étendre les bords du rect sur toute la toile : la work image est ainsi
  // pleinement opaque, sans marges transparentes/grises.
  if (image && rect) {
    try {
      if (typeof ctx.getImageData === 'function' && typeof ctx.putImageData === 'function') {
        const imageData = ctx.getImageData(0, 0, canvasWidth, canvasHeight);
        extendEdgesToCanvas(imageData.data, canvasWidth, canvasHeight, rect);
        ctx.putImageData(imageData, 0, 0);
      }
    } catch (error) {
      // Environnement sans getImageData/putImageData (ex. jsdom) ou toile non lisible.
      if (typeof console !== 'undefined' && typeof console.warn === 'function') {
        console.warn('drawWorkCanvas : extension des bords impossible, toile non pleinement opaque.', error);
      }
    }
  }

  if (options?.eraseHoles === true && holeMask && rect && sourceSize) {
    const sw = Math.max(1, sourceSize.width);
    const sh = Math.max(1, sourceSize.height);
    const offscreen = document.createElement('canvas');
    offscreen.width = canvasWidth;
    offscreen.height = canvasHeight;
    const octx = offscreen.getContext('2d');
    if (octx) {
      const data = octx.createImageData(canvasWidth, canvasHeight);
      for (let y = 0; y < canvasHeight; y += 1) {
        for (let x = 0; x < canvasWidth; x += 1) {
          const inside = x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height;
          if (!inside) continue;
          const src = {
            x: ((x - rect.x) / Math.max(1, rect.width)) * sw,
            y: ((y - rect.y) / Math.max(1, rect.height)) * sh,
          };
          const sx = Math.max(0, Math.min(sw - 1, Math.round(src.x)));
          const sy = Math.max(0, Math.min(sh - 1, Math.round(src.y)));
          const idx = sy * sw + sx;
          if (idx >= 0 && idx < holeMask.length && holeMask[idx] >= 128) {
            data.data[(y * canvasWidth + x) * 4 + 3] = 255;
          }
        }
      }
      octx.putImageData(data, 0, 0);
      ctx.globalCompositeOperation = 'destination-out';
      ctx.drawImage(offscreen, 0, 0);
      ctx.globalCompositeOperation = 'source-over';
    }
  }
  return canvas;
}

export function drawMaskCanvas(mask: Uint8Array, width: number, height: number): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Contexte 2D indisponible.');
  const data = ctx.createImageData(width, height);
  for (let i = 0; i < width * height; i += 1) {
    const v = mask[i] ?? 0;
    data.data[i * 4] = v;
    data.data[i * 4 + 1] = v;
    data.data[i * 4 + 2] = v;
    data.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(data, 0, 0);
  return canvas;
}

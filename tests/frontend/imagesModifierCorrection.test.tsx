import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
// ImagesPage is imported dynamically inside tests after mocks are set up.
import * as ie from '@/pages/images/imageEditor';

type Listener = (event: { payload: Record<string, unknown> }) => void;

class FakeImage {
  naturalWidth = 100;
  naturalHeight = 200;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(_value: string) {
    setTimeout(() => this.onload?.(), 0);
  }
}

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn((cmd: string, args: unknown) => {
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_status') return Promise.resolve(undefined);
    if (cmd === 'image_generate' || cmd === 'image_set_backend') return Promise.resolve(undefined);
    if (cmd === 'get_attachment_bytes') return Promise.resolve([[137, 80, 78, 71], 'image/png']);
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    return Promise.resolve(() => {});
  }),
}));

beforeEach(() => {
  vi.stubGlobal('Image', FakeImage as any);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:editor-url') });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Images modifier correction helpers', () => {
  it('clears sourceArtifactId when selecting history without artifact', () => {
    const formats = [{ id: '1:1', width: 100, height: 100 }];
    const session = ie.createEditorSession({
      image: {} as HTMLImageElement,
      url: 'u',
      sourceSize: { width: 4, height: 4 },
      formats,
      sourceArtifactId: 'art_original',
    });
    const referenceEntry = ie.createHistoryEntry({
      image: {} as HTMLImageElement,
      url: 'ref-u',
      sourceSize: { width: 2, height: 2 },
      format: formats[0],
      kind: 'source',
    });
    const withReference = ie.appendResult(session, referenceEntry);
    const selected = ie.selectHistoryBase(withReference, referenceEntry.id);
    expect(selected.sourceArtifactId).toBeUndefined();
  });

  it('selects portrait 9:21 instead of square collapse', () => {
    const formats = [
      { id: '1:1', width: 1024, height: 1024 },
      { id: '9:21', width: 672, height: 1568 },
      { id: '21:9', width: 1568, height: 672 },
    ];
    expect(ie.pickInitialFormat({ width: 672, height: 1568 }, formats).id).toBe('9:21');
    expect(ie.pickInitialFormat({ width: 1568, height: 672 }, formats).id).toBe('21:9');
  });

  it('keeps normal-eraser holes separate from uncovered regions in effective masks', () => {
    const formats = [{ id: '1:1', width: 100, height: 100 }];
    const session = ie.createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 3 }, formats });
    const painted = ie.paintMaskSource(session, 'hole', { x: session.transform.x + 1, y: session.transform.y + 1 }, 2);
    const out = ie.effectiveOutputMasks(painted);
    const expectedSize = painted.canvasWidth * painted.canvasHeight;
    expect(out.fillMask.length).toBe(expectedSize);
    expect(out.holeMask.length).toBe(expectedSize);
    expect(out.editMask.length).toBe(expectedSize);
    const fillCount = Array.from(out.fillMask).filter((v) => Number(v) >= 128).length;
    const holeCount = Array.from(out.holeMask).filter((v) => Number(v) >= 128).length;
    expect(holeCount).toBeGreaterThan(0);
    expect(fillCount).toBeGreaterThan(holeCount);
  });

  it('locks transform when edit mask is active', () => {
    const formats = [{ id: '1:1', width: 100, height: 100 }];
    const session = ie.createEditorSession({ image: {} as HTMLImageElement, url: 'u', sourceSize: { width: 4, height: 4 }, formats });
    const edited = ie.paintMaskSource(session, 'edit', { x: session.transform.x + 1, y: session.transform.y + 1 }, 2);
    expect(ie.canTransform(edited).allowed).toBe(false);
    expect(ie.canUseTool(edited, 'magicEraser').allowed).toBe(true);
    expect(ie.canUseTool(edited, 'eraser').allowed).toBe(false);
  });
});

describe('Images modifier correction navigation', () => {
  it('opens the editor for a gallery edit query', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
  });

  it('imports a reference and opens the editor without removing the reference url', async () => {
    window.history.replaceState({}, '', '/');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);
    const input = document.querySelector('.img-file-input') as HTMLInputElement;
    expect(input).toBeTruthy();
    const file = new File([new Uint8Array([1, 2, 3])], 'ref.png', { type: 'image/png' });
    fireEvent.change(input, { target: { files: [file] } });
    const edit = await screen.findByRole('button', { name: /Éditer l’image/ });
    const img = await screen.findByAltText('ref.png');
    const before = img.getAttribute('src');
    fireEvent.click(edit);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Retour à la page Image' }));
    const restored = await screen.findByAltText('ref.png');
    expect(restored.getAttribute('src')).toBe(before);
  });
});
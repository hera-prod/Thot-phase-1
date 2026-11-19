import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import * as ie from '@/pages/images/imageEditor';
import * as geom from '@/pages/images/imageGeometry';
import * as enc from '@/pages/images/imageEncoding';

type Listener = (event: { payload: Record<string, unknown> }) => void;
const listeners: Record<string, Listener> = {};
const invoked: Array<{ cmd: string; args: unknown }> = [];

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
    invoked.push({ cmd, args });
    if (cmd === 'image_list') return Promise.resolve({ items: [], next_offset: 0, has_more: false });
    if (cmd === 'image_status') return Promise.resolve(undefined);
    if (cmd === 'image_generate') return Promise.resolve(undefined);
    if (cmd === 'image_stage_work') return Promise.resolve({ token: 'a'.repeat(32) });
    if (cmd === 'get_attachment_bytes') return Promise.resolve([[137, 80, 78, 71], 'image/png']);
    return Promise.resolve(undefined);
  }),
  listen: vi.fn((event: string, callback: Listener) => {
    listeners[event] = callback;
    return Promise.resolve(() => {
      delete listeners[event];
      return undefined;
    });
  }),
}));

const EDITOR_FORMATS = [
  { id: '1:1', width: 1024, height: 1024 },
  { id: '16:9', width: 1280, height: 720 },
  { id: '9:16', width: 720, height: 1280 },
  { id: '3:2', width: 1248, height: 832 },
  { id: '2:3', width: 832, height: 1248 },
  { id: '4:3', width: 1152, height: 864 },
  { id: '3:4', width: 864, height: 1152 },
  { id: '5:4', width: 1120, height: 896 },
  { id: '4:5', width: 896, height: 1120 },
  { id: '21:9', width: 1568, height: 672 },
  { id: '9:21', width: 672, height: 1568 },
];

function fire(event: string, payload: Record<string, unknown>) {
  const callback = listeners[event];
  if (!callback) throw new Error(`Aucun listener pour ${event}`);
  act(() => callback({ payload }));
}

beforeEach(() => {
  invoked.length = 0;
  Object.keys(listeners).forEach((key) => delete listeners[key]);
  vi.stubGlobal('Image', FakeImage as any);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:image-${Math.random()}`) });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });

  const fakeCtx: any = {
    clearRect: vi.fn(),
    drawImage: vi.fn(),
    getImageData: vi.fn((_x: number, _y: number, w: number, h: number) => ({ data: new Uint8ClampedArray(Math.max(1, w * h * 4)), width: w, height: h })),
    putImageData: vi.fn(),
    createImageData: vi.fn((w: number, h: number) => ({ data: new Uint8ClampedArray(Math.max(0, w * h * 4)), width: w, height: h })),
    globalCompositeOperation: 'source-over',
    setTransform: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    stroke: vi.fn(),
    fillRect: vi.fn(),
    strokeRect: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
  };
  HTMLCanvasElement.prototype.getContext = vi.fn(() => fakeCtx) as any;
  HTMLCanvasElement.prototype.toBlob = vi.fn(function (_cb: (b: Blob | null) => void, _type?: string) {
    // replaced below
  }) as any;
  HTMLCanvasElement.prototype.toBlob = vi.fn(function (this: any, cb: (b: Blob | null) => void, type?: string) {
    cb(new Blob([new Uint8Array([137, 80, 78, 71])], { type: type || 'image/png' }));
  }) as any;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Images — format et historique éditeur', () => {
  it('reproduit les symptômes format / historique', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    // 1. Non-régression : la bande « Versions » est absente, le panneau Historique reste
    expect(screen.queryByText('Versions')).toBeNull();
    expect(screen.getByRole('button', { name: 'Afficher Source' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Afficher Résultat 1' })).toBeNull();
    expect(screen.getByRole('complementary', { name: 'Historique de la session' })).toBeTruthy();

    const formatButton = screen.getByRole('button', { name: '16:9 1280x720' });
    expect(formatButton).toHaveProperty('disabled', false);

    fireEvent.click(formatButton);

    // 3. Assertions immédiates
    const badge = document.querySelector('.img-canvas-format') as HTMLElement;
    expect(badge.textContent).toContain('16:9');
    expect(badge.textContent).toContain('1280');
    expect(badge.textContent).toContain('720');

    const canvas = screen.getByLabelText('Canevas de modification') as HTMLCanvasElement;
    expect(canvas.getAttribute('width')).toBe('1280');
    expect(canvas.getAttribute('height')).toBe('720');
    expect(canvas.width).toBe(1280);
    expect(canvas.height).toBe(720);

    const textarea = document.querySelector('#editor-instruction') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'extend the scene' } });
    const generateBtn = screen.getByRole('button', { name: 'Générer' });
    fireEvent.click(generateBtn);

    await waitFor(() => expect(invoked.some((entry) => entry.cmd === 'image_generate')).toBe(true));

    const generations = invoked.filter((entry) => entry.cmd === 'image_generate');
    const gen = generations[generations.length - 1];
    const args = gen.args as Record<string, any>;

    const seedInput = document.querySelector('#editor-seed') as HTMLInputElement;
    expect(seedInput.value).toBe('');
    expect(Number(seedInput.placeholder)).toBeGreaterThanOrEqual(0);
    expect(args.seed).toBe(Number(seedInput.placeholder));

    // 5. Assertions sur l'appel
    expect(args.aspectRatio).toBe('16:9');
    expect(args.canvasWidth).toBe(1280);
    expect(args.canvasHeight).toBe(720);

    const expectedTransform = geom.fitRectToCanvas({ width: 1280, height: 720 }, { width: 100, height: 200 });
    expect(args.sourceTransform).toBeDefined();
    expect(args.sourceTransform.x).toBeCloseTo(expectedTransform.x, 3);
    expect(args.sourceTransform.y).toBeCloseTo(expectedTransform.y, 3);
    expect(args.sourceTransform.width).toBeCloseTo(expectedTransform.width, 3);
    expect(args.sourceTransform.height).toBeCloseTo(expectedTransform.height, 3);

    expect(args.workToken).toBe('a'.repeat(32));
    expect(args.maskToken).toBeTruthy();
    expect(args.mode).toBeTruthy();
    expect(args.maskKind).toBeTruthy();

    const requestId = String(args.requestId);
    const sessionId = String(args.sessionId);
    const revision = String(args.revision);
    expect(sessionId).toBeTruthy();
    expect(revision).toBeTruthy();

    fire('artifact_created', { request_id: requestId, run_id: requestId, artifact_id: 'art_result' });
    fire('run_completed', { request_id: requestId, run_id: requestId, status: 'completed', artifact_id: 'art_result' });

    // 7. Le format reste inchangé après génération
    const badge2 = document.querySelector('.img-canvas-format') as HTMLElement;
    expect(badge2.textContent).toContain('16:9');
    expect(badge2.textContent).toContain('1280');
    expect(badge2.textContent).toContain('720');
    const canvas2 = screen.getByLabelText('Canevas de modification') as HTMLCanvasElement;
    expect(canvas2.width).toBe(1280);
    expect(canvas2.height).toBe(720);

    // 8. La bande « Versions » reste absente après génération
    expect(screen.queryByText('Versions')).toBeNull();
    const sourceButton = await screen.findByRole('button', { name: 'Afficher Source' });
    const resultButton = await screen.findByRole('button', { name: 'Afficher Résultat 1' });
    expect(sourceButton).toBeTruthy();
    expect(resultButton).toBeTruthy();
    expect(resultButton.getAttribute('aria-current')).toBe('true');

    fireEvent.click(sourceButton);
    await waitFor(() => expect(sourceButton.getAttribute('aria-current')).toBe('true'));

    fireEvent.click(resultButton);
    await waitFor(() => expect(resultButton.getAttribute('aria-current')).toBe('true'));

    expect(screen.getByLabelText('Canevas de modification')).toBeTruthy();
  }, 15000);

  it('expose les groupes Image/Vue et les métriques', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    expect(screen.getByLabelText('Image')).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Vue' })).toBeTruthy();
    expect(screen.getByTestId('img-zoom-value')).toBeTruthy();
    expect(screen.getAllByText('Vue')).toHaveLength(1);
    expect(document.querySelector('.img-canvas-footer input[type="range"]')).toBeNull();
    expect(screen.getByText(/^Image .* px \(\d+ % de la toile\)$/)).toBeTruthy();
    expect(screen.queryByText(/bords aimantés/i)).toBeNull();
    expect(screen.getByRole('button', { name: 'Déplacer / redimensionner' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Ajuster l’image à la toile' })).toBeTruthy();

    // cliquer Zoomer la vue puis Dézoomer la vue met à jour le pourcentage
    fireEvent.click(screen.getByRole('button', { name: 'Zoomer la vue' }));
    await waitFor(() => expect(screen.getByTestId('img-zoom-value').textContent).not.toBe('100 %'));
    const afterZoom = screen.getByTestId('img-zoom-value').textContent;
    fireEvent.click(screen.getByRole('button', { name: 'Dézoomer la vue' }));
    await waitFor(() => expect(screen.getByTestId('img-zoom-value').textContent).not.toBe(afterZoom));
    // Réinitialiser remet 100 %
    fireEvent.click(screen.getByRole('button', { name: 'Réinitialiser la vue' }));
    await waitFor(() => expect(screen.getByTestId('img-zoom-value').textContent).toBe('100 %'));

    const canvas = screen.getByLabelText('Canevas de modification') as HTMLCanvasElement;
    const headerBefore = screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent;
    const wheelEvent = new WheelEvent('wheel', { deltaY: -100, bubbles: true, cancelable: true, clientX: 10, clientY: 10 });
    canvas.dispatchEvent(wheelEvent);
    expect(wheelEvent.defaultPrevented).toBe(true);
    await waitFor(() => expect(screen.getByTestId('img-zoom-value').textContent).toContain('110 %'));
    expect(screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent).toBe(headerBefore);
  });

  it('le contrôle unique sépare la vue (molette) de la taille de l’image', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    const canvas = screen.getByLabelText('Canevas de modification') as HTMLCanvasElement;
    const headerBefore = screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent;
    expect(screen.getByTestId('img-zoom-value').textContent).toContain('100 %');

    fireEvent.wheel(canvas, { deltaY: -100 });

    await waitFor(() => expect(screen.getByTestId('img-zoom-value').textContent).toContain('110 %'));
    expect(screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent).toBe(headerBefore);
  });

  it('la molette change la vue sans changer l’image', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    const canvas = screen.getByLabelText('Canevas de modification') as HTMLCanvasElement;
    const before = screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent!;

    fireEvent.wheel(canvas, { deltaY: -100 });
    await waitFor(() => expect(screen.getByTestId('img-zoom-value').textContent).toContain('110 %'));
    expect(screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent).toBe(before);

    fireEvent.wheel(canvas, { deltaY: 100 });
    await waitFor(() => {
      const txt = screen.getByTestId('img-zoom-value').textContent!;
      const match = txt.match(/(\d+) %/);
      expect(Number(match![1])).toBeLessThan(110);
    });
    expect(screen.getByText(/^Image .* px \(\d+ % de la toile\)$/).textContent).toBe(before);
  });

  it('« Ajuster l’image à la toile » est cliquable sans erreur', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    const btn = screen.getByRole('button', { name: 'Ajuster l’image à la toile' });
    expect(() => fireEvent.click(btn)).not.toThrow();
  });

  it('ajoute au plus 3 références et les envoie au moteur', async () => {
    vi.spyOn(enc, 'sha256Bytes').mockResolvedValue('a'.repeat(64));
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    const input = screen.getByLabelText('Choisir des images de référence') as HTMLInputElement;
    const files = Array.from({ length: 4 }, (_, index) =>
      new File([new Uint8Array([1, 2, 3])], `ref${index + 1}.png`, { type: 'image/png' }),
    );
    fireEvent.change(input, { target: { files } });

    await waitFor(() => expect(screen.getByText('3/3')).toBeTruthy());
    expect(screen.getByText(/3 références supplémentaires maximum/i)).toBeTruthy();
    expect(document.querySelectorAll('.img-editor-refs .img-reference-card')).toHaveLength(3);

    const remove = screen.getByRole('button', { name: 'Retirer ref3.png' });
    fireEvent.click(remove);
    await waitFor(() => expect(screen.getByText('2/3')).toBeTruthy());

    fireEvent.change(document.querySelector('#editor-instruction') as HTMLTextAreaElement, { target: { value: 'ajoute une ambiance' } });
    fireEvent.click(screen.getByRole('button', { name: 'Générer' }));

    await waitFor(() => expect(invoked.some((entry) => entry.cmd === 'image_generate')).toBe(true));
    const gen = invoked.filter((entry) => entry.cmd === 'image_generate').pop();
    const args = gen?.args as Record<string, unknown>;
    expect((args.referenceTokens as string[]).length).toBe(2);
    expect((args.referenceSha256 as string[]).length).toBe(2);
    vi.restoreAllMocks();
  });

  it('ajoute les références supplémentaires par glisser-déposer', async () => {
    window.history.replaceState({}, '', '/?edit=art_gallery');
    const { ImagesPage } = await import('@/pages/images/ImagesPage');
    render(<ImagesPage />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retour à la page Image' })).toBeTruthy());

    expect(screen.getByText('3 références maximum, en plus de l\'image en cours (4 images au total).')).toBeTruthy();

    const dropZone = document.querySelector('.img-editor-refs') as HTMLElement;
    expect(dropZone).toBeTruthy();
    const files = Array.from({ length: 4 }, (_, index) =>
      new File([new Uint8Array([1, 2, 3])], `drop${index + 1}.png`, { type: 'image/png' }),
    );
    fireEvent.drop(dropZone, { dataTransfer: { files } });

    await waitFor(() => expect(screen.getByText('3/3')).toBeTruthy());
    expect(document.querySelectorAll('.img-editor-refs .img-reference-card')).toHaveLength(3);
    expect(screen.getByRole('alert').textContent).toContain('3 références supplémentaires maximum');
  });

});
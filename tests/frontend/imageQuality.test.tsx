import { describe, it, expect, vi, beforeEach } from 'vitest';
import { invoke } from '@/lib/ipc';
import { imageList, isDeprecatedQuality, type ImageItem } from '@/lib/ipcBridge';

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn(),
}));

const mockedInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function image(overrides: Partial<ImageItem> = {}): ImageItem {
  return {
    artifact_id: 'a1',
    display_name: 'Image',
    original_filename: 'image.png',
    created_at: '2026-09-24T10:00:00Z',
    kind: 'image',
    size_bytes: 1024,
    mime_detected: 'image/png',
    scopes: [],
    is_import: false,
    experimental: false,
    deprecated: false,
    ...overrides,
  };
}

beforeEach(() => {
  mockedInvoke.mockReset();
});

describe('Images — qualité et contrat de page', () => {
  it('UNVALIDATED (bruit 22/09) = deprecated', () => {
    expect(isDeprecatedQuality('UNVALIDATED (bruit au 22/09)')).toBe(true);
  });

  it('production qualifiée = non deprecated', () => {
    expect(isDeprecatedQuality('experimental-T2I512 (qualifié 23/09 spike_fix5)')).toBe(false);
  });

  it('sans qualité = non deprecated', () => {
    expect(isDeprecatedQuality(null)).toBe(false);
    expect(isDeprecatedQuality(undefined)).toBe(false);
  });

  it('imageList retourne ImageListPage et transmet offset, limite et provenance', async () => {
    mockedInvoke.mockResolvedValueOnce({
      items: [
        image({
          artifact_id: 'a1',
          display_name: 'Chat',
          quality: 'UNVALIDATED (bruit au 22/09)',
          deprecated: true,
          experimental: true,
          prompt: 'un chat',
          aspect_ratio: '1:1',
          width: 512,
          height: 512,
          seed: 42,
        }),
        image({ artifact_id: 'a2', display_name: 'Jardin', quality: 'qualified', seed: 0 }),
      ],
      next_offset: 2,
      has_more: true,
    });

    const page = await imageList({ offset: 0, limit: 24, include_imports: true, scope_id: 'scope-1' });

    expect(page.next_offset).toBe(2);
    expect(page.has_more).toBe(true);
    expect(page.items[0].deprecated).toBe(true);
    expect(isDeprecatedQuality(page.items[0].quality)).toBe(true);
    expect(page.items[0].prompt).toBe('un chat');
    expect(page.items[0].seed).toBe(42);
    expect(page.items[1].seed).toBe(0);
    expect(mockedInvoke).toHaveBeenCalledWith('image_list', {
      offset: 0,
      limit: 24,
      includeImports: true,
      scopeId: 'scope-1',
    });
  });

  it('imageList utilise 24 et offset 0 par défaut', async () => {
    mockedInvoke.mockResolvedValueOnce({ items: [], next_offset: 0, has_more: false });
    await imageList();
    expect(mockedInvoke).toHaveBeenCalledWith('image_list', {
      offset: 0,
      limit: 24,
      includeImports: false,
      scopeId: undefined,
    });
  });

  it('imageList convertit une erreur du pont en IMAGE_LIST_FAILED', async () => {
    mockedInvoke.mockRejectedValueOnce(new Error('base indisponible'));
    await expect(imageList()).rejects.toMatchObject({ code: 'IMAGE_LIST_FAILED', message: 'base indisponible' });
  });

  it('accepte une réponse historique tableau sans perdre la page', async () => {
    mockedInvoke.mockResolvedValueOnce([image({ artifact_id: 'legacy' })]);
    const page = await imageList();
    expect(page.items[0].artifact_id).toBe('legacy');
    expect(page.next_offset).toBe(1);
    expect(page.has_more).toBe(false);
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { toIpcError, cancelChat } from '@/lib/ipcBridge';
import { dedupeKey, stageMetaOf } from '@/features/attachments/attachmentTypes';
import { validateAttachments, WARN_IMAGE_BYTES } from '@/features/attachments/attachmentValidation';

vi.mock('@/lib/ipc', () => ({
  invoke: vi.fn(),
}));

describe('Phase 2 — erreurs visibles', () => {
  it('toIpcError structure code/message/retryable', () => {
    const e = toIpcError(new Error('timeout réseau'), 'ATTACH_PREPARE_FAILED');
    expect(e.code).toBe('ATTACH_PREPARE_FAILED');
    expect(e.retryable).toBe(true);
    expect(e.message).toContain('timeout');
  });

  it('toIpcError non-retryable par défaut', () => {
    const e = toIpcError(new Error('fichier illisible'), 'ATTACH_BYTES_FAILED');
    expect(e.code).toBe('ATTACH_BYTES_FAILED');
    expect(e.retryable).toBe(false);
  });

  it('message outil échoué contient id + latence', () => {
    const p = { tool_call_id: 'tc_1', result: 'boom', ok: false, latency_ms: 42 };
    const msg = `${p.tool_call_id} — ${p.result?.slice(0, 120)} (${p.latency_ms} ms)`;
    expect(msg).toContain('tc_1');
    expect(msg).toContain('42 ms');
  });

  it('cancelChat mappe cancel_run vers CANCEL_FAILED typée', async () => {
    const { invoke } = await import('@/lib/ipc');
    vi.mocked(invoke).mockRejectedValueOnce(new Error('sidecar indisponible'));
    await expect(cancelChat('req-1')).rejects.toMatchObject({ code: 'CANCEL_FAILED' });
  });

  it('cancelChat résout quand le sidecar accuse réception', async () => {
    const { invoke } = await import('@/lib/ipc');
    vi.mocked(invoke).mockResolvedValueOnce(undefined);
    await expect(cancelChat('req-2')).resolves.toBeUndefined();
    expect(invoke).toHaveBeenCalledWith('cancel_run', expect.objectContaining({ requestId: 'req-2' }));
  });
});

describe('Phase 3 — staging typé et dedup', () => {
  beforeEach(() => vi.clearAllMocks());

  it('stageMetaOf expose _stagedPath/_stageItemId/_previewToken', () => {
    const f = new File(['x'], 'a.png', { type: 'image/png' }) as File & { _stagedPath?: string; _stageItemId?: string; _previewToken?: string };
    f._stagedPath = '/tmp/staged/abc.png';
    f._stageItemId = 'sti_abc123';
    f._previewToken = '/tmp/staged/abc.png';
    const meta = stageMetaOf(f);
    expect(meta._stageItemId).toBe('sti_abc123');
    expect(meta._previewToken).toBe('/tmp/staged/abc.png');
  });

  it('dedupeKey distingue même nom si taille ou stageItemId diffère', () => {
    expect(dedupeKey('a.png', 10, 'sti_1')).toBe(dedupeKey('a.png', 10, 'sti_1'));
    expect(dedupeKey('a.png', 10, 'sti_1')).not.toBe(dedupeKey('a.png', 10, 'sti_2'));
    expect(dedupeKey('a.png', 10)).not.toBe(dedupeKey('a.png', 11));
    expect(dedupeKey('a.png', 0)).not.toBe(dedupeKey('b.png', 0));
  });

  it('WARN 5 Mo accepte avec warning au lieu de refuser (Phase 3.3a)', () => {
    const big = new File([new Uint8Array(WARN_IMAGE_BYTES + 1024)], 'big.png', { type: 'image/png' });
    const r = validateAttachments([big]);
    expect(r.errors).toHaveLength(0);
    expect(r.files).toHaveLength(1);
    expect(r.warnings.length).toBeGreaterThan(0);
    expect(r.warnings[0]).toContain('1536px');
  });
});

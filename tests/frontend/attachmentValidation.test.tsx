import { describe, it, expect } from 'vitest';
import { validateAttachments, MAX_ATTACHMENTS, MAX_IMAGE_BYTES, MAX_PDF_BYTES, WARN_IMAGE_BYTES } from '@/features/attachments/attachmentValidation';

describe('attachmentValidation', () => {
  it('MAX_ATTACHMENTS should be 4', () => {
    expect(MAX_ATTACHMENTS).toBe(4);
  });

  it('MAX_IMAGE_BYTES should be 20MB', () => {
    expect(MAX_IMAGE_BYTES).toBe(20 * 1024 * 1024);
  });

  it('MAX_PDF_BYTES should be 20MB', () => {
    expect(MAX_PDF_BYTES).toBe(20 * 1024 * 1024);
  });

  it('WARN_IMAGE_BYTES should be 5MB', () => {
    expect(WARN_IMAGE_BYTES).toBe(5 * 1024 * 1024);
  });

  it('accepts valid PNG files', () => {
    const file = new File(['test'], 'test.png', { type: 'image/png' });
    const result = validateAttachments([file]);
    expect(result.errors).toHaveLength(0);
    expect(result.files).toHaveLength(1);
  });

  it('accepts valid JPEG files', () => {
    const file = new File(['test'], 'test.jpg', { type: 'image/jpeg' });
    const result = validateAttachments([file]);
    expect(result.errors).toHaveLength(0);
  });

  it('accepts valid PDF files', () => {
    const file = new File(['test'], 'test.pdf', { type: 'application/pdf' });
    const result = validateAttachments([file]);
    expect(result.errors).toHaveLength(0);
  });

  it('rejects non-image non-pdf files', () => {
    const file = new File(['test'], 'test.txt', { type: 'text/plain' });
    const result = validateAttachments([file]);
    expect(result.errors.length).toBeGreaterThan(0);
    expect(result.files).toHaveLength(0);
  });

  it('rejects files larger than 20MB', () => {
    const file = new File([new Uint8Array(21 * 1024 * 1024)], 'test.png', { type: 'image/png' });
    const result = validateAttachments([file]);
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it('accepts multiple valid files up to MAX_ATTACHMENTS', () => {
    const files = [
      new File(['test'], 'test1.png', { type: 'image/png' }),
      new File(['test'], 'test2.png', { type: 'image/png' }),
      new File(['test'], 'test3.png', { type: 'image/png' }),
      new File(['test'], 'test4.png', { type: 'image/png' }),
    ];
    const result = validateAttachments(files);
    expect(result.files).toHaveLength(4);
  });

  it('warns on images larger than 5MB', () => {
    const file = new File([new Uint8Array(6 * 1024 * 1024)], 'test.png', { type: 'image/png' });
    const result = validateAttachments([file]);
    expect(result.warnings.length).toBeGreaterThan(0);
    expect(result.files).toHaveLength(1);
  });
});

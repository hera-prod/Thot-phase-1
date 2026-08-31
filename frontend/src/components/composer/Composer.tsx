// Composer Phase 1 — adapté hash+path (save_upload), plus de base64
// Fichiers confiés au parent via File[], preview locale via objectURL, upload via Tauri save_upload
import { useState, useRef, useEffect } from 'react';
import {
  Add24Regular, Send24Regular, Mic24Regular,
  Square24Regular, Attach24Regular, Globe24Regular,
  Beaker24Regular, Image24Regular, ChartMultiple24Regular,
} from '@fluentui/react-icons';

interface DroppedFile {
  file: File;
  previewUrl: string;
}

interface ComposerProps {
  variant: 'hero' | 'docked';
  isGenerating?: boolean;
  onSend?: (message: string, files?: File[]) => void;
  onStop?: () => void;
}

export function Composer({ variant, isGenerating, onSend, onStop }: ComposerProps) {
  const [message, setMessage] = useState('');
  const [showTools, setShowTools] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState<DroppedFile[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const dragCounterRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Tauri natif drag-drop (window) + fallback web — couvre le cas où l'OS drop n'arrive pas en dataTransfer
  useEffect(() => {
    let unlisten: (() => void) | null = null;
    // @tauri-apps/api est optionnel en dev web (Vite) — on try/catch
    import('@tauri-apps/api/event').then(({ listen }) => {
      // Tauri v2 émet `tauri://drag-drop` avec {paths: string[], position}
      listen('tauri://drag-drop', (event: unknown) => {
        const payload = (event as { payload?: { paths?: string[] } }).payload;
        const paths = (payload as { paths?: string[] })?.paths || [];
        console.debug('[Composer] tauri://drag-drop', paths);
      }).then((fn) => { unlisten = fn; }).catch(() => {});
    }).catch(() => {});

    // Fallback document-level pour capter les drops hors barre (évite que le navigateur ouvre le fichier)
    const onWindowDragOver = (e: DragEvent) => {
      if (e.dataTransfer?.types.includes('Files')) {
        e.preventDefault();
        setIsDragging(true);
      }
    };
    const onWindowDrop = (e: DragEvent) => {
      // Laisse Composer gérer en premier via onDrop local ; ce handler global évite le navigate
      if (e.dataTransfer?.types.includes('Files')) {
        e.preventDefault();
      }
    };
    window.addEventListener('dragover', onWindowDragOver);
    window.addEventListener('drop', onWindowDrop);
    return () => {
      if (unlisten) unlisten();
      window.removeEventListener('dragover', onWindowDragOver);
      window.removeEventListener('drop', onWindowDrop);
    };
  }, []);

  const handleSend = () => {
    if ((!message.trim() && droppedFiles.length === 0) || isGenerating) return;
    const files = droppedFiles.map((d) => d.file);
    onSend?.(message, files);
    setMessage('');
    setDroppedFiles((prev) => { prev.forEach((p) => URL.revokeObjectURL(p.previewUrl)); return []; });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current += 1;
    if (e.dataTransfer.types.includes('Files')) setIsDragging(true);
  };
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes('Files')) {
      e.dataTransfer.dropEffect = 'copy';
      setIsDragging(true);
    }
  };
  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current <= 0) {
      dragCounterRef.current = 0;
      setIsDragging(false);
    }
  };

  const processFiles = (files: FileList | File[]) => {
    const arr = Array.from(files).slice(0, 4 - droppedFiles.length);
    arr.forEach((file) => {
      // Accepte PNG/JPG/PDF uniquement, max 20MB PDF / 5MB image (validé aussi côté Rust)
      const isImage = file.type.startsWith('image/');
      const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
      if (!isImage && !isPdf) return;
      if (file.size > 20 * 1024 * 1024) return;
      if (isImage && file.size > 5 * 1024 * 1024) return;
      const previewUrl = isImage ? URL.createObjectURL(file) : '';
      setDroppedFiles((prev) => {
        if (prev.length >= 4) return prev;
        return [...prev, { file, previewUrl }];
      });
    });
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      processFiles(e.dataTransfer.files);
    } else if (e.dataTransfer.items) {
      // Fallback pour certains navigateurs / Tauri
      const files: File[] = [];
      for (let i = 0; i < e.dataTransfer.items.length; i++) {
        const item = e.dataTransfer.items[i];
        if (item.kind === 'file') {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) processFiles(files as unknown as FileList);
    }
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const pastedFiles: File[] = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind === 'file') {
        const file = items[i].getAsFile();
        if (file) pastedFiles.push(file);
      }
    }
    if (pastedFiles.length > 0) {
      e.preventDefault();
      processFiles(pastedFiles);
    }
  };

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) processFiles(e.target.files);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const removeFile = (idx: number) => {
    setDroppedFiles((prev) => {
      const copy = [...prev];
      if (copy[idx].previewUrl) URL.revokeObjectURL(copy[idx].previewUrl);
      copy.splice(idx, 1);
      return copy;
    });
  };

  const containerStyle: React.CSSProperties = variant === 'hero' ? {
    maxWidth: 960,
    width: '100%',
    margin: '28px auto 0',
  } : {
    position: 'sticky',
    bottom: 0,
    padding: '14px 24px 18px',
    background: 'linear-gradient(to top, var(--thot-canvas) 60%, transparent)',
    borderTop: '1px solid var(--thot-border-subtle)',
    zIndex: 5,
  };

  const innerDockedStyle: React.CSSProperties = variant === 'docked' ? {
    maxWidth: 960,
    width: '100%',
    margin: '0 auto',
  } : {};

  const wrapperStyle: React.CSSProperties = {
    background: isDragging ? 'var(--thot-gold-soft)' : 'var(--thot-surface-raised)',
    borderRadius: 'var(--thot-radius-xl)',
    boxShadow: 'var(--thot-shadow-float)',
    padding: variant === 'docked' ? '12px 14px' : '12px 14px',
    border: `1px ${isDragging ? 'dashed' : 'solid'} ${isDragging ? 'var(--thot-gold)' : 'var(--thot-border)'}`,
    minHeight: variant === 'docked' ? 56 : undefined,
    transition: 'var(--thot-transition-fast)',
  };

  return (
    <div
      ref={containerRef}
      style={{
        ...containerStyle,
        ...(isDragging ? {
          background: variant === 'docked'
            ? 'linear-gradient(to top, var(--thot-gold-soft) 60%, transparent)'
            : undefined,
          borderTop: variant === 'docked' ? '1px dashed var(--thot-gold)' : undefined,
        } : {}),
      }}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <div style={variant === 'docked' ? innerDockedStyle : undefined}>
      {/* Tools panel — Phase 1 minimal */}
      {showTools && (
        <div style={{
          marginBottom: 8, padding: 12, borderRadius: 'var(--thot-radius-md)',
          background: 'var(--thot-surface-raised)', border: '1px solid var(--thot-border)',
          boxShadow: 'var(--thot-shadow-card)',
        }}>
          {[
            { icon: <Attach24Regular />, label: 'Joindre un fichier (PNG/JPG/PDF)', onClick: () => fileInputRef.current?.click() },
            { icon: <Globe24Regular />, label: 'Recherche web — Phase 2', disabled: true },
            { icon: <Beaker24Regular />, label: 'Deep Research — Phase 2', disabled: true },
            { icon: <Image24Regular />, label: 'Vision (LFM2.5-VL-3B) auto en mode Agent' },
            { icon: <ChartMultiple24Regular />, label: 'OCR (GLM-OCR) auto en mode Agent' },
          ].map((tool) => (
            <button
              key={tool.label}
              onClick={tool.onClick}
              disabled={(tool as { disabled?: boolean }).disabled}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                padding: '8px 12px', borderRadius: 8, border: 'none',
                background: 'transparent', cursor: (tool as { disabled?: boolean }).disabled ? 'not-allowed' : 'pointer', fontSize: 14,
                color: (tool as { disabled?: boolean }).disabled ? 'var(--thot-text-faint)' : 'var(--thot-text)', fontFamily: 'var(--thot-font-family)',
                opacity: (tool as { disabled?: boolean }).disabled ? 0.6 : 1,
              }}
              onMouseEnter={(e) => { if (!(tool as { disabled?: boolean }).disabled) e.currentTarget.style.background = 'var(--thot-surface-2)'; }}
              onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
            >
              <span style={{ color: 'var(--thot-text-muted)' }}>{tool.icon}</span>
              {tool.label}
            </button>
          ))}
        </div>
      )}

      {/* Hidden file input — hash+path : on envoie le binaire au Rust, pas de base64 */}
      <input ref={fileInputRef} type="file" accept="image/png,image/jpeg,image/webp,application/pdf" multiple style={{ display: 'none' }} onChange={handleFileInput} />

      {/* Drop overlay — visible pendant le drag */}
      {isDragging && (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
          padding: '12px', marginBottom: 8, borderRadius: 'var(--thot-radius-md)',
          background: 'var(--thot-gold-soft)', border: '1px dashed var(--thot-gold)',
          color: 'var(--thot-gold)', fontSize: 13, fontWeight: 600,
        }}>
          <Attach24Regular /> Déposez l'image ou le PDF ici (trombone ou glisser-déposer)
        </div>
      )}

      {/* Previews — objectURL, pas de data: base64 */}
      {droppedFiles.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
          {droppedFiles.map((item, idx) => {
            const isPdf = item.file.type === 'application/pdf' || item.file.name.toLowerCase().endsWith('.pdf');
            return (
              <div key={idx} style={{ position: 'relative', width: 72, height: 72, borderRadius: 8, overflow: 'hidden', border: '1px solid var(--thot-border)', background: 'var(--thot-surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                {isPdf ? (
                  <span style={{ fontSize: 28 }}>📄</span>
                ) : (
                  <img src={item.previewUrl} alt={item.file.name} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                )}
                <button onClick={() => removeFile(idx)} aria-label="Retirer" style={{ position: 'absolute', top: 2, right: 2, width: 20, height: 20, borderRadius: '50%', border: 'none', background: 'rgba(0,0,0,0.6)', color: '#fff', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12 }}>×</button>
                <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, background: 'rgba(0,0,0,0.55)', color: '#fff', fontSize: 9, padding: '2px 4px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{item.file.name}</div>
              </div>
            );
          })}
        </div>
      )}

      {/* Main input row */}
      <div style={wrapperStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button
            onClick={() => setShowTools(!showTools)}
            aria-label="Outils"
            style={{
              width: 40, height: 40, borderRadius: 12, border: '1px solid var(--thot-border)',
              background: showTools ? 'var(--thot-surface-2)' : 'transparent', cursor: 'pointer', display: 'flex',
              alignItems: 'center', justifyContent: 'center', color: 'var(--thot-text-muted)',
              flexShrink: 0,
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-surface-2)'}
            onMouseLeave={(e) => e.currentTarget.style.background = showTools ? 'var(--thot-surface-2)' : 'transparent'}
          >
            <Add24Regular />
          </button>

          <div style={{ flex: 1, position: 'relative' }}>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder={droppedFiles.length ? 'Pose une question sur le fichier…' : 'Demandez n\'importe quoi…'}i…'}
              rows={1}
              style={{
                width: '100%', border: 'none', outline: 'none', resize: 'none',
                fontSize: 16, color: 'var(--thot-text)',
                fontFamily: 'var(--thot-font-family)',
                background: 'transparent', padding: '8px 0',
                lineHeight: 1.5,
              }}
            />
          </div>

          <button
            aria-label="Joindre"
            onClick={() => fileInputRef.current?.click()}
            style={{
              width: 40, height: 40, borderRadius: 'var(--thot-radius-pill)',
              border: 'none', background: 'transparent', cursor: 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--thot-text-muted)', flexShrink: 0,
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-surface-2)'}
            onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
          >
            <Attach24Regular />
          </button>

          <button
            aria-label="Microphone — Phase 2"
            disabled
            style={{
              width: 40, height: 40, borderRadius: 'var(--thot-radius-pill)',
              border: 'none', background: 'transparent', cursor: 'not-allowed',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--thot-text-faint)', flexShrink: 0, opacity: 0.5,
            }}
          >
            <Mic24Regular />
          </button>

          {isGenerating ? (
            <button
              onClick={onStop}
              aria-label="Arrêter"
              style={{
                width: 40, height: 40, borderRadius: 'var(--thot-radius-pill)',
                border: 'none', background: 'var(--thot-gold)', cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#fff', flexShrink: 0,
              }}
            >
              <Square24Regular />
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!message.trim() && droppedFiles.length === 0}
              aria-label="Envoyer"
              style={{
                width: 40, height: 40, borderRadius: 'var(--thot-radius-pill)',
                border: 'none', background: 'var(--thot-gold)', cursor: (!message.trim() && droppedFiles.length === 0) ? 'not-allowed' : 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#fff', flexShrink: 0,
                opacity: (!message.trim() && droppedFiles.length === 0) ? 0.5 : 1,
              }}
            >
              <Send24Regular />
            </button>
          )}
        </div>
      </div>

      {/* Drop hint */}
      {variant === 'hero' && droppedFiles.length === 0 && (
        <div style={{ marginTop: 12, textAlign: 'center' }}>
          <span style={{
            display: 'inline-flex', alignItems: 'center', gap: 8,
            padding: '8px 16px', borderRadius: 'var(--thot-radius-pill)',
            background: 'var(--thot-surface-raised)',
            border: '1px solid var(--thot-border)',
            fontSize: 13, color: 'var(--thot-text-muted)',
          }}>
            <Attach24Regular style={{ fontSize: 16 }} />
            Glissez-déposez une image ou un PDF ici (max 4 — 5 MB image, 20 MB PDF)
          </span>
        </div>
      )}
      </div>
    </div>
  );
}

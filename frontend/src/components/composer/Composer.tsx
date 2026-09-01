// Composer Phase 1 — adapté hash+path (save_upload), plus de base64
// Fichiers via AttachmentDraftProvider partagé (ChatHome + ChatConversation)
import { useState, useRef, useEffect } from 'react';
import {
  Add24Regular, Send24Regular, Mic24Regular,
  Square24Regular, Attach24Regular, Globe24Regular,
  Beaker24Regular, Image24Regular, ChartMultiple24Regular,
} from '@fluentui/react-icons';
import { useAttachmentDraft } from '../../features/attachments/AttachmentDraftContext';
import { validateAttachments } from '../../features/attachments/attachmentValidation';
import { showToast } from '../ui/Toast';
import './Composer.css';

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
  const { attachments, addFiles, removeAttachment, clearAttachments, isDraggingFiles, setDraggingFiles } = useAttachmentDraft();
  const droppedFiles: DroppedFile[] = attachments.map((a) => ({ file: a.file, previewUrl: a.previewUrl ?? '' }));
  const [isDraggingOverComposer, setIsDraggingOverComposer] = useState(false);
  const isDraggingGlobal = isDraggingFiles;
  const dragCounterRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleSend = () => {
    if ((!message.trim() && droppedFiles.length === 0) || isGenerating) return;
    const files = attachments.map((a) => a.file);
    onSend?.(message, files);
    setMessage('');
    clearAttachments();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Voile global + highlight Composer (spec Réponses.md P1)
  useEffect(() => {
    const onWindowDragEnter = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) setDraggingFiles(true); };
    const onWindowDragLeave = (e: DragEvent) => { if (e.relatedTarget === null) setDraggingFiles(false); };
    const onWindowDragOver = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); };
    const onWindowDrop = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); setDraggingFiles(false); setIsDraggingOverComposer(false); };
    window.addEventListener("dragenter", onWindowDragEnter);
    window.addEventListener("dragleave", onWindowDragLeave);
    window.addEventListener("dragover", onWindowDragOver);
    window.addEventListener("drop", onWindowDrop);
    return () => {
      window.removeEventListener("dragenter", onWindowDragEnter);
      window.removeEventListener("dragleave", onWindowDragLeave);
      window.removeEventListener("dragover", onWindowDragOver);
      window.removeEventListener("drop", onWindowDrop);
    };
  }, [setDraggingFiles]);

  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current += 1;
    if (e.dataTransfer.types.includes('Files')) { setDraggingFiles(true); setIsDraggingOverComposer(true); }
  };
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes('Files')) {
      e.dataTransfer.dropEffect = 'copy';
      setDraggingFiles(true);
      setIsDraggingOverComposer(true);
    }
  };
  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current <= 0) {
      dragCounterRef.current = 0;
      setIsDraggingOverComposer(false);
    }
  };

  const processFiles = (files: FileList | File[]) => {
    const arr = Array.from(files);
    const { files: accepted, errors } = validateAttachments(arr);
    if (errors.length > 0) showToast({ type: 'error', title: 'Fichiers ignores', message: errors.join(', ') });
    if (accepted.length > 0) addFiles(accepted);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDraggingOverComposer(false);
    setDraggingFiles(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) processFiles(files);
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files);
    if (files.length === 0) return;
    e.preventDefault();
    processFiles(files);
  };

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) processFiles(e.target.files);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const removeFile = (idx: number) => {
    const att = attachments[idx];
    if (att) removeAttachment(att.id);
  };

  const containerStyle: React.CSSProperties = variant === 'hero' ? {
    maxWidth: 820,
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
    maxWidth: 820,
    width: '100%',
    margin: '0 auto',
  } : {};

  const wrapperStyle: React.CSSProperties = {
    minHeight: variant === 'docked' ? 56 : undefined,
  };

  // Voile d'ambiance page (en-dehors Composer) — spec Réponses.md
  const showPageAmbient = isDraggingGlobal && !isDraggingOverComposer;

  return (
    <>
      {showPageAmbient && (
        <div className="pageDragAmbientOverlay" aria-hidden="true">
          <div className="pageDragHint">Déposez votre fichier dans la barre de saisie</div>
        </div>
      )}
      <div
        ref={containerRef}
        style={containerStyle}
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

      {/* Drop overlay — spec animé */}
      {isDraggingOverComposer && (
        <div style={{ marginBottom: 8, padding: '2px' }} aria-hidden="true">
          <span style={{ fontSize: 11, color: 'var(--thot-gold)', fontWeight: 600 }}>Déposez ici pour joindre</span>
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

      {/* Main input row — animated shell */}
      <div className={`composerShell ${isDraggingGlobal ? "composerShellAmbientHighlight" : ""} ${isDraggingOverComposer ? "composerShellActiveDrop" : ""}`} style={wrapperStyle}>
        {isDraggingOverComposer && (
          <div className="composerDropOverlay">
            <span className="composerDropIcon">＋</span>
            <span className="composerDropLabel">Déposez ici pour joindre</span>
          </div>
        )}
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
              placeholder={
                droppedFiles.length > 0
                  ? "Que souhaitez-vous analyser avec ce fichier ?"
                  : "Ecrivez votre message..."
              }
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
    </>
  );
}

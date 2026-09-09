// Composer Phase 1 — adapté hash+path (save_upload), plus de base64
// Fichiers via AttachmentDraftProvider partagé (ChatHome + ChatConversation)
import { useState, useRef, useEffect, useCallback } from 'react';
import {
  Add24Regular, Send24Regular, Mic24Regular,
  Square24Regular, Attach24Regular, Globe24Regular,
  Beaker24Regular, Image24Regular, ChartMultiple24Regular,
} from '@fluentui/react-icons';
import { useAttachmentDraft } from '../../features/attachments/AttachmentDraftContext';
import { validateAttachments } from '../../features/attachments/attachmentValidation';
import { showToast } from '../ui/Toast';
import { convertFileSrc } from "@tauri-apps/api/core";
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
  const droppedFiles: DroppedFile[] = attachments.map((a) => {
    let preview = a.previewUrl ?? '';
    if (!preview && a.stagedPath) {
      try { preview = convertFileSrc(a.stagedPath); } catch { preview = ''; }
    }
    return { file: a.file, previewUrl: preview };
  });
  const [isDraggingOverComposer, setIsDraggingOverComposer] = useState(false);
  const isDraggingGlobal = isDraggingFiles;
  const dragCounterRef = useRef(0);
  const lastNativeDropRef = useRef(0);
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

  // Machine à états annexe 09 §3 : Repos / Glisser détecté / Ciblage / Hors cible / Refus / Résolu
  // Source unique = onDragDropEvent natif (dragDropEnabled) + repli HTML5 (OLE exclusif Windows §2)
  const [dragUiState, setDragUiState] = useState<'repos'|'glisser_detecte'|'ciblage'|'hors_cible'|'refus'>('repos');
  const [dragFeedback, setDragFeedback] = useState<string>('');
  const [dragLive, setDragLive] = useState<string>('');
  const lastOverRef = useRef<number>(0);

  const isInsideComposer = useCallback((x: number, y: number) => {
    const el = containerRef.current;
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }, []);

  const validateHover = useCallback((pathsOrFiles: Array<{ name: string; size?: number }>): { ok: boolean; reason?: string } => {
    if (pathsOrFiles.length > 4) return { ok: false, reason: `Trop de fichiers (${pathsOrFiles.length}/4 max)` };
    for (const f of pathsOrFiles) {
      const name = f.name.toLowerCase();
      const isPdf = name.endsWith('.pdf');
      const isImg = name.endsWith('.png') || name.endsWith('.jpg') || name.endsWith('.jpeg') || name.endsWith('.webp');
      if (!isPdf && !isImg) return { ok: false, reason: `Type non pris en charge : ${f.name} (PNG/JPG/WEBP/PDF)` };
      if (f.size && isImg && f.size > 5*1024*1024) return { ok: false, reason: `${f.name} >5 Mo` };
      if (f.size && f.size > 20*1024*1024) return { ok: false, reason: `${f.name} >20 Mo` };
    }
    return { ok: true };
  }, []);

  useEffect(() => {
    const onWindowDragEnter = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      e.preventDefault();
      setDraggingFiles(true);
      setDragUiState('glisser_detecte');
      setDragFeedback('Déposez vos fichiers ici — PNG/JPG/WEBP/PDF, 4 max');
      setDragLive('Dépôt possible : glissez vers la barre de saisie');
    };
    const onWindowDragLeave = (e: DragEvent) => {
      if (e.relatedTarget === null) {
        setDraggingFiles(false);
        setIsDraggingOverComposer(false);
        setDragUiState('repos');
        setDragFeedback('');
        setDragLive('');
      }
    };
    const onWindowDragOver = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      e.preventDefault();
      // Throttle rafale §3 (quelques centaines ms, sans retarder drop)
      const now = Date.now();
      if (now - lastOverRef.current < 120) return;
      lastOverRef.current = now;
      const inside = isInsideComposer(e.clientX, e.clientY);
      // Pré-validation hover §5 (extensions visibles)
      const items = Array.from(e.dataTransfer.items ?? []).map(i => ({ name: (i as unknown as { getAsFile?: () => File }).getAsFile?.()?.name ?? 'fichier' }));
      // Fallback si items vides : utilise types
      const mock = items.length ? items : [{ name: 'fichier.png' }];
      const v = items.length ? validateHover(mock as unknown as Array<{name:string}>) : { ok: true };
      if (!v.ok) {
        setDragUiState('refus');
        setDragFeedback(v.reason ?? 'Type non pris en charge');
        setDraggingFiles(true);
        setIsDraggingOverComposer(inside);
        return;
      }
      if (inside) {
        setDragUiState('ciblage');
        setDragFeedback('Déposez ici pour joindre');
      } else {
        setDragUiState('hors_cible');
        setDragFeedback('Déposez dans la barre de saisie');
      }
      setDraggingFiles(true);
      setIsDraggingOverComposer(inside);
    };
    const onWindowDrop = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      e.preventDefault();
      setDraggingFiles(false);
      setIsDraggingOverComposer(false);
      setDragUiState('repos');
      setDragFeedback('');
      // Le drop effectif est traité par handleDrop du composer (même pipeline validation C12)
    };
    window.addEventListener("dragenter", onWindowDragEnter);
    window.addEventListener("dragleave", onWindowDragLeave);
    window.addEventListener("dragover", onWindowDragOver);
    window.addEventListener("drop", onWindowDrop);

    // Natif Tauri : source unique §3, position en pixels physiques §1
    let unlisten: (() => void) | undefined;
    let scaleFactor = window.devicePixelRatio || 1;
    import("@tauri-apps/api/webviewWindow").then(async ({ getCurrentWebviewWindow }) => {
      try { scaleFactor = await getCurrentWebviewWindow().scaleFactor(); } catch {}
      const win = getCurrentWebviewWindow();
      win.onDragDropEvent(async (event) => {
        const payload = (event as unknown as { payload: { type: string; position?: { x:number; y:number }; paths?: string[] } }).payload;
        const t = payload.type as 'enter'|'over'|'leave'|'drop'|'cancel';
        const now = Date.now();
        if (t === 'over' && now - lastOverRef.current < 120) return; // dedup rafale §3
        if (t === 'over') lastOverRef.current = now;

        if (t === 'enter') {
          setDraggingFiles(true);
          setDragUiState('glisser_detecte');
          setDragFeedback('Déposez vos fichiers ici');
          setDragLive('Dépôt possible');
          return;
        }
        if (t === 'over') {
          const pos = payload.position;
          let inside = false;
          if (pos) {
            const logical = { x: pos.x / scaleFactor, y: pos.y / scaleFactor };
            inside = isInsideComposer(logical.x, logical.y);
          }
          const paths = payload.paths ?? [];
          const v = validateHover(paths.map(p => ({ name: p })));
          if (!v.ok) {
            setDragUiState('refus');
            setDragFeedback(v.reason ?? 'Refus : type ou taille non conforme');
            setDraggingFiles(true);
            setIsDraggingOverComposer(inside);
            return;
          }
          setDraggingFiles(true);
          setIsDraggingOverComposer(inside);
          setDragUiState(inside ? 'ciblage' : 'hors_cible');
          setDragFeedback(inside ? 'Déposez ici pour joindre' : 'Glissez vers la barre de saisie');
          return;
        }
        if (t === 'leave' || t === 'cancel') {
          setDraggingFiles(false);
          setIsDraggingOverComposer(false);
          setDragUiState('repos');
          setDragFeedback('');
          setDragLive('');
          return;
        }
        if (t === 'drop') {
          lastNativeDropRef.current = Date.now();
          const paths = payload.paths ?? [];
          // Pré-validation §5 avant staging
          const v = validateHover(paths.map(p => ({ name: p })));
          if (!v.ok) {
            showToast({ type: 'error', title: 'Dépôt refusé', message: v.reason ?? 'Type non pris en charge' });
            setDraggingFiles(false);
            setIsDraggingOverComposer(false);
            setDragUiState('repos');
            return;
          }
          setDragUiState('repos');
          // Staging Rust annexe 10 : même forme pour drag/trombone → stage_dropped_paths
          try {
            const { stageDroppedPaths } = await import("../../lib/tauri");
            const requestId = crypto.randomUUID();
            const resp = await stageDroppedPaths(requestId, paths);
            // resp = {stageId, items:[{stageItemId, filename, mimeDetected, preview:{token}}], rejected}
            if (resp.rejected.length) {
              const msgs = resp.rejected.map(r => r.message).join(', ');
              showToast({ type: 'error', title: 'Fichiers rejetés', message: msgs });
            }
            if (resp.items.length) {
              // Convertit StagingItem → File avec vrais bytes pour preview (annexe 10 vignette)
              const stagedFiles: File[] = [];
              for (const it of resp.items) {
                try {
                  let file: File;
                  try {
                    const { invoke } = await import("@tauri-apps/api/core");
                    const bytes = await invoke<number[]>("get_staged_preview_bytes", { path: it.preview?.token ?? "" });
                    const blob = new Blob([new Uint8Array(bytes)], { type: it.mimeDetected });
                    file = new File([blob], it.filename, { type: it.mimeDetected });
                  } catch {
                    // Fallback si preview bytes non dispo (browser) → placeholder
                    const blob = new Blob([], { type: it.mimeDetected });
                    file = new File([blob], it.filename, { type: it.mimeDetected });
                    Object.defineProperty(file, 'size', { value: it.sizeBytes, writable: false });
                  }
                  (file as unknown as Record<string, unknown>)._stagedPath = it.preview?.token ?? "";
                  (file as unknown as Record<string, unknown>)._stageItemId = it.stageItemId;
                  (file as unknown as Record<string, unknown>)._previewToken = it.preview?.token;
                  stagedFiles.push(file);
                } catch {}
              }
              if (stagedFiles.length) {
                const { validateAttachments } = await import("../../features/attachments/attachmentValidation");
                const res = validateAttachments(stagedFiles);
                if (res.errors.length) showToast({ type: 'error', title: 'Fichiers ignorés', message: res.errors.join(', ') });
                if (res.files.length) {
                  window.dispatchEvent(new CustomEvent('thot:add-files', { detail: res.files }));
                  const details = res.warnings.length ? ` - ${res.warnings.join(', ')}` : '';
                  setDragLive(`${res.files.length} fichier(s) importé(s)${details}`);
                  showToast({ type: 'success', title: res.warnings.length ? 'Fichiers reçus (redimensionnés)' : 'Fichiers reçus', message: `${res.files.length} fichier(s)${details}` });
                }
              }
            }
          } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            showToast({ type: 'error', title: 'Dépôt refusé', message: msg });
            setDragLive(`Refus : ${msg}`);
            console.debug("[Composer] Tauri drop error", e);
          }
          setDraggingFiles(false);
          setIsDraggingOverComposer(false);
        }
      }).then((fn) => { unlisten = fn; }).catch(() => {});
    }).catch(() => {});

    return () => {
      window.removeEventListener("dragenter", onWindowDragEnter);
      window.removeEventListener("dragleave", onWindowDragLeave);
      window.removeEventListener("dragover", onWindowDragOver);
      window.removeEventListener("drop", onWindowDrop);
      if (unlisten) unlisten();
    };
  }, [setDraggingFiles, isInsideComposer, validateHover]);

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
    const { files: accepted, errors, warnings } = validateAttachments(arr);
    if (errors.length > 0) showToast({ type: 'error', title: 'Fichiers ignorés', message: errors.join(', ') });
    if (accepted.length > 0) {
      const details = warnings.length ? ` - ${warnings.join(', ')}` : '';
      showToast({ type: 'success', title: warnings.length ? 'Fichiers reçus (redimensionnés)' : 'Fichiers reçus', message: `${accepted.length} fichier(s)${details}` });
      addFiles(accepted);
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    // Annexe 09 §2 : dragDropEnabled natif exclusif Windows → HTML5 inactif en Tauri, évite 3×
    if (typeof window !== 'undefined' && (window as unknown as { __TAURI__?: unknown }).__TAURI__) return;
    if (Date.now() - lastNativeDropRef.current < 250) return; // dedup Tauri natif
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

  // Voile d'ambiance page (en-dehors Composer) — annexe 09 §3
  // Repos / glisser_detecte / ciblage / hors_cible / refus / resolu
  const showPageAmbient = dragUiState === 'glisser_detecte' || dragUiState === 'hors_cible';
  const isRefus = dragUiState === 'refus';
  const isCiblage = dragUiState === 'ciblage';

  return (
    <>
      {/* Live region accessibilité §4 : annonce état dépôt, résultat après drop */}
      <div aria-live="polite" aria-atomic="true" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>{dragLive}</div>
      {showPageAmbient && (
        <div className="pageDragAmbientOverlay" aria-hidden="true">
          <div className="pageDragHint">{dragFeedback || 'Déposez vos fichiers ici'}</div>
        </div>
      )}
      {isRefus && (
        <div className="pageDragAmbientOverlay pageDragAmbientRefus" aria-hidden="true">
          <div className="pageDragHint pageDragHintRefus">{dragFeedback || 'Type non pris en charge — PNG/JPG/WEBP/PDF, 4 max, 5 Mo image / 20 Mo PDF'}</div>
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

      {/* Main input row — animated shell : annexe 09 §3 pulsation vs statique si reduced-motion */}
      <div className={`composerShell ${isDraggingGlobal && !isRefus ? "composerShellAmbientHighlight" : ""} ${isCiblage ? "composerShellActiveDrop" : ""} ${isRefus ? "composerShellRefus" : ""}`} style={wrapperStyle} aria-dropeffect={isCiblage ? "copy" : isRefus ? "none" : undefined}>
        {isCiblage && (
          <div className="composerDropOverlay">
            <span className="composerDropIcon">＋</span>
            <span className="composerDropLabel">{dragFeedback || 'Déposez ici pour joindre'}</span>
          </div>
        )}
        {isRefus && isDraggingOverComposer && (
          <div className="composerDropOverlay composerDropOverlayRefus" aria-live="polite">
            <span className="composerDropIcon">✕</span>
            <span className="composerDropLabel">{dragFeedback}</span>
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

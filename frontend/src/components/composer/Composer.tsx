// Composer Phase 1 — adapté hash+path (save_upload), plus de base64
// Fichiers via AttachmentDraftProvider partagé (ChatHome + ChatConversation)
import { useState, useRef, useEffect, useCallback } from 'react';
import {
  Add24Regular, Send24Regular, Mic24Regular,
  Square24Regular, Globe24Regular, Beaker24Regular,
  Image24Regular, Attach24Regular,
} from '@fluentui/react-icons';
import { useAttachmentDraft } from '../../features/attachments/AttachmentDraftContext';
import { validateAttachments } from '../../features/attachments/attachmentValidation';
import { showToast } from '../ui/Toast';
import { useNavigate } from 'react-router-dom';
import { imageStatus, onImageStatus, stageDroppedPaths, getStagedPreviewBytes, type ImageStatus } from '../../lib/ipcBridge';
import { pathForFile } from '../../lib/ipc';
import './Composer.css';

interface DroppedFile {
  file: File;
  previewUrl: string;
}

interface ComposerProps {
  variant: 'hero' | 'docked';
  isGenerating?: boolean;
  blockedReason?: string | null;
  onSend?: (message: string, files?: File[]) => void;
  onStop?: () => void;
}

interface ToolEntry {
  id: string;
  icon: React.ReactNode;
  label: string;
  hint?: string;
  onClick?: () => void;
  disabled?: boolean;
}

/**
 * L'entrée « Studio Images » n'annonce que ce que le moteur a RÉELLEMENT répondu.
 * Le moteur est opt-in (`THOT_IMAGE_BACKEND=flux2` ou le drapeau UI) : l'absence
 * d'opt-in n'est pas une panne, « non activé » et « indisponible » sont deux états
 * distincts — les confondre faisait passer une feature vérifiée pour morte.
 */
function studioImagesEntry(engine: ImageStatus | null): { label: string; hint: string } {
  if (!engine) return { label: 'Studio Images — état du moteur…', hint: 'Diagnostic du moteur image en cours.' };
  if (engine.available) return { label: 'Studio Images — moteur disponible', hint: 'Ouvrir le studio image' };
  if (!engine.enabled) return { label: 'Studio Images — moteur non activé', hint: 'Le moteur image est opt-in : activez-le depuis le studio.' };
  return { label: 'Studio Images — moteur indisponible', hint: engine.reason || 'Le moteur image refuse une génération.' };
}

export function Composer({ variant, isGenerating, blockedReason, onSend, onStop }: ComposerProps) {
  const navigate = useNavigate();
  const [message, setMessage] = useState('');
  const [showTools, setShowTools] = useState(false);
  // Diagnostic du moteur image, lu sur le MÊME canal que la page Images
  // (`image_status`) — aucune source de vérité tierce, aucune affirmation en dur.
  const [engine, setEngine] = useState<ImageStatus | null>(null);
  const { attachments, addFiles, removeAttachment, clearAttachments, isDraggingFiles, setDraggingFiles } = useAttachmentDraft();
  const droppedFiles: DroppedFile[] = attachments.map((a) => ({
    file: a.file,
    previewUrl: a.previewUrl ?? ''
  }));
  const sendDisabled = (!message.trim() && droppedFiles.length === 0) || Boolean(blockedReason);
  const [isDraggingOverComposer, setIsDraggingOverComposer] = useState(false);
  const isDraggingGlobal = isDraggingFiles;
  const dragCounterRef = useRef(0);
  const lastNativeDropRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleSend = () => {
    if (blockedReason) return;
    if ((!message.trim() && droppedFiles.length === 0) || isGenerating) return;
    const files = attachments.map((a) => a.file);
    onSend?.(message, files);
    setMessage('');
    clearAttachments();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (blockedReason) return;
      handleSend();
    }
  };

  // Machine à états annexe 09 §3 : Repos / Glisser détecté / Ciblage / Hors cible / Refus / Résolu
  // Source unique = le chemin HTML5, résolu en chemins réels au drop (Electron)
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

  // Règle Phase 3.3a : WARN + resize uniquement. >5 Mo image = accepté (needsResize, réduit 1536 NaFlex
  // côté backend) ; seul >20 Mo refuse. Parse unique à la frontière drop, pas de refus dispersé.
  const validateHover = useCallback((pathsOrFiles: Array<{ name: string; size?: number }>): { ok: boolean; reason?: string } => {
    if (pathsOrFiles.length > 4) return { ok: false, reason: `Trop de fichiers (${pathsOrFiles.length}/4 max)` };
    for (const f of pathsOrFiles) {
      const name = f.name.toLowerCase();
      const isPdf = name.endsWith('.pdf');
      const isImg = name.endsWith('.png') || name.endsWith('.jpg') || name.endsWith('.jpeg') || name.endsWith('.webp');
      if (!isPdf && !isImg) return { ok: false, reason: `Type non pris en charge : ${f.name} (PNG/JPG/WEBP/PDF)` };
      if (f.size && f.size > 20*1024*1024) return { ok: false, reason: `${f.name} >20 Mo` };
    }
    return { ok: true };
  }, []);

  // Diagnostic du moteur image, LAZIE : à la première ouverture du panneau d'outils,
  // jamais au montage — une barre de saisie n'a pas à sonder le GPU pour être tapée.
  // Jamais avant l'écouteur, sinon `image_status` part dans le vide et l'entrée reste
  // sur « état du moteur… » pour le reste de la session.
  useEffect(() => {
    if (!showTools || engine) return;
    const requestId = crypto.randomUUID();
    let unlisten: (() => void) | null = null;
    let cancelled = false;
    void onImageStatus((status) => {
      if (status.request_id === requestId) setEngine(status);
    }).then((registered) => {
      if (cancelled) { registered(); return; }
      unlisten = registered;
      return imageStatus(requestId);
    }).catch(() => {
      // Diagnostic injoignable : l'entrée reste sur « état du moteur… », elle n'invente rien.
      if (!cancelled) setEngine(null);
    });
    return () => { cancelled = true; unlisten?.(); };
  }, [showTools, engine]);

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

      // Un seul chemin de dépôt : en conserver deux créait une double ingestion,
      // parce que le garde d'origine se serait éteint en silence quand la coque
      // a changé. Le chemin HTML5 résout les chemins réels au moment du drop.
    return () => {
      window.removeEventListener("dragenter", onWindowDragEnter);
      window.removeEventListener("dragleave", onWindowDragLeave);
      window.removeEventListener("dragover", onWindowDragOver);
      window.removeEventListener("drop", onWindowDrop);
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

  /**
   * Pipeline de dépôt : chemins réels -> staging backend -> vignette -> brouillon.
   * La validation par magic bytes reste côté backend ; le renderer ne fait que
   * réhydrater des `File` pour l'aperçu, comme le faisait le chemin natif.
   */
  const stageDropped = async (files: File[]) => {
    const paths = files.map((f) => pathForFile(f)).filter((p): p is string => typeof p === 'string' && p.length > 0);
    if (paths.length === 0) {
      // Aucun chemin résolu (source non-fichier) : on retombe sur les File bruts.
      processFiles(files);
      return;
    }
    try {
      const response = await stageDroppedPaths(crypto.randomUUID(), paths);
      if (response.rejected.length > 0) {
        showToast({ type: 'error', title: 'Fichiers rejetés', message: response.rejected.map((r) => r.message).join(', ') });
      }
      if (response.items.length === 0) return;
      const staged: File[] = [];
      for (const item of response.items) {
        try {
          const bytes = await getStagedPreviewBytes(item.preview?.token ?? '');
          // Copie dans un ArrayBuffer-owned : TS 7 refuse un Uint8Array dont le
          // buffer pourrait etre un SharedArrayBuffer.
          const blob = new Blob([bytes.slice().buffer as ArrayBuffer], { type: item.mimeDetected });
          const file = new File([blob], item.filename, { type: item.mimeDetected });
          (file as unknown as Record<string, unknown>)._stagedPath = item.preview?.token ?? '';
          (file as unknown as Record<string, unknown>)._stageItemId = item.stageItemId;
          (file as unknown as Record<string, unknown>)._previewToken = item.preview?.token;
          staged.push(file);
        } catch {
          // Vignette indisponible : on garde l'item avec une taille honnête.
          const blob = new Blob([], { type: item.mimeDetected });
          const file = new File([blob], item.filename, { type: item.mimeDetected });
          Object.defineProperty(file, 'size', { value: item.sizeBytes, writable: false });
          staged.push(file);
        }
      }
      processFiles(staged);
    } catch (error) {
      // Le staging backend peut être indisponible : on n'en perd pas le dépôt pour
      // autant. Les `File` bruts partent dans le brouillon, la validation par
      // magic bytes aura lieu à l'envoi.
      const message = error instanceof Error ? error.message : String(error);
      processFiles(files);
      setDragLive('Fichiers reçus (staging indisponible)');
      showToast({ type: 'info', title: 'Fichiers reçus', message: `Staging indisponible : ${message}` });
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (Date.now() - lastNativeDropRef.current < 250) return;
    dragCounterRef.current = 0;
    setIsDraggingOverComposer(false);
    setDraggingFiles(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length === 0) return;
    // Même pipeline que le chemin natif : on stadge les chemins réels pour que la
    // validation par magic bytes reste côté backend et non dans le renderer.
    void stageDropped(files);
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
    minHeight: variant === 'docked' ? 72 : undefined,
  };

  // Voile d'ambiance page (en-dehors Composer) — annexe 09 §3
  // Repos / glisser_detecte / ciblage / hors_cible / refus / resolu
  const showPageAmbient = dragUiState === 'glisser_detecte' || dragUiState === 'hors_cible';
  const isRefus = dragUiState === 'refus';
  const isCiblage = dragUiState === 'ciblage';

  const tools: ToolEntry[] = [
    { id: 'joindre', icon: <Attach24Regular />, label: 'Joindre un fichier (PNG/JPG/PDF)', onClick: () => fileInputRef.current?.click() },
    // « Recherche web » n'était pas une feature absente mais une feature
    // livrée : `web_research` est câblé, contractuel et mesuré. L'entrée
    // disabled mentait donc deux fois — elle refusait un clic possible, et
    // elle annonçait une phase qui existait depuis le 28/09. Elle ouvre les
    // réglages, parce que c'est là que se décide ce que la recherche a le
    // droit de faire : le réseau et le moteur ne se choisissent pas dans un
    // message. Le réglage lui-même reste sur le fichier, relu par le sidecar.
    {
      id: 'recherche-web',
      icon: <Globe24Regular />,
      label: 'Recherche web',
      hint: 'Activer, choisir le moteur et vérifier l’accès réseau',
      onClick: () => navigate('/settings'),
    },
    // Deep Research reste absent, et le dit : c'est un mode d'orchestration
    // (plusieurs requêtes, synthèse, rapport) qui n'existe pas ici. Une
    // entrée désactivée le déclare, ce qu'un silence ne ferait pas.
    { id: 'deep-research', icon: <Beaker24Regular />, label: 'Deep Research — non disponible', disabled: true },
    {
      id: 'studio-images',
      icon: <Image24Regular />,
      ...studioImagesEntry(engine),
      // Navigation applicative comme le reste de l'app (GeneratedImageCard, Sidebar) :
      // window.location.assign rechargeait la page entière et perdait le routeur, la
      // pile de navigation et les pièces jointes en cours de saisie.
      onClick: () => navigate('/images'),
    },
  ];

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
          <div className="pageDragHint pageDragHintRefus">{dragFeedback || 'Type non pris en charge — PNG/JPG/WEBP/PDF, 4 max, 20 Mo max (images >5 Mo réduites auto)'}</div>
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
          {tools.map((tool) => (
            <button
              key={tool.id}
              onClick={tool.onClick}
              disabled={tool.disabled}
              title={tool.hint}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                padding: '8px 12px', borderRadius: 8, border: 'none',
                background: 'transparent', cursor: tool.disabled ? 'not-allowed' : 'pointer', fontSize: 14,
                color: tool.disabled ? 'var(--thot-text-faint)' : 'var(--thot-text)', fontFamily: 'var(--thot-font-family)',
                opacity: tool.disabled ? 0.6 : 1,
              }}
              onMouseEnter={(e) => { if (!tool.disabled) e.currentTarget.style.background = 'var(--thot-surface-2)'; }}
              onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
            >
              <span style={{ color: 'var(--thot-text-muted)' }}>{tool.icon}</span>
              {tool.label}
            </button>
          ))}
        </div>
      )}

      {/* Hidden file input — hash+path : on envoie le binaire via electron/sidecar.cjs, pas de base64 */}
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
              disabled={sendDisabled}
              aria-label="Envoyer"
              style={{
                width: 40, height: 40, borderRadius: 'var(--thot-radius-pill)',
                border: 'none', background: 'var(--thot-gold)', cursor: sendDisabled ? 'not-allowed' : 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#fff', flexShrink: 0,
                opacity: sendDisabled ? 0.5 : 1,
              }}
            >
              <Send24Regular />
            </button>
          )}
        </div>
      </div>

      {blockedReason && (
        <p
          role="status"
          style={{
            margin: '8px 4px 0', fontSize: 12, lineHeight: 1.4,
            color: 'var(--thot-text-muted)',
          }}
        >
          {blockedReason}
        </p>
      )}

      </div>
      </div>
    </>
  );
}

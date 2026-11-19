import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft24Regular,
  Delete24Regular,
  Edit24Regular,
  Image24Regular,
  MoreHorizontal24Regular,
  Rename24Regular,
  Search24Regular,
} from '@fluentui/react-icons';
import './GalleryPage.css';
import {
  deleteImage,
  getAttachmentBytes,
  imageList,
  imageRename,
  isDeprecatedQuality,
  onArtifactCreated,
  type ImageItem,
} from '../../lib/ipcBridge';
import { showToast } from '../../components/ui/Toast';

type SortMode = 'recent' | 'oldest' | 'name';
type AssetStatus = 'idle' | 'loading' | 'ready' | 'error';
type AssetState = { status: AssetStatus; url?: string; mime?: string; error?: string };
type Asset = { url: string; mime: string };
type GroupFilter = { kind: 'space'; id: string; name: string } | { kind: 'none'; name: string };
type GroupRow = { key: string; name: string; filter: GroupFilter; items: ImageItem[] };

const PAGE_SIZE = 24;
const GROUP_PAGE_LIMIT = 48;
const GROUP_PAGE_CAP = 12; // 576 images maximum
const GROUP_PREVIEW_LIMIT = 6;

function filterToSpaceParams(filter: GroupFilter): { spaceId?: string; noSpace?: boolean } {
  return filter.kind === 'none' ? { noSpace: true } : { spaceId: filter.id };
}


function messageOf(error: unknown, fallback: string): string {
  const objectMessage = typeof error === 'object' && error !== null && 'message' in error
    ? (error as { message?: unknown }).message
    : null;
  if (typeof objectMessage === 'string' && objectMessage) return objectMessage;
  if (error instanceof Error && error.message) return error.message;
  const text = String(error ?? '').trim();
  return text || fallback;
}

function displayName(item: ImageItem): string {
  return item.display_name || item.original_filename || item.artifact_id;
}

function itemText(item: ImageItem): string {
  return [
    displayName(item),
    item.original_filename,
    item.prompt,
    item.backend,
    item.checkpoint,
    item.aspect_ratio,
  ].filter((value): value is string => typeof value === 'string').join(' ').toLocaleLowerCase('fr-FR');
}

function dateValue(item: ImageItem): number {
  if (!item.created_at) return 0;
  const value = Date.parse(item.created_at);
  return Number.isNaN(value) ? 0 : value;
}

function sortItems(items: ImageItem[], mode: SortMode): ImageItem[] {
  return [...items].sort((left, right) => {
    if (mode === 'name') return displayName(left).localeCompare(displayName(right), 'fr-FR', { sensitivity: 'base' });
    const difference = mode === 'oldest' ? dateValue(left) - dateValue(right) : dateValue(right) - dateValue(left);
    return difference !== 0 ? difference : right.artifact_id.localeCompare(left.artifact_id);
  });
}

function scalarText(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  return String(value);
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return 'taille inconnue';
  if (bytes < 1024) return `${bytes} o`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} Ko`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} Mo`;
}

function formatDate(value: string | null): string {
  if (!value) return 'date inconnue';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function dimensions(item: ImageItem): string {
  const width = scalarText(item.width);
  const height = scalarText(item.height);
  if (!width || !height) return 'dimensions non renseignées';
  return `${width} × ${height}`;
}

function safeDownloadName(item: ImageItem): string {
  const source = item.original_filename || item.display_name || `${item.artifact_id}.png`;
  const cleaned = source.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return cleaned.toLocaleLowerCase('fr-FR').endsWith('.png') ? cleaned : `${cleaned || 'image'}.png`;
}

function GalleryCard({
  item,
  index,
  asset,
  onOpen,
  onEdit,
  onLoad,
  onRetry,
  onImageError,
  menuOpen,
  onToggleMenu,
  onDelete,
  onRename,
}: {
  item: ImageItem;
  index: number;
  asset?: AssetState;
  onOpen: (item: ImageItem, trigger: HTMLButtonElement) => void;
  onEdit: (item: ImageItem) => void;
  onLoad: (artifactId: string) => void;
  onRetry: (artifactId: string) => void;
  onImageError: (artifactId: string) => void;
  menuOpen: boolean;
  onToggleMenu: (artifactId: string) => void;
  onDelete: (item: ImageItem) => void;
  onRename: (item: ImageItem) => void;
}) {
  const cardRef = useRef<HTMLElement>(null);
  const requestedRef = useRef(false);
  const status = asset?.status ?? 'idle';
  const isDeprecated = item.deprecated || isDeprecatedQuality(item.quality);

  useEffect(() => {
    if (status === 'ready' || requestedRef.current) return;
    const node = cardRef.current;
    if (!node) return;
    const request = () => {
      if (requestedRef.current) return;
      requestedRef.current = true;
      onLoad(item.artifact_id);
    };
    const Observer = window.IntersectionObserver ?? (typeof IntersectionObserver === 'undefined' ? undefined : IntersectionObserver);
    if (typeof Observer === 'undefined') {
      request();
      return;
    }
    const observer = new Observer((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        request();
      }
    }, { threshold: 0.01 });
    observer.observe(node);
    return () => observer.disconnect();
  }, [item.artifact_id, onLoad, status]);

  const cardClass = `gallery-card gallery-card--${index % 7 === 0 ? 'wide' : index % 5 === 0 ? 'tall' : 'standard'}`;

  return (
    <article ref={cardRef} className={cardClass} role="listitem">
      <button
        type="button"
        className="gallery-card-open"
        onClick={(event) => onOpen(item, event.currentTarget)}
        aria-label={`Ouvrir le détail de ${displayName(item)}`}
      >
        <span className="gallery-card-media">
          {status === 'ready' && asset?.url ? (
            <img src={asset.url} alt={displayName(item)} onError={() => onImageError(item.artifact_id)} />
          ) : status === 'error' ? (
            <span className="gallery-card-placeholder gallery-card-placeholder--error" aria-hidden="true">!</span>
          ) : (
            <span className={`gallery-card-placeholder${status === 'loading' ? ' is-loading' : ''}`} aria-hidden="true" />
          )}
          {item.is_import && <span className="gallery-badge gallery-badge--import">Import</span>}
          {isDeprecated && <span className="gallery-badge gallery-badge--deprecated">Essai obsolète</span>}
          {!isDeprecated && item.experimental && <span className="gallery-badge gallery-badge--experimental">Expérimental</span>}
        </span>
        <span className="gallery-card-copy">
          <strong>{displayName(item)}</strong>
          <span>{item.is_import ? 'Import image' : 'Création locale'}</span>
        </span>
      </button>
      <div className="gallery-card-actions" data-gallery-menu>
        <button
          type="button"
          className="gallery-card-menu-trigger"
          aria-label={`Actions pour ${displayName(item)}`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={(event) => {
            event.stopPropagation();
            onToggleMenu(item.artifact_id);
          }}
        >
          <MoreHorizontal24Regular aria-hidden="true" />
        </button>
        {menuOpen && (
          <div className="gallery-card-menu" role="menu">
            <button
              type="button"
              role="menuitem"
              className="gallery-card-menu-item"
              onClick={(event) => {
                event.stopPropagation();
                onEdit(item);
              }}
            >
              <Edit24Regular aria-hidden="true" />
              Modifier l’image
            </button>
            <button
              type="button"
              role="menuitem"
              className="gallery-card-menu-item"
              onClick={(event) => {
                event.stopPropagation();
                onRename(item);
              }}
            >
              <Rename24Regular aria-hidden="true" />
              Renommer l’image
            </button>
            <div className="gallery-card-menu-separator" role="separator" />
            <button
              type="button"
              role="menuitem"
              className="gallery-card-menu-item is-danger"
              onClick={(event) => {
                event.stopPropagation();
                onDelete(item);
              }}
            >
              <Delete24Regular aria-hidden="true" />
              Supprimer
            </button>
          </div>
        )}
      </div>
      {status === 'loading' && <span className="gallery-card-status" role="status">Chargement de l’aperçu…</span>}
      {status === 'error' && (
        <div className="gallery-card-error" role="alert">
          <span>Aperçu indisponible.</span>
          <button type="button" className="gallery-link-button" onClick={() => onRetry(item.artifact_id)}>Réessayer</button>
        </div>
      )}
    </article>
  );
}

function DetailModal({
  item,
  asset,
  onClose,
  onEdit,
  onRetry,
}: {
  item: ImageItem;
  asset?: AssetState;
  onClose: () => void;
  onEdit: (item: ImageItem) => void;
  onRetry: (artifactId: string) => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = `gallery-detail-title-${item.artifact_id}`;
  const prompt = scalarText(item.prompt);
  const aspectRatio = scalarText(item.aspect_ratio);
  const seed = scalarText(item.seed);
  const backend = scalarText(item.backend);
  const checkpoint = scalarText(item.checkpoint);
  const quality = scalarText(item.quality);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
      if (!focusable || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previous?.focus();
    };
  }, [onClose]);

  return (
    <div className="gallery-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section ref={dialogRef} className="gallery-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="gallery-modal-head">
          <div>
            <span className="gallery-kicker">Fiche image</span>
            <h2 id={titleId}>{displayName(item)}</h2>
          </div>
          <div className="gallery-modal-head-actions">
            <button
              type="button"
              className="gallery-modal-edit"
              onClick={() => onEdit(item)}
            >
              <Edit24Regular aria-hidden="true" />
              Modifier l’image
            </button>
            <button ref={closeRef} type="button" className="gallery-close" onClick={onClose} aria-label="Fermer le détail">×</button>
          </div>
        </div>
        <div className="gallery-modal-body">
          <div className="gallery-modal-media">
            {asset?.status === 'ready' && asset.url ? (
              <img src={asset.url} alt={displayName(item)} />
            ) : asset?.status === 'error' ? (
              <div className="gallery-modal-load" role="alert">
                <strong>Aperçu indisponible</strong>
                <button type="button" className="gallery-link-button" onClick={() => onRetry(item.artifact_id)}>Réessayer</button>
              </div>
            ) : (
              <div className="gallery-modal-load" role="status">Chargement de l’image…</div>
            )}
          </div>
          <dl className="gallery-detail-list">
            <div><dt>Instruction</dt><dd>{prompt || 'non renseigné'}</dd></div>
            <div><dt>Dimensions</dt><dd>{dimensions(item)}</dd></div>
            <div><dt>Format</dt><dd>{aspectRatio || 'non renseigné'}</dd></div>
            <div><dt>Graine</dt><dd>{seed || 'non renseignée'}</dd></div>
            <div><dt>Modèle / checkpoint</dt><dd>{checkpoint || 'non renseigné'}</dd></div>
            <div><dt>Backend</dt><dd>{backend || 'non renseigné'}</dd></div>
            <div><dt>Date</dt><dd>{formatDate(item.created_at)}</dd></div>
            <div><dt>Taille</dt><dd>{formatBytes(item.size_bytes)}</dd></div>
            <div><dt>Qualité</dt><dd>{quality || 'non renseignée'}</dd></div>
          </dl>
        </div>
        <div className="gallery-modal-foot">
          <span className="gallery-modal-id">{item.artifact_id}</span>
          {asset?.status === 'ready' && asset.url ? (
            <a className="gallery-download" href={asset.url} download={safeDownloadName(item)}>Télécharger PNG</a>
          ) : (
            <span className="gallery-download is-disabled" aria-disabled="true">Téléchargement indisponible</span>
          )}
        </div>
      </section>
    </div>
  );
}

function DeleteConfirmModal({
  item,
  deleting,
  onCancel,
  onConfirm,
}: {
  item: ImageItem;
  deleting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = `gallery-delete-title-${item.artifact_id}`;

  useEffect(() => {
    cancelRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !deleting) {
        event.preventDefault();
        onCancel();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [deleting, onCancel]);

  return (
    <div className="gallery-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !deleting) onCancel(); }}>
      <section className="gallery-modal gallery-delete-modal" role="alertdialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="gallery-delete-head">
          <span className="gallery-delete-mark" aria-hidden="true"><Delete24Regular /></span>
          <div>
            <span className="gallery-kicker">Action irréversible</span>
            <h2 id={titleId}>Retirer cette image ?</h2>
          </div>
        </div>
        <div className="gallery-delete-body">
          <p className="gallery-delete-intro">Cette image sera retirée de votre galerie locale.</p>
          <div className="gallery-delete-name">{displayName(item)}</div>
          <p className="gallery-delete-note">Le fichier et son historique restent conservés dans le registre local.</p>
        </div>
        <div className="gallery-delete-actions">
          <button ref={cancelRef} type="button" className="gallery-secondary-button" onClick={onCancel} disabled={deleting}>Conserver l’image</button>
          <button type="button" className="gallery-delete-button" onClick={onConfirm} disabled={deleting}>
            <Delete24Regular aria-hidden="true" />
            {deleting ? 'Suppression…' : 'Supprimer l’image'}
          </button>
        </div>
      </section>
    </div>
  );
}

function RenameModal({
  item,
  value,
  onChange,
  error,
  renaming,
  onCancel,
  onSubmit,
}: {
  item: ImageItem;
  value: string;
  onChange: (value: string) => void;
  error: string | null;
  renaming: boolean;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = `gallery-rename-title-${item.artifact_id}`;
  const inputId = `gallery-rename-input-${item.artifact_id}`;
  const canSubmit = value.trim().length > 0 && !renaming;

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !renaming) {
        event.preventDefault();
        onCancel();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [renaming, onCancel]);

  return (
    <div className="gallery-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !renaming) onCancel(); }}>
      <section className="gallery-modal gallery-delete-modal gallery-rename-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="gallery-delete-head">
          <span className="gallery-delete-mark gallery-rename-mark" aria-hidden="true"><Rename24Regular /></span>
          <div>
            <span className="gallery-kicker">Nom affiché</span>
            <h2 id={titleId}>Renommer l’image</h2>
          </div>
        </div>
        <form onSubmit={(event) => { event.preventDefault(); if (canSubmit) onSubmit(); }}>
          <div className="gallery-delete-body">
            <p className="gallery-delete-intro">Choisissez le nom qui apparaîtra dans la galerie.</p>
            <label className="sr-only" htmlFor={inputId}>Nouveau nom</label>
            <input
              ref={inputRef}
              id={inputId}
              type="text"
              className="gallery-rename-input"
              value={value}
              onChange={(event) => onChange(event.target.value)}
              autoFocus
              maxLength={200}
              disabled={renaming}
            />
            {error && <p className="gallery-rename-error" role="alert">{error}</p>}
            <p className="gallery-delete-note">Les rattachements et les pièces jointes existants sont conservés.</p>
          </div>
          <div className="gallery-delete-actions">
            <button type="button" className="gallery-secondary-button" onClick={onCancel} disabled={renaming}>Annuler</button>
            <button type="submit" className="gallery-primary-button" disabled={!canSubmit}>
              <Rename24Regular aria-hidden="true" />
              {renaming ? 'Renommage…' : 'Renommer'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

export function GalleryPage() {
  const [items, setItems] = useState<ImageItem[]>([]);
  const [includeImports, setIncludeImports] = useState(false);
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<SortMode>('recent');
  const [nextOffset, setNextOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [moreError, setMoreError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ImageItem | null>(null);
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ImageItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [renameTarget, setRenameTarget] = useState<ImageItem | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [groupBySpace, setGroupBySpace] = useState(false);
  const [groupItems, setGroupItems] = useState<ImageItem[]>([]);
  const [groupLoading, setGroupLoading] = useState(false);
  const [groupError, setGroupError] = useState<string | null>(null);
  const [groupCapped, setGroupCapped] = useState(false);
  const [groupFilter, setGroupFilter] = useState<GroupFilter | null>(null);
  const [assetStates, setAssetStates] = useState<Record<string, AssetState>>({});

  const navigate = useNavigate();
  const mountedRef = useRef(true);
  const assetsRef = useRef<Map<string, Asset>>(new Map());
  const assetStatesRef = useRef<Record<string, AssetState>>({});
  const loadingAssetsRef = useRef<Set<string>>(new Set());
  const deletedArtifactsRef = useRef<Set<string>>(new Set());
  const requestSerialRef = useRef(0);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const groupBySpaceRef = useRef(false);
  const groupFilterRef = useRef<GroupFilter | null>(null);
  const itemsFilterKeyRef = useRef<string | null>(null);

  const updateAssetState = useCallback((artifactId: string, next: AssetState) => {
    assetStatesRef.current = { ...assetStatesRef.current, [artifactId]: next };
    if (mountedRef.current) setAssetStates(assetStatesRef.current);
  }, []);

  const revokeAsset = useCallback((artifactId: string) => {
    const asset = assetsRef.current.get(artifactId);
    if (!asset) return;
    assetsRef.current.delete(artifactId);
    if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(asset.url);
  }, []);

  const clearAssets = useCallback(() => {
    assetsRef.current.forEach((asset) => {
      if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(asset.url);
    });
    assetsRef.current.clear();
    assetStatesRef.current = {};
    loadingAssetsRef.current.clear();
    deletedArtifactsRef.current.clear();
    if (mountedRef.current) setAssetStates({});
  }, []);

  const loadThumbnail = useCallback(async (artifactId: string) => {
    if (deletedArtifactsRef.current.has(artifactId)) return;
    const existing = assetsRef.current.get(artifactId);
    if (existing) {
      updateAssetState(artifactId, { status: 'ready', url: existing.url, mime: existing.mime });
      return;
    }
    if (loadingAssetsRef.current.has(artifactId)) return;
    loadingAssetsRef.current.add(artifactId);
    updateAssetState(artifactId, { status: 'loading' });
    const generation = requestSerialRef.current;
    try {
      const [bytes, mime] = await getAttachmentBytes(artifactId);
      if (deletedArtifactsRef.current.has(artifactId) || !mountedRef.current || generation !== requestSerialRef.current) return;
      const current = assetsRef.current.get(artifactId);
      if (current) {
        updateAssetState(artifactId, { status: 'ready', url: current.url, mime: current.mime });
        return;
      }
      if (typeof URL.createObjectURL !== 'function') throw new Error('URL d’image indisponible dans cet environnement.');
      const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mime || 'image/png' }));
      if (!url) throw new Error('URL d’image vide.');
      if (!mountedRef.current || generation !== requestSerialRef.current) {
        if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
        return;
      }
      const asset = { url, mime: mime || 'image/png' };
      assetsRef.current.set(artifactId, asset);
      updateAssetState(artifactId, { status: 'ready', ...asset });
    } catch (error) {
      if (!mountedRef.current || generation !== requestSerialRef.current) return;
      updateAssetState(artifactId, { status: 'error', error: messageOf(error, 'Aperçu indisponible.') });
    } finally {
      loadingAssetsRef.current.delete(artifactId);
    }
  }, [updateAssetState]);

  const retryThumbnail = useCallback((artifactId: string) => {
    loadingAssetsRef.current.delete(artifactId);
    void loadThumbnail(artifactId);
  }, [loadThumbnail]);

  const markImageError = useCallback((artifactId: string) => {
    revokeAsset(artifactId);
    updateAssetState(artifactId, { status: 'error', error: 'Le fichier image ne peut pas être décodé.' });
  }, [revokeAsset, updateAssetState]);

  const loadFirstPage = useCallback(async (
    filter: GroupFilter | null = null,
    options: { preserveAssets?: boolean } = {},
  ) => {
    const serial = ++requestSerialRef.current;
    if (!options.preserveAssets) clearAssets();
    if (options.preserveAssets) {
      assetStatesRef.current = Object.fromEntries(
        Object.entries(assetStatesRef.current).filter(([, next]) => next.status !== 'loading'),
      );
      loadingAssetsRef.current.clear();
      if (mountedRef.current) setAssetStates(assetStatesRef.current);
    }
    setSelected(null);
    setMenuOpen(null);
    setDeleteTarget(null);
    // Un changement de filtre ne doit jamais laisser la grille précédente sous
    // le nouveau titre : si les items affichés viennent d'un autre filtre, on
    // les vide avant la requête. Un rechargement du même filtre les conserve au
    // contraire, pour qu'un échec garde la galerie visible sous le bandeau.
    const filterKey = `${includeImports ? 'imports' : 'generated'}|${
      filter ? (filter.kind === 'none' ? 'none' : `space:${filter.id}`) : 'all'
    }`;
    const sameFilterAsDisplayed = itemsFilterKeyRef.current === filterKey;
    if (!sameFilterAsDisplayed) {
      setItems([]);
      setNextOffset(0);
      setHasMore(false);
    }
    setMoreError(null);
    setLoadError(null);
    setLoading(true);
    const spaceParams = filter ? filterToSpaceParams(filter) : {};
    try {
      const page = await imageList({ offset: 0, limit: PAGE_SIZE, include_imports: includeImports, ...spaceParams });
      if (!mountedRef.current || serial !== requestSerialRef.current) return;
      setItems(page.items);
      itemsFilterKeyRef.current = filterKey;
      setNextOffset(page.next_offset);
      setHasMore(page.has_more);
    } catch (error) {
      if (!mountedRef.current || serial !== requestSerialRef.current) return;
      // Même filtre : les items restent et l'erreur s'affiche en bandeau.
      // Filtre différent : la liste a déjà été vidée, l'écran d'erreur complet
      // prend la place sans montrer les images de l'ancien filtre.
      setLoadError(messageOf(error, 'Impossible de charger la galerie.'));
    } finally {
      if (mountedRef.current && serial === requestSerialRef.current) setLoading(false);
    }
  }, [clearAssets, includeImports]);

  const loadMore = useCallback(async () => {
    if (!hasMore || loadingMore) return;
    const serial = requestSerialRef.current;
    const filter = groupFilterRef.current;
    const spaceParams = filter ? filterToSpaceParams(filter) : {};
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await imageList({ offset: nextOffset, limit: PAGE_SIZE, include_imports: includeImports, ...spaceParams });
      if (!mountedRef.current || serial !== requestSerialRef.current) return;
      setItems((current) => {
        const known = new Set(current.map((item) => item.artifact_id));
        return [...current, ...page.items.filter((item) => !known.has(item.artifact_id))];
      });
      setNextOffset(page.next_offset);
      setHasMore(page.has_more);
    } catch (error) {
      if (!mountedRef.current || serial !== requestSerialRef.current) return;
      setMoreError(messageOf(error, 'Impossible de charger la suite.'));
    } finally {
      if (mountedRef.current && serial === requestSerialRef.current) setLoadingMore(false);
    }
  }, [hasMore, includeImports, loadingMore, nextOffset]);

  const loadGroupItems = useCallback(async () => {
    const serial = ++requestSerialRef.current;
    setGroupLoading(true);
    setGroupError(null);
    setGroupCapped(false);
    setGroupItems([]);
    const collected: ImageItem[] = [];
    const known = new Set<string>();
    let capped = false;
    try {
      let offset = 0;
      for (let pageIndex = 0; pageIndex < GROUP_PAGE_CAP; pageIndex += 1) {
        const page = await imageList({ offset, limit: GROUP_PAGE_LIMIT, include_imports: includeImports });
        if (!mountedRef.current || serial !== requestSerialRef.current) return;
        for (const item of page.items) {
          if (known.has(item.artifact_id)) continue;
          known.add(item.artifact_id);
          collected.push(item);
        }
        if (!page.has_more) {
          capped = false;
          break;
        }
        offset = page.next_offset || offset + GROUP_PAGE_LIMIT;
        if (pageIndex === GROUP_PAGE_CAP - 1) capped = true;
      }
      setGroupItems(collected);
      setGroupCapped(capped);
    } catch (error) {
      if (!mountedRef.current || serial !== requestSerialRef.current) return;
      setGroupError(messageOf(error, 'Impossible de charger les espaces.'));
      setGroupItems(collected);
    } finally {
      if (mountedRef.current && serial === requestSerialRef.current) setGroupLoading(false);
    }
  }, [includeImports]);

  const reloadActiveView = useCallback(() => {
    const filter = groupFilterRef.current;
    if (groupBySpaceRef.current && !filter) {
      void loadGroupItems();
      return;
    }
    void loadFirstPage(filter);
  }, [loadFirstPage, loadGroupItems]);

  useEffect(() => {
    if (!menuOpen) return;
    const closeMenu = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element) || !target.closest('[data-gallery-menu]')) {
        setMenuOpen(null);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(null);
    };
    document.addEventListener('click', closeMenu);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('click', closeMenu);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [menuOpen]);

  const openDetail = useCallback((item: ImageItem, trigger: HTMLButtonElement) => {
    setMenuOpen(null);
    lastFocusedRef.current = trigger;
    setSelected(item);
    if (!assetsRef.current.has(item.artifact_id)) void loadThumbnail(item.artifact_id);
  }, [loadThumbnail]);

  const closeDetail = useCallback(() => {
    setSelected(null);
    const trigger = lastFocusedRef.current;
    lastFocusedRef.current = null;
    window.setTimeout(() => {
      if (trigger?.isConnected) trigger.focus();
    }, 0);
  }, []);

  const toggleMenu = useCallback((artifactId: string) => {
    setMenuOpen((current) => current === artifactId ? null : artifactId);
  }, []);

  const requestDelete = useCallback((item: ImageItem) => {
    setMenuOpen(null);
    setDeleteTarget(item);
  }, []);

  const requestEdit = useCallback((item: ImageItem) => {
    setMenuOpen(null);
    if (selected?.artifact_id === item.artifact_id) closeDetail();
    navigate(`/images?edit=${encodeURIComponent(item.artifact_id)}`);
  }, [closeDetail, navigate, selected]);

  const requestRename = useCallback((item: ImageItem) => {
    setMenuOpen(null);
    setRenameTarget(item);
    setRenameValue(displayName(item));
    setRenameError(null);
  }, []);

  const submitRename = useCallback(async () => {
    if (!renameTarget || renaming) return;
    const name = renameValue.trim();
    if (!name) return;
    const artifactId = renameTarget.artifact_id;
    setRenaming(true);
    setRenameError(null);
    try {
      const result = await imageRename(artifactId, name);
      if (!mountedRef.current) return;
      const nextName = result.display_name || name;
      const renamed = (item: ImageItem) => item.artifact_id === artifactId ? { ...item, display_name: nextName } : item;
      setItems((current) => current.map(renamed));
      setGroupItems((current) => current.map(renamed));
      setSelected((current) => current?.artifact_id === artifactId ? { ...current, display_name: nextName } : current);
      setRenameTarget(null);
      showToast({ type: 'success', title: 'Image renommée', message: 'Le nouveau nom est maintenant affiché.' });
    } catch (error) {
      if (!mountedRef.current) return;
      setRenameError(messageOf(error, 'Le renommage a échoué.'));
    } finally {
      if (mountedRef.current) setRenaming(false);
    }
  }, [renameTarget, renaming, renameValue]);

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget || deleting) return;
    const artifactId = deleteTarget.artifact_id;
    setDeleting(true);
    try {
      await deleteImage(artifactId);
      deletedArtifactsRef.current.add(artifactId);
      revokeAsset(artifactId);
      loadingAssetsRef.current.delete(artifactId);
      const nextAssetStates = { ...assetStatesRef.current };
      delete nextAssetStates[artifactId];
      assetStatesRef.current = nextAssetStates;
      setAssetStates(nextAssetStates);
      setItems((current) => current.filter((item) => item.artifact_id !== artifactId));
      setGroupItems((current) => current.filter((item) => item.artifact_id !== artifactId));
      setNextOffset((current) => Math.max(0, current - 1));
      if (selected?.artifact_id === artifactId) closeDetail();
      setDeleteTarget(null);
      showToast({ type: 'success', title: 'Image supprimée', message: 'L’image a été retirée de la galerie.' });
    } catch (error) {
      showToast({ type: 'error', title: 'Suppression impossible', message: messageOf(error, 'Réessayez.') });
    } finally {
      setDeleting(false);
    }
  }, [closeDetail, deleteTarget, deleting, revokeAsset, selected]);

  const visibleItems = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase('fr-FR');
    const filtered = normalizedQuery ? items.filter((item) => itemText(item).includes(normalizedQuery)) : items;
    return sortItems(filtered, sort);
  }, [items, query, sort]);

  const groupedVisibleItems = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase('fr-FR');
    const filtered = normalizedQuery ? groupItems.filter((item) => itemText(item).includes(normalizedQuery)) : groupItems;
    return sortItems(filtered, sort);
  }, [groupItems, query, sort]);

  const groupRows = useMemo<GroupRow[]>(() => {
    const noSpaceItems: ImageItem[] = [];
    const spaceBuckets = new Map<string, { name: string; items: ImageItem[] }>();
    for (const item of groupedVisibleItems) {
      const spaces = item.spaces ?? [];
      if (spaces.length === 0) {
        noSpaceItems.push(item);
        continue;
      }
      for (const space of spaces) {
        const bucket = spaceBuckets.get(space.id) ?? { name: space.name, items: [] };
        if (!bucket.items.some((existing) => existing.artifact_id === item.artifact_id)) {
          bucket.items.push(item);
        }
        spaceBuckets.set(space.id, bucket);
      }
    }
    const rows: GroupRow[] = [];
    if (noSpaceItems.length > 0) {
      rows.push({ key: 'none', name: 'Sans espace', filter: { kind: 'none', name: 'Sans espace' }, items: noSpaceItems });
    }
    const orderedSpaces = [...spaceBuckets.entries()].sort((left, right) => left[1].name.localeCompare(right[1].name, 'fr'));
    for (const [id, bucket] of orderedSpaces) {
      rows.push({
        key: `space:${id}`,
        name: bucket.name,
        filter: { kind: 'space', id, name: bucket.name },
        items: bucket.items,
      });
    }
    return rows;
  }, [groupedVisibleItems]);

  const isRowsView = groupBySpace && !groupFilter;
  const displayedCount = isRowsView ? groupItems.length : visibleItems.length;

  const toggleGrouping = useCallback(() => {
    const next = !groupBySpaceRef.current;
    groupBySpaceRef.current = next;
    groupFilterRef.current = null;
    setGroupFilter(null);
    setGroupBySpace(next);
    if (next) {
      void loadGroupItems();
      return;
    }
    setGroupLoading(false);
    setGroupError(null);
    void loadFirstPage(null);
  }, [loadFirstPage, loadGroupItems]);

  const openGroup = useCallback((filter: GroupFilter) => {
    groupFilterRef.current = filter;
    setGroupFilter(filter);
    void loadFirstPage(filter, { preserveAssets: true });
  }, [loadFirstPage]);

  const backToRows = useCallback(() => {
    groupFilterRef.current = null;
    setGroupFilter(null);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      assetsRef.current.forEach((asset) => {
        if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(asset.url);
      });
      assetsRef.current.clear();
      assetStatesRef.current = {};
      loadingAssetsRef.current.clear();
    };
  }, []);

  useEffect(() => {
    void reloadActiveView();
  }, [reloadActiveView]);

  useEffect(() => {
    if (!isRowsView) return;
    const previewIds = groupRows.flatMap((row) => row.items.slice(0, GROUP_PREVIEW_LIMIT).map((item) => item.artifact_id));
    for (const artifactId of previewIds) {
      const state = assetStatesRef.current[artifactId];
      if (state?.status === 'ready' || state?.status === 'error') continue;
      if (loadingAssetsRef.current.has(artifactId)) continue;
      void loadThumbnail(artifactId);
    }
  }, [isRowsView, groupRows, assetStates, loadThumbnail]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | null = null;
    const registration = onArtifactCreated(() => {
      if (!disposed) void reloadActiveView();
    });
    void registration.then((registeredUnlisten) => {
      if (disposed) registeredUnlisten();
      else unlisten = registeredUnlisten;
    }).catch(() => undefined);
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [reloadActiveView]);

  const skeletonItems = Array.from({ length: 8 }, (_, index) => index);

  return (
    <div className="gallery-page" aria-busy={loading || loadingMore}>
      <header className="gallery-header">
        <div className="gallery-header-mark" aria-hidden="true"><Image24Regular /></div>
        <div className="gallery-header-copy">
          <span className="gallery-kicker">Registre local · images</span>
          <h1>Galerie</h1>
          <p>Les images conservées par l’atelier, avec leur provenance et leur format d’origine.</p>
        </div>
        <div className="gallery-count" aria-live="polite" aria-label={`${displayedCount} ${displayedCount > 1 ? 'images affichées' : 'image affichée'}`}>
          <strong>{displayedCount}</strong>{' '}
          <span>{displayedCount > 1 ? 'images affichées' : 'image affichée'}</span>
          {query && <small>sur {(isRowsView ? groupItems.length : items.length)} chargées</small>}
        </div>
      </header>

      <section className="gallery-toolbar" aria-label="Filtres de la galerie">
        <div className="gallery-search">
          <Search24Regular aria-hidden="true" />
          <label className="sr-only" htmlFor="gallery-search">Rechercher dans la galerie</label>
          <input
            id="gallery-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Rechercher une instruction, un modèle…"
          />
        </div>
        <label className="gallery-switch">
          <input type="checkbox" checked={includeImports} onChange={(event) => setIncludeImports(event.target.checked)} />
          <span className="gallery-switch-track" aria-hidden="true"><i /></span>
          <span>Afficher les imports</span>
        </label>
        <label className="gallery-sort">
          <span>Trier</span>
          <select value={sort} onChange={(event) => setSort(event.target.value as SortMode)}>
            <option value="recent">Plus récentes</option>
            <option value="oldest">Plus anciennes</option>
            <option value="name">Nom</option>
          </select>
        </label>
        <button
          type="button"
          className={`gallery-group-toggle${groupBySpace ? ' is-active' : ''}`}
          aria-pressed={groupBySpace}
          onClick={toggleGrouping}
        >
          {groupBySpace ? 'Voir toutes les images' : 'Grouper par espace'}
        </button>
      </section>

      {groupFilter && (
        <div className="gallery-group-header">
          <button
            type="button"
            className="gallery-group-back"
            aria-label="Revenir à toutes les lignes"
            onClick={backToRows}
          >
            <ArrowLeft24Regular aria-hidden="true" />
          </button>
          <div>
            <span className="gallery-kicker">Groupe consulté</span>
            <h2>{groupFilter.kind === 'none' ? 'Sans espace' : `Espace : ${groupFilter.name}`}</h2>
          </div>
        </div>
      )}

      {!isRowsView && loadError && items.length > 0 && (
        <div
          role="alert"
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
            margin: '0 0 16px', padding: '12px 16px', borderRadius: 12,
            background: 'var(--thot-danger-soft)', border: '1px solid var(--thot-danger)',
            color: 'var(--thot-danger)', fontSize: 14,
          }}
        >
          <span>
            La galerie n’a pas pu être rechargée ; les images affichées sont les dernières connues.
            <span style={{ display: 'block', fontSize: 12, opacity: 0.8, marginTop: 2 }}>{loadError}</span>
          </span>
          <button
            type="button"
            onClick={() => void loadFirstPage(groupFilterRef.current)}
            style={{
              flexShrink: 0, padding: '6px 12px', borderRadius: 'var(--thot-radius-sm)',
              border: '1px solid var(--thot-danger)', background: 'transparent',
              color: 'var(--thot-danger)', cursor: 'pointer', fontSize: 13,
              fontWeight: 600, fontFamily: 'var(--thot-font-family)',
            }}
          >
            Réessayer
          </button>
        </div>
      )}

      {isRowsView ? (
        groupLoading ? (
          <div className="gallery-grid gallery-grid--skeleton" role="status" aria-label="Chargement des espaces">
            {skeletonItems.map((index) => <div className={`gallery-skeleton gallery-skeleton--${index % 3}`} key={index}><span /><i /></div>)}
          </div>
        ) : groupError ? (
          <section className="gallery-state gallery-state--error" role="alert">
            <span className="gallery-kicker">Les espaces n’ont pas pu être lus</span>
            <h2>Le regroupement est momentanément indisponible.</h2>
            <p>{groupError}</p>
            <button type="button" className="gallery-primary-button" onClick={() => void loadGroupItems()}>Réessayer</button>
          </section>
        ) : groupItems.length === 0 ? (
          <section className="gallery-state">
            <span className="gallery-kicker">Le registre est encore vierge</span>
            <h2>Il n’y a pas encore d’image à montrer.</h2>
            <p>Une première création depuis le studio apparaîtra dans cette collection.</p>
            <a className="gallery-primary-button" href="/images">Ouvrir le studio</a>
          </section>
        ) : groupRows.length === 0 ? (
          <section className="gallery-state">
            <span className="gallery-kicker">Recherche sans résultat</span>
            <h2>Aucune image ne correspond à « {query} ».</h2>
            <button type="button" className="gallery-secondary-button" onClick={() => setQuery('')}>Effacer la recherche</button>
          </section>
        ) : (
          <div className="gallery-groups">
            {groupRows.map((row) => (
              <section className="gallery-group" key={row.key} aria-label={row.name}>
                <div className="gallery-group-head">
                  <h3>{row.name}</h3>
                  <span className="gallery-group-count">
                    {row.items.length} {row.items.length > 1 ? 'images' : 'image'}
                  </span>
                  <button type="button" className="gallery-group-see-all" onClick={() => openGroup(row.filter)}>
                    Tout voir
                  </button>
                </div>
                <div className="gallery-group-previews">
                  {row.items.slice(0, GROUP_PREVIEW_LIMIT).map((item) => {
                    const state = assetStates[item.artifact_id];
                    return (
                      <button
                        type="button"
                        className="gallery-group-preview"
                        key={item.artifact_id}
                        onClick={(event) => openDetail(item, event.currentTarget)}
                        aria-label={`Ouvrir le détail de ${displayName(item)}`}
                      >
                        {state?.status === 'ready' && state.url ? (
                          <img src={state.url} alt={displayName(item)} />
                        ) : (
                          <span className={`gallery-group-preview-placeholder${state?.status === 'loading' ? ' is-loading' : ''}`} aria-hidden="true" />
                        )}
                      </button>
                    );
                  })}
                </div>
              </section>
            ))}
            {groupCapped && (
              <p className="gallery-group-capped">Regroupement limité aux 576 images les plus récentes.</p>
            )}
          </div>
        )
      ) : loading ? (
        <div className="gallery-grid gallery-grid--skeleton" role="status" aria-label="Chargement de la galerie">
          {skeletonItems.map((index) => <div className={`gallery-skeleton gallery-skeleton--${index % 3}`} key={index}><span /><i /></div>)}
        </div>
      ) : loadError && items.length === 0 ? (
        <section className="gallery-state gallery-state--error" role="alert">
          <span className="gallery-kicker">La page n’a pas pu être lue</span>
          <h2>La galerie est momentanément indisponible.</h2>
          <p>{loadError}</p>
          <button type="button" className="gallery-primary-button" onClick={() => void loadFirstPage(groupFilterRef.current)}>Réessayer</button>
        </section>
      ) : items.length === 0 ? (
        <section className="gallery-state">
          <span className="gallery-kicker">Le registre est encore vierge</span>
          <h2>Il n’y a pas encore d’image à montrer.</h2>
          <p>Une première création depuis le studio apparaîtra dans cette collection.</p>
          <a className="gallery-primary-button" href="/images">Ouvrir le studio</a>
        </section>
      ) : visibleItems.length === 0 ? (
        <section className="gallery-state">
          <span className="gallery-kicker">Recherche sans résultat</span>
          <h2>Aucune image ne correspond à « {query} ».</h2>
          <button type="button" className="gallery-secondary-button" onClick={() => setQuery('')}>Effacer la recherche</button>
        </section>
      ) : (
        <div className="gallery-grid" role="list" aria-label="Images enregistrées">
          {visibleItems.map((item, index) => (
            <GalleryCard
              key={item.artifact_id}
              item={item}
              index={index}
              asset={assetStates[item.artifact_id]}
              onOpen={openDetail}
              onLoad={(artifactId) => void loadThumbnail(artifactId)}
               onRetry={retryThumbnail}
               onImageError={markImageError}
               menuOpen={menuOpen === item.artifact_id}
               onToggleMenu={toggleMenu}
               onDelete={requestDelete}
               onEdit={requestEdit}
               onRename={requestRename}
             />
          ))}
        </div>
      )}

      {!isRowsView && !loading && !loadError && items.length > 0 && (
        <div className="gallery-pagination">
          {moreError && <p className="gallery-pagination-error" role="alert">{moreError}</p>}
          {hasMore ? (
            <button type="button" className="gallery-secondary-button" onClick={() => void loadMore()} disabled={loadingMore}>
              {loadingMore ? 'Chargement…' : moreError ? 'Réessayer le chargement' : 'Charger 24 images de plus'}
            </button>
          ) : (
            <span>Fin du registre chargé</span>
          )}
        </div>
      )}

       {selected && (
         <DetailModal
           item={selected}
           asset={assetStates[selected.artifact_id]}
           onClose={closeDetail}
           onEdit={requestEdit}
           onRetry={retryThumbnail}
         />
       )}
       {deleteTarget && (
         <DeleteConfirmModal
           item={deleteTarget}
           deleting={deleting}
           onCancel={() => {
             if (!deleting) setDeleteTarget(null);
           }}
           onConfirm={() => void confirmDelete()}
         />
       )}
       {renameTarget && (
         <RenameModal
           item={renameTarget}
           value={renameValue}
           onChange={setRenameValue}
           error={renameError}
           renaming={renaming}
           onCancel={() => {
             if (!renaming) setRenameTarget(null);
           }}
           onSubmit={() => void submitRename()}
         />
       )}
     </div>
  );
}

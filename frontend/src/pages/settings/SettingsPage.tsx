// Thot Phase 1 — Réglages : un seul bloc, « Recherche web ».
// L'écran ne montre que ce que le moteur confirme : une case cochée ici n'est un
// réglage qu'une fois revenue relue par le sidecar. Jamais d'état optimiste, parce
// qu'un réglage annoncé puis ignoré est la pire des deux maddies.
import { useCallback, useEffect, useRef, useState } from 'react';
import './SettingsPage.css';
import {
  onWebSettings,
  webSettingsRequest,
  webSettingsSet,
  type WebSettingsPayload,
} from '../../lib/ipcBridge';
import { DepotIntegritySection } from '../../components/settings/DepotIntegritySection';
import { ModelesSection } from '../../components/settings/ModelesSection';

const WRITE_TIMEOUT_MS = 12_000;
const MAX_SEARXNG_URL_CHARS = 300;

type WebSettingsPatch = { enabled?: boolean; provider?: string; searxng_url?: string };

// `source` dit d'où vient la valeur : le dire en clair vaut mieux qu'un mot opaque.
const SOURCE_TEXTE: Record<string, string> = {
  defaut: 'réglage par défaut',
  fichier: 'fichier de réglages',
  env: 'variable d’environnement',
};

function sourceTexte(source: string): string {
  return SOURCE_TEXTE[source] ?? source;
}

// Les quatre `SOURCE_TYPES` du backend (`services/web/types.py:69`), en mots.
// Un mot du protocole dans une liste de choix n'apprend rien à l'utilisateur ;
// un mot traduit se trompe, celui-ci non.
const COUVERTURE_LIBELLE: Record<string, string> = {
  general: 'web général',
  news: 'actualités',
  academic: 'recherche académique',
  official: 'sources officielles',
};

function createId(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
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

export function SettingsPage() {
  const [settings, setSettings] = useState<WebSettingsPayload | null>(null);
  const [urlDraft, setUrlDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [listenersReady, setListenersReady] = useState(false);
  const mountedRef = useRef(true);
  const requestIdRef = useRef<string | null>(null);
  const writeTimeoutRef = useRef<number | null>(null);

  const clearWriteTimeout = useCallback(() => {
    if (writeTimeoutRef.current === null) return;
    window.clearTimeout(writeTimeoutRef.current);
    writeTimeoutRef.current = null;
  }, []);

  const requestState = useCallback(async () => {
    const requestId = createId('web-settings');
    requestIdRef.current = requestId;
    try {
      await webSettingsRequest(requestId);
    } catch (error) {
      if (mountedRef.current) {
        setFailure(`Réglages injoignables : ${messageOf(error, 'commande injoignable.')}`);
      }
    }
  }, []);

  const handleSettings = useCallback((payload: WebSettingsPayload) => {
    if (!mountedRef.current) return;
    // Une réponse d'une autre requête est périmée : l'appliquer écraserait l'état
    // affiché avec une valeur dépassée.
    if (requestIdRef.current && payload.request_id !== requestIdRef.current) return;
    requestIdRef.current = null;
    clearWriteTimeout();
    setSettings(payload);
    // La charge utile vient du sidecar : on la lit telle qu'elle est, sans lui
    // demander d'être un objet parfait — une clé absente est un champ vide.
    setUrlDraft(typeof payload.searxng_url === 'string' ? payload.searxng_url : '');
    setBusy(false);
  }, [clearWriteTimeout]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (writeTimeoutRef.current !== null) {
        window.clearTimeout(writeTimeoutRef.current);
        writeTimeoutRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    const unlisteners: Array<() => void> = [];
    const registration = onWebSettings(handleSettings);
    void registration
      .then((unlisten) => {
        if (disposed) {
          unlisten();
          return;
        }
        unlisteners.push(unlisten);
        if (mountedRef.current) setListenersReady(true);
      })
      .catch((error: unknown) => {
        if (!disposed && mountedRef.current) {
          setFailure(messageOf(error, 'Impossible d’écouter les réglages du moteur.'));
        }
      });
    return () => {
      disposed = true;
      unlisteners.forEach((unlisten) => { unlisten(); });
    };
  }, [handleSettings]);

  useEffect(() => {
    // Jamais avant l'écouteur : sinon la réponse part dans le vide et la page
    // reste sur « Interrogation » pour toute la session.
    if (!listenersReady) return;
    void requestState();
  }, [listenersReady, requestState]);

  const applyChange = useCallback(async (patch: WebSettingsPatch) => {
    if (!mountedRef.current) return;
    setBusy(true);
    clearWriteTimeout();
    // Une écriture dont la relecture n'arrive pas laisserait les cases bloquées
    // sans rien dire : on rend la main au bout d'un délai, sans effacer une
    // erreur plus précise déjà rendue.
    writeTimeoutRef.current = window.setTimeout(() => {
      writeTimeoutRef.current = null;
      if (!mountedRef.current) return;
      setBusy(false);
      setFailure((actuelle) => actuelle ?? 'Le moteur n’a pas renvoyé l’état des réglages.');
    }, WRITE_TIMEOUT_MS);
    try {
      await webSettingsSet(patch);
      // La seule vérité est la relecture : ce que le sidecar rend, pas ce qu'on
      // voulait écrire. Une écriture refusée se voit ici, jamais dans la case.
      await requestState();
    } catch (error) {
      clearWriteTimeout();
      if (!mountedRef.current) return;
      setBusy(false);
      setFailure(messageOf(error, 'Impossible d’enregistrer ce réglage.'));
    }
  }, [clearWriteTimeout, requestState]);

  const commitUrl = useCallback(() => {
    if (!settings) return;
    const trimmed = urlDraft.trim();
    if (trimmed === (settings.searxng_url || '').trim()) return;
    void applyChange({ searxng_url: trimmed });
  }, [applyChange, settings, urlDraft]);

  const ready = settings !== null;
  const enabled = settings?.enabled ?? false;
  const effective = settings?.effective_enabled ?? false;
  const writable = settings?.writable ?? false;
  const provider = settings?.provider ?? '';
  const options = settings?.provider_options ?? [];
  // Le mode suit l'interrupteur : éteint, on choisit un moteur pour plus tard.
  const searxngChoisi = provider === 'searxng';
  const providerConnu = options.some((option) => option.name === provider);
  const ecritureLibre = ready && writable && !busy;
  const selectLibre = ecritureLibre && enabled;

  return (
    <div className="set-page">
      <section className="set-hero" aria-labelledby="settings-title">
        <div className="set-hero-copy">
          <span className="set-kicker">Atelier local · réglages</span>
          <h1 id="settings-title">Réglages</h1>
          <p>
            Ce que l’atelier autorise, et sur quelle source. Chaque changement est écrit sur le
            disque puis relu par le moteur : l’écran montre ce qui est vraiment en vigueur.
          </p>
        </div>
        <span className="set-hero-mark" aria-hidden="true" />
      </section>

      {/* Accès internet — premier bloc de la page (ordre : Accès internet → Dépôts → Modèles) :
          la seule sortie réseau de l'atelier. */}
      <section className="set-block" aria-labelledby="internet-title" aria-busy={busy}>
        <header className="set-block-head">
          <div>
            <span className="set-kicker">Sortie réseau</span>
            <h2 id="internet-title">Accès internet</h2>
            <p>Outil concerné : <code>web_research</code> — la seule sortie réseau de l’atelier.</p>
          </div>
          <span className={`set-source${ready && !writable ? ' is-locked' : ''}`}>
            {ready ? `Origine : ${sourceTexte(settings.source)}` : 'Interrogation du moteur…'}
          </span>
        </header>

        {failure && <p className="set-alert" role="alert">{failure}</p>}
        {ready && settings.error && <p className="set-alert" role="alert">{settings.error}</p>}
        {ready && !writable && (
          <p className="set-note" role="status">
            Le mode est imposé par une variable d’environnement : les cases sont en lecture seule et
            rien de ce que vous choisirez ici ne sera appliqué.
          </p>
        )}

        <div className="set-row">
          <div className="set-row-copy">
            <span className="set-row-label">Recherche web</span>
            <span className="set-row-help">
              Autorise l’atelier à consulter des sources externes et à citer ce qu’il y trouve.
            </span>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            aria-label="Autoriser la recherche web"
            className={`set-switch${enabled ? ' is-on' : ''}`}
            disabled={!ecritureLibre}
            onClick={() => void applyChange({ enabled: !enabled })}
          >
            <span className="set-switch-knob" aria-hidden="true" />
          </button>
        </div>

        <p className="set-mode">
          <span className="set-mode-token">{ready ? settings.mode : '—'}</span>
          <span className="set-mode-text">
            {effective
              ? 'En vigueur : le moteur peut consulter le web et citer ses sources.'
              : 'En vigueur : le moteur ne consulte pas le web.'}
          </span>
        </p>
        {ready && effective !== enabled && (
          <p className="set-note is-warn" role="status">
            Le réglage affiché n’est pas celui qui s’applique : c’est l’environnement qui décide.
          </p>
        )}
        {ready && !enabled && (
          <p className="set-note" role="status">
            Outil fermé : seules les sources déjà indexées dans l’atelier seront citées, et une
            question sans réponse locale rendra une lacune explicite plutôt qu’une invention.
          </p>
        )}

        <div className="set-field">
          <label className="set-label" htmlFor="web-provider">Moteur de recherche</label>
          <select
            id="web-provider"
            className="set-select"
            value={providerConnu ? provider : ''}
            disabled={!selectLibre}
            onChange={(event) => void applyChange({ provider: event.target.value })}
          >
            {!providerConnu && <option value="">Moteur hors liste</option>}
            {options.map((option) => (
              <option key={option.name} value={option.name}>
                {option.label}
                {option.configured ? '' : ` — non configuré : ${option.requires}`}
              </option>
            ))}
          </select>
          <p className="set-field-help">
            {enabled
              ? 'Automatique laisse le registre choisir dans son ordre de préférence.'
              : 'Réactivable : le choix est conservé, il ne sert qu’une fois l’outil rouvert.'}
          </p>
        </div>

        {searxngChoisi && (
          <div className="set-field">
            <label className="set-label" htmlFor="web-searxng-url">Adresse de l’instance SearXNG</label>
            <input
              id="web-searxng-url"
              className="set-input"
              type="url"
              inputMode="url"
              maxLength={MAX_SEARXNG_URL_CHARS}
              placeholder="http://127.0.0.1:8080"
              value={urlDraft}
              disabled={!selectLibre}
              onChange={(event) => setUrlDraft(event.target.value)}
              onBlur={commitUrl}
              onKeyDown={(event) => {
                if (event.key !== 'Enter') return;
                event.preventDefault();
                commitUrl();
              }}
            />
            <p className="set-field-help">
              Enregistrée quand vous quittez le champ — une instance locale, par exemple
              http://127.0.0.1:8080.
            </p>
          </div>
        )}

        <ul className="set-providers" aria-label="État des moteurs de recherche">
          {options.map((option) => (
            <li key={option.name} className={`set-provider${option.selected ? ' is-selected' : ''}`}>
              <div className="set-provider-head">
                <span className="set-provider-label">{option.label}</span>
                <span className={`set-badge${option.configured ? ' is-ok' : ' is-missing'}`}>
                  {option.configured ? 'Configuré' : 'Non configuré'}
                </span>
              </div>
              {option.coverage && <p className="set-provider-coverage">{option.coverage}</p>}
              {/* `covers` était dans le contrat du payload et jamais rendu. Sans
                  lui, « Actualités » n'est couvert par personne sans que rien ne
                  le dise : l'utilisateur choisit un moteur, une recherche
                  d'actualité échoue en `NO_PROVIDER`, et la page n'avait rien
                  qui l'explique. Les trois mots sont ceux de `SOURCE_TYPES`. */}
              {option.covers && option.covers.length > 0 && (
                <p className="set-provider-covers">
                  Couvre : {option.covers.map((c) => COUVERTURE_LIBELLE[c] ?? c).join(' · ')}
                </p>
              )}
              {!option.configured && option.requires && (
                <p className="set-provider-requires">Nécessite : {option.requires}</p>
              )}
            </li>
          ))}
        </ul>

        {ready && (
          <p className="set-write" role="status">
            {settings.written
              ? 'Le moteur a confirmé cet état.'
              : 'Le moteur n’a pas enregistré la dernière modification.'}
          </p>
        )}
      </section>

      {/* Dépôts — deuxième bloc (ordre : Accès internet → Dépôts → Modèles) : sujet différent
          de l'accès internet, ils touchent des documents déjà versés. */}
      <DepotIntegritySection />

      {/* Modèles de l'atelier — troisième bloc (ordre : Accès internet → Dépôts → Modèles). */}
      <ModelesSection />
    </div>
  );
}

// Carte de progression d'un run image — partagée par le studio (ImagesPage) et la
// conversation (ChatConversation). Le vocabulaire des étapes appartient au moteur image,
// pas à une page : l'extraction laisse le studio rendu à l'identique (mêmes classes
// `img-progress*`, même DOM).
import './image.css';

export const STAGE_ORDER = [
  'demarrage',
  // Phase d'admission GPU : elle précède `production` dans le flux du moteur
  // (loop.py émet admission puis production AVANT la première ligne du worker).
  // Sans elle, un run en attente d'admission s'affiche à 0 % avec une liste d'étapes vide.
  'admission',
  'production',
  'texte-chargement',
  'texte-inference',
  'texte-liberation',
  'transformer-chargement',
  'transformer-assemblage',
  'vae-chargement',
  'diffusion',
  'vae-inference',
  'sauvegarde',
  'publication',
  'transfert',
] as const;

export type StageId = (typeof STAGE_ORDER)[number];

/** État d'avancement d'un run : les classes d'étape franchies, l'étape courante et le chrono. */
export type RunProgress = {
  done: string[];
  currentId: string;
  label: string;
  step: number | null;
  total: number | null;
  startedAt: number;
};

/** Forme minimale d'un `run_stage` acceptée par `advanceRunProgress`. */
export type RunProgressEvent = {
  stage?: string | null;
  status?: string | null;
  step?: number | null;
  total?: number | null;
};

export function stageLabel(id: string): string {
  switch (id) {
    case 'demarrage': return 'Préparation';
    case 'admission': return 'Admission du moteur image';
    case 'production': return 'Production';
    case 'texte-chargement': return "Chargement de l'encodeur texte";
    case 'texte-inference': return 'Encodage du prompt';
    case 'texte-liberation': return "Libération de l'encodeur";
    case 'transformer-chargement': return 'Chargement du transformer';
    case 'transformer-assemblage': return 'Assemblage des poids';
    case 'vae-chargement': return 'Chargement du VAE';
    case 'diffusion': return 'Diffusion';
    case 'vae-inference': return 'Décodage VAE';
    case 'sauvegarde': return 'Sauvegarde du PNG';
    case 'publication': return 'Publication locale';
    case 'transfert': return 'Transfert vers l’atelier';
    default: return id || 'Production';
  }
}

/**
 * Dérive l'avancement d'un `run_stage`. Fonction pure : le studio et la conversation
 * obtiennent le même état d'affichage à partir du même flux d'événements.
 */
export function advanceRunProgress(current: RunProgress | null, event: RunProgressEvent): RunProgress {
  const currentId = event.stage || 'production';
  const done = new Set(current?.done ?? []);
  const index = STAGE_ORDER.indexOf(currentId as StageId);
  if (index >= 0) STAGE_ORDER.slice(0, index).forEach((stage) => done.add(stage));
  return {
    done: [...done],
    currentId,
    label: event.status || stageLabel(currentId),
    step: event.step ?? null,
    total: event.total ?? null,
    startedAt: current?.startedAt ?? Date.now(),
  };
}

export function ProgressCard({ progress, elapsed }: { progress: RunProgress; elapsed: number }) {
  const currentIndex = progress.currentId ? STAGE_ORDER.indexOf(progress.currentId as StageId) : -1;
  const diffusionFraction = progress.currentId === 'diffusion' && progress.total
    ? Math.max(0, Math.min(1, ((progress.step ?? 0) - 0.5) / progress.total))
    : 0;
  const completed = Math.max(0, Math.min(100, Math.round(((currentIndex + diffusionFraction) / STAGE_ORDER.length) * 100)));
  return (
    <div className="img-progress" role="status" aria-live="polite" aria-label="Progression de la génération">
      <div className="img-progress-head">
        <div>
          <strong>Génération en cours</strong>
          <span>{progress.label || stageLabel(progress.currentId)}</span>
        </div>
        <span>{elapsed}s · {completed}%</span>
      </div>
      <div className="img-progress-track" aria-hidden="true"><span style={{ width: `${completed}%` }} /></div>
      <ol className="img-steps">
        {STAGE_ORDER.map((id, index) => {
          const done = progress.done.includes(id);
          const active = id === progress.currentId;
          if (index < currentIndex - 3) return null;
          if (!done && !active && index > currentIndex + 1) return null;
          return (
            <li key={id} className={done ? 'done' : active ? 'active' : ''}>
              <span>{active && progress.label ? progress.label : stageLabel(id)}</span>
              {active && id === 'diffusion' && progress.step !== null && progress.total !== null && (
                <span className="img-segments" aria-label={`Étape de diffusion ${progress.step} sur ${progress.total}`}>
                  {Array.from({ length: progress.total }, (_, indexSegment) => (
                    <i key={indexSegment} className={indexSegment < (progress.step ?? 0) ? 'fill' : ''} />
                  ))}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

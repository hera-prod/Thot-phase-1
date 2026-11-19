// Carte d'une image GÉNÉRÉE par le moteur, rendue dans une bulle assistant.
// Distincte de la vignette d'un envoi (120 px) : l'image a été créée ici, elle mérite sa
// présence. La lecture en grand reste celle de la Galerie — on ne duplique pas sa modale
// de détail, on y renvoie avec l'identifiant d'artefact affiché sous l'image.
//
// ACTIONS DE LA FIGURE : la galerie laisse la carte muette (galerie.md §5.3 — le
// périmètre exact des actions « reste à confirmer »), sauf UNE, ici, dans l'unique
// vue où elle dit vrai : « verser au dépôt ». La fiche espaces.md §6 réserve le
// dépôt aux documents volontairement mis à disposition, et la procédure de
// promotion d'un fichier de discussion vers le dépôt est un point ouvert
// (espaces.md:241) ; la cible n'est JAMAIS choisie ici — le serveur la déduit du
// rattachement de la discussion (espace_conversation_partager). Sans
// `conversationId`, la carte reste muette : aucun versement sans origine.
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { showToast } from '../../components/ui/Toast';
import { promouvoirVersDepot } from '../../lib/ipcBridge';
import './image.css';

export interface GeneratedImageCardProps {
  images?: string[];
  artifactIds?: string[];
  /** La discussion où la carte est affichée : la portée SOURCE d'un versement.
   *  Absente, l'action n'est pas exposée — aucun versement sans origine. */
  conversationId?: string;
}

export function GeneratedImageCard({ images, artifactIds, conversationId }: GeneratedImageCardProps) {
  if (!images || images.length === 0) return null;

  return (
    <div className="genimg-stack">
      {images.map((src, index) => {
        const artifactId = artifactIds?.[index];
        const name = artifactId ? `Image générée ${artifactId}` : `Image générée ${index + 1}`;
        return (
          <figure className="genimg" key={src}>
            <Link to="/gallery" className="genimg-frame" aria-label={`${name} — ouvrir dans la galerie`}>
              <img className="genimg-image img-reveal" src={src} alt={name} />
            </Link>
            {(artifactId && conversationId) && (
              <VerseAuDepot conversationId={conversationId} artifactId={artifactId} />
            )}
            <figcaption className="genimg-caption">
              <span className="genimg-kicker">Image générée</span>
              {artifactId && <span className="genimg-id" title={artifactId}>{artifactId}</span>}
              <Link to="/gallery" className="genimg-link">Galerie</Link>
            </figcaption>
          </figure>
        );
      })}
    </div>
  );
}

/** Un fichier de discussion du dépôt de l'espace : un nom, un clic, un état. */
function VerseAuDepot({ conversationId, artifactId }: { conversationId: string; artifactId: string }) {
  const [enCours, setEnCours] = useState(false);
  const [verse, setVerse] = useState(false);

  async function verser() {
    if (enCours || verse) return;
    setEnCours(true);
    try {
      const reponse = await promouvoirVersDepot(conversationId, artifactId);
      // Le serveur est la seule réponse : ok manquant n'est pas un succès
      // silencieux, c'est un pont qui n'a rien dit.
      if (!reponse || reponse.ok !== true) {
        throw new Error("Le dépôt n'a pas confirmé le versement.");
      }
      setVerse(true);
      showToast({
        type: 'success',
        title: reponse.created === false ? 'Déjà dans le dépôt' : 'Versé au dépôt',
        message: `L'image est désormais lisible par toutes les discussions de cet espace. Elle reste dans la galerie.`,
      });
    } catch (e) {
      showToast({
        type: 'error',
        title: 'Versement impossible',
        message: messageDe(e, 'Cette image n\'a pas rejoint le dépôt.'),
      });
    } finally {
      setEnCours(false);
    }
  }

  return (
    <button
      type="button"
      onClick={() => void verser()}
      disabled={enCours || verse}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 8,
        padding: '6px 12px', borderRadius: 'var(--thot-radius-pill)',
        border: '1px solid var(--thot-border)', background: 'transparent',
        color: verse ? 'var(--thot-text-faint)' : 'var(--thot-text-muted)',
        cursor: enCours || verse ? 'not-allowed' : 'pointer',
        fontSize: 12, fontFamily: 'var(--thot-font-family)',
      }}
    >
      {verse ? 'Au dépôt' : enCours ? 'Versement…' : 'Verser au dépôt'}
    </button>
  );
}

/** Le pont rejette avec une chaîne nue : ni `Error`, ni message vide. */
function messageDe(e: unknown, defaut: string): string {
  const message = e instanceof Error ? e.message : String(e ?? "");
  return message.trim() || defaut;
}

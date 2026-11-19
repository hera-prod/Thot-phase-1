// Thot Phase 1 — Intégrité des dépôts, dans les Réglages.
//
// La section rend QUATRE états, et le quatrième est celui qu'on oublie d'écrire :
//
//   1. sain et lecture complète → « rien à signaler », et on l'a vraiment regardé ;
//   2. anomalies               → le bouton, avec le détail par espace ;
//   3. lecture incomplète      → une table n'a pas répondu. PAS « zéro anomalie » ;
//   4. appel échoué             → « état inconnu », jamais « sain ».
//
// Les états 3 et 4 existent pour une seule raison : une absence d'information qui
// se déguise en bonne nouvelle. La réparation se fie à ce rapport ; y écrire sur la
// foi d'une lecture incomplète produirait des modifications injustifiées. Dire
// « je n'ai pas su » coûte un mot et vaut mieux qu'un mensonge rassurant.

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  checkWorkspaceDepots,
  repairWorkspaceDepots,
  type DepotIntegrityReport,
  type DepotRepairReport,
} from '../../lib/ipcBridge';
import { DepotRepairModal } from './DepotRepairModal';
import './DepotIntegritySection.css';

type Etat =
  | { genre: 'sain' }
  | { genre: 'inconnu' }
  | { genre: 'incomplet'; rapport: DepotIntegrityReport }
  | { genre: 'anomalies'; rapport: DepotIntegrityReport };

const MOTIF_LISIBLE: Record<string, string> = {
  index_manquant: 'pas indexé',
  nom_vide: 'sans nom affichable',
  blob_manquant: 'contenu introuvable',
  fichier_absent: 'fichier disparu du disque',
  lien_mort: 'lien sans document',
  doublon: 'contenu en double',
};

function motif(type: string): string {
  return MOTIF_LISIBLE[type] ?? type;
}

function messageOf(error: unknown): string {
  const objet = typeof error === 'object' && error !== null && 'message' in error
    ? (error as { message?: unknown }).message
    : null;
  if (typeof objet === 'string' && objet) return objet;
  const texte = String(error ?? '').trim();
  return texte || 'service injoignable';
}

const VERIFY_TIMEOUT_MS = 12_000;
const REPAIR_TIMEOUT_MS = 60_000;
const MSG_DELAI_VERIF = 'Le moteur n’a pas répondu dans le délai imparti. L’état des dépôts reste inconnu.';
const MSG_DELAI_REPARATION = 'Le moteur n’a pas répondu dans le délai imparti. La réparation a peut-être continué côté moteur : relancez la vérification.';

function avecDelai<T>(promesse: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const minuteur = window.setTimeout(() => reject(new Error(message)), ms);
    promesse.then(
      (valeur) => { window.clearTimeout(minuteur); resolve(valeur); },
      (erreur) => { window.clearTimeout(minuteur); reject(erreur); },
    );
  });
}

export function DepotIntegritySection() {
  const [etat, setEtat] = useState<Etat | null>(null);
  const [bilan, setBilan] = useState<DepotRepairReport | null>(null);
  const [demande, setDemande] = useState(false);
  const [enCours, setEnCours] = useState(false);
  const [echec, setEchec] = useState<string | null>(null);
  const monteRef = useRef(true);
  const verifEnCoursRef = useRef<boolean>(false);
  const reparationEnCoursRef = useRef<boolean>(false);

  const verifier = useCallback(async () => {
    if (verifEnCoursRef.current) return;
    verifEnCoursRef.current = true;
    setEtat(null);
    try {
      const rapport = await avecDelai(checkWorkspaceDepots(), VERIFY_TIMEOUT_MS, MSG_DELAI_VERIF);
      if (!monteRef.current) return;
      setEchec(null);
      // Le tri des états est ici, une fois pour toutes : l'écran ne décide pas
      // « sain » ou « pas sain » à trois endroits différents.
      if (!rapport.lecture_complete) setEtat({ genre: 'incomplet', rapport });
      else if (rapport.total === 0) setEtat({ genre: 'sain' });
      else setEtat({ genre: 'anomalies', rapport });
    } catch (error) {
      if (!monteRef.current) return;
      // Un échec n'est PAS un dépôt sain. C'est l'état le plus important du
      // fichier : c'est lui qui empêche l'écran d'affirmer une bonne nouvelle
      // qu'il n'a pas vérifiée.
      setEtat({ genre: 'inconnu' });
      setEchec(messageOf(error));
    } finally {
      verifEnCoursRef.current = false;
    }
  }, []);

  useEffect(() => {
    monteRef.current = true;
    void verifier();
    return () => {
      monteRef.current = false;
    };
  }, [verifier]);

  const reparer = useCallback(async () => {
    if (reparationEnCoursRef.current) return;
    reparationEnCoursRef.current = true;
    setEnCours(true);
    setEchec(null);
    try {
      const rapport = await avecDelai(repairWorkspaceDepots(), REPAIR_TIMEOUT_MS, MSG_DELAI_REPARATION);
      if (!monteRef.current) return;
      setBilan(rapport);
      setDemande(false);
      // On relit l'état après coup au lieu de supposer la réparation réussie :
      // `non_repares` peut contenir un échec d'écriture, et l'écran doit le dire.
      await verifier();
    } catch (error) {
      if (!monteRef.current) return;
      setEchec(messageOf(error));
      setDemande(false);
    } finally {
      if (monteRef.current) setEnCours(false);
      reparationEnCoursRef.current = false;
    }
  }, [verifier]);

  // Une anomalie non réparée n'est pas forcément « des octets disparus » : un
  // `extraction_absente` en `echec` se relance depuis son dépôt. Le message du
  // bilan doit suivre ce que `non_repares` contient réellement, pas un cas
  // particulier transformé en règle générale.
  const nonRepares = bilan?.non_repares ?? [];
  const reessaisRestants = nonRepares.filter(
    (anomalie) =>
      anomalie.type === 'extraction_absente' && anomalie.ingest_state === 'echec',
  ).length;
  const autresNonRepares = nonRepares.length - reessaisRestants;

  return (
    <section className="set-block" aria-labelledby="depot-integrite-title" aria-busy={enCours}>
      <header className="set-block-head">
        <div>
          <span className="set-kicker">Vos documents</span>
          <h2 id="depot-integrite-title">Dépôts</h2>
        </div>
        {etat?.genre === 'anomalies' && (
          <span className="set-source is-alerte">
            {etat.rapport.reparable > 0 &&
              `${etat.rapport.reparable} à réparer`}
            {etat.rapport.reparable > 0 && etat.rapport.a_reessayer > 0 && ' · '}
            {etat.rapport.a_reessayer > 0 &&
              `${etat.rapport.a_reessayer} à réessayer`}
          </span>
        )}
      </header>

      {etat === null && <p className="set-note" role="status">Vérification des dépôts…</p>}

      {etat?.genre === 'inconnu' && (
        <>
          <p className="set-alert" role="alert">
            {echec ?? 'Le moteur de l’atelier ne répond pas.'} Rien n’a été modifié, et rien
            ne peut être affirmé — ni sain, ni abîmé.
          </p>
          <button type="button" className="set-button" onClick={() => void verifier()} disabled={enCours}>
            Réessayer
          </button>
        </>
      )}

      {etat?.genre === 'incomplet' && (
        <>
          <p className="set-alert" role="alert">
            Vérification incomplète : une partie de la base n’a pas répondu. Ce qui suit n’est
            qu’un compte rendu partiel — pas un « zéro anomalie ».
          </p>
          <ul className="set-depot-diagnostics">
            {etat.rapport.diagnostics.map((ligne) => (
              <li key={ligne}>{ligne}</li>
            ))}
          </ul>
          <button type="button" className="set-button" onClick={() => void verifier()} disabled={enCours}>
            Réessayer
          </button>
        </>
      )}

      {etat?.genre === 'sain' && (
        <p className="set-note" role="status">
          Tous vos dépôts sont exploitables. Chaque document déposé est trouvable par
          l’orchestrateur.
        </p>
      )}

      {etat?.genre === 'anomalies' && (
        <>
          <p className="set-note" role="status">
            {etat.rapport.total} anomalie{etat.rapport.total > 1 ? 's' : ''} dans{' '}
            {etat.rapport.espaces.length} espace{etat.rapport.espaces.length > 1 ? 's' : ''} —
            des documents existent mais l’orchestrateur ne les retrouve pas.
          </p>

          <ul className="set-depot-espaces">
            {etat.rapport.espaces.map((espace) => (
              <li key={espace.workspace_id} className="set-depot-espace">
                <div className="set-depot-espace-tete">
                  <span className="set-depot-espace-nom">{espace.nom}</span>
                  <span className="set-depot-espace-compte">
                    {espace.total} anomalie{espace.total > 1 ? 's' : ''}
                  </span>
                </div>
                <ul className="set-depot-anomalies">
                  {Object.entries(espace.par_type).map(([type, nombre]) => (
                    <li key={type}>
                      {nombre} · {motif(type)}
                    </li>
                  ))}
                </ul>
                {(espace.a_reessayer ?? 0) > 0 && (
                  <p className="set-depot-reessai">
                    {espace.a_reessayer} échec
                    {espace.a_reessayer > 1 ? 's' : ''} d’extraction relançable
                    {espace.a_reessayer > 1 ? 's' : ''} depuis le dépôt de cet espace —
                    un réessai charge un modèle, il ne fait pas partie de la réparation.
                    {' '}
                    <Link to={`/espaces/${espace.workspace_id}/depot`}>
                      Ouvrir le dépôt de {espace.nom_connu ? espace.nom : 'cet espace'}
                    </Link>
                  </p>
                )}
              </li>
            ))}
          </ul>

          {etat.rapport.reparable > 0 ? (
            <button
              type="button"
              className="set-button set-button-reparer"
              onClick={() => setDemande(true)}
              disabled={enCours}
            >
              Réparer les dépôts
            </button>
          ) : (
            <p className="set-note" role="status">
              {etat.rapport.a_reessayer > 0
                ? 'Rien de réparable ici : les échecs d’extraction se relancent depuis le dépôt de leur espace, avec un modèle. La réparation, elle, n’en charge aucun.'
                : 'Rien de réparable ici : les anomalies concernent des octets disparus ou des liens incohérents, qui ne se devinent pas. Elles sont signalées, pas corrigées.'}
            </p>
          )}
        </>
      )}

      {bilan && (
        <p className="set-write" role="status">
          {bilan.non_repares.length > 0 ? (
            <>
              {bilan.apres.total} anomalie{bilan.apres.total > 1 ? 's' : ''} subsistent et
              n’ont pas été touchées.
              {reessaisRestants > 0 &&
                ` ${reessaisRestants} échec${
                  reessaisRestants > 1 ? 's' : ''
                } d’extraction se relance${
                  reessaisRestants > 1 ? 'nt' : ''
                } depuis le dépôt de leur espace.`}
              {autresNonRepares > 0 && ' Les octets disparus, eux, ne se réinventent pas.'}
            </>
          ) : (
            <>
              Dépôts réparés. {bilan.apres.total === 0
                ? 'Tout est à nouveau trouvable par l’orchestrateur.'
                : `${bilan.apres.total} anomalie(s) signalée(s) restent à votre attention.`}
            </>
          )}
        </p>
      )}

      {echec && etat?.genre !== 'inconnu' && (
        <p className="set-alert" role="alert">{echec}</p>
      )}

      {demande && etat?.genre === 'anomalies' && (
        <DepotRepairModal
          rapport={etat.rapport}
          repairing={enCours}
          onCancel={() => (enCours ? undefined : setDemande(false))}
          onConfirm={() => void reparer()}
        />
      )}
    </section>
  );
}
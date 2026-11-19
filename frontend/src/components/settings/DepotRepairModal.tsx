// Thot Phase 1 — Confirmation de réparation des dépôts.
//
// CE QUE CE DIALOGUE NE DIT PAS, ET POURQUOI : il ne dit pas « action
// irréversible ». La réparation n'écrit qu'un index et un nom, et ne supprime
// rien — l'affirmer dans l'autre sens apprendrait à l'utilisateur à ignorer les
// avertissements de cet écran, le jour où un vrai dialogue de suppression
// apparaîtra ici. Il dit ce qui va être réparé, ce qui ne sera pas touché, et
// que les octets restent.
//
// Le composant calque sa mécanique du dialogue de suppression de la galerie
// (focus sur Annuler, Échap, clic sur le fond) — et rien de son texte.

import { useEffect, useRef } from 'react';
import type { DepotIntegrityReport } from '../../lib/ipcBridge';

// Les mots des refus viennent du backend (`RAISONS_DE_REFUS`) : l'écran ne réinvente
// pas son propre vocabulaire pour les anomalies, il affiche celui du service. Un
// refus non reconnu ici est montré tel quel plutôt que masqué.
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

// Combien d'exemples le dialogue nomme avant de dire « et les autres aussi ».
// Un dialogue est une décision, pas un inventaire : au-delà d'un certain nombre,
// il faut dire où il s'arrête — sinon l'utilisateur croit que la liste est tout.
const EXEMPLES_MAX = 10;

// Les types que la réparation touche RÉELLEMENT — la même paire que
// `TYPES_REPARABLES` côté service. Les anomalies d'extraction (`extraction_absente`
// en `echec` notamment) ne sont pas réparées : elles se relancent depuis le dépôt.
// Les confondre ici ferait promettre une action qui n'a pas lieu.
const TYPES_REPARABLES = new Set(['index_manquant', 'nom_vide']);

/** Une anomalie, décrite en une ligne : où, quoi, et ce qu'on en fera. */
function Ligne({ type, nom }: { type: string; nom: string }) {
  return (
    <li className="set-depot-ligne">
      <span className="set-depot-ligne-type">{motif(type)}</span>
      <span className="set-depot-ligne-nom">{nom}</span>
    </li>
  );
}

export function DepotRepairModal({
  rapport,
  repairing,
  onCancel,
  onConfirm,
}: {
  rapport: DepotIntegrityReport;
  repairing: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titreId = 'set-depot-reparer-titre';

  useEffect(() => {
    cancelRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      // Tant que la réparation court, le fond et la touche Échap ne font rien :
      // fermer au milieu laisserait l'écran afficher un état déjà faux.
      if (event.key === 'Escape' && !repairing) {
        event.preventDefault();
        onCancel();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [repairing, onCancel]);

  return (
    <div
      className="set-modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !repairing) onCancel();
      }}
    >
      {/* `dialog` et non `alertdialog` : rien ici n'est destructif. Le rôle
          d'alerte ferait sur-appréhender une opération sans risque. */}
      <section className="set-modal" role="dialog" aria-modal="true" aria-labelledby={titreId}>
        <header className="set-modal-head">
          <div>
            <span className="set-kicker">Dépôts</span>
            <h2 id={titreId}>Réparer les dépôts ?</h2>
          </div>
        </header>

        <div className="set-modal-body">
          <p className="set-modal-intro">
            {rapport.reparable} document{rapport.reparable > 1 ? 's' : ''} à remettre en
            recherche dans {rapport.espaces.length} espace{rapport.espaces.length > 1 ? 's' : ''}.
            Ils existent déjà : l’orchestrateur ne les trouvait simplement pas.
          </p>

          {rapport.reparable > 0 && (
            <>
              <p className="set-modal-sous-titre">Sera réparé</p>
              <ul className="set-depot-lignes">
                {(() => {
                  // Le plafond porte sur le TOTAL, pas sur chaque espace. plafonné par
                  // espace, un dialogue de 40 dépôts abîmés alignerait 160 lignes —
                  // l'utilisateur n'y lirait plus rien, et le point important (rien
                  // n'est supprimé) disparaîtait sous la liste.
                  const tous = rapport.espaces.flatMap((espace) =>
                    (espace.exemples ?? [])
                      .filter((exemple) => TYPES_REPARABLES.has(exemple.type))
                      .map((exemple) => ({
                        ...exemple,
                        espace: espace.nom_connu ? espace.nom : espace.workspace_id,
                      })));
                  const visibles = tous.slice(0, EXEMPLES_MAX);
                  return visibles.map((exemple, index) => (
                    <Ligne
                      key={`${exemple.espace}-${exemple.type}-${exemple.display_name}-${index}`}
                      type={exemple.type}
                      nom={`${exemple.display_name} — ${exemple.espace}`}
                    />
                  ));
                })()}
              </ul>
              {/* Le reste est dit, pas caché : un dialogue qui montre 10 lignes sur 37
                  sans le dire laisserait croire qu'il n'y a rien d'autre. */}
              {rapport.reparable > EXEMPLES_MAX && (
                <p className="set-modal-note">
                  {rapport.reparable} documents au total ; les {EXEMPLES_MAX} premiers sont
                  listés ici, tous seront réparés.
                </p>
              )}
            </>
          )}

          {rapport.refuse > 0 && (
            <>
              <p className="set-modal-sous-titre">Ne sera PAS touché</p>
              <ul className="set-depot-lignes">
                {rapport.espaces
                  .filter((espace) => espace.refuse > 0)
                  .map((espace) => (
                    Object.entries(espace.refuses_par_type ?? {}).map(([type, nombre]) => (
                      <Ligne
                        key={`refus-${espace.workspace_id}-${type}`}
                        type={type}
                        // Le type vient du service : l'écran n'invente pas son propre
                        // vocabulaire pour une anomalie qu'il n'a pas inventée.
                        nom={`${nombre} · ${espace.nom_connu ? espace.nom : espace.workspace_id}`}
                      />
                    ))
                  ))}
              </ul>
              <p className="set-modal-note">
                Des octets disparus ne se devinent pas. Ces éléments sont signalés, pas corrigés.
              </p>
            </>
          )}

          {(rapport.a_reessayer ?? 0) > 0 && (
            <>
              <p className="set-modal-sous-titre">À réessayer depuis son dépôt</p>
              <ul className="set-depot-lignes">
                {(() => {
                  // Le compte par espace ne dit pas QUELS fichiers relancer. On nomme
                  // donc les échecs d'extraction réellement relançables, avec le même
                  // plafond que « Sera réparé » pour que le dialogue reste une décision.
                  const reessais = rapport.espaces.flatMap((espace) =>
                    (espace.exemples ?? [])
                      .filter(
                        (exemple) =>
                          exemple.type === 'extraction_absente' &&
                          exemple.ingest_state === 'echec',
                      )
                      .map((exemple) => ({
                        ...exemple,
                        espace: espace.nom_connu ? espace.nom : espace.workspace_id,
                      })));
                  return reessais.slice(0, EXEMPLES_MAX).map((exemple, index) => (
                    <Ligne
                      key={`reessai-${exemple.espace}-${exemple.display_name}-${index}`}
                      type="échec d’extraction"
                      nom={`${exemple.display_name} — ${exemple.espace}`}
                    />
                  ));
                })()}
              </ul>
              {(rapport.a_reessayer ?? 0) > EXEMPLES_MAX && (
                <p className="set-modal-note">
                  {rapport.a_reessayer} échecs au total ; les {EXEMPLES_MAX} premiers sont
                  listés ici, tous se relancent depuis le dépôt de leur espace.
                </p>
              )}
              <p className="set-modal-note">
                Ces échecs d’extraction ne font pas partie de la réparation : un réessai
                charge un modèle et se lance depuis le dépôt de l’espace, avec le document
                sous les yeux.
              </p>
            </>
          )}

          <p className="set-modal-note set-modal-note-fort">
            Aucun fichier n’est supprimé. Vos octets, vos noms de fichier et vos documents
            restent exactement où ils sont.
          </p>
        </div>

        <footer className="set-modal-actions">
          <button
            ref={cancelRef}
            type="button"
            className="set-button set-button-calme"
            onClick={onCancel}
            disabled={repairing}
          >
            Annuler
          </button>
          <button
            type="button"
            className="set-button set-button-reparer"
            onClick={onConfirm}
            disabled={repairing || rapport.reparable === 0}
          >
            {repairing ? 'Réparation…' : 'Réparer'}
          </button>
        </footer>
      </section>
    </div>
  );
}
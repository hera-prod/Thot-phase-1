// Le déplacement d'une discussion est un DEUX-TEMPS (espaces.md §7.3) :
// d'abord une évaluation qui ne touche à rien, puis une application confirmée.
// Ce dialogue porte les deux, et ne laisse jamais l'écran croire que le
// rattachement a bougé tant que le service ne l'a pas dit.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  deplacerConversationEspace,
  evaluerDeplacementConversation,
  listWorkspaces,
  type RapportDeplacementConversation,
  type Workspace,
} from "../../lib/ipcBridge";
import { showToast } from "../ui/Toast";

/** Le pont rejette parfois avec une chaîne nue : ni `Error`, ni message vide. */
function messageDe(error: unknown, defaut: string): string {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return message.trim() || defaut;
}

/** Le rapport d'application ajoute des clés que l'interface du pont ne nomme pas. */
type RetourDeplacement = RapportDeplacementConversation & {
  syntheses_erreurs?: Array<{ fiche: string; raison: string }>;
};

type Etape = "choix" | "confirmation";

export interface DeplacerDiscussionDialogProps {
  conversationId: string;
  conversationTitle: string;
  sourceWorkspaceId: string | null;
  sourceWorkspaceName?: string | null;
  onClose: () => void;
  onMoved: (targetWorkspaceId: string, targetWorkspaceName: string) => void;
}

function phraseFichiers(n: number): string {
  return n > 1
    ? `${n} fichiers liés suivront la discussion`
    : `${n} fichier lié suivra la discussion`;
}

function phraseRestants(n: number): string {
  return n > 1
    ? `${n} fichiers du dépôt de l'espace d'origine restent dans cet espace`
    : `${n} fichier du dépôt de l'espace d'origine reste dans cet espace`;
}

function phraseFiches(n: number): string {
  return n > 1
    ? `${n} fiches de mémoire de l'espace d'origine seront marquées à réviser`
    : `${n} fiche de mémoire de l'espace d'origine sera marquée à réviser`;
}

/** Au plus cinq noms cités : au-delà, la liste devient un mur. */
function citerNoms(liens: Array<{ display_name: string }>): string {
  const noms = liens.slice(0, 5).map((lien) => `« ${lien.display_name} »`);
  return liens.length > 5 ? `${noms.join(", ")}, …` : noms.join(", ");
}

export function DeplacerDiscussionDialog({
  conversationId,
  conversationTitle,
  sourceWorkspaceId,
  sourceWorkspaceName,
  onClose,
  onMoved,
}: DeplacerDiscussionDialogProps) {
  const [espaces, setEspaces] = useState<Workspace[]>([]);
  const [chargement, setChargement] = useState(true);
  const [erreurChargement, setErreurChargement] = useState<string | null>(null);
  const [cible, setCible] = useState("");
  const [etape, setEtape] = useState<Etape>("choix");
  const [rapport, setRapport] = useState<RapportDeplacementConversation | null>(null);
  const [erreur, setErreur] = useState<string | null>(null);
  const [enCours, setEnCours] = useState(false);
  const titreId = useId();
  const selectId = useId();
  const selectRef = useRef<HTMLSelectElement>(null);
  const retourRef = useRef<HTMLButtonElement>(null);

  // La liste ne propose que des destinations RÉELLES : l'espace courant et la
  // corbeille n'en sont pas. Le service les exclut déjà ; le filtre local le
  // rend vrai même si un jour il ne le faisait plus.
  useEffect(() => {
    let monte = true;
    setChargement(true);
    setErreurChargement(null);
    listWorkspaces()
      .then((liste) => {
        if (!monte) return;
        setEspaces(
          liste.filter(
            (espace) => espace.state !== "trashed" && espace.id !== sourceWorkspaceId,
          ),
        );
      })
      .catch((error: unknown) => {
        if (!monte) return;
        setErreurChargement(messageDe(error, "Les espaces n'ont pas pu être chargés."));
      })
      .finally(() => {
        if (monte) setChargement(false);
      });
    return () => {
      monte = false;
    };
  }, [sourceWorkspaceId]);

  // Fermer est interdit pendant une requête : laisser partir l'écran pendant
  // que le service écrit ferait perdre le fil du résultat.
  const fermer = useCallback(() => {
    if (enCours) return;
    onClose();
  }, [enCours, onClose]);

  useEffect(() => {
    const surTouche = (event: KeyboardEvent) => {
      if (event.key === "Escape") fermer();
    };
    document.addEventListener("keydown", surTouche);
    return () => document.removeEventListener("keydown", surTouche);
  }, [fermer]);

  // Le focus entre par l'action NON destructive : le champ du choix, puis le
  // retour, jamais le bouton qui déplace.
  useEffect(() => {
    if (chargement) return;
    if (etape === "choix") selectRef.current?.focus();
    else retourRef.current?.focus();
  }, [chargement, etape]);

  const continuer = async () => {
    if (!cible || enCours) return;
    setEnCours(true);
    setErreur(null);
    try {
      const evaluation = await evaluerDeplacementConversation(conversationId, cible);
      setRapport(evaluation);
      setEtape("confirmation");
    } catch (error: unknown) {
      setErreur(messageDe(error, "Le déplacement n'a pas pu être évalué."));
    } finally {
      setEnCours(false);
    }
  };

  const confirmer = async () => {
    const destination = espaces.find((espace) => espace.id === cible);
    if (!destination || enCours) return;
    setEnCours(true);
    setErreur(null);
    try {
      const retour = (await deplacerConversationEspace(
        conversationId,
        destination.id,
      )) as RetourDeplacement;
      showToast({
        type: "success",
        title: "Discussion déplacée",
        message: `Elle est désormais rattachée à « ${destination.name} ».`,
      });
      if ((retour.syntheses_erreurs?.length ?? 0) > 0) {
        showToast({
          type: "warning",
          title: "Déplacée, mais certaines fiches de mémoire n'ont pas pu être révisées.",
        });
      }
      onMoved(destination.id, destination.name);
      onClose();
    } catch (error: unknown) {
      const message = messageDe(error, "Le déplacement n'a pas pu être appliqué.");
      setErreur(message);
      // On reste à l'étape 2 : la destination et le rapport sont encore là,
      // et l'utilisateur peut réessayer sans reconstruire son choix.
      showToast({ type: "error", title: "Déplacement impossible", message });
    } finally {
      setEnCours(false);
    }
  };

  const destination = espaces.find((espace) => espace.id === cible) ?? null;
  const sourceNommee = sourceWorkspaceId
    ? sourceWorkspaceName?.trim() || "l'espace d'origine"
    : null;

  return (
    <div
      role="presentation"
      onClick={fermer}
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.4)",
        zIndex: 100,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titreId}
        onClick={(event) => event.stopPropagation()}
        style={{
          background: "var(--thot-surface-raised)",
          borderRadius: 16,
          padding: 24,
          boxShadow: "var(--thot-shadow-float)",
          maxWidth: 460,
          width: "90%",
        }}
      >
        <h3 id={titreId} style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>
          Déplacer « {conversationTitle} » ?
        </h3>

        {chargement ? (
          <p style={{ fontSize: 14, color: "var(--thot-text-muted)" }}>
            Chargement des espaces…
          </p>
        ) : erreurChargement ? (
          <>
            <p
              role="alert"
              style={{ fontSize: 14, color: "var(--thot-danger)", marginBottom: 20 }}
            >
              {erreurChargement}
            </p>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button type="button" onClick={fermer} style={boutonSecondaire}>
                Fermer
              </button>
            </div>
          </>
        ) : espaces.length === 0 ? (
          <>
            <p style={{ fontSize: 14, color: "var(--thot-text-muted)", marginBottom: 20 }}>
              Aucun autre espace disponible.
            </p>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button type="button" onClick={fermer} style={boutonSecondaire}>
                Fermer
              </button>
            </div>
          </>
        ) : etape === "choix" ? (
          <>
            <label
              htmlFor={selectId}
              style={{
                display: "block",
                fontSize: 13,
                fontWeight: 600,
                marginBottom: 6,
                color: "var(--thot-text)",
              }}
            >
              Espace de destination
            </label>
            <select
              id={selectId}
              ref={selectRef}
              value={cible}
              disabled={enCours}
              onChange={(event) => {
                setCible(event.target.value);
                setErreur(null);
              }}
              style={{
                width: "100%",
                padding: "9px 12px",
                borderRadius: "var(--thot-radius-sm)",
                border: "1px solid var(--thot-border)",
                background: "var(--thot-surface-raised)",
                color: "var(--thot-text)",
                fontSize: 14,
                fontFamily: "var(--thot-font-family)",
                marginBottom: 16,
              }}
            >
              <option value="">Choisir un espace…</option>
              {espaces.map((espace) => (
                <option key={espace.id} value={espace.id}>
                  {espace.name}
                </option>
              ))}
            </select>

            {erreur && (
              <p
                role="alert"
                style={{ fontSize: 13, color: "var(--thot-danger)", marginBottom: 16 }}
              >
                {erreur}
              </p>
            )}

            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button type="button" onClick={fermer} style={boutonSecondaire}>
                Annuler
              </button>
              <button
                type="button"
                onClick={() => void continuer()}
                disabled={!cible || enCours}
                style={{
                  ...boutonPrincipal,
                  opacity: !cible || enCours ? 0.5 : 1,
                  cursor: !cible || enCours ? "not-allowed" : "pointer",
                }}
              >
                Continuer
              </button>
            </div>
          </>
        ) : (
          <>
            <p style={{ fontSize: 14, color: "var(--thot-text-muted)", marginBottom: 14 }}>
              {sourceNommee
                ? `De « ${sourceNommee} » vers « ${destination?.name ?? ""} ».`
                : `Cette discussion n'est rattachée à aucun espace. Elle rejoindra « ${destination?.name ?? ""} ».`}
            </p>

            {rapport && (
              <ul
                style={{
                  listStyle: "none",
                  padding: 0,
                  margin: "0 0 20px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  fontSize: 13,
                  color: "var(--thot-text)",
                  lineHeight: 1.5,
                }}
              >
                <li>L'historique de la discussion reste intact.</li>
                {rapport.contributions.length > 0 && (
                  <li>{phraseFichiers(rapport.contributions.length)}</li>
                )}
                {rapport.lost_links.length > 0 && (
                  <li>
                    {phraseRestants(rapport.lost_links.length)}
                    <span style={{ display: "block", color: "var(--thot-text-muted)" }}>
                      {citerNoms(rapport.lost_links)}
                    </span>
                  </li>
                )}
                {rapport.syntheses_a_invalider.length > 0 && (
                  <li>{phraseFiches(rapport.syntheses_a_invalider.length)}</li>
                )}
                <li>Aucun fichier n'est copié ni supprimé.</li>
              </ul>
            )}

            {erreur && (
              <p
                role="alert"
                style={{ fontSize: 13, color: "var(--thot-danger)", marginBottom: 16 }}
              >
                {erreur}
              </p>
            )}

            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                type="button"
                ref={retourRef}
                onClick={() => {
                  setEtape("choix");
                  setErreur(null);
                }}
                disabled={enCours}
                style={boutonSecondaire}
              >
                Retour
              </button>
              <button
                type="button"
                onClick={() => void confirmer()}
                disabled={enCours}
                style={{
                  ...boutonPrincipal,
                  opacity: enCours ? 0.5 : 1,
                  cursor: enCours ? "not-allowed" : "pointer",
                }}
              >
                Déplacer
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

const boutonSecondaire: React.CSSProperties = {
  padding: "8px 16px",
  borderRadius: "var(--thot-radius-sm)",
  border: "1px solid var(--thot-border)",
  background: "transparent",
  cursor: "pointer",
  fontSize: 14,
  fontFamily: "var(--thot-font-family)",
  color: "var(--thot-text)",
};

const boutonPrincipal: React.CSSProperties = {
  padding: "8px 16px",
  borderRadius: "var(--thot-radius-sm)",
  border: "none",
  background: "var(--thot-gold)",
  cursor: "pointer",
  fontSize: 14,
  fontWeight: 600,
  fontFamily: "var(--thot-font-family)",
  color: "#fff",
};

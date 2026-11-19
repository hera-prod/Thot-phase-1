// pages/espaces/EspacePage.tsx — un espace et ses trois vues
// (maquettes 05-2 Discussions, 05-3 Dépôt, plus l'onglet Mémoire de 05-2)
//
// LE POINT DE DÉSACCORD AVEC LA MAQUETTE 05-3, ET IL EST ASSUMÉ : la maquette
// montre une arborescence `docs/`, `content/`, `scripts/` avec un « Chemin »
// copiable (`/docs/README.md`) et un bouton « Versions précédentes ». Un espace
// n'a AUCUN dossier : il ne possède aucun octet (C17), et C10:172 interdit le
// dossier physique par espace. Une arborescence affichée serait une illusion —
// on montrerait un chemin qui n'existe nulle part, et le copier donnerait un
// chemin faux. Le dépôt est donc une VUE DE PORTÉE : la liste des documents que
// l'UTILISATEUR y a versés, et rien d'autre.
//
// LE DÉPÔT N'EST PAS LA MÉMOIRE, et l'écran le dit. Le dépôt APPORTE des
// fichiers ; la mémoire RETIENT des informations validées. Un fichier déposé
// dans une discussion y reste et n'apparaît pas ici : le confondre avec la
// mémoire ferait passer « ce que j'ai préparé pour ce projet » pour « ce que
// Thot sait de ce projet ».
//
// Ce que cet écran ne fait pas : il ne PURGE rien. Retirer un document du dépôt
// ne touche qu'un lien (`removed_at`) ; la purge est de la maintenance, avec
// dépendances (`backend_espaces.md:187-191`).
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Add24Regular,
  ArrowClockwise24Regular,
  ArrowLeft24Regular,
  ArrowMove24Regular,
  ArrowUpload24Regular,
  Brain24Regular,
  Chat24Regular,
  DataHistogram24Regular,
  Delete24Regular,
  Dismiss24Regular,
  Document24Regular,
  DocumentFolder24Regular,
  DocumentPdf24Regular,
  DocumentText24Regular,
  Edit24Regular,
  ErrorCircle24Regular,
  Image24Regular,
  MoreHorizontal24Regular,
  Pen24Regular,
} from "@fluentui/react-icons";
import { showToast } from "../../components/ui/Toast";
import { DeplacerDiscussionDialog } from "../../components/espaces/DeplacerDiscussionDialog";
import { pathForFile } from "../../lib/ipc";
import {
  deleteConversation,
  getWorkspace,
  listWorkspaces,
  listWorkspaceConversations,
  listWorkspaceDepot,
  MAX_PICKER_ATTACHMENT_BYTES,
  onArtifactIngestion,
  prepareDepotFiles,
  relancerExtraction,
  renameConversation,
  renommerDocumentDepot,
  stageDepotBytes,
  stageDepotPaths,
  unshareWorkspaceFile,
  type DepotFile,
  type DepotHiddenFile,
  type DepotHiddenReason,
  type DepotStage,
  type DepotInventory,
  type Workspace,
  type WorkspaceConversation,
  type WorkspaceStats,
  type MemoryElement,
  type MemoryEntretien,
  type MemorySynthesis,
  chargerMemoireEspace,
  corrigerMemoireEspace,
  forcerMemoireEspace,
  supprimerMemoireEspace,
} from "../../lib/ipcBridge";
import { useInferenceActivity } from "../../features/inference/InferenceActivityContext";
import { formatDateLongue, formatNombre, formatOctets, formatRelatif, teinteEspace } from "./format";

type Onglet = "discussions" | "depot" | "memoire";

const ONGLETS: ReadonlyArray<{ cle: Onglet; libelle: string }> = [
  { cle: "discussions", libelle: "Discussions" },
  { cle: "depot", libelle: "Dépôt" },
  { cle: "memoire", libelle: "Mémoire" },
];

function lireOnglet(brut: string | undefined): Onglet {
  return ONGLETS.some((o) => o.cle === brut) ? (brut as Onglet) : "discussions";
}

export function EspacePage() {
  const { id, onglet } = useParams<{ id: string; onglet?: string }>();
  const navigate = useNavigate();
  const actif = lireOnglet(onglet);

  const [espace, setEspace] = useState<Workspace | null>(null);
  const [stats, setStats] = useState<WorkspaceStats | null>(null);
  const [discussions, setDiscussions] = useState<WorkspaceConversation[]>([]);
  const [chargement, setChargement] = useState(true);
  const [erreur, setErreur] = useState<string | null>(null);
  const [depot, setDepot] = useState<DepotInventory | null>(null);
  const [chargementDepot, setChargementDepot] = useState(true);
  const [erreurDepot, setErreurDepot] = useState<string | null>(null);

  // Actions d'une discussion. Trois états distincts : l'édition du titre, la
  // confirmation de suppression et le menu ouvert. Les mêler obligerait à
  // deviner lequel est vrai.
  const [discussionRenommee, setDiscussionRenommee] = useState<string | null>(null);
  const [nouveauTitre, setNouveauTitre] = useState("");
  const [discussionASupprimer, setDiscussionASupprimer] = useState<string | null>(null);
  const [menuDiscussion, setMenuDiscussion] = useState<string | null>(null);
  // La discussion dont on demande le déplacement, telle qu'affichée : le
  // dialogue a besoin de son titre et de son rattachement sans relire la liste.
  const [discussionADeplacer, setDiscussionADeplacer] = useState<WorkspaceConversation | null>(null);

  const loadSeqRef = useRef(0);
  const depotSeqRef = useRef(0);
  const depotIdRef = useRef<string | null>(null);
  const menuDiscussionRef = useRef<HTMLDivElement>(null);

  const charger = useCallback(() => {
    if (!id) return;
    const sequence = loadSeqRef.current + 1;
    loadSeqRef.current = sequence;
    setChargement(true);
    // L'espace et ses discussions tiennent dans ces deux appels. Le dépôt n'y
    // figure pas : il a sa propre lecture, `chargerDepot`, et c'est elle qui
    // alimente les DEUX endroits de l'écran où un dépôt est compté.
    getWorkspace(id)
      .then(({ workspace, stats: s }) => {
        if (loadSeqRef.current !== sequence) return null;
        setEspace(workspace);
        setStats(s);
        setErreur(null);
        return listWorkspaceConversations(id);
      })
      .then((discussionList) => {
        if (!discussionList || loadSeqRef.current !== sequence) return;
        setDiscussions(discussionList);
      })
      .catch(() => {
        if (loadSeqRef.current !== sequence) return;
        // Espace introuvable et base injoignable ne doivent pas se confondre à
        // l'écran : on affiche le refus, pas un espace vide.
        setErreur("Cet espace est introuvable ou la base est injoignable.");
        setEspace(null);
        // `discussions` est conservé : un échec de lecture ne doit pas effacer
        // la liste déjà affichée sous l'écran d'erreur.
      })
      .finally(() => {
        if (loadSeqRef.current === sequence) setChargement(false);
      });
  }, [id]);

  /** Relit seulement les compteurs de l'espace après une écriture mémoire. */
  const rafraichirStats = useCallback(() => {
    if (!id) return;
    getWorkspace(id)
      .then(({ stats: s }) => setStats(s))
      .catch(() => {
        // Un compteur périmé n'est pas une erreur d'écriture : la fiche est
        // enregistrée, l'écran rechargera au prochain passage.
      });
  }, [id]);

  // L'inventaire est lu ICI, une fois, et la page le passe à l'onglet comme au
  // panneau de droite. Les deux affichent donc la MÊME mesure : l'inventaire ne
  // se recalcule pas en deux endroits, et `stats.fichiers` / `stats.octets` ne
  // viennent plus d'un second appel au service sur un dépôt vivant.
  const chargerDepot = useCallback(() => {
    if (!id) return;
    const sequence = depotSeqRef.current + 1;
    depotSeqRef.current = sequence;
    // Un autre espace ne doit jamais afficher l'inventaire du précédent.
    if (depotIdRef.current !== id) {
      depotIdRef.current = id;
      setDepot(null);
    }
    setChargementDepot(true);
    listWorkspaceDepot(id)
      .then((inventaire) => {
        if (depotSeqRef.current !== sequence) return;
        setDepot(inventaire);
        setErreurDepot(null);
      })
      .catch((e: unknown) => {
        if (depotSeqRef.current !== sequence) return;
        // Un échec de rafraîchissement conserve l'inventaire déjà affiché ;
        // `erreurDepot` porte seul la vérité de l'erreur.
        setErreurDepot(messageDe(e, "Le dépôt de cet espace n'a pas pu être lu."));
      })
      .finally(() => {
        if (depotSeqRef.current === sequence) setChargementDepot(false);
      });
  }, [id]);

  useEffect(charger, [charger]);
  useEffect(chargerDepot, [chargerDepot]);

  // Fermeture du menu au clic extérieur et sur Échap. La ref couvre le bouton
  // ET le menu : un pointerdown DANS la zone ne ferme donc rien, sinon le clic
  // sur « Renommer » serait annulé avant même d'atteindre son gestionnaire.
  useEffect(() => {
    if (menuDiscussion === null) return;
    const surPression = (event: PointerEvent) => {
      if (menuDiscussionRef.current?.contains(event.target as Node)) return;
      setMenuDiscussion(null);
    };
    const surTouche = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuDiscussion(null);
    };
    document.addEventListener("pointerdown", surPression);
    document.addEventListener("keydown", surTouche);
    return () => {
      document.removeEventListener("pointerdown", surPression);
      document.removeEventListener("keydown", surTouche);
    };
  }, [menuDiscussion]);

  // La modale de suppression se ferme sur Échap comme le menu, mais son effet
  // est distinct : ouvrir l'une ne doit pas dépendre de l'état de l'autre.
  useEffect(() => {
    if (discussionASupprimer === null) return;
    const surTouche = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDiscussionASupprimer(null);
    };
    document.addEventListener("keydown", surTouche);
    return () => document.removeEventListener("keydown", surTouche);
  }, [discussionASupprimer]);

  // Le rafraîchissement déclenché par un événement doit toujours appeler la
  // DERNIÈRE version de `chargerDepot` (elle change avec l'identifiant d'espace).
  // La ref évite de recréer l'abonnement — donc de perdre un événement — à
  // chaque navigation.
  const chargerDepotRef = useRef(chargerDepot);
  useEffect(() => {
    chargerDepotRef.current = chargerDepot;
  }, [chargerDepot]);

  // Abonnement au MONTAGE, pas au clic sur l'onglet : une extraction lancée
  // pendant qu'on lit les discussions doit déjà être suivie quand on revient.
  // Le filtre sur l'espace est local ; aucun rechargement pour un autre espace.
  useEffect(() => {
    if (!id) return;
    let monte = true;
    let desabonner: (() => void) | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const rafraichirBientot = () => {
      if (!monte) return;
      // Coalescence : un lot de vingt fichiers produit vingt terminaux, mais un
      // seul inventaire relu. Un délai plus long ne perdrait rien ; 300 ms est
      // assez court pour que l'écran ne paraisse pas figé.
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        if (monte) chargerDepotRef.current();
      }, 300);
    };

    try {
      onArtifactIngestion((evenement) => {
        if (!monte || evenement.workspace_id !== id) return;
        rafraichirBientot();
      })
        .then((off) => {
          if (monte) desabonner = off;
          else off();
        })
        .catch(() => {
          // Pas de pont : le repli périodique couvre encore l'inventaire.
        });
    } catch {
      // Un renderer sans pont ne doit pas faire tomber la page.
    }

    return () => {
      monte = false;
      if (timer !== null) clearTimeout(timer);
      if (desabonner) desabonner();
    };
  }, [id]);

  // Repli LENT : un événement perdu (pont absent, redémarrage) ne doit pas
  // laisser un état figé jusqu'à la prochaine navigation. 60 s est un plancher
  // de sécurité, pas une boucle de surveillance.
  useEffect(() => {
    if (!id || actif !== "depot") return;
    const intervalle = setInterval(() => {
      chargerDepotRef.current();
    }, 60_000);
    return () => clearInterval(intervalle);
  }, [id, actif]);

  const fichiersDepot = depot?.files ?? [];
  const poidsDepot = fichiersDepot.reduce((acc, f) => acc + (f.size_bytes || 0), 0);
  // Une taille que le service n'a pas trouvée n'est pas un zéro : la somme est
  // alors un plan MINIMAL, et l'écrire « 2,6 Mo » sans le dire serait le même
  // mensonge que le « 0 o » du fichier concerné.
  const poidsDepotMinimal = fichiersDepot.some((f) => !f.size_known);

  const aller = (cible: Onglet) => {
    navigate(cible === "discussions" ? `/espaces/${id}` : `/espaces/${id}/${cible}`);
  };

  const nouvelleDiscussion = () => {
    navigate(`/chat/${crypto.randomUUID()}`, { state: { workspaceId: id } });
  };

  /** Ferme le menu puis pré-remplit l'édition : « Sans titre » n'est pas un nom. */
  const ouvrirRenommage = (conversationId: string, titreActuel: string) => {
    setMenuDiscussion(null);
    setNouveauTitre(titreActuel === "Sans titre" ? "" : titreActuel);
    setDiscussionRenommee(conversationId);
  };

  const annulerRenommage = () => {
    setDiscussionRenommee(null);
    setNouveauTitre("");
  };

  const renommer = async (conversationId: string) => {
    // Un titre vide n'est pas un renommage : on laisse le champ ouvert, sans
    // appeler le service — c'est l'utilisateur qui corrige, pas un échec.
    const titre = nouveauTitre.trim();
    if (!titre) return;
    try {
      await renameConversation(conversationId, titre);
      setDiscussions((actuelles) =>
        actuelles.map((d) => (d.id === conversationId ? { ...d, title: titre } : d)),
      );
      showToast({
        type: "success",
        title: "Discussion renommée",
        message: `Elle s'appelle désormais « ${titre} ».`,
      });
      annulerRenommage();
    } catch (e) {
      // L'édition reste ouverte : le titre saisi ne doit pas disparaître parce
      // que le service a refusé.
      showToast({
        type: "error",
        title: "Renommage impossible",
        message: messageDe(e, "Cette discussion garde son nom actuel."),
      });
    }
  };

  const demanderSuppression = (conversationId: string) => {
    setMenuDiscussion(null);
    setDiscussionASupprimer(conversationId);
  };

  const demanderDeplacement = (discussion: WorkspaceConversation) => {
    setMenuDiscussion(null);
    setDiscussionADeplacer(discussion);
  };

  const supprimer = async () => {
    if (!discussionASupprimer) return;
    const conversationId = discussionASupprimer;
    try {
      await deleteConversation(conversationId);
      setDiscussions((actuelles) => actuelles.filter((d) => d.id !== conversationId));
      // Le panneau de droite compte les discussions du service : sans cette
      // relecture, il afficherait un total que la liste ne montre plus.
      rafraichirStats();
      showToast({
        type: "success",
        title: "Discussion supprimée",
        // Aucun chemin de restauration n'existe : ne pas le promettre.
        message: "Elle est dans la corbeille pour sept jours.",
      });
    } catch (e) {
      showToast({
        type: "error",
        title: "Suppression impossible",
        message: messageDe(e, "Cette discussion n'a pas pu être supprimée."),
      });
    } finally {
      // La confirmation se referme dans les deux cas : un échec ne doit pas
      // laisser une modale qui prétendrait que l'action est encore en attente.
      setDiscussionASupprimer(null);
    }
  };

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0, overflow: "hidden" }}>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
        <header style={{ padding: "20px 28px 0", borderBottom: "1px solid var(--thot-border-subtle)", flexShrink: 0 }}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
            <button
              type="button"
              onClick={() => navigate("/espaces")}
              aria-label="Retour aux espaces"
              style={{ background: "none", border: "none", cursor: "pointer", color: "var(--thot-text-muted)", padding: 4, marginTop: 2 }}
            >
              <ArrowLeft24Regular />
            </button>
            <span
              aria-hidden="true"
              style={{
                width: 56, height: 56, borderRadius: 14, display: "inline-flex", flexShrink: 0,
                alignItems: "center", justifyContent: "center",
                background: teinteEspace(id ?? ""), color: "var(--thot-text)",
              }}
            >
              <DocumentFolder24Regular style={{ fontSize: 28 }} />
            </span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <h1 style={{ fontSize: 20, fontWeight: 700 }}>{espace?.name ?? "Espace"}</h1>
                {/* L'état est RENDU, pas seulement transporté : un espace en
                    corbeille reste atteignable pour être restauré, et l'écran
                    doit pouvoir le dire plutôt que de laisser croire qu'il
                    existe normalement. */}
                {espace?.state === "trashed" && (
                  <span
                    style={{
                      fontSize: 11, fontWeight: 600, padding: "2px 8px",
                      borderRadius: "var(--thot-radius-pill)",
                      background: "var(--thot-surface-2)", color: "var(--thot-text-muted)",
                      border: "1px solid var(--thot-border)",
                    }}
                  >
                    En corbeille
                  </span>
                )}
              </div>
              {espace?.description && (
                <p style={{ fontSize: 13, color: "var(--thot-text-muted)", marginTop: 2 }}>{espace.description}</p>
              )}
            </div>
          </div>

          <nav role="tablist" aria-label="Vues de l'espace" style={{ display: "flex", gap: 20, marginTop: 16 }}>
            {ONGLETS.map((o) => {
              const actifOnglet = actif === o.cle;
              return (
                <button
                  key={o.cle}
                  type="button"
                  role="tab"
                  aria-selected={actifOnglet}
                  onClick={() => aller(o.cle)}
                  style={{
                    padding: "9px 2px", border: "none", background: "transparent", cursor: "pointer",
                    fontSize: 14, fontWeight: actifOnglet ? 600 : 400,
                    fontFamily: "var(--thot-font-family)",
                    color: actifOnglet ? "var(--thot-text)" : "var(--thot-text-muted)",
                    borderBottom: actifOnglet ? "2px solid var(--thot-gold)" : "2px solid transparent",
                  }}
                >
                  {o.libelle}
                </button>
              );
            })}
          </nav>
        </header>

        <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 24 }}>
          {erreur ? (
            <div role="alert" style={{
              display: "flex", alignItems: "center", gap: 10, padding: 16, borderRadius: 12,
              background: "var(--thot-danger-soft)", color: "var(--thot-danger)", fontSize: 14,
            }}>
              <ErrorCircle24Regular style={{ fontSize: 18 }} />
              <span style={{ flex: 1 }}>{erreur}</span>
              <button
                type="button"
                onClick={charger}
                style={{
                  flexShrink: 0, padding: "6px 12px", borderRadius: "var(--thot-radius-sm)",
                  border: "1px solid var(--thot-danger)", background: "transparent",
                  color: "var(--thot-danger)", cursor: "pointer", fontSize: 13,
                  fontWeight: 600, fontFamily: "var(--thot-font-family)",
                }}
              >
                Réessayer
              </button>
            </div>
          ) : chargement ? (
            <div style={{ padding: "64px 0", textAlign: "center", color: "var(--thot-text-muted)" }}>Chargement…</div>
          ) : actif === "discussions" ? (
            <>
              <VueDiscussions
                discussions={discussions}
                // `workspaceId` voyage dans l'etat de navigation : c'est lui qui
                // permet au bouton Retour de la discussion de revenir ICI.
                onOpen={(cid) => navigate(`/chat/${cid}`, { state: { workspaceId: id } })}
                onNouvelle={nouvelleDiscussion}
                enCorbeille={espace?.state === "trashed"}
                menuDiscussion={menuDiscussion}
                menuDiscussionRef={menuDiscussionRef}
                onBasculerMenu={(cid) =>
                  setMenuDiscussion((ouvert) => (ouvert === cid ? null : cid))
                }
                discussionRenommee={discussionRenommee}
                nouveauTitre={nouveauTitre}
                onNouveauTitre={setNouveauTitre}
                onOuvrirRenommage={ouvrirRenommage}
                onConfirmerRenommage={renommer}
                onAnnulerRenommage={annulerRenommage}
                discussionASupprimer={discussionASupprimer}
                onDemanderSuppression={demanderSuppression}
                onConfirmerSuppression={supprimer}
                onAnnulerSuppression={() => setDiscussionASupprimer(null)}
                onDemanderDeplacement={demanderDeplacement}
              />
              {discussionADeplacer && (
                <DeplacerDiscussionDialog
                  conversationId={discussionADeplacer.id}
                  conversationTitle={discussionADeplacer.title || "Sans titre"}
                  sourceWorkspaceId={id ?? null}
                  sourceWorkspaceName={espace?.name}
                  onClose={() => setDiscussionADeplacer(null)}
                  onMoved={() => {
                    setDiscussions((actuelles) =>
                      actuelles.filter((d) => d.id !== discussionADeplacer.id),
                    );
                    rafraichirStats();
                  }}
                />
              )}
            </>
          ) : actif === "depot" ? (
            <VueDepot
              workspaceId={id}
              enCorbeille={espace?.state === "trashed"}
              fichiers={depot?.files ?? []}
              masques={depot?.hidden ?? []}
              dossierPhysique={depot?.is_physical_folder === true}
              chargement={chargementDepot}
              erreur={erreurDepot}
              recharger={chargerDepot}
            />
          ) : (
            <VueMemoire stats={stats} workspaceId={id} onRefreshStats={rafraichirStats} />          )}
        </div>
      </div>

      <aside style={{ width: 320, borderLeft: "1px solid var(--thot-border)", padding: 24, overflow: "auto", flexShrink: 0 }}>
        <section>
          <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 14 }}>À propos de cet espace</h2>
          {/* `files` est l'inventaire RENDU dans l'onglet Dépôt, pas un second
              appel au service : deux appels mesureraient deux états d'un dépôt
              vivant, et l'écran afficherait deux chiffres contradictoires. */}
          <dl style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "10px 12px", fontSize: 13, margin: 0 }}>
            <dt style={{ color: "var(--thot-text-muted)" }}>Créé le</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>{formatDateLongue(espace?.created_at)}</dd>
            <dt style={{ color: "var(--thot-text-muted)" }}>Discussions</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>{formatNombre(stats?.discussions ?? 0)}</dd>
            <dt style={{ color: "var(--thot-text-muted)" }}>Fichiers</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>{formatNombre(fichiersDepot.length)}</dd>
            <dt style={{ color: "var(--thot-text-muted)" }}>Mémoire acceptée</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>{formatNombre(stats?.memoire ?? 0)}</dd>
            <dt style={{ color: "var(--thot-text-muted)" }}>Poids du dépôt</dt>
            {/* Aucun plafond n'affiche : aucun contrat n'en définit un. La
                maquette 05-3 écrit « 245 Mo / 10 Go », et le « 10 Go » n'a
                aucune source. On mesure, on n'impose pas. */}
            <dd style={{ margin: 0, textAlign: "right" }}>
              {formatOctets(poidsDepot)}{poidsDepotMinimal ? " au moins" : ""}
            </dd>
          </dl>
        </section>

        <section style={{ marginTop: 28 }}>
          <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>Mémoire de l'espace</h2>
          <p style={{ fontSize: 12, color: "var(--thot-text-muted)", lineHeight: 1.5, marginBottom: 12 }}>
            Seules les informations que vous avez validées sont comptées ici. Une fiche en attente
            n'est pas une mémoire : c'est une proposition.
          </p>
          <Repartition stats={stats} />
        </section>
      </aside>
    </div>
  );
}

function Repartition({ stats }: { stats: WorkspaceStats | null }) {
  const par = stats?.memoire_par_statut;
  const lignes = (["candidate", "accepted", "stale", "rejected"] as const).map((cle) => ({
    cle,
    libelle: { candidate: "En attente", accepted: "Acceptées", stale: "Périmées", rejected: "Refusées" }[cle],
    n: par?.[cle] ?? 0,
  }));
  const total = lignes.reduce((acc, l) => acc + l.n, 0);

  if (total === 0) {
    return (
      <p style={{ fontSize: 13, color: "var(--thot-text-faint)" }}>
        Aucune fiche pour l'instant. Rien n'entre en mémoire sans votre validation.
      </p>
    );
  }
  return (
    <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
      {lignes.filter((l) => l.n > 0).map((l) => (
        <li key={l.cle} style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
          <span style={{ color: l.cle === "accepted" ? "var(--thot-text)" : "var(--thot-text-muted)" }}>{l.libelle}</span>
          <span style={{ fontWeight: 600 }}>{l.n}</span>
        </li>
      ))}
    </ul>
  );
}

function VueDiscussions({
  discussions,
  onOpen,
  onNouvelle,
  enCorbeille,
  menuDiscussion,
  menuDiscussionRef,
  onBasculerMenu,
  discussionRenommee,
  nouveauTitre,
  onNouveauTitre,
  onOuvrirRenommage,
  onConfirmerRenommage,
  onAnnulerRenommage,
  discussionASupprimer,
  onDemanderSuppression,
  onConfirmerSuppression,
  onAnnulerSuppression,
  onDemanderDeplacement,
}: {
  discussions: WorkspaceConversation[];
  onOpen: (id: string) => void;
  onNouvelle: () => void;
  enCorbeille: boolean;
  menuDiscussion: string | null;
  menuDiscussionRef: React.RefObject<HTMLDivElement | null>;
  onBasculerMenu: (id: string) => void;
  discussionRenommee: string | null;
  nouveauTitre: string;
  onNouveauTitre: (titre: string) => void;
  onOuvrirRenommage: (id: string, titreActuel: string) => void;
  onConfirmerRenommage: (id: string) => void | Promise<void>;
  onAnnulerRenommage: () => void;
  discussionASupprimer: string | null;
  onDemanderSuppression: (id: string) => void;
  onConfirmerSuppression: () => void | Promise<void>;
  onAnnulerSuppression: () => void;
  onDemanderDeplacement: (discussion: WorkspaceConversation) => void;
}) {
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 16 }}>
        <button
          type="button"
          onClick={onNouvelle}
          // Un espace en corbeille refuse toute création côté serveur : laisser
          // le bouton actif ne ferait qu'annoncer un échec.
          disabled={enCorbeille}
          title={enCorbeille ? "Un espace en corbeille n'accepte pas de nouvelle discussion" : undefined}
          style={{
            display: "inline-flex", alignItems: "center", gap: 8, padding: "8px 16px",
            borderRadius: "var(--thot-radius-pill)", border: "none",
            background: "var(--thot-gold)", color: "#fff",
            cursor: enCorbeille ? "not-allowed" : "pointer",
            opacity: enCorbeille ? 0.5 : 1,
            fontSize: 14, fontWeight: 600, fontFamily: "var(--thot-font-family)",
          }}
        >
          <Add24Regular style={{ fontSize: 16 }} />
          Nouvelle discussion
        </button>
      </div>
      {discussions.length === 0 ? (
        <Vide
          titre="Aucune discussion"
          texte="Démarrez une discussion : elle sera rattachée à cet espace. Les fichiers que vous y déposez lui restent propres, et ne la suivent pas : pour partager des fichiers avec toutes les discussions, versez-les depuis l'onglet Dépôt."
        />
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 2 }}>
          {discussions.map((d) => {
            const titre = d.title || "Sans titre";
            const enRenommage = discussionRenommee === d.id;
            return (
              <li key={d.id}>
                {/* Le fond de survol vit sur le conteneur : le bouton principal
                    garde un fond transparent, sinon deux fonds se superposent. */}
                <div
                  ref={menuDiscussion === d.id ? menuDiscussionRef : undefined}
                  style={{
                    position: "relative", display: "flex", alignItems: "center", gap: 4,
                    padding: "4px 6px", borderRadius: 10,
                    transition: "var(--thot-transition-fast)",
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = "var(--thot-surface-2)"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                >
                  {enRenommage ? (
                    // Formulaire inline, dans un div : aucun bouton parent ne
                    // peut intercepter la saisie ni l'ouvrir au clavier.
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flex: 1, minWidth: 0, padding: "6px 4px" }}>
                      <span aria-hidden="true" style={{ display: "inline-flex", color: "var(--thot-text-faint)" }}>
                        <Chat24Regular />
                      </span>
                      <input
                        autoFocus
                        value={nouveauTitre}
                        onChange={(e) => onNouveauTitre(e.target.value)}
                        onKeyDown={(e) => {
                          // Entrée valide, Échap annule : deux clés, deux
                          // intentions, aucune conversion silencieuse.
                          if (e.key === "Enter") void onConfirmerRenommage(d.id);
                          else if (e.key === "Escape") onAnnulerRenommage();
                        }}
                        aria-label="Nouveau titre de la discussion"
                        style={{
                          flex: 1, minWidth: 0, padding: "4px 8px", fontSize: 14,
                          border: "1px solid var(--thot-gold)", borderRadius: 6,
                          background: "var(--thot-surface-subtle)", color: "var(--thot-text)",
                          fontFamily: "var(--thot-font-family)",
                        }}
                      />
                      <button
                        type="button"
                        onClick={() => void onConfirmerRenommage(d.id)}
                        aria-label="Valider le nouveau titre"
                        title="Valider"
                        style={{
                          padding: "4px 8px", fontSize: 12, border: "none",
                          background: "var(--thot-gold)", color: "#fff", borderRadius: 6,
                          cursor: "pointer", fontFamily: "var(--thot-font-family)",
                        }}
                      >
                        ✓
                      </button>
                      <button
                        type="button"
                        onClick={onAnnulerRenommage}
                        aria-label="Annuler le renommage"
                        title="Annuler"
                        style={{
                          padding: "4px 8px", fontSize: 12, border: "none",
                          background: "var(--thot-surface-2)", color: "var(--thot-text-muted)",
                          borderRadius: 6, cursor: "pointer", fontFamily: "var(--thot-font-family)",
                        }}
                      >
                        ✕
                      </button>
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() => onOpen(d.id)}
                        style={{
                          display: "flex", alignItems: "center", gap: 12, flex: 1, minWidth: 0,
                          textAlign: "left", padding: "8px 8px", borderRadius: 8, border: "none",
                          cursor: "pointer", background: "transparent",
                          fontFamily: "var(--thot-font-family)", color: "var(--thot-text)",
                        }}
                      >
                        <span aria-hidden="true" style={{ display: "inline-flex", color: "var(--thot-text-faint)" }}>
                          <Chat24Regular />
                        </span>
                        <span style={{ flex: 1, minWidth: 0, fontSize: 14, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {titre}
                        </span>
                        <span style={{ fontSize: 12, color: "var(--thot-text-faint)" }}>{formatRelatif(d.updated_at || d.created_at)}</span>
                      </button>
                      <div style={{ position: "relative", flexShrink: 0 }}>
                        <button
                          type="button"
                          aria-label={`Actions pour « ${titre} »`}
                          aria-expanded={menuDiscussion === d.id}
                          onClick={() => onBasculerMenu(d.id)}
                          style={{
                            display: "inline-flex", alignItems: "center", justifyContent: "center",
                            padding: 4, background: "none", border: "none", cursor: "pointer",
                            color: "var(--thot-text-muted)", borderRadius: 6,
                          }}
                        >
                          <MoreHorizontal24Regular />
                        </button>
                        {menuDiscussion === d.id && (
                          <div
                            role="group"
                            aria-label="Actions de la discussion"
                            style={{
                              position: "absolute", right: 0, top: "100%", marginTop: 4,
                              background: "var(--thot-surface-raised)", border: "1px solid var(--thot-border)",
                              borderRadius: 10, boxShadow: "var(--thot-shadow-float)",
                              zIndex: 20, minWidth: 140, overflow: "hidden",
                            }}
                          >
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); onOuvrirRenommage(d.id, d.title || ""); }}
                              style={{
                                display: "flex", alignItems: "center", gap: 8, width: "100%",
                                padding: "8px 12px", border: "none", background: "transparent",
                                cursor: "pointer", fontSize: 13, color: "var(--thot-text)",
                                fontFamily: "var(--thot-font-family)",
                              }}
                              onMouseEnter={(e) => { e.currentTarget.style.background = "var(--thot-surface-2)"; }}
                              onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                            >
                              <Edit24Regular style={{ fontSize: 14 }} /> Renommer
                            </button>
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); onDemanderDeplacement(d); }}
                              style={{
                                display: "flex", alignItems: "center", gap: 8, width: "100%",
                                padding: "8px 12px", border: "none", borderTop: "1px solid var(--thot-border-subtle)",
                                background: "transparent", cursor: "pointer", fontSize: 13, color: "var(--thot-text)",
                                fontFamily: "var(--thot-font-family)",
                              }}
                              onMouseEnter={(e) => { e.currentTarget.style.background = "var(--thot-surface-2)"; }}
                              onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                            >
                              <ArrowMove24Regular style={{ fontSize: 14 }} /> Déplacer vers un espace…
                            </button>
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); onDemanderSuppression(d.id); }}
                              style={{
                                display: "flex", alignItems: "center", gap: 8, width: "100%",
                                padding: "8px 12px", border: "none", borderTop: "1px solid var(--thot-border-subtle)",
                                background: "transparent", cursor: "pointer", fontSize: 13,
                                color: "var(--thot-danger)", fontFamily: "var(--thot-font-family)",
                              }}
                              onMouseEnter={(e) => { e.currentTarget.style.background = "var(--thot-danger-soft)"; }}
                              onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                            >
                              <Delete24Regular style={{ fontSize: 14 }} /> Supprimer
                            </button>
                          </div>
                        )}
                      </div>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {/* Le backend ne détruit rien : il met en corbeille sept jours. La phrase
          le dit pour que « Supprimer » ne se lise pas comme une perte. */}
      {discussionASupprimer !== null && (
        <div
          style={{
            position: "fixed", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
            background: "rgba(0,0,0,0.4)", zIndex: 100,
          }}
          onClick={onAnnulerSuppression}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="titre-suppression-discussion"
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "var(--thot-surface-raised)", borderRadius: 16, padding: 24,
              boxShadow: "var(--thot-shadow-float)", maxWidth: 360, width: "90%",
            }}
          >
            <h3 id="titre-suppression-discussion" style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>
              Supprimer cette discussion ?
            </h3>
            <p style={{ fontSize: 14, color: "var(--thot-text-muted)", marginBottom: 20 }}>
              Elle sera retirée des vues actives et placée dans la corbeille pendant sept jours.
            </p>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                type="button"
                onClick={onAnnulerSuppression}
                // Action non destructive : le focus entre dans le dialogue sans
                // exposer l'utilisateur au bouton qui supprime.
                autoFocus
                style={{
                  padding: "8px 16px", borderRadius: "var(--thot-radius-sm)",
                  border: "1px solid var(--thot-border)", background: "transparent",
                  cursor: "pointer", fontSize: 14, fontFamily: "var(--thot-font-family)",
                  color: "var(--thot-text)",
                }}
              >
                Annuler
              </button>
              <button
                type="button"
                onClick={() => void onConfirmerSuppression()}
                aria-label="Supprimer la discussion"
                style={{
                  padding: "8px 16px", borderRadius: "var(--thot-radius-sm)",
                  border: "none", background: "var(--thot-danger)",
                  cursor: "pointer", fontSize: 14, fontFamily: "var(--thot-font-family)",
                  color: "#fff",
                }}
              >
                Supprimer
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Le service refuse plus de huit pièces par appel : on verse par lots. */
const PAR_LOT = 8;

type SourcesDepot = {
  /** Chemins réels : le serveur lit le fichier, les octets ne traversent pas le pont. */
  chemins: string[];
  octets: Array<{ nom: string; octets: Uint8Array }>;
  /** Fichiers refusés AVANT lecture (taille) : dits à l'utilisateur, jamais silencieux. */
  refuses: string[];
};

/**
 * `kind` vient du service, qui l'a déduit de la SIGNATURE du fichier
 * (`attachments.py:103`) : un PDF est `pdf`, jamais `image`. Un type et son nom
 * affiché tiennent au même endroit, pour qu'ils ne puissent pas diverger.
 */
const KIND: Record<string, { libelle: string; icone: React.ReactNode }> = {
  pdf: { libelle: "PDF", icone: <DocumentPdf24Regular /> },
  image: { libelle: "Image", icone: <Image24Regular /> },
  sheet: { libelle: "Tableur", icone: <DataHistogram24Regular /> },
  text: { libelle: "Texte", icone: <DocumentText24Regular /> },
};

const KIND_INCONNU = { libelle: "Document", icone: <Document24Regular /> };

function decrireKind(kind: string): { libelle: string; icone: React.ReactNode } {
  return KIND[kind] ?? KIND_INCONNU;
}

/** [singulier, pluriel] — l'accord se décide ici, pas dans la phrase. */
const MOTIF_ECARTE: Record<DepotHiddenReason, [string, string]> = {
  supprime: ["supprimé", "supprimés"],
  sans_nom: ["sans nom affichable", "sans nom affichable"],
  lien_orphelin: ["lien sans document", "liens sans document"],
};

/**
 * La phrase qui dit ce que le dépôt N'AFFICHE pas.
 *
 * `hidden` compte pour l'écran, il ne s'affiche pas : y lire un `artifact_id`
 * mettrait un identifiant technique dans une phrase destinée à l'utilisateur. On
 * cite donc les MOTIFS, une fois chacun, et `null` pour un dépôt sain — un dépôt
 * sans anomalie n'a rien à dire de son propre silence.
 */
function phraseEcarte(masques: DepotHiddenFile[]): string | null {
  const total = masques.length;
  if (total === 0) return null;
  const motifs = [...new Set(masques.map((item) => item.reason))].map((raison) => {
    // Un motif hors vocabulaire se recopie tel quel : le jeter serait ironique, car
    // c'est lui la preuve que le dépôt cache encore autre chose.
    const libelle = MOTIF_ECARTE[raison];
    return libelle ? (total > 1 ? libelle[1] : libelle[0]) : raison;
  });
  return `${formatNombre(total)} document${total > 1 ? "s" : ""} ${
    total > 1 ? "ne sont pas affichés" : "n'est pas affiché"
  } dans ce dépôt : ${motifs.join(", ")}.`;
}

/** Le pont rejette avec une chaîne nue : ni `Error`, ni message vide. */
function messageDe(e: unknown, defaut: string): string {
  const message = e instanceof Error ? e.message : String(e ?? "");
  return message.trim() || defaut;
}

async function sourcesDe(fichiers: File[]): Promise<SourcesDepot> {
  const sources: SourcesDepot = { chemins: [], octets: [], refuses: [] };
  for (const fichier of fichiers) {
    const chemin = pathForFile(fichier);
    if (chemin) {
      sources.chemins.push(chemin);
    } else if (fichier.size > MAX_PICKER_ATTACHMENT_BYTES) {
      // `arrayBuffer()` matérialiserait un fichier que le service refusera :
      // la taille se vérifie AVANT la lecture, pas après.
      sources.refuses.push(`${fichier.name} : maximum 20 Mo`);
    } else {
      sources.octets.push({ nom: fichier.name, octets: new Uint8Array(await fichier.arrayBuffer()) });
    }
  }
  return sources;
}

/**
 * Verse des fichiers au dépôt : staging, puis promotion vers l'espace.
 *
 * Il n'existe pas de commande « ajouter au dépôt » : c'est
 * `attachments_prepare` qui décide de la portée d'un artefact, et qui refuse un
 * espace en corbeille. Le nom d'origine est renvoyé PAR LOT, parallèle aux
 * identifiants — sans lui, l'inventaire afficherait une empreinte.
 *
 * Un lot qui échoue n'annule pas les précédents : on s'arrête là, on rend
 * l'erreur, et le composant prévient de ce qui est effectivement entré. Écrire
 * l'inventaire à moitié serait un mensonge de plus.
 */
async function verserAuDepot(
  workspaceId: string,
  requestId: string,
  sources: SourcesDepot,
): Promise<{ deposes: number; rejets: string[]; echec: string | null }> {
  const ids: string[] = [];
  const noms: string[] = [];
  const rejets: string[] = [...sources.refuses];
  let deposes = 0;
  const retenir = (stage: DepotStage) => {
    for (const item of stage.items) {
      ids.push(item.stage_item_id);
      noms.push(item.name);
    }
    for (const refus of stage.rejected) rejets.push(refus.message ?? refus.name ?? "Fichier refusé");
  };
  try {
    for (let debut = 0; debut < sources.chemins.length; debut += PAR_LOT) {
      retenir(await stageDepotPaths(requestId, sources.chemins.slice(debut, debut + PAR_LOT)));
    }
    for (const source of sources.octets) {
      retenir(await stageDepotBytes(requestId, source.nom, source.octets));
    }
    for (let debut = 0; debut < ids.length; debut += PAR_LOT) {
      const reponse = await prepareDepotFiles(
        workspaceId,
        ids.slice(debut, debut + PAR_LOT),
        noms.slice(debut, debut + PAR_LOT),
        requestId,
      );
      deposes += reponse.artifact_ids.length;
    }
  } catch (e) {
    return { deposes, rejets, echec: messageDe(e, "Le dépôt a refusé ces fichiers.") };
  }
  return { deposes, rejets, echec: null };
}

/**
 * Les étiquettes d'ingestion, DÉRIVÉES du vocabulaire du service
 * (`backend/ingestion/etat.py` : depose, en_cours, extrait, tronque,
 * binaire_refuse, vide, echec, inconnu). depot.md §6 : « L'état d'ingestion
 * est visible et distingue disponibilité, traitement partiel et échec » —
 * trois familles, plus l'inconnu qui se dit sans se faire passer pour un
 * prêt. Toute valeur hors vocabulaire retombe sur `inconnu` : un écran qui
 * broderait un état inattendu inventerait une promesse.
 */
const ETIQUETTES_INGESTION: Record<string, { label: string; couleur: string; grimace: boolean }> = {
  depose: { label: "En attente d'extraction", couleur: "var(--thot-text-faint)", grimace: false },
  en_cours: { label: "Extraction en cours", couleur: "var(--thot-info)", grimace: false },
  extrait: { label: "Prêt", couleur: "var(--thot-success)", grimace: false },
  tronque: { label: "Indexé en partie", couleur: "var(--thot-info)", grimace: false },
  binaire_refuse: { label: "Non textuel", couleur: "var(--thot-text-faint)", grimace: false },
  vide: { label: "Aucun texte reconnu", couleur: "var(--thot-text-faint)", grimace: false },
  echec: { label: "Échec d'extraction", couleur: "var(--thot-danger)", grimace: true },
  inconnu: { label: "État inconnu", couleur: "var(--thot-text-faint)", grimace: false },
};

/** Les états qu'une relance serveur peut améliorer (miroir de `_ETATS_REESSAYABLES`). */
const ETATS_REESSAYABLES = new Set(["depose", "echec", "partiel", "tronque"]);

function etiquetteIngestion(f: DepotFile): { label: string; couleur: string; grimace: boolean; raison: string } {
  const etat = ETIQUETTES_INGESTION[f.ingest_state ?? "inconnu"] ?? ETIQUETTES_INGESTION["inconnu"];
  return { ...etat, raison: (f.ingest_reason ?? "").trim() };
}

function VueDepot({
  workspaceId,
  enCorbeille,
  fichiers,
  masques,
  dossierPhysique,
  chargement,
  erreur,
  recharger,
}: {
  workspaceId: string | undefined;
  enCorbeille: boolean;
  /** L'inventaire mesuré par la page : l'onglet ne le recalcule pas. */
  fichiers: DepotFile[];
  /** Ce que le service écarte du dépôt, motif par motif. */
  masques: DepotHiddenFile[];
  dossierPhysique: boolean;
  chargement: boolean;
  erreur: string | null;
  recharger: () => void;
}) {
  const [survol, setSurvol] = useState(false);
  const [enCours, setEnCours] = useState(false);
  const [enRetrait, setEnRetrait] = useState<string | null>(null);
  const [enReessai, setEnReessai] = useState<string | null>(null);
  const [renomme, setNomme] = useState<string | null>(null);
  const [brouillon, setBrouillon] = useState("");
  const [enRenommage, setEnRenommage] = useState<string | null>(null);
  const survolsRef = useRef(0);
  const choixRef = useRef<HTMLInputElement>(null);

  // Les trois états disent QUELLE écriture est en cours, et rien d'autre : c'est
  // leur réunion qui décide s'il y en a une. Un dépôt, un retrait et un
  // renommage ne peuvent donc pas se chevaucher, et il n'y a pas de quatrième
  // état à synchroniser.
  const ecriture = enCours || enRetrait !== null || enRenommage !== null;

  function ouvrirRenommage(f: DepotFile) {
    setNomme(f.artifact_id);
    setBrouillon(f.display_name);
  }

  // Fermeture sans écriture : aucun appel, aucun toast. Ne rien changer est un
  // état, pas une opération.
  function annulerNom() {
    setNomme(null);
    setBrouillon("");
  }

  async function validerNom(artifactId: string, nomActuel: string) {
    if (!workspaceId || enRenommage !== null) return;
    const nom = brouillon.trim();
    // Le nom identique ferme l'édition sans appel : renommer vers le nom courant
    // n'est pas une écriture, et le serveur n'a rien à décider ici.
    if (!nom || nom === nomActuel) {
      annulerNom();
      return;
    }
    setEnRenommage(artifactId);
    try {
      const reponse = await renommerDocumentDepot(workspaceId, artifactId, nom);
      annulerNom();
      showToast({
        type: "success",
        title: "Nom actualisé",
        message: `Le document s'appelle désormais « ${reponse.display_name} ». Son contenu n'a pas bougé, et le moteur de recherche connaît déjà le nouveau nom.`,
      });
      recharger();
    } catch (e) {
      showToast({
        type: "error",
        title: "Renommage impossible",
        message: messageDe(e, "Ce document garde son nom actuel."),
      });
    } finally {
      setEnRenommage(null);
    }
  }

  async function deposer(choisis: File[]) {
    if (!workspaceId || enCorbeille || ecriture || choisis.length === 0) return;
    setEnCours(true);
    try {
      const { deposes, rejets, echec } = await verserAuDepot(
        workspaceId,
        crypto.randomUUID(),
        await sourcesDe(choisis),
      );
      if (echec) {
        // UN SEUL message quand le versement s'arrête en cours de route :
        // « Ajout interrompu » suivi de « Ajouté au dépôt » se contredirait, et
        // l'utilisateur ne saurait pas ce qui est réellement entré. Le message
        // nomme donc la part entrée et la part perdue.
        showToast({
          type: "error",
          title: deposes > 0 ? "Dépôt partiel" : "Ajout interrompu",
          message: [
            deposes > 0
              ? `${formatNombre(deposes)} document${deposes > 1 ? "s ont" : " a"} été ${deposes > 1 ? "versés" : "versé"} dans le dépôt, le reste non`
              : null,
            ...rejets,
            echec,
          ]
            .filter(Boolean)
            .join(" · "),
        });
      } else {
        if (rejets.length > 0) {
          showToast({ type: "error", title: "Fichiers refusés", message: rejets.join(" · ") });
        }
        if (deposes > 0) {
          showToast({
            type: "success",
            title: "Ajouté au dépôt",
            message: `${formatNombre(deposes)} document${deposes > 1 ? "s" : ""}, lisible${deposes > 1 ? "s" : ""} par toutes les discussions de cet espace.`,
          });
        }
      }
      // UN SEUL chemin de rafraîchissement, et seulement si le dépôt a bougé :
      // l'inventaire relu remplace tous les compteurs affichés à l'écran.
      if (deposes > 0) recharger();
    } catch (e) {
      showToast({
        type: "error",
        title: "Lecture impossible",
        message: messageDe(e, "Ces fichiers n'ont pas pu être lus."),
      });
    } finally {
      setEnCours(false);
    }
  }

  async function retirer(artifactId: string, nom: string) {
    if (!workspaceId || ecriture) return;
    setEnRetrait(artifactId);
    try {
      const reponse = await unshareWorkspaceFile(workspaceId, artifactId);
      showToast({
        type: "success",
        title: reponse.already_removed ? "Déjà retiré du dépôt" : "Retiré du dépôt",
        message: `${nom} reste lisible par les discussions qui l'ont reçu : seul le dépôt s'en est défait.`,
      });
      recharger();
    } catch (e) {
      showToast({
        type: "error",
        title: "Retrait impossible",
        message: messageDe(e, "Ce document reste dans le dépôt."),
      });
    } finally {
      setEnRetrait(null);
    }
  }

  async function reessayer(f: DepotFile) {
    if (!workspaceId || enCorbeille || enReessai !== null || ecriture) return;
    setEnReessai(f.artifact_id);
    try {
      const reponse = await relancerExtraction(f.artifact_id);
      if (reponse.accepted) {
        showToast({
          type: "success",
          title: "Extraction relancée",
          message: `${f.display_name} est de nouveau en file. Son état se mettra à jour ici à la fin du traitement.`,
        });
        // On relit l'état réel : la relance a pu être acceptée alors qu'un autre
        // travail vient de se terminer, et l'écran ne doit pas deviner.
        recharger();
      } else {
        // `accepted: false` n'est PAS un échec : c'est le service qui dit qu'un
        // travail couvre déjà le document. Le message reprend sa raison, jamais
        // un « relancé » que rien ne prouve.
        showToast({
          type: "info",
          title: "Rien n'a été relancé",
          message:
            reponse.reason?.trim() ||
            "Ce document est déjà en file ou en cours d'extraction.",
        });
      }
    } catch (e) {
      showToast({
        type: "error",
        title: "Relance impossible",
        message: messageDe(e, "L'extraction de ce document n'a pas pu être relancée."),
      });
    } finally {
      setEnReessai(null);
    }
  }

  // `preventDefault` d'abord, toujours : c'est lui qui empêche le navigateur
  // d'ouvrir l'objet qu'on largue sur la page. Le survol, lui, ne s'allume que
  // pour un glisser de fichiers — un texte déposé ici n'a rien à y faire.
  const surEntree = (e: React.DragEvent) => {
    e.preventDefault();
    if (enCorbeille || !e.dataTransfer.types.includes("Files")) return;
    survolsRef.current += 1;
    setSurvol(true);
  };

  const surSortie = (e: React.DragEvent) => {
    e.preventDefault();
    survolsRef.current = Math.max(0, survolsRef.current - 1);
    if (survolsRef.current === 0) setSurvol(false);
  };

  const surDepot = (e: React.DragEvent) => {
    e.preventDefault();
    survolsRef.current = 0;
    setSurvol(false);
    void deposer(Array.from(e.dataTransfer.files ?? []));
  };

  const surChoix = (e: React.ChangeEvent<HTMLInputElement>) => {
    const choisis = Array.from(e.target.files ?? []);
    // Le champ se vide : sans cela, rechoisir le même fichier ne déclencherait
    // rien et l'écran semblerait ignorer le dépôt.
    e.target.value = "";
    void deposer(choisis);
  };

  const poids = fichiers.reduce((acc, f) => acc + (f.size_bytes || 0), 0);
  const poidsMinimal = fichiers.some((f) => !f.size_known);
  const avertissement = !chargement && !erreur ? phraseEcarte(masques) : null;
  const indisponible = enCorbeille || ecriture;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 6, flexWrap: "wrap" }}>
        <h2 style={{ fontSize: 15, fontWeight: 600 }}>Dépôt</h2>
        {!chargement && (
          <span style={{ fontSize: 12, color: "var(--thot-text-faint)" }}>
            {formatNombre(fichiers.length)} document{fichiers.length > 1 ? "s" : ""} · {formatOctets(poids)}{poidsMinimal ? " au moins" : ""}
          </span>
        )}
        {/* La phrase suit la réponse du service : elle ne peut pas annoncer
            « pas un dossier » le jour où le dépôt en serait un. */}
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--thot-text-faint)" }}>
          {dossierPhysique ? "Un dossier, en lecture seule" : "Vue de portée, pas un dossier"}
        </span>
      </div>
      <p style={{ fontSize: 13, color: "var(--thot-text-muted)", lineHeight: 1.55, marginBottom: 20, maxWidth: 640 }}>
        Le dépôt ne contient que les fichiers que vous y déposez. Chacun devient lisible par
        toutes les discussions de cet espace, dès maintenant. Un fichier déposé dans une
        discussion y reste, et n'apparaît pas ici. Ce n'est pas la mémoire : la mémoire retient
        des informations que vous avez validées, pas des fichiers.
      </p>

      <div
        onDragEnter={surEntree}
        onDragOver={surEntree}
        onDragLeave={surSortie}
        onDrop={surDepot}
        style={{
          display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap",
          padding: "14px 16px", marginBottom: 18, borderRadius: 12,
          border: `1px dashed ${survol ? "var(--thot-gold)" : "var(--thot-border)"}`,
          background: survol ? "var(--thot-gold-pale)" : "var(--thot-surface-subtle)",
          transition: "var(--thot-transition-fast)",
        }}
      >
        <span aria-hidden="true" style={{ display: "inline-flex", color: "var(--thot-text-faint)" }}>
          <ArrowUpload24Regular />
        </span>
        <span style={{ flex: 1, minWidth: 200, fontSize: 13, lineHeight: 1.5, color: "var(--thot-text-muted)" }}>
          {enCorbeille
            ? "Cet espace est en corbeille : il n'accepte plus de dépôt."
            : // Deux chemins, deux limites : le glisser ne transmet qu'un chemin,
              // alors que la sélection sérialise les octets dans la ligne JSON du
              // protocole, plafonnée à 256 Ko (`inference/ipc.py:26`). Annoncer
              // « vingt mégaoctets » serait faux pour l'un des deux.
              "Glissez vos fichiers ici, ou choisissez-les. PDF, images et tableurs. Glissez plutôt les gros fichiers : la sélection envoie les octets et reste limitée aux petits."}
        </span>
        <button
          type="button"
          onClick={() => choixRef.current?.click()}
          disabled={indisponible}
          title={enCorbeille ? "Un espace en corbeille n'accepte plus de dépôt" : undefined}
          style={{
            display: "inline-flex", alignItems: "center", gap: 8, padding: "8px 16px",
            borderRadius: "var(--thot-radius-pill)", border: "none",
            background: "var(--thot-gold)", color: "#fff",
            cursor: indisponible ? "not-allowed" : "pointer",
            opacity: indisponible ? 0.5 : 1,
            fontSize: 14, fontWeight: 600, fontFamily: "var(--thot-font-family)",
          }}
        >
          <Add24Regular style={{ fontSize: 16 }} />
          {enCours ? "Ajout en cours…" : "Ajouter des fichiers"}
        </button>
        <input
          ref={choixRef}
          type="file"
          multiple
          accept=".png,.jpg,.jpeg,.webp,.pdf,.xlsx,.xls,.ods,.csv"
          aria-label="Fichiers à déposer dans cet espace"
          disabled={indisponible}
          style={{ display: "none" }}
          onChange={surChoix}
        />
      </div>

      {/* Le dépôt n'affiche que des fichiers ; ce que le service en écarte ne se
          perd pas dans le silence — une ligne suffit, et elle ne nomme aucun
          identifiant technique. */}
      {avertissement && (
        <p style={{
          fontSize: 12, lineHeight: 1.55, color: "var(--thot-text-muted)",
          margin: "0 0 16px", paddingLeft: 10, borderLeft: "2px solid var(--thot-border)",
        }}>
          {avertissement}
        </p>
      )}

      {erreur ? (
        <div role="alert" style={{
          display: "flex", alignItems: "center", gap: 10, padding: 16, borderRadius: 12,
          background: "var(--thot-danger-soft)", color: "var(--thot-danger)", fontSize: 14,
        }}>
          <ErrorCircle24Regular style={{ fontSize: 18 }} />
          {erreur}
          <button
            type="button"
            onClick={recharger}
            style={{
              marginLeft: "auto", padding: "4px 12px", borderRadius: "var(--thot-radius-pill)",
              border: "1px solid var(--thot-danger)", background: "transparent",
              color: "var(--thot-danger)", cursor: "pointer",
              fontSize: 12, fontFamily: "var(--thot-font-family)",
            }}
          >
            Réessayer
          </button>
        </div>
      ) : chargement ? (
        <div style={{ padding: "40px 0", textAlign: "center", color: "var(--thot-text-muted)" }}>Chargement…</div>
      ) : fichiers.length === 0 ? (
        <Vide
          icone={<DocumentFolder24Regular style={{ fontSize: 40 }} />}
          titre="Dépôt vide"
          texte="Rien n'a encore été versé ici. Le dépôt accueille les fichiers que vous déposez dans cet espace, et rien d'autre : les fichiers d'une discussion lui restent propres, et la mémoire garde ce que vous avez validé."
          action={
            <button
              type="button"
              onClick={() => choixRef.current?.click()}
              disabled={indisponible}
              style={{
                display: "inline-flex", alignItems: "center", gap: 8, marginTop: 18, padding: "8px 18px",
                borderRadius: "var(--thot-radius-pill)", border: "none",
                background: "var(--thot-gold)", color: "#fff",
                cursor: indisponible ? "not-allowed" : "pointer",
                opacity: indisponible ? 0.5 : 1,
                fontSize: 14, fontWeight: 600, fontFamily: "var(--thot-font-family)",
              }}
            >
              <ArrowUpload24Regular style={{ fontSize: 16 }} />
              Déposer un premier document
            </button>
          }
        />
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
          {fichiers.map((f) => {
            const { libelle, icone } = decrireKind(f.kind);
            // Date ABSOLUE et non relative : `formatRelatif` écrit « lundi 20 mai »
            // sans l'année, et un dépôt vieux de deux ans se lirait comme
            // récent. Un inventaire se date en entier.
            // Une taille absente n'est pas nulle : « 0 o » afficherait un
            // fichier vide qui ne l'est pas, alors que le service dit ne pas
            // connaître le poids.
            const meta = [libelle, f.size_known ? formatOctets(f.size_bytes) : "taille inconnue", formatDateLongue(f.added_at)]
              .filter(Boolean)
              .join(" · ");
            return (
              <li
                key={f.artifact_id}
                style={{
                  display: "flex", alignItems: "center", gap: 12, padding: "11px 6px",
                  borderBottom: "1px solid var(--thot-border-subtle)",
                }}
              >
                <span aria-hidden="true" style={{ display: "inline-flex", color: "var(--thot-text-faint)" }}>
                  {icone}
                </span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  {renomme === f.artifact_id ? (
                    <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input
                        value={brouillon}
                        onChange={(e) => setBrouillon(e.target.value)}
                        onKeyDown={(e) => {
                          // Enter valide, Échap annule : deux clés, deux
                          // intentions, aucune conversion silencieuse.
                          if (e.key === "Enter") void validerNom(f.artifact_id, f.display_name);
                          else if (e.key === "Escape") annulerNom();
                        }}
                        aria-label={`Nouveau nom pour ${f.display_name}`}
                        autoFocus
                        style={{
                          flex: 1, minWidth: 120, padding: "4px 10px", fontSize: 14,
                          border: "1px solid var(--thot-gold)", borderRadius: 6,
                          background: "var(--thot-surface-subtle)", color: "var(--thot-text)",
                          fontFamily: "var(--thot-font-family)",
                        }}
                      />
                      <button
                        type="button"
                        onClick={() => void validerNom(f.artifact_id, f.display_name)}
                        disabled={enRenommage !== null || !brouillon.trim()}
                        style={{
                          padding: "5px 10px", borderRadius: "var(--thot-radius-pill)", border: "none",
                          background: "var(--thot-gold)", color: "#fff",
                          cursor: brouillon.trim() ? "pointer" : "not-allowed",
                          fontSize: 12, fontFamily: "var(--thot-font-family)",
                        }}
                      >
                        {enRenommage === f.artifact_id ? "Enregistrement…" : "Enregistrer"}
                      </button>
                      <button
                        type="button"
                        onClick={annulerNom}
                        disabled={enRenommage !== null}
                        aria-label="Annuler le renommage"
                        title="Annuler"
                        style={{
                          padding: "5px 10px", borderRadius: "var(--thot-radius-pill)",
                          border: "1px solid var(--thot-border)", background: "transparent",
                          color: "var(--thot-text-muted)",
                          cursor: enRenommage !== null ? "not-allowed" : "pointer",
                          fontSize: 12, fontFamily: "var(--thot-font-family)",
                        }}
                      >
                        Annuler
                      </button>
                    </span>
                  ) : (
                    <span style={{
                      display: "block", fontSize: 14, fontWeight: 500,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {f.display_name}
                    </span>
                  )}
                  <span style={{ display: "block", fontSize: 12, color: "var(--thot-text-faint)" }}>{meta}</span>
                  {(() => {
                    // L'état d'ingestion est dit EN TEXTE, pas par une seule
                    // pastille muette : la raison du service s'affiche pour un
                    // échec, et le tooltip porte la même raison pour les
                    // autres états (depot.md §3.2 : le traitement signale
                    // son état).
                    const etat = etiquetteIngestion(f);
                    return (
                      <>
                        <span
                          title={etat.raison || etat.label}
                          style={{
                            display: "inline-block", marginTop: 2, padding: "1px 8px",
                            borderRadius: "var(--thot-radius-pill)", fontSize: 11,
                            border: `1px solid ${etat.grimace ? etat.couleur : "var(--thot-border)"}`,
                            color: etat.couleur,
                          }}
                        >
                          {etat.label}
                        </span>
                        {etat.grimace && etat.raison && (
                          <span style={{
                            display: "block", fontSize: 12, color: "var(--thot-danger)",
                            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                          }}>
                            {etat.raison}
                          </span>
                        )}
                      </>
                    );
                  })()}
                </span>
                {(ETATS_REESSAYABLES.has(f.ingest_state ?? "") ||
                  f.ingest_state === "en_cours") && (
                  <button
                    type="button"
                    onClick={() => void reessayer(f)}
                    disabled={
                      f.ingest_state === "en_cours" ||
                      enReessai !== null ||
                      ecriture ||
                      enCorbeille
                    }
                    aria-label={`Réessayer l'extraction de ${f.display_name}`}
                    title={
                      f.ingest_state === "en_cours"
                        ? "Extraction déjà en cours"
                        : "Relancer l'extraction"
                    }
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px",
                      borderRadius: "var(--thot-radius-pill)", border: "1px solid var(--thot-border)",
                      background: "transparent", color: "var(--thot-text-muted)",
                      cursor:
                        f.ingest_state === "en_cours" || enReessai !== null || ecriture
                          ? "not-allowed"
                          : "pointer",
                      opacity:
                        f.ingest_state === "en_cours" ||
                        (enReessai !== null && enReessai !== f.artifact_id)
                          ? 0.4
                          : 1,
                      fontSize: 12, fontFamily: "var(--thot-font-family)",
                    }}
                  >
                    <ArrowClockwise24Regular style={{ fontSize: 13 }} />
                    {enReessai === f.artifact_id
                      ? "Relance…"
                      : f.ingest_state === "en_cours"
                        ? "En cours"
                        : "Réessayer"}
                  </button>
                )}
                {renomme !== f.artifact_id && (
                  <button
                    type="button"
                    onClick={() => ouvrirRenommage(f)}
                    disabled={ecriture}
                    aria-label={`Renommer ${f.display_name}`}
                    title="Renommer"
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px",
                      borderRadius: "var(--thot-radius-pill)", border: "1px solid var(--thot-border)",
                      background: "transparent", color: "var(--thot-text-muted)",
                      cursor: ecriture ? "not-allowed" : "pointer",
                      opacity: ecriture && renomme !== f.artifact_id ? 0.4 : 1,
                      fontSize: 12, fontFamily: "var(--thot-font-family)",
                    }}
                  >
                    <Pen24Regular style={{ fontSize: 13 }} />
                    Renommer
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => retirer(f.artifact_id, f.display_name)}
                  disabled={enRetrait !== null}
                  aria-label={`Retirer ${f.display_name} du dépôt`}
                  title="Retirer du dépôt"
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px",
                    borderRadius: "var(--thot-radius-pill)", border: "1px solid var(--thot-border)",
                    background: "transparent", color: "var(--thot-text-muted)",
                    cursor: enRetrait !== null ? "not-allowed" : "pointer",
                    opacity: enRetrait !== null && enRetrait !== f.artifact_id ? 0.4 : 1,
                    fontSize: 12, fontFamily: "var(--thot-font-family)",
                  }}
                >
                  <Dismiss24Regular style={{ fontSize: 13 }} />
                  {enRetrait === f.artifact_id ? "Retrait…" : "Retirer"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Statuts réels de la mémoire (`05_classe_memoire.md:45-49`). */
type FiltreStatut = "tous" | "accepted" | "candidate" | "stale" | "rejected";

const LIBELLES_FILTRE_STATUT: Record<FiltreStatut, string> = {
  tous: "Tous les statuts",
  accepted: "Acceptées",
  candidate: "En attente",
  stale: "Périmées",
  rejected: "Refusées",
};

/** Ce que le service rend sous `rafraichissement` après un entretien. */
type RafraichissementMemoire = {
  statut?: string;
  raison?: string;
  fiches?: { crees?: number; mis_a_jour?: number };
  synthese?: { statut?: string };
};

type ReponseForcage = {
  elements?: MemoryElement[];
  synthese?: MemorySynthesis | null;
  rafraichissement?: RafraichissementMemoire;
};

function raisonPasEncore(raison?: string): string {
  if (raison === "activite_recente") {
    return "L'espace a eu de l'activité récemment ; la mémoire attend un moment calme.";
  }
  if (raison === "seuil_tours") {
    return "Le seuil de tours n'est pas encore atteint.";
  }
  return "Les conditions de rafraîchissement ne sont pas encore réunies.";
}

/** Une seule lecture du statut rendu : l'écran ne devine jamais ce qui s'est passé. */
function messageForcage(rafraichissement?: RafraichissementMemoire): {
  type: "success" | "info";
  title: string;
  message: string;
} {
  const statut = rafraichissement?.statut;
  if (statut === "effectue") {
    const crees = rafraichissement?.fiches?.crees ?? 0;
    const misAJour = rafraichissement?.fiches?.mis_a_jour ?? 0;
    const statutSynthese = rafraichissement?.synthese?.statut;
    const synthese =
      statutSynthese === "creee"
        ? " La synthèse a été mise à jour."
        : statutSynthese === "inchangee"
          ? " La synthèse est inchangée."
          : "";
    return {
      type: "success",
      title: "Mémoire entretenue",
      message: `${crees} fiche(s) créée(s), ${misAJour} mise(s) à jour.${synthese}`,
    };
  }
  if (statut === "rien_a_promouvoir") {
    return { type: "info", title: "Mémoire à jour", message: "Aucun nouveau fait à retenir." };
  }
  if (statut === "differe") {
    return {
      type: "info",
      title: "Entretien différé",
      message: "Une inférence est en cours ; réessayez quand elle sera terminée.",
    };
  }
  if (statut === "pas_encore") {
    return { type: "info", title: "Pas encore", message: raisonPasEncore(rafraichissement?.raison) };
  }
  return {
    type: "info",
    title: "Mémoire",
    message: "Le service n'a pas précisé ce qu'il a fait.",
  };
}

function VueMemoire({
  stats,
  workspaceId,
  onRefreshStats,
}: {
  stats: WorkspaceStats | null;
  workspaceId?: string | null;
  onRefreshStats?: () => void;
}) {
  const [elements, setElements] = useState<MemoryElement[]>([]);
  const [synthese, setSynthese] = useState<MemorySynthesis | null>(null);
  const [entretien, setEntretien] = useState<MemoryEntretien | null>(null);
  const [chargement, setChargement] = useState<boolean>(false);
  const [enCours, setEnCours] = useState<boolean>(false);
  const { memory } = useInferenceActivity();
  // Le ref double le state : deux clics dans le même tour de boucle ne doivent
  // pas lancer deux entretiens concurrents avant le premier rendu.
  const enCoursRef = useRef(false);
  const [filtreTexte, setFiltreTexte] = useState<string>("");
  const [filtreStatut, setFiltreStatut] = useState<FiltreStatut>("tous");
  const [edition, setEdition] = useState<{
    entryId: string;
    contenu: string;
    original: string;
    version?: number;
  } | null>(null);
  const [enregistrement, setEnregistrement] = useState<string | null>(null);
  const [confirmationSuppression, setConfirmationSuppression] = useState<string | null>(null);
  const [suppressionEnCours, setSuppressionEnCours] = useState<string | null>(null);

  /** Relit le snapshot du service : aucune mutation locale n'est jamais affichée. */
  const recharger = useCallback(async () => {
    if (!workspaceId) return;
    const snapshot = await chargerMemoireEspace(workspaceId);
    setElements(snapshot.elements);
    setSynthese(snapshot.synthese);
    setEntretien(snapshot.entretien ?? null);
  }, [workspaceId]);

  // Quand l'entretien se termine, le snapshot lu avant sa fin est périmé : on le
  // relit. Le provider nomme l'espace ; sans identifiant, on recharge quand même
  // car l'événement vient de la page courante.
  const previousMemoryStatusRef = useRef(memory.status);
  useEffect(() => {
    const previous = previousMemoryStatusRef.current;
    previousMemoryStatusRef.current = memory.status;
    if (previous !== "running" || memory.status !== "idle") return;
    if (memory.workspaceId && memory.workspaceId !== workspaceId) return;
    void recharger().catch(() => {});
  }, [memory.status, memory.workspaceId, workspaceId, recharger]);

  useEffect(() => {
    if (!workspaceId) {
      // Pas d'écriture ni d'état « chargé » sans espace : les boutons restent
      // désactivés et la liste vide.
      setElements([]);
      setSynthese(null);
      setEntretien(null);
      setChargement(false);
      return;
    }
    // Lecture seule, garde `actif` : StrictMode monte deux fois et le second
    // montage ne doit pas laisser une réponse périmée écrire l'écran.
    let actif = true;
    setChargement(true);
    chargerMemoireEspace(workspaceId)
      .then((snapshot) => {
        if (!actif) return;
        setElements(snapshot.elements);
        setSynthese(snapshot.synthese);
        setEntretien(snapshot.entretien ?? null);
      })
      .catch(() => {
        if (!actif) return;
        setElements([]);
        setSynthese(null);
        setEntretien(null);
      })
      .finally(() => {
        if (actif) setChargement(false);
      });
    return () => {
      actif = false;
    };
  }, [workspaceId]);

  async function forcerMemoire() {
    if (!workspaceId || enCoursRef.current) return;
    enCoursRef.current = true;
    setEnCours(true);
    try {
      const reponse = (await forcerMemoireEspace(workspaceId)) as ReponseForcage;
      await recharger();
      onRefreshStats?.();
      showToast(messageForcage(reponse?.rafraichissement));
    } catch {
      showToast({
        type: "error",
        title: "Entretien impossible",
        message: "La mémoire n'a pas pu être entretenue. Réessayez.",
      });
    } finally {
      enCoursRef.current = false;
      setEnCours(false);
    }
  }

  function ouvrirEdition(el: MemoryElement) {
    setConfirmationSuppression(null);
    setEdition({
      entryId: el.entry_id,
      contenu: el.content,
      original: el.content,
      version: el.version,
    });
  }

  async function enregistrerCorrection() {
    if (!workspaceId || !edition || enregistrement !== null) return;
    const contenu = edition.contenu.trim();
    // Contenu vide ou identique : rien à écrire. L'interface ferme l'édition
    // sans appeler le service, ce qui évite une version incrémentée pour rien.
    if (!contenu || contenu === edition.original.trim()) {
      setEdition(null);
      return;
    }
    setEnregistrement(edition.entryId);
    try {
      const reponse = (await corrigerMemoireEspace(
        workspaceId,
        edition.entryId,
        contenu,
        edition.version,
      )) as {
        ok?: boolean;
        statut?: string;
        correction?: { ok?: boolean; code?: string };
      };
      // L'écriture hors entretien est refusée quand le verrou mémoire est tenu :
      // rien n'a été écrit, la saisie reste ouverte pour un nouvel essai.
      if (reponse?.ok === false && reponse?.statut === "differe") {
        showToast({
          type: "error",
          title: "Correction impossible",
          message: "Entretien mémoire en cours, réessayez.",
        });
        return;
      }
      const correction = reponse?.correction;
      if (correction?.ok === false || correction?.code === "REVISION_CONFLICT") {
        // Conflit de révision : rien n'a été écrit. On relit le snapshot, mais
        // l'éditeur reste OUVERT pour que l'utilisateur voie la version reçue.
        await recharger().catch(() => {});
        onRefreshStats?.();
        showToast({
          type: "error",
          title: "Correction impossible",
          message: "La fiche a changé entre-temps. Relisez-la avant de réessayer.",
        });
        return;
      }
      await recharger();
      onRefreshStats?.();
      setEdition(null);
      showToast({
        type: "success",
        title: "Fiche corrigée",
        message: "La mémoire a été mise à jour.",
      });
    } catch {
      // Une erreur typée (conflit levé par l'IPC) suit le même chemin : le
      // snapshot est relu et la saisie n'est pas perdue.
      await recharger().catch(() => {});
      onRefreshStats?.();
      showToast({
        type: "error",
        title: "Correction impossible",
        message: "La fiche n'a pas pu être corrigée. Relisez-la avant de réessayer.",
      });
    } finally {
      setEnregistrement(null);
    }
  }

  async function supprimerFiche(entryId: string) {
    if (!workspaceId || suppressionEnCours !== null) return;
    setSuppressionEnCours(entryId);
    try {
      const reponse = (await supprimerMemoireEspace(workspaceId, entryId)) as {
        ok?: boolean;
        statut?: string;
        suppression?: { ok?: boolean; statut?: string };
      };
      const differee =
        (reponse?.ok === false && reponse?.statut === "differe")
        || (reponse?.suppression?.ok === false && reponse?.suppression?.statut === "differe");
      if (differee) {
        // Rien n'a été supprimé : la fiche reste affichée, la confirmation
        // reste ouverte et l'utilisateur peut réessayer après l'entretien.
        showToast({
          type: "error",
          title: "Suppression impossible",
          message: "Entretien mémoire en cours, réessayez.",
        });
        return;
      }
      await recharger();
      onRefreshStats?.();
      setConfirmationSuppression(null);
      showToast({
        type: "success",
        title: "Fiche supprimée",
        message: "Elle ne sera pas recréée automatiquement à partir du même contenu.",
      });
    } catch {
      showToast({
        type: "error",
        title: "Suppression impossible",
        message: "La fiche n'a pas pu être supprimée. Réessayez.",
      });
    } finally {
      setSuppressionEnCours(null);
    }
  }

  const recherche = filtreTexte.trim().toLowerCase();
  const elementsFiltres = elements.filter((el) => {
    if (filtreStatut !== "tous" && (el.status ?? "") !== filtreStatut) return false;
    if (!recherche) return true;
    return `${el.content} ${el.origin ?? ""} ${el.origin_context ?? ""}`
      .toLowerCase()
      .includes(recherche);
  });
  const filtreActif = filtreStatut !== "tous" || recherche !== "";

  // Le verrou mémoire est global : le forçage est donc désactivé même quand
  // l'entretien en cours appartient à un autre espace. La ligne de statut, elle,
  // ne s'affiche que pour l'espace courant.
  const memoireVerrouillee = memory.status === "running";
  const entretienEnCours =
    memory.status === "running" && memory.workspaceId === workspaceId;
  const boutonDesactive = enCours || chargement || !workspaceId || memoireVerrouillee;
  const actionFicheDesactivee =
    enCours || enregistrement !== null || suppressionEnCours !== null || !workspaceId;
  const titreForcer = !workspaceId
    ? "Espace inconnu : l'entretien est indisponible"
    : memoireVerrouillee
      ? "Entretien mémoire en cours"
      : undefined;

  const statutEntretien = (() => {
    if (entretienEnCours) return "Entretien mémoire en cours…";
    if (!entretien) return null;
    if (entretien.etat === "en_cours") return "Entretien mémoire en cours…";
    if (entretien.etat === "differe") return "Entretien différé : une inférence est en cours.";
    if (entretien.etat === "a_jour") return "À jour.";
    if (entretien.etat !== "en_attente") return null;
    const base = `En attente — ${entretien.tours}/${entretien.seuil_tours} tours.`;
    const activiteRecente =
      entretien.inactivite_s !== null && entretien.inactivite_s < entretien.seuil_inactivite_s;
    return activiteRecente
      ? `${base} Activité récente ; l'entretien partira après 5 min d'inactivité.`
      : `${base} Prêt à démarrer.`;
  })();

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 6 }}>
        <h2 style={{ fontSize: 15, fontWeight: 600 }}>Mémoire</h2>
        <span style={{ fontSize: 12, color: "var(--thot-text-faint)" }}>
          {formatNombre(stats?.memoire ?? 0)} acceptée{stats?.memoire === 1 ? "" : "s"}
        </span>
        <button
          type="button"
          onClick={forcerMemoire}
          disabled={boutonDesactive}
          title={titreForcer}
          style={{
            marginLeft: "auto",
            padding: "6px 10px",
            fontSize: 12,
            borderRadius: "var(--thot-radius-sm, 6px)",
            border: "1px solid var(--thot-border)",
            background: "var(--thot-surface, transparent)",
            color: "var(--thot-text)",
            cursor: boutonDesactive ? "not-allowed" : "pointer",
            opacity: boutonDesactive ? 0.5 : 1,
            fontFamily: "var(--thot-font-family)",
          }}
        >
          {enCours ? "Entretien en cours…" : "Forcer la mémoire"}
        </button>
      </div>
      <p style={{ fontSize: 13, color: "var(--thot-text-muted)", lineHeight: 1.55, marginBottom: 20, maxWidth: 640 }}>
        La mémoire est alimentée automatiquement à partir de vos échanges et de leur provenance.
        Vous pouvez corriger ou supprimer une fiche ; une fiche supprimée ne sera pas recréée
        automatiquement à partir du même contenu.
      </p>
      {statutEntretien && (
        <p role="status" style={{ fontSize: 13, color: "var(--thot-text-muted)", marginBottom: 12 }}>
          {statutEntretien}
        </p>
      )}
      <Repartition stats={stats} />

      {synthese && (
        <section style={{ marginTop: 20 }}>
          <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Synthèse d'espace</h3>
          <div
            style={{
              border: "1px solid var(--thot-border)",
              borderRadius: "var(--thot-radius-md, 8px)",
              padding: "10px 12px",
              fontSize: 13,
              lineHeight: 1.55,
              whiteSpace: "pre-line",
            }}
          >
            {synthese.content}
          </div>
          <p style={{ fontSize: 11, color: "var(--thot-text-faint)", marginTop: 4 }}>
            v{synthese.version} · {formatDateLongue(synthese.created_at)}
          </p>
        </section>
      )}

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 8,
          marginTop: 20,
          marginBottom: 10,
        }}
      >
        <input
          type="search"
          aria-label="Filtrer par contenu ou origine"
          placeholder="Filtrer par contenu ou origine…"
          value={filtreTexte}
          onChange={(e) => setFiltreTexte(e.target.value)}
          style={{
            flex: "1 1 220px",
            minWidth: 180,
            padding: "6px 10px",
            fontSize: 12,
            borderRadius: "var(--thot-radius-sm, 6px)",
            border: "1px solid var(--thot-border)",
            background: "var(--thot-surface, transparent)",
            color: "var(--thot-text)",
            fontFamily: "var(--thot-font-family)",
          }}
        />
        <select
          aria-label="Filtrer par statut"
          value={filtreStatut}
          onChange={(e) => setFiltreStatut(e.target.value as FiltreStatut)}
          style={{
            padding: "6px 10px",
            fontSize: 12,
            borderRadius: "var(--thot-radius-sm, 6px)",
            border: "1px solid var(--thot-border)",
            background: "var(--thot-surface, transparent)",
            color: "var(--thot-text)",
            fontFamily: "var(--thot-font-family)",
          }}
        >
          {(Object.keys(LIBELLES_FILTRE_STATUT) as FiltreStatut[]).map((statut) => (
            <option key={statut} value={statut}>
              {LIBELLES_FILTRE_STATUT[statut]}
            </option>
          ))}
        </select>
        <span aria-live="polite" style={{ fontSize: 12, color: "var(--thot-text-faint)" }}>
          {formatNombre(elementsFiltres.length)} fiche{elementsFiltres.length === 1 ? "" : "s"}
        </span>
      </div>

      <div>
        {chargement && (
          <p style={{ fontSize: 12, color: "var(--thot-text-faint)" }}>Chargement de la mémoire…</p>
        )}
        {!chargement && elementsFiltres.length > 0 && (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
            {elementsFiltres.map((el) => (
              <li
                key={el.entry_id}
                style={{
                  border: "1px solid var(--thot-border)",
                  borderRadius: "var(--thot-radius-md, 8px)",
                  padding: "10px 12px",
                }}
              >
                {edition?.entryId === el.entry_id ? (
                  <div>
                    <textarea
                      aria-label="Contenu corrigé de la fiche"
                      value={edition.contenu}
                      onChange={(e) =>
                        setEdition((courant) =>
                          courant ? { ...courant, contenu: e.target.value } : courant,
                        )
                      }
                      rows={3}
                      style={{
                        width: "100%",
                        padding: "8px 10px",
                        fontSize: 13,
                        lineHeight: 1.5,
                        borderRadius: "var(--thot-radius-sm, 6px)",
                        border: "1px solid var(--thot-border)",
                        background: "var(--thot-surface, transparent)",
                        color: "var(--thot-text)",
                        fontFamily: "var(--thot-font-family)",
                        resize: "vertical",
                      }}
                    />
                    <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                      <button
                        type="button"
                        onClick={enregistrerCorrection}
                        disabled={
                          enregistrement !== null ||
                          !edition.contenu.trim() ||
                          edition.contenu.trim() === edition.original.trim()
                        }
                        style={styleBoutonFiche(
                          enregistrement !== null ||
                            !edition.contenu.trim() ||
                            edition.contenu.trim() === edition.original.trim(),
                        )}
                      >
                        {enregistrement === el.entry_id ? "Enregistrement…" : "Enregistrer"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setEdition(null)}
                        disabled={enregistrement !== null}
                        style={styleBoutonFiche(enregistrement !== null)}
                      >
                        Annuler
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5 }}>{el.content}</p>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 6, fontSize: 11, color: "var(--thot-text-faint)" }}>
                      <span>{el.category ?? "—"}</span>
                      <span>{el.affirmation_status ?? el.status ?? "—"}</span>
                      <span>{el.created_at ?? "—"}</span>
                      <span>{el.origin_context ?? el.origin ?? "—"}</span>
                      <span>{el.freshness ?? "—"}</span>
                      <span>{el.sources_count ?? 0} source(s)</span>
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 8 }}>
                      <button
                        type="button"
                        onClick={() => ouvrirEdition(el)}
                        disabled={actionFicheDesactivee}
                        style={styleBoutonFiche(actionFicheDesactivee)}
                      >
                        <Pen24Regular style={{ fontSize: 13 }} />
                        Corriger
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmationSuppression(el.entry_id)}
                        disabled={actionFicheDesactivee}
                        style={styleBoutonFiche(actionFicheDesactivee)}
                      >
                        <Dismiss24Regular style={{ fontSize: 13 }} />
                        Supprimer
                      </button>
                    </div>
                    {confirmationSuppression === el.entry_id && (
                      <div
                        role="alert"
                        style={{
                          display: "flex",
                          flexWrap: "wrap",
                          alignItems: "center",
                          gap: 8,
                          marginTop: 10,
                          padding: "8px 10px",
                          border: "1px solid var(--thot-danger)",
                          borderRadius: "var(--thot-radius-sm, 6px)",
                          fontSize: 12,
                        }}
                      >
                        <span style={{ flex: "1 1 260px" }}>
                          Supprimer définitivement ? Elle ne sera pas recréée automatiquement à partir du même contenu.
                        </span>
                        <button
                          type="button"
                          onClick={() => supprimerFiche(el.entry_id)}
                          disabled={suppressionEnCours !== null}
                          style={styleBoutonFiche(suppressionEnCours !== null)}
                        >
                          {suppressionEnCours === el.entry_id ? "Suppression…" : "Confirmer"}
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmationSuppression(null)}
                          disabled={suppressionEnCours !== null}
                          style={styleBoutonFiche(suppressionEnCours !== null)}
                        >
                          Annuler
                        </button>
                      </div>
                    )}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
        {!chargement && elementsFiltres.length === 0 && (
          <p style={{ fontSize: 12, color: "var(--thot-text-faint)", marginTop: 4, maxWidth: 640, lineHeight: 1.55 }}>
            {filtreActif
              ? "Aucune fiche ne correspond à ce filtre."
              : "Aucune fiche de mémoire pour le moment. Elle se remplira automatiquement après quelques échanges, ou utilisez « Forcer la mémoire »."}
          </p>
        )}
      </div>
      <p style={{ fontSize: 12, color: "var(--thot-text-faint)", marginTop: 18, maxWidth: 640 }}>
        Les compteurs ci-dessus sont réels et proviennent de la base.
      </p>
    </div>
  );
}

function styleBoutonFiche(desactive: boolean): React.CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "5px 10px",
    borderRadius: "var(--thot-radius-pill)",
    border: "1px solid var(--thot-border)",
    background: "transparent",
    color: "var(--thot-text-muted)",
    cursor: desactive ? "not-allowed" : "pointer",
    opacity: desactive ? 0.4 : 1,
    fontSize: 12,
    fontFamily: "var(--thot-font-family)",
  };
}

function Vide({
  titre,
  texte,
  icone,
  action,
}: {
  titre: string;
  texte: string;
  /** L'icône par défaut est celle de la mémoire : un état vide qui emprunte
   *  l'emblème d'un autre onglet ment sur ce qu'il annonce. */
  icone?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", padding: "56px 20px" }}>
      <span aria-hidden="true" style={{ color: "var(--thot-text-faint)", marginBottom: 16 }}>
        {icone ?? <Brain24Regular style={{ fontSize: 40 }} />}
      </span>
      <h3 style={{ fontSize: 17, fontWeight: 600, marginBottom: 8 }}>{titre}</h3>
      <p style={{ fontSize: 13, color: "var(--thot-text-muted)", maxWidth: 460, lineHeight: 1.55 }}>{texte}</p>
      {action}
    </div>
  );
}

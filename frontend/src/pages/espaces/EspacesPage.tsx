// pages/espaces/EspacesPage.tsx — accueil des espaces (maquette 05-1)
//
// Ce que cet écran est, et ce qu'il n'est pas : une VUE de conteneurs. Il ne
// présente aucun fichier, aucun chemin, aucun quota. Un espace ne possède aucun
// octet (C17), donc les seuls chiffres qu'il peut afficher sont des COMPTEURS
// RECALCULÉS par le service, jamais des colonnes stockées
// (`01_classe_espace.md:66-68`) — une carte ne ment donc pas après une
// suppression.
//
// Les trois écarts assumés avec la maquette sont documentés dans `format.ts` :
// « Mémoire » compte les fiches acceptées et non un statut « Active » inexistant,
// aucun plafond n'est affiché, et rien n'est copiable comme un chemin.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Add24Regular,
  Chat24Regular,
  DocumentFolder24Regular,
  Document24Regular,
  ErrorCircle24Regular,
  Brain24Regular,
  Star24Filled,
} from "@fluentui/react-icons";
import { SearchInput } from "../../components/ui/SearchInput";
import { showToast } from "../../components/ui/Toast";
import {
  createWorkspace,
  listWorkspaces,
  type Workspace,
} from "../../lib/ipcBridge";
import { formatNombre, teinteEspace } from "./format";

/** Statuts de mémoire réels, dans l'ordre où ils se lisent. Ni « active », ni « vide ». */
const STATUTS = [
  { cle: "candidate", libelle: "candidates" },
  { cle: "accepted", libelle: "acceptées" },
  { cle: "stale", libelle: "périmées" },
  { cle: "rejected", libelle: "refusées" },
] as const;

export function EspacesPage() {
  const navigate = useNavigate();
  const [espaces, setEspaces] = useState<Workspace[]>([]);
  const [chargement, setChargement] = useState(true);
  const [erreur, setErreur] = useState<string | null>(null);
  const [recherche, setRecherche] = useState("");
  const [nouveau, setNouveau] = useState(false);
  const [nom, setNom] = useState("");
  const [description, setDescription] = useState("");
  const [creation, setCreation] = useState(false);

  const charger = useCallback(() => {
    setChargement(true);
    listWorkspaces()
      .then((entres) => {
        setEspaces(entres);
        setErreur(null);
      })
      .catch(() => {
        // Une base injoignable ne doit pas ressembler à « aucun espace » : on le
        // dit, sinon l'écran vide est lu comme une perte de données.
        setErreur("Impossible de lire les espaces.");
        setEspaces([]);
      })
      .finally(() => setChargement(false));
  }, []);

  useEffect(charger, [charger]);

  const filtres = useMemo(() => {
    const q = recherche.trim().toLowerCase();
    if (!q) return espaces;
    return espaces.filter((e) => {
      const nomEspace = e.name.toLowerCase();
      const desc = (e.description ?? "").toLowerCase();
      return nomEspace.includes(q) || desc.includes(q);
    });
  }, [espaces, recherche]);

  async function creer() {
    const taille = nom.trim();
    if (!taille || creation) return;
    setCreation(true);
    try {
      const espace = await createWorkspace(taille, description);
      setNouveau(false);
      setNom("");
      setDescription("");
      showToast({ type: "success", title: "Espace créé", message: espace.name });
      navigate(`/espaces/${espace.id}`);
    } catch {
      showToast({ type: "error", title: "Création impossible", message: "Vérifie le nom." });
    } finally {
      setCreation(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "auto", padding: "32px 32px 40px" }}>
      <div style={{ width: "100%", maxWidth: 1180, margin: "0 auto", display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 24, flexWrap: "wrap" }}>
          <div>
            <h1 style={{ fontSize: "var(--thot-text-page-title)", fontWeight: 700, marginBottom: 6 }}>Espaces</h1>
            <p style={{ fontSize: 14, color: "var(--thot-text-muted)" }}>
              Organisez vos projets, discussions, fichiers et mémoires.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <SearchInput
              placeholder="Rechercher un espace…"
              width={300}
              value={recherche}
              onChange={setRecherche}
            />
            <button
              type="button"
              onClick={() => setNouveau(true)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 8, padding: "9px 18px",
                borderRadius: "var(--thot-radius-pill)", border: "none",
                background: "var(--thot-gold)", color: "#fff", cursor: "pointer",
                fontSize: 14, fontWeight: 600, fontFamily: "var(--thot-font-family)",
              }}
            >
              <Add24Regular style={{ fontSize: 16 }} />
              Nouvel espace
            </button>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "28px 0 16px" }}>
          <h2 style={{ fontSize: 15, fontWeight: 600 }}>Tous les espaces</h2>
          <span style={{ fontSize: 12, color: "var(--thot-text-faint)" }}>{formatNombre(espaces.length)}</span>
        </div>

        {erreur ? (
          <div role="alert" style={{
            display: "flex", alignItems: "center", gap: 10, padding: 16, borderRadius: 12,
            background: "var(--thot-danger-soft)", color: "var(--thot-danger)", fontSize: 14,
          }}>
            <ErrorCircle24Regular style={{ fontSize: 18 }} />
            {erreur}
          </div>
        ) : chargement ? (
          <div style={{ padding: "64px 0", textAlign: "center", color: "var(--thot-text-muted)" }}>Chargement…</div>
        ) : espaces.length === 0 ? (
          <Vide nom={nom} onOuvrir={() => setNouveau(true)} />
        ) : filtres.length === 0 ? (
          <div style={{ padding: "64px 0", textAlign: "center", color: "var(--thot-text-muted)" }}>
            Aucun espace ne correspond à « {recherche} ».
          </div>
        ) : (
          <div style={{
            display: "grid", gap: 16,
            gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          }}>
            {filtres.map((espace) => <CarteEspace key={espace.id} espace={espace} onOpen={() => navigate(`/espaces/${espace.id}`)} />)}
          </div>
        )}

        <button
          type="button"
          onClick={() => setNouveau(true)}
          style={{
            marginTop: 20, display: "flex", alignItems: "center", gap: 14, width: "100%",
            padding: "18px 20px", borderRadius: 14, textAlign: "left", cursor: "pointer",
            border: "1px dashed var(--thot-border)", background: "transparent",
            fontFamily: "var(--thot-font-family)",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--thot-surface-2)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
        >
          <span aria-hidden="true" style={{ fontSize: 20, lineHeight: 1, color: "var(--thot-text-faint)" }}>+</span>
          <span>
            <span style={{ display: "block", fontSize: 14, fontWeight: 600, color: "var(--thot-text)" }}>
              Créer un nouvel espace
            </span>
            <span style={{ display: "block", fontSize: 13, color: "var(--thot-text-muted)" }}>
              Organisez un projet, invitez des fichiers, discutez et construisez votre mémoire.
            </span>
          </span>
        </button>
      </div>

      {nouveau && (
        <ModalCreation
          nom={nom}
          description={description}
          enCours={creation}
          onNom={setNom}
          onDescription={setDescription}
          onFermer={() => setNouveau(false)}
          onValider={creer}
        />
      )}
    </div>
  );
}

function CarteEspace({ espace, onOpen }: { espace: Workspace; onOpen: () => void }) {
  const s = espace.stats;
  const parStatut = s.memoire_par_statut;
  // Un espace « en mémoire » se lit par ses statuts réels. Sans détail, un
  // nombre seul ferait croire à une vérité acquise.
  const detailMemoire = STATUTS.filter((st) => (parStatut?.[st.cle] ?? 0) > 0);

  return (
    <button
      type="button"
      onClick={onOpen}
      style={{
        display: "flex", flexDirection: "column", gap: 12, padding: 18, textAlign: "left",
        borderRadius: 16, cursor: "pointer", fontFamily: "var(--thot-font-family)",
        border: "1px solid var(--thot-border)", background: `linear-gradient(160deg, ${teinteEspace(espace.id)}, transparent 70%)`,
        boxShadow: "0 1px 2px rgba(0,0,0,0.04)",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--thot-gold)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--thot-border)"; }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 52, height: 52, borderRadius: "50%", display: "inline-flex",
          alignItems: "center", justifyContent: "center", flexShrink: 0,
          background: teinteEspace(espace.id), color: "var(--thot-text)",
        }}
      >
        <DocumentFolder24Regular style={{ fontSize: 26 }} />
      </span>

      <span style={{ minHeight: 0 }}>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 15, fontWeight: 600, color: "var(--thot-text)" }}>{espace.name}</span>
          {espace.stats.memoire > 0 && (
            <Star24Filled title="Cet espace a des mémoires acceptées" style={{ fontSize: 13, color: "var(--thot-gold)" }} />
          )}
        </span>
        {espace.description && (
          <span style={{ display: "block", fontSize: 13, color: "var(--thot-text-muted)", marginTop: 4, lineHeight: 1.45 }}>
            {espace.description}
          </span>
        )}
      </span>

      <span style={{ display: "flex", gap: 14, marginTop: "auto", paddingTop: 4, flexWrap: "wrap" }}>
        <Compteur icone={<Chat24Regular style={{ fontSize: 13 }} />} valeur={formatNombre(s.discussions)} libelle="Discussions" />
        <Compteur icone={<Document24Regular style={{ fontSize: 13 }} />} valeur={formatNombre(s.fichiers)} libelle="Fichiers" />
        <Compteur icone={<Brain24Regular style={{ fontSize: 13 }} />} valeur={formatNombre(s.memoire)} libelle="Mémoire" />
      </span>

      {detailMemoire.length > 0 && (
        <span style={{ fontSize: 11, color: "var(--thot-text-faint)" }}>
          {detailMemoire.map((st) => `${parStatut[st.cle]} ${st.libelle}`).join(" · ")}
        </span>
      )}
    </button>
  );
}

function Compteur({ icone, valeur, libelle }: { icone: React.ReactNode; valeur: string; libelle: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: "var(--thot-text-muted)" }}>
      <span aria-hidden="true" style={{ display: "inline-flex", color: "var(--thot-text-faint)" }}>{icone}</span>
      <span style={{ fontWeight: 600, color: "var(--thot-text)" }}>{valeur}</span>
      {libelle}
    </span>
  );
}

function Vide({ nom, onOuvrir }: { nom: string; onOuvrir: () => void }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", padding: "72px 20px" }}>
      <span aria-hidden="true" style={{ color: "var(--thot-text-faint)", marginBottom: 18 }}>
        <DocumentFolder24Regular style={{ fontSize: 48 }} />
      </span>
      <h3 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>Aucun espace</h3>
      <p style={{ fontSize: 14, color: "var(--thot-text-muted)", maxWidth: 420, marginBottom: 20 }}>
        Un espace regroupe des discussions, des fichiers et une mémoire révisable. Il ne possède
        aucun fichier : les siens restent à ses conversations.
      </p>
      <button
        type="button"
        onClick={onOuvrir}
        style={{
          padding: "9px 20px", borderRadius: "var(--thot-radius-pill)", border: "none",
          background: "var(--thot-gold)", color: "#fff", cursor: "pointer",
          fontSize: 14, fontWeight: 600, fontFamily: "var(--thot-font-family)",
        }}
      >
        Créer « {nom} »
      </button>
    </div>
  );
}

function ModalCreation({
  nom, description, enCours, onNom, onDescription, onFermer, onValider,
}: {
  nom: string;
  description: string;
  enCours: boolean;
  onNom: (v: string) => void;
  onDescription: (v: string) => void;
  onFermer: () => void;
  onValider: () => void;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Nouvel espace"
      onClick={onFermer}
      style={{ position: "fixed", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.4)", zIndex: 100 }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--thot-surface-raised)", borderRadius: 16, padding: 24,
          boxShadow: "var(--thot-shadow-float)", width: "90%", maxWidth: 440,
        }}
      >
        <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>Nouvel espace</h3>
        <p style={{ fontSize: 13, color: "var(--thot-text-muted)", marginBottom: 18 }}>
          Un nom suffit. La description et les fichiers viendront ensuite.
        </p>
        <label style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }} htmlFor="espace-nom">
          Nom
        </label>
        <input
          id="espace-nom"
          value={nom}
          onChange={(e) => onNom(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") onValider(); if (e.key === "Escape") onFermer(); }}
          autoFocus
          placeholder="Refonte du site web"
          style={{
            width: "100%", padding: "9px 12px", fontSize: 14, marginBottom: 14,
            border: "1px solid var(--thot-border)", borderRadius: "var(--thot-radius-sm)",
            background: "var(--thot-surface-2)", color: "var(--thot-text)",
            fontFamily: "var(--thot-font-family)", outline: "none",
          }}
        />
        <label style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }} htmlFor="espace-desc">
          Description <span style={{ fontWeight: 400, color: "var(--thot-text-faint)" }}>(facultative)</span>
        </label>
        <textarea
          id="espace-desc"
          value={description}
          onChange={(e) => onDescription(e.target.value)}
          rows={3}
          placeholder="Design, structure, contenu et développement."
          style={{
            width: "100%", padding: "9px 12px", fontSize: 14,
            border: "1px solid var(--thot-border)", borderRadius: "var(--thot-radius-sm)",
            background: "var(--thot-surface-2)", color: "var(--thot-text)",
            fontFamily: "var(--thot-font-family)", outline: "none", resize: "vertical",
          }}
        />
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 20 }}>
          <button
            type="button"
            onClick={onFermer}
            style={{
              padding: "8px 16px", borderRadius: "var(--thot-radius-sm)",
              border: "1px solid var(--thot-border)", background: "transparent",
              cursor: "pointer", fontSize: 14, color: "var(--thot-text)", fontFamily: "var(--thot-font-family)",
            }}
          >
            Annuler
          </button>
          <button
            type="button"
            onClick={onValider}
            disabled={!nom.trim() || enCours}
            style={{
              padding: "8px 16px", borderRadius: "var(--thot-radius-sm)", border: "none",
              background: "var(--thot-gold)", color: "#fff", cursor: nom.trim() ? "pointer" : "not-allowed",
              fontSize: 14, fontWeight: 600, fontFamily: "var(--thot-font-family)", opacity: nom.trim() ? 1 : 0.5,
            }}
          >
            {enCours ? "Création…" : "Créer"}
          </button>
        </div>
      </div>
    </div>
  );
}

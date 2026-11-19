// pages/espaces/format.ts — mise en forme des compteurs d'un espace.
//
// D'un seul endroit, parce que les deux écrans affichent les mêmes nombres et
// qu'un écart entre eux serait lu comme une panne. Les règles ci-dessous ne sont
// pas des préférences : chacune corrige un écart entre la maquette et le contrat.
//
// 1. « Mémoire » = fiches ACCEPTÉES. La maquette 05-1 affiche « Active », un
//    statut qui n'existe pas dans C17 — les statuts réels sont candidate,
//    accepted, stale et rejected (`05_classe_memoire.md:45-49`).
// 2. Le dépôt n'affiche AUCUN plafond. La maquette 05-3 écrit « 245 Mo / 10 Go »
//    alors qu'aucun contrat ne définit de quota ; afficher un plafond inventerait
//    une règle (`02_classe_depot.md:74-86`).
// 3. Un espace n'a pas de dossier, donc aucun chemin à afficher ni à copier
//    (C10:172). Il n'y a donc aucune fonction de chemin dans ce fichier, par
//    construction : ce serait la seule façon d'en réintroduire un.

/** `1 234 567` → `1,2 Mo`. L'unité est choisie par magnitude, comme le reste du projet. */
export function formatOctets(octets: number): string {
  const n = Number.isFinite(octets) && octets > 0 ? octets : 0;
  if (n < 1024) return `${n} o`;
  const unites = ["ko", "Mo", "Go"];
  let valeur = n / 1024;
  let i = 0;
  while (valeur >= 1024 && i < unites.length - 1) {
    valeur /= 1024;
    i += 1;
  }
  // Une décimale, et le point décimal français — le reste de l'interface est en
  // `fr-FR`, et « 1.2 Mo » dans une page française se lit comme une faute.
  return `${valeur.toFixed(1).replace('.', ',')} ${unites[i]}`;
}

/** Nombre entier avec séparateur de milliers fin : `1 234`. */
export function formatNombre(n: number): string {
  return String(Math.max(0, Math.trunc(Number.isFinite(n) ? n : 0))).replace(
    /\B(?=(\d{3})+(?!\d))/g,
    " "
  );
}

const JOURS = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"];

/** Écart lisible : « il y a 3 h », « il y a 2 j ». Jamais « NaN ». */
export function formatRelatif(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const secondes = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secondes < 60) return "à l'instant";
  if (secondes < 3600) {
    const m = Math.floor(secondes / 60);
    return `il y a ${m} min`;
  }
  if (secondes < 86400) {
    const h = Math.floor(secondes / 3600);
    return `il y a ${h} h`;
  }
  if (secondes < 604800) {
    const j = Math.floor(secondes / 86400);
    return j === 1 ? "hier" : `il y a ${j} jours`;
  }
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
}

/** `12 mai 2024` — la date longue des panneaux d'information. */
export function formatDateLongue(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
}

export { JOURS };

/**
 * Teinte stable d'une carte, dérivée de l'IDENTITÉ de l'espace — jamais de son
 * contenu ni d'un aléatoire à chaque rendu. Deux justifications : le dégradé ne
 * doit pas changer au rechargement, et `Math.random()` ferait divergir deux
 * onglets ouverts sur le même espace.
 */
export function teinteEspace(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return TEINTES[h % TEINTES.length];
}

const TEINTES = [
  "rgba(147, 112, 219, 0.22)",
  "rgba(219, 112, 147, 0.22)",
  "rgba(96, 165, 250, 0.22)",
  "rgba(52, 199, 160, 0.22)",
  "rgba(224, 149, 62, 0.22)",
  "rgba(158, 158, 158, 0.22)",
];

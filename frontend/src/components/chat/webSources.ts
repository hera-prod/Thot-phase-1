// Les sources d'un web_research, lues dans le résultat que le backend a rendu.
//
// Pourquoi relire le texte et non seulement `evidence_ids` : `evidence_ids` dit
// QUOI a été cité, jamais OÙ ça vient. Un `web_3a8d42eca2d0:3` est un
// identifiant opaque, et une liste d'identifiants opaques sous une réponse
// n'est pas une source — c'est du bruit déplacé. L'URL et la date ne sont
// dans aucun autre champ de l'événement, donc ce parseur EST la lecture.
//
// Une seule grammaire, celle du rendu de `services/web/research.py:_render` :
// un bloc de quatre lignes par page, dans cet ordre — titre, URL, date, puis
// les `"evidence_id"`. Une page dont l'URL manque est ignorée : mieux vaut
// n'afficher que le décompte qu'inventer une provenance.
export interface WebSource {
  publicId: string;
  url: string;
  host: string;
  fetchedAt: string;
  ageDays: string;
  status: string;
  evidenceIds: string[];
}

const TITRE = /^—\s/;
const URL_LIGNE = /^\s*URL\s:\s(\S+)$/;
const RECUPERE = /^\s*récupéré le\s(.+?)\s\((.+?)\sj\)\s·\sstatut\s(\S+)/;
const PREUVES = /"evidence_id":\s(.+?)\s+\(source\s(web_[0-9a-f]{12})\)/;

/** Un hôte lisible, jamais une URL rejouable : le lien s'ouvre, il ne s'exécute pas. */
function hoteDe(url: string): string {
  try {
    return new URL(url).hostname || url;
  } catch {
    // Sans schéma, `new URL` échoue et rendrait l'URL ENTIÈRE comme hôte — donc
    // « fr.wikipedia.org/wiki/X » s'afficherait comme un nom de domaine. On
    // prend ce qui précède la première barre oblique, et c'est tout.
    return url.split('/')[0] || url;
  }
}

function nouvelleSource(url: string): WebSource {
  return { publicId: '', url, host: hoteDe(url), fetchedAt: '', ageDays: '', status: '', evidenceIds: [] };
}

export function parseWebSources(result: string | undefined): WebSource[] {
  if (!result) return [];
  const sources: WebSource[] = [];
  // C'est l'URL qui crée la source, jamais le titre : le backend écrit le
  // titre, PUIS l'URL, PUIS la date et les preuves. Un premier essai faisait
  // porter la date et les preuves au bloc *suivant*, parce que l'URL fermait
  // le bloc courant — et les deux tests de cette fiche l'ont attrapé.

  for (const ligne of result.split('\n')) {
    if (TITRE.test(ligne)) continue;

    const url = URL_LIGNE.exec(ligne);
    if (url) {
      sources.push(nouvelleSource(url[1]));
      continue;
    }

    const cible = sources[sources.length - 1];
    if (!cible) continue;

    const date = RECUPERE.exec(ligne);
    if (date) {
      cible.fetchedAt = date[1];
      cible.ageDays = date[2];
      cible.status = date[3];
      continue;
    }

    const preuves = PREUVES.exec(ligne);
    if (preuves) {
      cible.evidenceIds.push(...(preuves[1].match(/"[^"]+"/g) ?? []).map((q) => q.slice(1, -1)));
      if (!cible.publicId) cible.publicId = preuves[2];
    }
  }

  return sources;
}

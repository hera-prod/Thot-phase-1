import { MODEL_CATALOG, type ModelCatalogEntry } from '../../features/models/modelCatalog';
import './ModelesSection.css';

function licenceBadge(kind: ModelCatalogEntry['licenseKind']) {
  if (kind === 'permissive') return { className: 'is-ok', label: 'Permissif' };
  if (kind === 'community') return { className: 'is-community', label: 'Communautaire' };
  return { className: 'is-danger', label: 'Non commercial' };
}

function commercialPhrase(use: ModelCatalogEntry['commercialUse']): string {
  if (use === 'allowed') return 'Usage commercial autorisé';
  if (use === 'conditional') return 'Usage commercial sous conditions';
  return 'Usage commercial interdit';
}

export function ModelesSection() {
  const hasNonCommercial = MODEL_CATALOG.some((m) => m.licenseKind === 'non_commercial');

  return (
    <section className="set-block" aria-labelledby="models-title">
      <header className="set-block-head">
        <div>
          <span className="set-kicker">Carte d’identité</span>
          <h2 id="models-title">Modèles de l’atelier</h2>
        </div>
        <span className="set-source">7 composants</span>
      </header>

      <p className="set-note">
        Les poids des modèles restent sur cette machine ; les licences sont celles des modèles d’origine.
      </p>

      {hasNonCommercial && (
        <p className="set-note is-warn">
          Un composant de poids est non commercial : seuls ces poids ne peuvent pas être utilisés à des fins commerciales. Le code de Thot reste sous licence MIT.
        </p>
      )}

      <div className="set-models">
        {MODEL_CATALOG.map((model) => {
          const badge = licenceBadge(model.licenseKind);
          const metaParts = [
            model.quantization,
            model.context === null ? null : `${model.context.toLocaleString('fr-FR')} jetons`,
          ].filter((value): value is string => value !== null && value !== '');
          return (
            <article key={model.id} className="set-model">
              <div className="set-model-head">
                <div>
                  <h3 className="set-model-role">{model.roleLabel}</h3>
                  <p className="set-model-name">
                    {model.name} <span className="set-model-vendor">· {model.vendor}</span>
                  </p>
                </div>
                <span className={`set-badge ${badge.className}`}>{badge.label}</span>
              </div>
              {metaParts.length > 0 && (
                <p className="set-model-meta">{metaParts.join(' · ')}</p>
              )}
              <div className="set-model-license">
                <span>{model.license}</span>
                <span>{commercialPhrase(model.commercialUse)}</span>
              </div>
            </article>
          );
        })}
      </div>

      <p className="set-note">Licences détaillées : licenses/README.md</p>
    </section>
  );
}
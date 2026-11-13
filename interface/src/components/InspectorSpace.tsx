import type { InspectorSelection, Project } from '../types';
import { NON_RENSEIGNE, ROLE_LABELS } from '../constants';
import { formatValue } from '../lib/format';

interface Props {
  selection: InspectorSelection | null;
  project: Project | null;
}

export default function InspectorSpace({ selection, project }: Props) {
  if (!selection) {
    return (
      <div className="panel">
        <h2>Inspecteur</h2>
        <p className="muted">Cliquez sur une entité de la carte pour voir ses attributs lus dans le fichier chargé.</p>
      </div>
    );
  }
  const keys = selection.properties ? Object.keys(selection.properties).sort() : [];
  return (
    <div className="panel">
      <h2>Inspecteur</h2>
      <div className="card">
        <div className="row">
          <span className="badge">{selection.sourceName}</span>
          <span className="badge">{ROLE_LABELS[selection.role]}</span>
          <span className="badge">{selection.geometryType || NON_RENSEIGNE}</span>
        </div>
        {keys.length === 0 ? (
          <p className="muted" style={{ marginTop: 8 }}>{NON_RENSEIGNE} — aucune propriété dans le fichier.</p>
        ) : (
          <div className="props" style={{ marginTop: 8 }}>
            {keys.map((k) => (
              <div key={k}>
                <span className="k">{k}</span>
                <span className="v">{formatValue(selection.properties?.[k])}</span>
              </div>
            ))}
          </div>
        )}
        <p className="muted small" style={{ marginBottom: 0 }}>
          Valeurs copiées du fichier chargé, jamais recalculées. Absent = « {NON_RENSEIGNE} ».
          {project && <> Projet actif : <strong>{project.name}</strong>.</>}
        </p>
      </div>
    </div>
  );
}

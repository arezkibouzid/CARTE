import { useEffect, useState } from 'react';
import type { Project } from '../types';
import { NON_DISPONIBLE } from '../constants';

interface Props {
  project: Project;
}

/** Fichiers usuels sondés en HEAD sous /data/… — seuls les existants (HTTP 200) sont affichés, copiés tels quels. */
const PROBES = [
  'COMPARAISON.md', 'REPRODUCTION.md', 'README.md', 'decision_process.txt', 'comparaison.csv',
  'protocol.json', 'validation.json', 'verification.json', 'metrics.json', 'delivery_manifest.json',
  'audit.json', 'report.log', 'tests.log', 'tests_checks.log', 'validation_report.md',
];
const IMAGE_PROBES = ['comparaison_controlee.png', 'published_vs_rerun.png', 'revue_routiere.png', 'map_itineraries.png'];

interface FoundFile {
  url: string;
  name: string;
  kind: 'text' | 'json' | 'image';
  content?: string;
}

export default function ResultsSpace({ project }: Props) {
  const [files, setFiles] = useState<FoundFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setLoading(true);
      setError(null);
      const found: FoundFile[] = [];
      for (const base of project.resultPaths) {
        for (const name of PROBES) {
          const url = `${base}/${name}`;
          try {
            const res = await fetch(url, { method: 'HEAD' });
            if (!res.ok) continue;
            const kind = name.endsWith('.json') ? 'json' : 'text';
            const res2 = await fetch(url);
            const content = await res2.text();
            if (cancelled) return;
            found.push({ url, name: `${base}/${name}`, kind, content: content.slice(0, 20000) });
          } catch {
            /* fichier absent : on ignore */
          }
        }
        for (const name of IMAGE_PROBES) {
          const url = `${base}/${name}`;
          try {
            const res = await fetch(url, { method: 'HEAD' });
            if (res.ok && !cancelled) found.push({ url, name: url, kind: 'image' });
          } catch {
            /* absent */
          }
        }
      }
      if (!cancelled) {
        setFiles(found);
        setLoading(false);
      }
    }
    run();
    return () => { cancelled = true; };
  }, [project]);

  return (
    <div className="panel">
      <h2>Résultats</h2>
      <p className="muted small">
        Les verdicts affichés sont <strong>copiés des fichiers existants du dépôt</strong> (lus sous <code>/data/</code>),
        jamais recalculés par l’interface.
      </p>

      <h3>Traitements (tous non disponibles)</h3>
      <div className="row">
        {['Recalculer les itinéraires', 'Refaire l’extraction TIFF', 'Relancer les contrôles', 'Exporter un nouveau livrable'].map((t) => (
          <button key={t} className="btn unavailable" disabled>{t} — {NON_DISPONIBLE}</button>
        ))}
      </div>

      <h3>Dossiers de résultats du projet</h3>
      {project.resultPaths.length === 0 && <p className="muted">Aucun dossier de résultats déclaré.</p>}
      {project.resultPaths.map((p) => <div key={p}><code>{p}</code></div>)}

      {loading && <p className="muted">Lecture des fichiers du dépôt…</p>}
      {error && <p className="badge err">{error}</p>}
      {!loading && files.length === 0 && project.resultPaths.length > 0 && (
        <p className="muted">Aucun fichier reconnu trouvé dans ces dossiers.</p>
      )}

      {files.map((f) => (
        <div key={f.url} className="card">
          <div className="title small">{f.name}</div>
          {f.kind === 'image' ? (
            <img className="thumb" src={f.url} alt={f.name} />
          ) : f.kind === 'json' ? (
            <pre className="filecontent">{f.content}</pre>
          ) : (
            <pre className="filecontent">{f.content}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

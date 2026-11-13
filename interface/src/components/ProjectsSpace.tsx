import { useState } from 'react';
import type { Project } from '../types';
import { formatDate } from '../lib/format';

interface Props {
  projects: Project[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: (name: string) => void;
  onDelete: (id: string) => void;
}

export default function ProjectsSpace({ projects, activeId, onSelect, onCreate, onDelete }: Props) {
  const [name, setName] = useState('');
  return (
    <div className="panel">
      <h2>Projets</h2>
      <p className="muted small">
        Les projets démo sont lus en lecture seule depuis le dépôt (servis sous <code>/data/</code>). Les projets créés
        ici ne vivent que dans ce navigateur (localStorage) ; aucun fichier du dépôt n’est modifié.
      </p>
      {projects.map((p) => (
        <div key={p.id} className={`card${p.id === activeId ? ' active' : ''}`} onClick={() => onSelect(p.id)} style={{ cursor: 'pointer' }}>
          <div className="title">{p.name}</div>
          <div className="muted small">{p.description}</div>
          <div className="row small muted">
            <span>{p.sources.length} source(s)</span>
            <span>créé le {formatDate(p.createdAt)}</span>
            <span style={{ marginLeft: 'auto' }}>
              {p.id === activeId && <span className="badge ok">actif</span>}{' '}
              {!p.id.startsWith('demo-') && (
                <button className="btn danger" onClick={(e) => { e.stopPropagation(); onDelete(p.id); }}>
                  Supprimer
                </button>
              )}
            </span>
          </div>
        </div>
      ))}
      <h3>Nouveau projet</h3>
      <div className="row">
        <input type="text" placeholder="Nom du projet" value={name} onChange={(e) => setName(e.target.value)} style={{ flex: 1 }} />
        <button
          className="btn primary"
          disabled={!name.trim()}
          onClick={() => { onCreate(name.trim()); setName(''); }}
        >
          Créer
        </button>
      </div>
    </div>
  );
}

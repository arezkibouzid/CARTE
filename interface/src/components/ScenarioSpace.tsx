import { useRef, useState } from 'react';
import type { DrawMode, Project, ScenarioImage } from '../types';
import { DEMO_IMAGE_SUGGESTIONS } from '../data/demoProjects';
import { formatValue } from '../lib/format';

interface Props {
  project: Project;
  onUpdate: (fn: (p: Project) => Project) => void;
  drawMode: DrawMode;
  setDrawMode: (m: DrawMode) => void;
  pendingPointName: string;
  setPendingPointName: (s: string) => void;
  pendingZone: [number, number][];
  pendingZoneKind: 'deployment' | 'restricted';
  startZoneDraw: (kind: 'deployment' | 'restricted') => void;
  clearPendingZone: () => void;
}

export default function ScenarioSpace({
  project, onUpdate, drawMode, setDrawMode, pendingPointName, setPendingPointName,
  pendingZone, pendingZoneKind, startZoneDraw, clearPendingZone,
}: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [manualLon, setManualLon] = useState('');
  const [manualLat, setManualLat] = useState('');

  const sc = project.scenario;

  function patchScenario(patch: Partial<Project['scenario']>) {
    onUpdate((p) => ({ ...p, scenario: { ...p.scenario, ...patch } }));
  }

  function addPointManual() {
    const lon = parseFloat(manualLon.replace(',', '.'));
    const lat = parseFloat(manualLat.replace(',', '.'));
    if (Number.isNaN(lon) || Number.isNaN(lat)) return;
    const name = pendingPointName.trim() || `Point ${sc.points.length + 1}`;
    patchScenario({ points: [...sc.points, { id: `p-${Date.now()}`, name, lon, lat }] });
    setPendingPointName('');
    setManualLon('');
    setManualLat('');
  }

  function movePoint(id: string, dir: -1 | 1) {
    const pts = [...sc.points];
    const i = pts.findIndex((p) => p.id === id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= pts.length) return;
    [pts[i], pts[j]] = [pts[j], pts[i]];
    patchScenario({ points: pts });
  }

  function addImageFile(f: File) {
    const img: ScenarioImage = { id: `img-${Date.now()}`, name: f.name, url: URL.createObjectURL(f), origin: 'upload' };
    patchScenario({ images: [...sc.images, img] });
  }

  return (
    <div className="panel">
      <h2>Scénario</h2>
      <p className="muted small">
        Saisie libre : l’application affiche l’ordre tel quel et ne l’interprète jamais. Les traitements (géocodage,
        OCR, calcul d’itinéraires…) restent « Non disponible ».
      </p>

      <h3>Consigne / ordre (texte libre)</h3>
      <textarea
        value={sc.text}
        placeholder="Écrire l’ordre ici (ex. PD5 → WP1 → … → PA5, éviter RA…)"
        onChange={(e) => patchScenario({ text: e.target.value })}
      />

      <h3>Points de l’ordre ({sc.points.length})</h3>
      <div className="row">
        <input type="text" placeholder="Nom du point (ex. WP1)" value={pendingPointName} onChange={(e) => setPendingPointName(e.target.value)} style={{ flex: 1 }} />
        <button
          className={`btn ${drawMode === 'point' ? 'primary' : ''}`}
          onClick={() => setDrawMode(drawMode === 'point' ? null : 'point')}
        >
          {drawMode === 'point' ? 'Cliquez sur la carte…' : 'Placer sur la carte'}
        </button>
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <input type="text" placeholder="lon" value={manualLon} onChange={(e) => setManualLon(e.target.value)} style={{ width: 90 }} />
        <input type="text" placeholder="lat" value={manualLat} onChange={(e) => setManualLat(e.target.value)} style={{ width: 90 }} />
        <button className="btn" onClick={addPointManual} disabled={!manualLon || !manualLat}>Ajouter (coordonnées)</button>
      </div>
      <ol className="scenario-points">
        {sc.points.map((p, i) => (
          <li key={p.id}>
            <span className="ord">{i + 1}.</span>
            <strong>{p.name}</strong>
            <span className="coords">
              {p.lon !== undefined && p.lat !== undefined ? `${p.lon.toFixed(5)}, ${p.lat.toFixed(5)}` : formatValue(null)}
            </span>
            <button className="btn" onClick={() => movePoint(p.id, -1)} disabled={i === 0}>↑</button>
            <button className="btn" onClick={() => movePoint(p.id, 1)} disabled={i === sc.points.length - 1}>↓</button>
            <button className="btn danger" onClick={() => patchScenario({ points: sc.points.filter((x) => x.id !== p.id) })}>✕</button>
          </li>
        ))}
      </ol>

      <h3>Zones dessinées ({sc.zones.length})</h3>
      <div className="row">
        <button className={`btn ${drawMode === 'zone-deployment' ? 'primary' : ''}`} onClick={() => startZoneDraw('deployment')}>
          Dessiner une DA (déploiement)
        </button>
        <button className={`btn ${drawMode === 'zone-restricted' ? 'primary' : ''}`} onClick={() => startZoneDraw('restricted')}>
          Dessiner une RA (à éviter)
        </button>
        {pendingZone.length > 0 && (
          <>
            <span className="zone-pending">{pendingZone.length} sommet(s) — double-clic sur la carte pour fermer la zone {pendingZoneKind === 'deployment' ? 'DA' : 'RA'}</span>
            <button className="btn danger" onClick={() => { clearPendingZone(); setDrawMode(null); }}>Annuler</button>
          </>
        )}
      </div>
      <ul className="scenario-points">
        {sc.zones.map((z) => (
          <li key={z.id}>
            <strong>{z.name}</strong> <span className="muted small">({z.kind === 'deployment' ? 'aire de déploiement' : 'zone à éviter'}, {z.coords.length} sommets)</span>
            <span style={{ marginLeft: 'auto' }}>
              <button className="btn danger" onClick={() => patchScenario({ zones: sc.zones.filter((x) => x.id !== z.id) })}>✕</button>
            </span>
          </li>
        ))}
      </ul>

      <h3>Images jointes ({sc.images.length})</h3>
      <div className="row">
        <input ref={fileRef} type="file" accept="image/*,.pdf" multiple onChange={(e) => {
          for (const f of Array.from(e.target.files ?? [])) addImageFile(f);
          if (fileRef.current) fileRef.current.value = '';
        }} />
      </div>
      <div className="row">
        <select
          defaultValue=""
          onChange={(e) => {
            if (e.target.value) patchScenario({ images: [...sc.images, { id: `img-${Date.now()}`, name: e.target.value.split('/').pop() ?? 'image', url: e.target.value, origin: 'depot' }] });
            e.target.value = '';
          }}
          style={{ flex: 1 }}
        >
          <option value="">Ajouter une image du dépôt…</option>
          {DEMO_IMAGE_SUGGESTIONS.map((u) => <option key={u} value={u}>{u}</option>)}
        </select>
      </div>
      {sc.images.map((img) => (
        <div key={img.id} className="card">
          <div className="row">
            <strong>{img.name}</strong>
            <span className="badge">{img.origin === 'depot' ? 'dépôt' : 'upload'}</span>
            <span style={{ marginLeft: 'auto' }}>
              <button className="btn danger" onClick={() => patchScenario({ images: sc.images.filter((x) => x.id !== img.id) })}>Retirer</button>
            </span>
          </div>
          {img.url.match(/\.(png|jpe?g|gif|webp)$/i) ? <img className="thumb" src={img.url} alt={img.name} /> : <p className="muted small">{img.url}</p>}
        </div>
      ))}
    </div>
  );
}

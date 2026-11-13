import { useState } from 'react';
import type { DataPreset, Options, SheetInfo } from '../types';

interface Props {
  sheet: SheetInfo | null;
  options: Options;
  onChange: (patch: Partial<Options>) => void;
}

const PRESETS: { id: Exclude<DataPreset, 'custom'>; title: string; text: string; set: Partial<Options> }[] = [
  { id: 'tiff', title: 'Image TIFF seule', text: 'Le réseau vient uniquement de la carte scannée. Aucune donnée externe.', set: { useOsm: false, useBdtopo: false } },
  { id: 'tiff_osm', title: 'TIFF + OSM', text: 'La carte scannée, avec OpenStreetMap en plus (contrôle ou complément).', set: { useOsm: true, useBdtopo: true } },
  { id: 'tiff_no_osm', title: 'TIFF sans OSM', text: 'La carte scannée, contrôlée avec la BD TOPO de l’IGN, sans OpenStreetMap.', set: { useOsm: false, useBdtopo: true } },
];

export function presetOf(o: Options): DataPreset {
  if (!o.useOsm && !o.useBdtopo) return 'tiff';
  if (o.useOsm) return 'tiff_osm';
  return 'tiff_no_osm';
}

export default function ConfigPanel({ sheet, options: o, onChange }: Props) {
  const [advanced, setAdvanced] = useState(false);
  const preset = presetOf(o);
  return (
    <div className="panel">
      <h3>1 · Données utilisées</h3>
      <div className="choice-grid">
        {PRESETS.map((p) => (
          <button key={p.id} className={`choice ${preset === p.id ? 'on' : ''}`} onClick={() => onChange(p.set)}>
            <span className="choice-title">{p.title}</span>
            <span className="choice-text">{p.text}</span>
          </button>
        ))}
      </div>
      <div className="checks">
        <label className="check locked"><input type="checkbox" checked readOnly /> Carte scannée (TIFF) — toujours utilisée</label>
        <label className="check">
          <input type="checkbox" checked={o.useOsm} disabled={!sheet?.has_osm} onChange={(e) => onChange({ useOsm: e.target.checked })} />
          OpenStreetMap
        </label>
        {o.useOsm && (
          <div className="sub">
            <label className="check">
              <input type="radio" name="osm" checked={o.osmMode === 'reference'} onChange={() => onChange({ osmMode: 'reference' })} />
              <span><b>Référence de contrôle</b> — l’itinéraire vient de la carte ; OSM sert à le vérifier.</span>
            </label>
            <label className="check">
              <input type="radio" name="osm" checked={o.osmMode === 'fill'} onChange={() => onChange({ osmMode: 'fill' })} />
              <span><b>Compléter le réseau</b> — les routes d’OSM absentes de la carte peuvent être empruntées.</span>
            </label>
          </div>
        )}
        <label className="check">
          <input type="checkbox" checked={o.useBdtopo} disabled={!sheet?.has_bdtopo} onChange={(e) => onChange({ useBdtopo: e.target.checked })} />
          BD TOPO (IGN) — contrôle de l’itinéraire
        </label>
      </div>

      <h3>2 · Lecture de la carte</h3>
      <p className="muted small">Comment le réseau routier est lu dans l’image (modèle d’apprentissage U-Net).</p>
      <div className="choice-grid">
        {(sheet?.models ?? []).map((m) => (
          <button key={m.id} className={`choice ${o.model === m.id ? 'on' : ''}`} onClick={() => onChange({ model: m.id })}>
            <span className="choice-title">{m.label}{m.id === sheet?.default_model ? ' ★' : ''}</span>
            <span className="choice-text">{m.description}</span>
          </button>
        ))}
      </div>

      <h3>3 · Itinéraire</h3>
      <div className="seg">
        <button className={o.objective === 'length' ? 'on' : ''} onClick={() => onChange({ objective: 'length' })}>Le plus court</button>
        <button className={o.objective === 'time' ? 'on' : ''} onClick={() => onChange({ objective: 'time' })}>Le plus rapide</button>
      </div>
      <label className="slider">
        <span>Vitesse maximale du convoi : <b>{o.speedCap} km/h</b></span>
        <input type="range" min={30} max={130} step={5} value={o.speedCap} onChange={(e) => onChange({ speedCap: Number(e.target.value) })} />
      </label>
      <p className="muted small">La durée suit les limitations légales (50 km/h en agglomération, 80 km/h sur route) sans dépasser cette vitesse.</p>

      <h3>4 · Incertitude</h3>
      <label className="slider">
        <span>Seuil d’incertitude : <b>{Math.round(o.tau * 100)} %</b></span>
        <input type="range" min={0.05} max={0.7} step={0.05} value={o.tau} onChange={(e) => onChange({ tau: Number(e.target.value) })} />
      </label>
      <p className="muted small">
        Une route est <b className="txt-doubt">incertaine</b> si le modèle lui donne moins de {Math.round(o.tau * 100)} % de chances d’être une vraie route.
        Affichez la couche « Incertitude » pour les voir sur la carte.
      </p>
      <label className="check">
        <input type="checkbox" checked={o.penalize} onChange={(e) => onChange({ penalize: e.target.checked })} />
        Éviter les routes incertaines dans le calcul
      </label>
      {o.penalize && (
        <label className="slider sub">
          <span>Force de l’évitement : <b>×{1 + o.strength}</b> sur le coût d’une route incertaine</span>
          <input type="range" min={1} max={9} step={1} value={o.strength} onChange={(e) => onChange({ strength: Number(e.target.value) })} />
        </label>
      )}

      <button className="link" onClick={() => setAdvanced((a) => !a)}>{advanced ? '▾' : '▸'} Options avancées</button>
      {advanced && (
        <div className="sub">
          <label className="slider">
            <span>Seuil de tracé pour l’extraction animée : <b>{o.traceThreshold.toFixed(2)}</b></span>
            <input type="range" min={0.3} max={0.7} step={0.05} value={o.traceThreshold} onChange={(e) => onChange({ traceThreshold: Number(e.target.value) })} />
          </label>
          <p className="muted small">0,50 est la valeur utilisée pour les itinéraires. Plus bas : plus de routes, plus de bruit.</p>
        </div>
      )}
    </div>
  );
}

import type { ExtractResult, LayerState, Options, ToolId } from '../types';
import { STAGES } from './MapView';

// ---------------------------------------------------------------------------- barre d'outils
// Icônes en SVG (aucune police d'emoji requise : rendu identique sur tous les postes)
const svg = (d: string, fill = false) => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill={fill ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d={d} />
  </svg>
);
const ICONS: Record<string, JSX.Element> = {
  pan: svg('M7 11V6a1.5 1.5 0 0 1 3 0v4M10 10V4.5a1.5 1.5 0 0 1 3 0V10M13 10V5.5a1.5 1.5 0 0 1 3 0V11M16 11V8.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-2.7L3.5 15a1.6 1.6 0 0 1 2.6-1.9L7 14.5V11'),
  start: <span className="glyph">D</span>,
  end: <span className="glyph">A</span>,
  waypoint: svg('M12 5v14M5 12h14'),
  rect: svg('M4 6h16v12H4z'),
  area: svg('M12 3l8.5 6.2-3.2 9.8H6.7L3.5 9.2z'),
  avoid: svg('M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM5.6 5.6l12.8 12.8'),
  zfrom: <span className="glyph">ZD</span>,
  zto: <span className="glyph">ZA</span>,
  fit: svg('M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'),
  clear: svg('M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3'),
};
const TOOLS: { id: ToolId; label: string }[] = [
  { id: 'pan', label: 'Naviguer et inspecter (clic sur une route)' },
  { id: 'start', label: 'Placer le départ' },
  { id: 'end', label: 'Placer l’arrivée' },
  { id: 'waypoint', label: 'Ajouter une étape' },
  { id: 'rect', label: 'Tracer un rectangle (zone d’intérêt)' },
  { id: 'area', label: 'Dessiner la zone de déploiement' },
  { id: 'avoid', label: 'Dessiner une zone à éviter' },
];

export function Toolbar({ tool, setTool, onFit, onClear }: { tool: ToolId; setTool: (t: ToolId) => void; onFit: () => void; onClear: () => void }) {
  return (
    <div className="toolbar">
      {TOOLS.map((t) => (
        <button key={t.id} className={`tool ${tool === t.id ? 'on' : ''} t-${t.id}`} title={t.label} onClick={() => setTool(t.id)}>
          {ICONS[t.id]}
        </button>
      ))}
      <span className="sep" />
      <button className="tool" title="Voir toute la carte" onClick={onFit}>{ICONS.fit}</button>
      <button className="tool danger" title="Tout effacer (points, zones, résultats)" onClick={onClear}>{ICONS.clear}</button>
    </div>
  );
}

export const HINTS: Record<ToolId, string> = {
  pan: 'Glissez pour vous déplacer · molette pour zoomer · clic sur une route pour ses informations.',
  start: 'Cliquez sur la carte pour placer le départ.',
  end: 'Cliquez sur la carte pour placer l’arrivée.',
  waypoint: 'Cliquez pour ajouter des étapes (dans l’ordre). Échap pour terminer.',
  rect: 'Cliquez-glissez pour tracer un rectangle, puis choisissez une action.',
  area: 'Zone de déploiement : un clic par sommet, double-clic pour fermer. Échap pour annuler.',
  avoid: 'Zone à éviter : un clic par sommet, double-clic pour fermer. Échap pour annuler.',
  zfrom: 'Zone de départ : un clic par sommet, double-clic pour fermer. Échap pour annuler.',
  zto: 'Zone d’arrivée : un clic par sommet, double-clic pour fermer. Échap pour annuler.',
};

// ---------------------------------------------------------------------------- couches et légende
const LAYERS: { id: keyof LayerState; label: string; swatch: string }[] = [
  { id: 'scan', label: 'Carte scannée (IGN)', swatch: 'sw-scan' },
  { id: 'prob', label: 'Probabilité de route (U-Net)', swatch: 'sw-prob' },
  { id: 'uncert', label: 'Incertitude', swatch: 'sw-uncert' },
  { id: 'network', label: 'Réseau extrait', swatch: 'sw-net' },
  { id: 'bdtopo', label: 'BD TOPO', swatch: 'sw-bd' },
  { id: 'osm', label: 'OpenStreetMap', swatch: 'sw-osm' },
  { id: 'refs', label: 'Itinéraires de contrôle', swatch: 'sw-refs' },
  { id: 'hard', label: 'Zones de manœuvre difficile', swatch: 'sw-hard' },
];

const MODEL_LAYERS: (keyof LayerState)[] = ['prob', 'uncert', 'network'];

export function LayerControl({ layers, setLayers, options, setTau, loading }: {
  layers: LayerState; setLayers: (l: LayerState) => void; options: Options; setTau: (t: number) => void; loading: Set<string>;
}) {
  return (
    <div className="layers">
      <div className="layers-title">Couches</div>
      {LAYERS.filter((l) => !(options.model === 'none' && MODEL_LAYERS.includes(l.id))).map((l) => (
        <label key={l.id} className="layer">
          <input type="checkbox" checked={layers[l.id]} onChange={(e) => setLayers({ ...layers, [l.id]: e.target.checked })} />
          <span className={`sw ${l.swatch}`} />
          {l.label}
          {loading.has(l.id) && <span className="spin" />}
        </label>
      ))}
      {options.model !== 'none' && (layers.uncert || layers.network) && (
        <div className="layer-sub">
          <label className="slider">
            <span>Seuil d’incertitude <b>{Math.round(options.tau * 100)} %</b></span>
            <input type="range" min={0.05} max={0.7} step={0.05} value={options.tau} onChange={(e) => setTau(Number(e.target.value))} />
          </label>
          {layers.uncert && (
            <div className="legend col small">
              <span><i className="sw sw-doubt" /> route tracée mais incertaine</span>
              <span><i className="sw sw-weak" /> route possible, non tracée</span>
              <span><i className="sw sw-sure" /> route tracée sûre</span>
            </div>
          )}
          {layers.network && <div className="small muted">Réseau extrait : bleu = sûr, rouge = sous le seuil.</div>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- lecture de l'extraction pas à pas
const STAGE_INFO: Record<(typeof STAGES)[number], { title: string; text: string }> = {
  scan: { title: 'Carte scannée', text: 'L’image IGN telle qu’elle est fournie : le point de départ.' },
  probability: { title: 'Probabilité de route', text: 'Le réseau de neurones estime pour chaque pixel la probabilité d’être une route. Plus c’est foncé, plus il est sûr.' },
  mask: { title: 'Masque des routes', text: 'On garde les pixels dont la probabilité dépasse le seuil de tracé.' },
  skeleton: { title: 'Lignes centrales', text: 'Chaque route est amincie en une ligne d’un pixel au milieu de la chaussée.' },
  graph: { title: 'Réseau routier', text: 'Les lignes deviennent un réseau de tronçons et de carrefours (points blancs ; impasses en rouge), utilisable pour les itinéraires.' },
};

export function ExtractionPlayer({ result, stage, progress, playing, onStage, onPlay, onClose }: {
  result: ExtractResult; stage: number; progress: number; playing: boolean;
  onStage: (s: number) => void; onPlay: (p: boolean) => void; onClose: () => void;
}) {
  const st = STAGES[stage];
  const info = STAGE_INFO[st];
  return (
    <div className="player">
      <div className="player-steps">
        {STAGES.map((s, i) => (
          <button key={s} className={`step ${i === stage ? 'on' : ''} ${i < stage ? 'done' : ''}`} onClick={() => { onPlay(false); onStage(i); }}>
            <span className="n">{i + 1}</span>{STAGE_INFO[s].title}
          </button>
        ))}
      </div>
      <div className="player-body">
        <div className="player-text">
          <b>{stage + 1}. {info.title}</b> — {info.text}
          {st === 'graph' && (
            <div className="small muted">
              {Math.round(result.graph.features.length * progress)} / {result.graph.features.length} tronçons · {result.stats.km} km · {result.stats.junctions} carrefours
            </div>
          )}
        </div>
        <div className="player-ctl">
          <button className="btn small" onClick={() => { onPlay(false); onStage(Math.max(0, stage - 1)); }} disabled={stage === 0}>◀</button>
          <button className="btn small primary" onClick={() => {
            if (stage === STAGES.length - 1 && progress >= 1) onStage(0);
            onPlay(!playing);
          }}>{playing ? '⏸' : '▶'}</button>
          <button className="btn small" onClick={() => { onPlay(false); onStage(Math.min(STAGES.length - 1, stage + 1)); }} disabled={stage === STAGES.length - 1}>▶|</button>
          <button className="btn small" onClick={onClose} title="Fermer l’extraction">✕</button>
        </div>
      </div>
      <div className="player-bar"><div style={{ width: `${((stage + (st === 'graph' ? progress : 0)) / (STAGES.length - 1 + 1)) * 100}%` }} /></div>
    </div>
  );
}

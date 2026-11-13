import { useEffect, useRef, useState } from 'react';
import { getPdf, uploadPdf } from '../api';
import type { LonLat, PdfExercise, PdfResult, Scenario, SheetInfo, ToolId } from '../types';

interface Props {
  sheet: SheetInfo | null;
  scenario: Scenario;
  tool: ToolId;
  setTool: (t: ToolId) => void;
  onChange: (patch: Partial<Scenario>) => void;
  onLoadExercise: (ex: PdfExercise) => void;
  onCompute: () => void;
  computing: boolean;
}

const fmt = (p?: LonLat) => (p ? `${p[1].toFixed(5)} N, ${p[0].toFixed(5)} E` : '—');

export default function ScenarioPanel({ sheet, scenario: sc, tool, setTool, onChange, onLoadExercise, onCompute, computing }: Props) {
  const [pdf, setPdf] = useState<PdfResult | null>(null);
  const [pdfErr, setPdfErr] = useState('');
  const [pdfBusy, setPdfBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!sheet?.has_pdf) return;
    getPdf(sheet.id).then(setPdf).catch((e) => setPdfErr(String(e.message ?? e)));
  }, [sheet?.id]);

  const move = (i: number, d: -1 | 1) => {
    const w = [...sc.waypoints];
    const j = i + d;
    if (j < 0 || j >= w.length) return;
    [w[i], w[j]] = [w[j], w[i]];
    onChange({ waypoints: w });
  };
  const ready = !!sc.start && !!sc.end;

  return (
    <div className="panel">
      <h3>Exercice du PDF</h3>
      <div className="row">
        <select
          value=""
          onChange={(e) => {
            const ex = pdf?.exercises.find((x) => x.number === Number(e.target.value));
            if (ex) onLoadExercise(ex);
          }}
          style={{ flex: 1 }}
          disabled={!pdf?.exercises.length}
        >
          <option value="">{pdf?.exercises.length ? `Charger un exercice (${pdf.exercises.length} disponibles)…` : 'Aucun PDF'}</option>
          {pdf?.exercises.map((x) => (
            <option key={x.number} value={x.number}>
              Exercice {x.number} — {x.objective === 'time' ? 'le plus rapide' : 'le plus court'}
              {x.round_trip ? ', aller-retour' : ''}{x.waypoints.length ? `, ${x.waypoints.length} étapes (${x.waypoint_order === 'fixed' ? 'ordre imposé' : 'ordre libre'})` : ''}
            </option>
          ))}
        </select>
        <button className="btn" title="Lire un autre PDF d’exercices" onClick={() => file.current?.click()} disabled={pdfBusy}>
          {pdfBusy ? '…' : 'Importer'}
        </button>
        <input ref={file} type="file" accept="application/pdf" hidden onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          setPdfBusy(true);
          setPdfErr('');
          try {
            setPdf(await uploadPdf(f));
          } catch (err) {
            setPdfErr((err as Error).message);
          } finally {
            setPdfBusy(false);
            e.target.value = '';
          }
        }} />
      </div>
      {pdfErr && <p className="error small">{pdfErr}</p>}
      {sc.label && (
        <div className="card exercise">
          <div className="title">{sc.label}</div>
          {sc.requirements?.map((r, i) => <div key={i} className="small">• {r}</div>)}
        </div>
      )}

      <h3>Points</h3>
      <ul className="pts">
        <li>
          <span className="pin-mini start">D</span>
          <span className="pt-name">Départ</span>
          <span className="coords">{fmt(sc.start)}</span>
          <button className={`btn small ${tool === 'start' ? 'primary' : ''}`} onClick={() => setTool(tool === 'start' ? 'pan' : 'start')}>Placer</button>
        </li>
        {sc.waypoints.map((w, i) => (
          <li key={i}>
            <span className="pin-mini wp">{i + 1}</span>
            <span className="pt-name">Étape {i + 1}</span>
            <span className="coords">{fmt(w)}</span>
            <button className="btn small" onClick={() => move(i, -1)} disabled={i === 0} title="Monter">↑</button>
            <button className="btn small" onClick={() => move(i, 1)} disabled={i === sc.waypoints.length - 1} title="Descendre">↓</button>
            <button className="btn small danger" onClick={() => onChange({ waypoints: sc.waypoints.filter((_, j) => j !== i) })} title="Retirer">✕</button>
          </li>
        ))}
        <li>
          <span className="pin-mini end">A</span>
          <span className="pt-name">Arrivée</span>
          <span className="coords">{fmt(sc.end)}</span>
          <button className={`btn small ${tool === 'end' ? 'primary' : ''}`} onClick={() => setTool(tool === 'end' ? 'pan' : 'end')}>Placer</button>
        </li>
      </ul>
      <div className="row">
        <button className={`btn ${tool === 'waypoint' ? 'primary' : ''}`} onClick={() => setTool(tool === 'waypoint' ? 'pan' : 'waypoint')}>+ Ajouter une étape</button>
        {sc.waypoints.length > 1 && (
          <div className="seg small">
            <button className={sc.waypointOrder === 'fixed' ? 'on' : ''} onClick={() => onChange({ waypointOrder: 'fixed' })} title="Les étapes sont visitées dans l’ordre de la liste">Ordre imposé</button>
            <button className={sc.waypointOrder === 'free' ? 'on' : ''} onClick={() => onChange({ waypointOrder: 'free' })} title="L’ordre le plus avantageux est cherché automatiquement">Ordre libre</button>
          </div>
        )}
      </div>
      <label className="check">
        <input type="checkbox" checked={sc.roundTrip} onChange={(e) => onChange({ roundTrip: e.target.checked })} />
        Aller-retour par deux itinéraires différents
      </label>

      <h3>Zones</h3>
      <ul className="pts">
        <li>
          <span className="swatch area" />
          <span className="pt-name">Zone de déploiement</span>
          <span className="coords">{sc.area ? `${sc.area.length} sommets` : 'toute la carte'}</span>
          <button className={`btn small ${tool === 'area' ? 'primary' : ''}`} onClick={() => setTool(tool === 'area' ? 'pan' : 'area')}>Dessiner</button>
          {sc.area && <button className="btn small danger" onClick={() => onChange({ area: undefined })}>✕</button>}
        </li>
        {sc.avoid.map((z, i) => (
          <li key={i}>
            <span className="swatch avoid" />
            <span className="pt-name">Zone à éviter {i + 1}</span>
            <span className="coords">{z.length} sommets</span>
            <button className="btn small danger" onClick={() => onChange({ avoid: sc.avoid.filter((_, j) => j !== i) })}>✕</button>
          </li>
        ))}
      </ul>
      <button className={`btn ${tool === 'avoid' ? 'primary' : ''}`} onClick={() => setTool(tool === 'avoid' ? 'pan' : 'avoid')}>+ Zone à éviter</button>
      <p className="muted small">Polygone : un clic par sommet, double-clic pour fermer. Vous pouvez aussi tracer un rectangle avec l’outil ▭ de la carte.</p>

      <button className="btn primary big" disabled={!ready || computing} onClick={onCompute}>
        {computing ? 'Calcul en cours…' : 'Calculer l’itinéraire'}
      </button>
      {!ready && <p className="muted small center">Placez au moins un départ et une arrivée.</p>}
    </div>
  );
}

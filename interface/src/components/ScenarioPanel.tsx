import { useEffect, useRef, useState } from 'react';
import { getCapabilities, getPdf, uploadPdf } from '../api';
import type { Capabilities, DistinctParams, LonLat, PdfExercise, PdfResult, Scenario, SheetInfo, ToolId } from '../types';

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
  const [caps, setCaps] = useState<Capabilities | null>(null);
  useEffect(() => {
    getCapabilities().then(setCaps).catch(() => setCaps(null));
  }, []);
  const distinct = sc.mode === 'distinct';
  const d = sc.distinct;
  const setD = (p: Partial<DistinctParams>) => onChange({ distinct: { ...d, ...p } });

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
  const ready = distinct ? (!!sc.zoneFrom || !!sc.start) && (!!sc.zoneTo || !!sc.end) : !!sc.start && !!sc.end;

  return (
    <div className="panel">
      <h3>Type de calcul</h3>
      <div className="seg">
        <button className={!distinct ? 'on' : ''} onClick={() => onChange({ mode: 'single' })}>Un itinéraire</button>
        <button className={distinct ? 'on' : ''} onClick={() => onChange({ mode: 'distinct' })}>Itinéraires distincts (contraintes)</button>
      </div>
      <p className="muted small">
        {distinct
          ? 'Plusieurs itinéraires d’une zone de départ à une zone d’arrivée, éloignés les uns des autres, sur les seules routes praticables par le véhicule choisi ; virages difficiles, zones de manœuvre difficile et contrôle de chaque contrainte.'
          : 'Le meilleur itinéraire entre un départ et une arrivée, avec étapes, aller-retour et zones.'}
      </p>

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

      {distinct && (
        <>
          <h3>Contraintes</h3>
          <div className="row">
            <label className="field">Itinéraires
              <input type="number" min={1} max={6} value={d.k} onChange={(e) => setD({ k: Math.max(1, Math.min(6, Number(e.target.value) || 1)) })} />
            </label>
            <label className="field">Écart minimal (km)
              <input type="number" min={0} max={50} step={0.5} value={d.sepKm} onChange={(e) => setD({ sepKm: Math.max(0, Number(e.target.value) || 0) })} />
            </label>
          </div>
          <label className="check">
            <input type="checkbox" checked={d.noIntersect} onChange={(e) => setD({ noIntersect: e.target.checked })} />
            Les itinéraires ne se croisent pas
          </label>
          <label className="field">Véhicule du convoi
            <select value={d.vehicle} onChange={(e) => setD({ vehicle: e.target.value })}>
              {(caps?.vehicles ?? [{ id: d.vehicle, label: d.vehicle, width_m: 0 }]).map((v) => (
                <option key={v.id} value={v.id}>{v.label}{v.width_m ? ` — ${v.width_m} m de large` : ''}</option>
              ))}
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={d.turns} onChange={(e) => setD({ turns: e.target.checked })} />
            Tenir compte des virages aux carrefours (évite les virages serrés)
          </label>

          <h3>Zones de départ et d’arrivée</h3>
          <ul className="pts">
            {([['zoneFrom', 'zfrom', 'Zone de départ', 'start'], ['zoneTo', 'zto', 'Zone d’arrivée', 'end']] as const).map(([key, t, name, pt]) => (
              <li key={key}>
                <span className={`swatch ${t}`} />
                <span className="pt-name">{name}</span>
                <span className="coords">{sc[key] ? `${sc[key]!.length} sommets` : sc[pt] ? `disque de ${d.zoneRadiusKm} km autour de ${pt === 'start' ? 'D' : 'A'}` : '—'}</span>
                <button className={`btn small ${tool === t ? 'primary' : ''}`} onClick={() => setTool(tool === t ? 'pan' : t)}>Dessiner</button>
                {sc[key] && <button className="btn small danger" onClick={() => onChange({ [key]: undefined })}>✕</button>}
              </li>
            ))}
          </ul>
          {(!sc.zoneFrom || !sc.zoneTo) && (
            <label className="slider">
              <span>Rayon des zones non dessinées (autour de D et A) : <b>{d.zoneRadiusKm} km</b></span>
              <input type="range" min={1} max={20} step={0.5} value={d.zoneRadiusKm} onChange={(e) => setD({ zoneRadiusKm: Number(e.target.value) })} />
            </label>
          )}
          {d.k > 1 && <p className="muted small">Les zones doivent être plus larges que l’écart demandé ({d.sepKm} km), sinon les itinéraires ne peuvent pas partir ou arriver assez loin les uns des autres.</p>}
        </>
      )}

      <h3>Points</h3>
      <ul className="pts">
        <li>
          <span className="pin-mini start">D</span>
          <span className="pt-name">Départ</span>
          <span className="coords">{fmt(sc.start)}</span>
          <button className={`btn small ${tool === 'start' ? 'primary' : ''}`} onClick={() => setTool(tool === 'start' ? 'pan' : 'start')}>Placer</button>
        </li>
        {!distinct && sc.waypoints.map((w, i) => (
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
      {!distinct && (<>
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
      </>)}

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
        {computing ? 'Calcul en cours…' : distinct ? 'Calculer les itinéraires' : 'Calculer l’itinéraire'}
      </button>
      {!ready && <p className="muted small center">{distinct ? 'Dessinez les zones de départ et d’arrivée, ou placez D et A.' : 'Placez au moins un départ et une arrivée.'}</p>}
    </div>
  );
}

import { useState } from 'react';
import type { CompareResult, ExtractResult, RouteResult } from '../types';

interface Props {
  route: RouteResult | null;
  compares: Partial<Record<'bdtopo' | 'osm', CompareResult | 'loading'>>;
  extraction: ExtractResult | null;
  error: string;
}

const pct = (v?: number) => (v === undefined || v === null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)} %`);
const NAMES = { bdtopo: 'BD TOPO (IGN)', osm: 'OpenStreetMap' } as const;

function download(name: string, data: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data)], { type: 'application/geo+json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export default function ResultsPanel({ route, compares, extraction, error }: Props) {
  const [showLog, setShowLog] = useState(false);
  return (
    <div className="panel">
      {error && <div className="card errcard">{error}</div>}
      {!route && !extraction && !error && (
        <p className="muted">Aucun résultat pour l’instant. Calculez un itinéraire (onglet Scénario) ou tracez un rectangle sur la carte pour voir l’extraction du réseau.</p>
      )}

      {route && (
        <>
          <h3>Itinéraire</h3>
          <div className="kpis">
            <div className="kpi"><span>Distance</span><b>{route.total.length_km.toFixed(1)} km</b></div>
            <div className="kpi"><span>Durée</span><b>{Math.round(route.total.duration_min)} min</b></div>
            <div className="kpi"><span>Calcul</span><b>{route.timing_s.total?.toFixed(1)} s</b></div>
          </div>
          {route.visit_order && route.visit_order.length > 2 && (
            <p className="small">Ordre de visite : <b>{route.visit_order.map((k) => (k === 'PD' ? 'Départ' : k === 'PA' ? 'Arrivée' : k.replace('WP', 'Étape '))).join(' → ')}</b></p>
          )}
          {route.legs.map((l, i) => (
            <div key={i} className="card">
              <div className="title">{route.legs.length > 1 ? (i === 0 ? 'Aller' : 'Retour') : 'Trajet'} : {l.length_km.toFixed(1)} km · {Math.round(l.duration_min)} min</div>
              <div className="small muted">
                Vitesse moyenne {Math.round(l.mean_speed_kmh)} km/h · {l.km_built_up.toFixed(1)} km en agglomération
                {l.km_below_prob_threshold !== undefined && <> · <span className="txt-doubt">{l.km_below_prob_threshold.toFixed(1)} km incertains</span></>}
              </div>
              <div className="small">
                {Object.entries(l.km_by_speed_limit).map(([k, v]) => {
                  const lim = k.split(' ')[0];
                  const driven = (k.match(/driven at (\d+)/) ?? [])[1];
                  const label = k.includes('built-up') ? 'agglomération' : Number(lim) > Number(driven) ? `limité à ${driven} km/h par le convoi` : 'hors agglomération';
                  return <span key={k} className="badge">{v.toFixed(1)} km à {lim} km/h ({label})</span>;
                })}
              </div>
              {(!l.inside_area || !l.continuous) && <div className="error small">Attention : {!l.inside_area && 'sort de la zone de déploiement. '}{!l.continuous && 'trajet discontinu.'}</div>}
            </div>
          ))}

          {route.compare.length > 0 && <h3>Comparaison (contrôle)</h3>}
          {route.compare.map((w) => {
            const c = compares[w];
            return (
              <div key={w} className={`card ref ${w}`}>
                <div className="title">{NAMES[w]}</div>
                {c === 'loading' || !c ? <div className="small muted">calcul…</div> : c.error ? <div className="small error">{c.error}</div> : (
                  <div className="small">
                    {c.total!.length_km.toFixed(1)} km · {Math.round(c.total!.duration_min)} min — notre trajet est
                    <b> {pct(c.length_gap)}</b> en distance, <b>{pct(c.time_gap)}</b> en durée ; tracés communs à <b>{Math.round((c.overlap ?? 0) * 100)} %</b>.
                  </div>
                )}
              </div>
            );
          })}

          {route.probability_penalty && (
            <p className="small muted">Routes incertaines pénalisées : {route.probability_penalty.km_below_threshold} km du réseau sous le seuil de {Math.round(route.probability_penalty.threshold * 100)} %.</p>
          )}
          <div className="row">
            <button className="btn" onClick={() => download('itineraire.geojson', route.route)}>Exporter (GeoJSON)</button>
            <button className="link" onClick={() => setShowLog((s) => !s)}>{showLog ? '▾' : '▸'} Décisions du calcul</button>
          </div>
          {showLog && <ul className="log small">{route.log.map((l, i) => <li key={i}>{l}</li>)}</ul>}
        </>
      )}

      {extraction && (
        <>
          <h3>Extraction de la zone</h3>
          <div className="kpis">
            <div className="kpi"><span>Routes lues</span><b>{extraction.stats.km} km</b></div>
            <div className="kpi"><span>Carrefours</span><b>{extraction.stats.junctions}</b></div>
            <div className="kpi"><span>Calcul</span><b>{extraction.timing_s.total?.toFixed(1)} s</b></div>
          </div>
          <div className="small muted">
            {extraction.stats.sections} tronçons · {extraction.stats.dead_ends} impasses · {Math.round(extraction.stats.largest_piece_share * 100)} % du réseau d’un seul tenant
            {extraction.stats.mean_probability_on_roads !== null && <> · confiance moyenne {Math.round((extraction.stats.mean_probability_on_roads ?? 0) * 100)} %</>}
          </div>
          {extraction.stats.bdtopo?.completeness !== undefined && (
            <div className="card ref bdtopo small">
              Contrôle BD TOPO : <b>{Math.round((extraction.stats.bdtopo.completeness ?? 0) * 100)} %</b> des routes BD TOPO retrouvées,
              <b> {Math.round((extraction.stats.bdtopo.correctness ?? 0) * 100)} %</b> des routes lues confirmées.
            </div>
          )}
          <button className="btn" onClick={() => download('reseau_extrait.geojson', extraction.graph)}>Exporter le réseau (GeoJSON)</button>
        </>
      )}
    </div>
  );
}

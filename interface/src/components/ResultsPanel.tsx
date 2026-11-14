import { useState } from 'react';
import type { ApplyTextOrder } from './TextOrder';
import type { MapContext, BBox, CompareResult, ConvoysResult, ExtractResult, ItinerariesResult, RouteResult } from '../types';
import { IT_COLORS } from './MapView';
import { ConvoysView, RouteChat, WhyPanel } from './RouteExplain';

interface Props {
  route: RouteResult | null;
  multi: ItinerariesResult | null;
  compares: Partial<Record<'bdtopo' | 'osm', CompareResult | 'loading'>>;
  extraction: ExtractResult | null;
  error: string;
  convoys: ConvoysResult | null;
  onFocus: (b: BBox) => void;
  uiContext: () => Record<string, unknown>;
  sheetId?: string;
  onTextOrder?: ApplyTextOrder;
  mapCtx?: () => MapContext;
  /** Étape suivante : ce trajet entre dans le journal de mission, la suivante part de son arrivée. */
  onContinue?: () => void;
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

export default function ResultsPanel({ route, multi, compares, extraction, error, convoys, onFocus, uiContext, sheetId, onTextOrder, mapCtx, onContinue }: Props) {
  const [showLog, setShowLog] = useState(false);
  return (
    <div className="panel">
      {error && <div className="card errcard" style={{ whiteSpace: 'pre-line' }}>{error}</div>}
      {multi && <MultiResult m={multi} />}
      {convoys && <ConvoysView r={convoys} />}
      {!route && !multi && !convoys && !extraction && !error && (
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
                {l.mean_confidence !== undefined && <> · confiance du modèle {l.mean_confidence.toFixed(2)}{l.km_low_confidence ? <span className="txt-doubt"> ({l.km_low_confidence} km peu sûrs)</span> : null}</>}
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

          {route.uncertainty_cost && (
            <p className="small muted">Coût d’incertitude (TIFF seul) d’intensité {route.uncertainty_cost.strength} : chaque mètre coûte (1 + {route.uncertainty_cost.strength} × risque).
              {route.legs[0]?.uncertainty_cost_share !== undefined && <> Il représente {Math.round((route.legs[0].uncertainty_cost_share ?? 0) * 100)} % du coût du trajet.</>}</p>
          )}
          {route.uncertainty_cost_note && <p className="small muted">{route.uncertainty_cost_note[0].toUpperCase() + route.uncertainty_cost_note.slice(1)}.</p>}
          {route.history && (
            <p className="small history-note">Journal de mission : {route.history.mode === 'forbid'
              ? <>les {route.history.km_of_previous_legs} km des {route.history.legs} étape(s) précédente(s) sont interdits{route.history.relaxed_m ? <>, rouverts à moins de {route.history.relaxed_m} m du point partagé faute d’autre passage</> : null}</>
              : <>routes des étapes précédentes pénalisées</>} ; repris : {route.history.km_reused} km.</p>
          )}
          {onContinue && <button className="btn small" onClick={onContinue} title="Ce trajet entre dans le journal de mission ; la prochaine étape part de son arrivée, sans repasser par lui">Étape suivante depuis l’arrivée →</button>}
          <WhyPanel route={route} onFocus={onFocus} />
          {route.probability_penalty && (
            <p className="small muted">Routes incertaines pénalisées : {route.probability_penalty.km_below_threshold} km du réseau sous le seuil de {Math.round(route.probability_penalty.threshold * 100)} %.</p>
          )}
          <div className="row">
            <button className="btn" onClick={() => download('itineraire.geojson', route.route)}>Exporter (GeoJSON)</button>
            <button className="link" onClick={() => setShowLog((s) => !s)}>{showLog ? '▾' : '▸'} Décisions du calcul</button>
          </div>
          {showLog && <ul className="log small">{route.log.map((l, i) => <li key={i}>{l}</li>)}</ul>}
          <RouteChat route={route} uiContext={uiContext} sheetId={sheetId} onTextOrder={onTextOrder} mapCtx={mapCtx} />
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

function MultiResult({ m }: { m: ItinerariesResult }) {
  const [showLog, setShowLog] = useState(false);
  const its = m.itineraries.features.map((f) => f.properties);
  const nOk = m.checks.filter((c) => c.ok).length;
  const exportAll = () => download('itineraires_distincts.geojson', {
    type: 'FeatureCollection',
    features: [
      ...m.itineraries.features.map((f) => ({ ...f, properties: { kind: 'itinerary', ...f.properties } })),
      ...m.turns.features.map((f) => ({ ...f, properties: { kind: 'difficult_turn', ...f.properties } })),
      ...m.difficult_areas.features.map((f) => ({ ...f, properties: { kind: 'difficult_area', ...f.properties } })),
    ],
  });
  return (
    <>
      <h3>Itinéraires distincts</h3>
      <div className="kpis">
        <div className="kpi"><span>Itinéraires</span><b>{its.length}</b></div>
        <div className="kpi"><span>Contraintes</span><b className={m.all_ok ? 'check-ok' : 'check-ko'}>{nOk}/{m.checks.length} ✓</b></div>
        <div className="kpi"><span>Calcul</span><b>{m.timing_s.total?.toFixed(0)} s</b></div>
      </div>
      <p className="small muted">
        {m.network} · {m.vehicle}{m.solver.turn_aware ? ' · virages aux carrefours tarifés' : ''} · {m.solver.feasible_sets} ensemble(s) valide(s) comparé(s)
      </p>
      {its.map((p) => (
        <div key={p.id} className="card">
          <div className="title"><span className="it-sw" style={{ background: IT_COLORS[p.index % IT_COLORS.length] }} />{p.id} : {p.length_km.toFixed(1)} km · {Math.round(p.time_min)} min</div>
          {p.roads.length > 0 && <div className="small">Par {p.roads.join(' → ')}</div>}
          <div className="small muted">
            {p.turns_critical > 0 ? <span className="check-ko">{p.turns_critical} virage(s) critique(s)</span> : '0 virage critique'} · {p.turns_difficult} difficile(s)
            {p.min_radius_m !== null && <> · rayon minimal {Math.round(p.min_radius_m)} m</>}
          </div>
          <div className="small muted">
            {p.builtup_km.toFixed(1)} km en zone bâtie · {p.bridges} pont(s) · {p.tunnels} tunnel(s)
            {p.narrow_km > 0 && <> · <span className="check-ko">{p.narrow_km.toFixed(1)} km trop étroits pour croiser</span></>}
            {p.width_unknown_km > 0 && <> · largeur inconnue sur {p.width_unknown_km.toFixed(1)} km</>}
            {p.steep_km > 0 && <> · {p.steep_km.toFixed(1)} km de forte pente</>}
          </div>
        </div>
      ))}
      <h3>Contrôle des contraintes</h3>
      <ul className="checks small">
        {m.checks.map((c, i) => (
          <li key={i} className={c.ok ? 'check-ok' : 'check-ko'}>{c.ok ? '✓' : '✗'} {c.label}{c.detail ? ` — ${c.detail}` : ''}</li>
        ))}
      </ul>
      {m.difficult_areas.features.length > 0 && (
        <>
          <h3>Zones de manœuvre difficile (3 km autour des itinéraires)</h3>
          <ul className="small">
            {m.difficult_areas.features.map((f, i) => {
              const q = f.properties as { kind: string; features: number; area_km2: number };
              return <li key={i}>{q.kind} : {q.features} ({q.area_km2} km²)</li>;
            })}
          </ul>
        </>
      )}
      <p className="small muted">Points rouges : virages critiques (rayon &lt; 15 m) ; orange : difficiles (&lt; 30 m). Cliquez sur un point pour le détail.</p>
      <div className="row">
        <button className="btn" onClick={exportAll}>Exporter (GeoJSON)</button>
        <button className="link" onClick={() => setShowLog((s) => !s)}>{showLog ? '▾' : '▸'} Décisions du calcul</button>
      </div>
      {showLog && (
        <ul className="log small">
          {m.log.map((l, i) => <li key={i}>{l}</li>)}
          <li>Temps : {Object.entries(m.timing_s).map(([k, v]) => `${k} ${v} s`).join(' · ')}</li>
        </ul>
      )}
    </>
  );
}

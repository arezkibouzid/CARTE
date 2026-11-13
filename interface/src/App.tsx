import { useCallback, useEffect, useRef, useState } from 'react';
import { bboxOfPolygon, getCompare, getSheets, getStatus, postExtract, postItineraries, postRoute } from './api';
import ConfigPanel from './components/ConfigPanel';
import ScenarioPanel from './components/ScenarioPanel';
import ResultsPanel from './components/ResultsPanel';
import OrdersPanel from './components/OrdersPanel';
import MapView, { STAGES } from './components/MapView';
import { ExtractionPlayer, HINTS, LayerControl, Toolbar } from './components/MapOverlays';
import type {
  BackendStatus, BBox, CompareResult, ExtractResult, ItinerariesResult, LayerState, LonLat, Options, PdfExercise, RouteResult, Scenario, SheetInfo, ToolId,
} from './types';

type Tab = 'config' | 'scenario' | 'results' | 'orders';
const TABS: { id: Tab; label: string }[] = [
  { id: 'config', label: '1 · Configurer' },
  { id: 'scenario', label: '2 · Scénario' },
  { id: 'results', label: '3 · Résultats' },
  { id: 'orders', label: '4 · Ordres' },
];

const STORE = 'amiad-ui-v2';
const EMPTY_SCENARIO: Scenario = {
  waypoints: [], waypointOrder: 'fixed', roundTrip: false, avoid: [], mode: 'single',
  distinct: { k: 3, sepKm: 5, noIntersect: true, vehicle: 'pfm_carrier', turns: true, zoneRadiusKm: 8 },
};
const DEFAULT_OPTIONS: Options = {
  model: 'nodice', objective: 'length', speedCap: 80, useBdtopo: true, useOsm: false, osmMode: 'reference',
  tau: 0.3, penalize: false, strength: 3, traceThreshold: 0.5,
};
const DEFAULT_LAYERS: LayerState = { scan: true, prob: false, uncert: false, network: false, bdtopo: false, osm: false, refs: true, hard: true };

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(STORE);
    const v = raw ? JSON.parse(raw)[key] : undefined;
    return v ? { ...fallback, ...v } : fallback;
  } catch {
    return fallback;
  }
}

export default function App() {
  const [sheets, setSheets] = useState<SheetInfo[]>([]);
  const [sheetId, setSheetId] = useState<string>(() => load('sheet', { id: '' }).id);
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [backendError, setBackendError] = useState('');
  const [tab, setTab] = useState<Tab>('config');
  const [options, setOptions] = useState<Options>(() => load('options', DEFAULT_OPTIONS));
  const [layers, setLayers] = useState<LayerState>(() => load('layers', DEFAULT_LAYERS));
  const [scenario, setScenario] = useState<Scenario>(() => load('scenario', EMPTY_SCENARIO));
  const [tool, setTool] = useState<ToolId>('pan');
  const [draft, setDraft] = useState<LonLat[]>([]);
  const [rect, setRect] = useState<BBox | null>(null);
  const [focus, setFocus] = useState<BBox | null>(null);
  const [fitKey, setFitKey] = useState(0);
  const [route, setRoute] = useState<RouteResult | null>(null);
  const [multi, setMulti] = useState<ItinerariesResult | null>(null);
  const [compares, setCompares] = useState<Partial<Record<'bdtopo' | 'osm', CompareResult | 'loading'>>>({});
  const [computing, setComputing] = useState(false);
  const [error, setError] = useState('');
  const [extraction, setExtraction] = useState<{ result: ExtractResult; stage: number; progress: number } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [loadingLayers, setLoadingLayers] = useState<Set<string>>(new Set());
  const routeAbort = useRef<AbortController | null>(null);
  const extractAbort = useRef<AbortController | null>(null);

  const sheet = sheets.find((s) => s.id === sheetId) ?? sheets[0] ?? null;

  // ------------------------------------------------------------------ démarrage : feuilles et état du back end
  useEffect(() => {
    getSheets().then((s) => {
      setSheets(s);
      if (!s.find((x) => x.id === sheetId) && s[0]) setSheetId(s[0].id);
    }).catch(() => setBackendError('Le serveur de calcul ne répond pas. Lancez interface/run.sh sur le cluster.'));
  }, []);
  useEffect(() => {
    let stop = false;
    const poll = () => getStatus().then((st) => {
      if (stop) return;
      setStatus(st);
      if (!st.ready) setTimeout(poll, 2000);
    }).catch(() => !stop && setTimeout(poll, 4000));
    poll();
    return () => {
      stop = true;
    };
  }, []);
  useEffect(() => {
    if (sheet && !sheet.models.find((m) => m.id === options.model)) setOptions((o) => ({ ...o, model: sheet.default_model }));
  }, [sheet]);
  useEffect(() => {
    try {
      localStorage.setItem(STORE, JSON.stringify({ sheet: { id: sheetId }, options, layers, scenario }));
    } catch {
      /* stockage indisponible : sans conséquence */
    }
  }, [sheetId, options, layers, scenario]);

  const patchScenario = (p: Partial<Scenario>) => setScenario((s) => ({ ...s, ...p }));
  const patchOptions = (p: Partial<Options>) => setOptions((o) => ({ ...o, ...p }));

  // ------------------------------------------------------------------ outils de la carte
  const changeTool = (t: ToolId) => {
    setTool(t);
    setDraft([]);
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setDraft([]);
        setTool('pan');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const onPoint = (kind: 'start' | 'end' | 'waypoint', p: LonLat) => {
    if (kind === 'start') patchScenario({ start: p });
    if (kind === 'end') patchScenario({ end: p });
    if (kind === 'waypoint') setScenario((s) => ({ ...s, waypoints: [...s.waypoints, p] }));
    if (kind !== 'waypoint') setTool('pan');
    setTab('scenario');
  };
  const onMovePoint = (kind: 'start' | 'end' | 'waypoint', i: number, p: LonLat) => {
    if (kind === 'waypoint') setScenario((s) => ({ ...s, waypoints: s.waypoints.map((w, j) => (j === i ? p : w)) }));
    else patchScenario({ [kind]: p });
  };
  const onPolygonClose = () => {
    if (draft.length >= 3) {
      if (tool === 'area') patchScenario({ area: draft });
      if (tool === 'avoid') setScenario((s) => ({ ...s, avoid: [...s.avoid, draft] }));
      if (tool === 'zfrom') patchScenario({ zoneFrom: draft, mode: 'distinct' });
      if (tool === 'zto') patchScenario({ zoneTo: draft, mode: 'distinct' });
    }
    setDraft([]);
    setTool('pan');
  };
  const ringOf = (b: BBox): LonLat[] => [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]];

  // ------------------------------------------------------------------ calcul d'itinéraire puis contrôles en parallèle
  const compute = useCallback(async () => {
    const distinct = scenario.mode === 'distinct';
    if (!sheet || (distinct ? !(scenario.zoneFrom || scenario.start) || !(scenario.zoneTo || scenario.end) : !scenario.start || !scenario.end)) return;
    routeAbort.current?.abort();
    const ctl = new AbortController();
    routeAbort.current = ctl;
    setComputing(true);
    setError('');
    setCompares({});
    if (distinct) {
      try {
        const m = await postItineraries(sheet.id, scenario, options, ctl.signal);
        setMulti(m);
        setRoute(null);
        setTab('results');
      } catch (e) {
        if (!ctl.signal.aborted) {
          setError((e as Error).message);
          setMulti(null);
          setTab('results');
        }
      } finally {
        if (!ctl.signal.aborted) setComputing(false);
      }
      return;
    }
    setMulti(null);
    try {
      const r = await postRoute(sheet.id, scenario, options, ctl.signal);
      setRoute(r);
      setTab('results');
      setLayers((l) => ({ ...l, refs: true }));
      const pending: Partial<Record<'bdtopo' | 'osm', 'loading'>> = {};
      for (const w of r.compare) pending[w] = 'loading';
      setCompares(pending);
      for (const w of r.compare) {
        getCompare(r.route_id, w, ctl.signal)
          .then((c) => setCompares((cs) => ({ ...cs, [w]: c })))
          .catch((e) => !ctl.signal.aborted && setCompares((cs) => ({ ...cs, [w]: { which: w, error: (e as Error).message } })));
      }
    } catch (e) {
      if (!ctl.signal.aborted) {
        setError((e as Error).message);
        setRoute(null);
        setTab('results');
      }
    } finally {
      if (!ctl.signal.aborted) setComputing(false);
    }
  }, [sheet, scenario, options]);

  // ------------------------------------------------------------------ extraction animée dans un rectangle
  const extract = async (b: BBox) => {
    if (!sheet) return;
    extractAbort.current?.abort();
    const ctl = new AbortController();
    extractAbort.current = ctl;
    setExtracting(true);
    setError('');
    setFocus(b);
    try {
      const r = await postExtract(sheet.id, options.model, b, options.traceThreshold, options.useBdtopo, ctl.signal);
      setExtraction({ result: r, stage: 0, progress: 0 });
      setPlaying(true);
    } catch (e) {
      if (!ctl.signal.aborted) {
        setError((e as Error).message);
        setTab('results');
      }
    } finally {
      if (!ctl.signal.aborted) setExtracting(false);
    }
  };

  useEffect(() => {
    if (!playing || !extraction) return;
    const last = STAGES.length - 1;
    if (extraction.stage < last) {
      const t = setTimeout(() => setExtraction((x) => (x ? { ...x, stage: x.stage + 1, progress: 0 } : x)), extraction.stage === 0 ? 1100 : 1800);
      return () => clearTimeout(t);
    }
    const t0 = performance.now() - extraction.progress * 2600;
    let raf = 0;
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / 2600);
      setExtraction((x) => (x ? { ...x, progress: k } : x));
      if (k < 1) raf = requestAnimationFrame(tick);
      else setPlaying(false);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, extraction?.stage]);

  // ------------------------------------------------------------------ exercices du PDF
  const loadExercise = (ex: PdfExercise) => {
    const pt = (k: string) => ex.points[k] as LonLat;
    const sc: Scenario = {
      ...EMPTY_SCENARIO,
      mode: scenario.mode,
      distinct: scenario.distinct,
      label: `Exercice ${ex.number}`,
      start: pt(ex.start),
      end: pt(ex.end),
      waypoints: ex.waypoints.map(pt),
      waypointOrder: ex.waypoint_order === 'free' ? 'free' : 'fixed',
      roundTrip: ex.round_trip,
      area: ex.area.length >= 3 ? ex.area.map(pt) : undefined,
      avoid: [],
      requirements: [...ex.requirements, ...ex.notes.map((n) => `Remarque : ${n}`)],
    };
    setScenario(sc);
    patchOptions({ objective: ex.objective, speedCap: ex.speed_cap_kmh });
    setRoute(null);
    setMulti(null);
    setCompares({});
    const all = Object.values(ex.points) as LonLat[];
    setFocus(bboxOfPolygon(all));
  };

  const clearAll = () => {
    setScenario((s) => ({ ...EMPTY_SCENARIO, mode: s.mode, distinct: s.distinct }));
    setRoute(null);
    setMulti(null);
    setCompares({});
    setExtraction(null);
    setPlaying(false);
    setRect(null);
    setDraft([]);
    setTool('pan');
    setError('');
  };

  const onLoading = useCallback((what: string, on: boolean) => {
    setLoadingLayers((s) => {
      const n = new Set(s);
      if (on) n.add(what);
      else n.delete(what);
      return n;
    });
  }, []);

  const doneSteps = status ? Object.values(status.steps).filter((v) => v.startsWith('prêt')).length : 0;
  const totalSteps = status ? Object.keys(status.steps).length : 0;
  const readyCompares: Partial<Record<'bdtopo' | 'osm', CompareResult>> = {};
  for (const [k, v] of Object.entries(compares)) if (v && v !== 'loading') readyCompares[k as 'bdtopo' | 'osm'] = v;

  const rectMenu = rect && (
    <div className="menu">
      <div className="menu-title">Zone sélectionnée</div>
      <button className="btn primary" onClick={() => { const r = rect; setRect(null); extract(r); }} disabled={extracting || options.model === 'none'}
        title={options.model === 'none' ? 'Choisissez un modèle U-Net (Configurer → Lecture de la carte)' : undefined}>{extracting ? 'Extraction…' : '▶ Voir l’extraction du réseau'}</button>
      <button className="btn" onClick={() => { patchScenario({ zoneFrom: ringOf(rect), mode: 'distinct' }); setRect(null); setTab('scenario'); }}>Zone de départ (itinéraires distincts)</button>
      <button className="btn" onClick={() => { patchScenario({ zoneTo: ringOf(rect), mode: 'distinct' }); setRect(null); setTab('scenario'); }}>Zone d’arrivée (itinéraires distincts)</button>
      <button className="btn" onClick={() => { patchScenario({ area: ringOf(rect) }); setRect(null); setTab('scenario'); }}>Zone de déploiement</button>
      <button className="btn" onClick={() => { setScenario((s) => ({ ...s, avoid: [...s.avoid, ringOf(rect)] })); setRect(null); setTab('scenario'); }}>Zone à éviter</button>
      <button className="btn" onClick={() => setFocus([...rect] as BBox)}>Zoomer</button>
      <button className="btn danger" onClick={() => setRect(null)}>Supprimer</button>
    </div>
  );

  return (
    <div className="app">
      <header className="app-header">
        <h1>AMIAD</h1>
        <select value={sheet?.id ?? ''} onChange={(e) => { setSheetId(e.target.value); clearAll(); }} title="Feuille de carte">
          {sheets.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
        <span className={`status ${status?.ready ? 'ok' : 'wait'}`} title={status ? Object.entries(status.steps).map(([k, v]) => `${k} : ${v}`).join('\n') : ''}>
          {backendError ? 'Serveur indisponible' : !status ? 'Connexion…' : status.ready ? 'Prêt' : `Préparation ${doneSteps}/${totalSteps}…`}
        </span>
        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>
              {t.label}{t.id === 'results' && (route || multi || extraction) ? ' •' : ''}
            </button>
          ))}
        </nav>
      </header>
      {backendError && <div className="banner">{backendError}</div>}
      <div className="app-body">
        <aside className="side">
          {tab === 'config' && <ConfigPanel sheet={sheet} options={options} onChange={patchOptions} />}
          {tab === 'scenario' && (
            <ScenarioPanel sheet={sheet} scenario={scenario} tool={tool} setTool={changeTool} onChange={patchScenario}
              onLoadExercise={loadExercise} onCompute={compute} computing={computing} />
          )}
          {tab === 'orders' && (
            <OrdersPanel
              uiContext={() => ({
                sheet: sheet?.label, mode: scenario.mode, start: scenario.start, end: scenario.end, waypoints: scenario.waypoints,
                waypoint_order: scenario.waypoints.length > 1 ? scenario.waypointOrder : undefined, round_trip: scenario.roundTrip || undefined,
                deployment_area: scenario.area, zones_to_avoid: scenario.avoid.length ? scenario.avoid : undefined,
                departure_zone: scenario.zoneFrom, arrival_zone: scenario.zoneTo,
                options: { reading_model: options.model, objective: options.objective, convoy_max_speed_kmh: options.speedCap },
                distinct_constraints: scenario.mode === 'distinct' ? scenario.distinct : undefined,
                route_result: route ? { length_km: route.total.length_km, duration_min: Math.round(route.total.duration_min), network: route.network,
                  km_built_up: route.legs.reduce((s, l) => s + l.km_built_up, 0) } : undefined,
                distinct_result: multi ? { network: multi.network, vehicle: multi.vehicle, all_constraints_ok: multi.all_ok,
                  itineraries: multi.itineraries.features.map((f) => ({ id: f.properties.id, length_km: f.properties.length_km, time_min: f.properties.time_min,
                    bridges: f.properties.bridges, critical_turns: f.properties.turns_critical, builtup_km: f.properties.builtup_km })) } : undefined,
              })}
              onApplyToMap={(m) => {
                setScenario((s) => ({ ...s, start: m.start ?? s.start, end: m.end ?? s.end, waypoints: m.waypoints ?? s.waypoints, area: m.area ?? s.area }));
                const pts = [m.start, m.end, ...(m.waypoints ?? []), ...(m.area ?? [])].filter(Boolean) as LonLat[];
                if (pts.length) setFocus(bboxOfPolygon(pts));
              }}
            />
          )}
          {tab === 'results' && <ResultsPanel route={route} multi={multi} compares={compares} extraction={extraction?.result ?? null} error={error} />}
          {tab !== 'scenario' && tab !== 'orders' && (scenario.mode === 'distinct' ? (scenario.zoneFrom || scenario.start) && (scenario.zoneTo || scenario.end) : scenario.start && scenario.end) && (
            <button className="btn primary big sticky" onClick={compute} disabled={computing}>{computing ? 'Calcul en cours…' : scenario.mode === 'distinct' ? 'Calculer les itinéraires' : 'Calculer l’itinéraire'}</button>
          )}
        </aside>
        <div className="map-wrap">
          <MapView
            sheet={sheet} options={options} layers={layers} scenario={scenario} tool={tool} draftPolygon={draft} rect={rect}
            rectMenu={rectMenu} route={route} multi={multi} compares={readyCompares} extraction={extraction} fitKey={fitKey} focus={focus}
            onPoint={onPoint} onMovePoint={onMovePoint} onPolygonVertex={(p) => setDraft((d) => [...d, p])} onPolygonClose={onPolygonClose}
            onRectangle={(b) => { setRect(b); setTool('pan'); }} onLoading={onLoading}
          />
          <Toolbar tool={tool} setTool={changeTool} onFit={() => setFitKey((k) => k + 1)} onClear={clearAll} />
          <LayerControl layers={layers} setLayers={setLayers} options={options} setTau={(t) => patchOptions({ tau: t })} loading={loadingLayers} />
          <div className="hint">{HINTS[tool]}{draft.length > 0 && ` (${draft.length} sommet${draft.length > 1 ? 's' : ''})`}</div>
          {(computing || extracting) && <div className="busy">{computing ? (scenario.mode === 'distinct' ? `Recherche de ${scenario.distinct.k} itinéraires distincts… (jusqu’à 1 min la première fois)` : 'Calcul de l’itinéraire…') : 'Extraction du réseau…'}</div>}
          {extraction && (
            <ExtractionPlayer result={extraction.result} stage={extraction.stage} progress={extraction.progress} playing={playing}
              onStage={(s) => setExtraction((x) => (x ? { ...x, stage: s, progress: s === STAGES.length - 1 ? 1 : 0 } : x))}
              onPlay={(pl) => { if (pl && extraction.stage === STAGES.length - 1 && extraction.progress >= 1) setExtraction((x) => (x ? { ...x, stage: 0, progress: 0 } : x)); setPlaying(pl); }}
              onClose={() => { setExtraction(null); setPlaying(false); }} />
          )}
        </div>
      </div>
    </div>
  );
}

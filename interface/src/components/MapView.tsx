import { useEffect, useRef, useState, type ReactNode } from 'react';
import maplibregl, { type GeoJSONSource, type Map as MLMap, type RasterTileSource } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { FeatureCollection, Feature } from 'geojson';
import { getVectors, tileUrl } from '../api';
import type { BBox, CompareResult, ExtractResult, LayerState, LonLat, Options, RouteResult, Scenario, SheetInfo, ToolId } from '../types';

export const STAGES = ['scan', 'probability', 'mask', 'skeleton', 'graph'] as const;

interface Props {
  sheet: SheetInfo | null;
  options: Options;
  layers: LayerState;
  scenario: Scenario;
  tool: ToolId;
  draftPolygon: LonLat[];
  rect: BBox | null;
  rectMenu?: ReactNode;
  route: RouteResult | null;
  compares: Partial<Record<'bdtopo' | 'osm', CompareResult>>;
  extraction: { result: ExtractResult; stage: number; progress: number } | null;
  fitKey: number;
  focus: BBox | null;
  onPoint: (kind: 'start' | 'end' | 'waypoint', p: LonLat) => void;
  onMovePoint: (kind: 'start' | 'end' | 'waypoint', index: number, p: LonLat) => void;
  onPolygonVertex: (p: LonLat) => void;
  onPolygonClose: () => void;
  onRectangle: (b: BBox) => void;
  onLoading: (what: string, on: boolean) => void;
}

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };
const VECTOR_LAYERS = ['bdtopo', 'osm', 'network'] as const;

const STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#eef0f3' } }],
};

function ringOf(b: BBox): LonLat[] {
  return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]];
}

function setData(map: MLMap, id: string, data: FeatureCollection) {
  (map.getSource(id) as GeoJSONSource | undefined)?.setData(data);
}

export default function MapView(p: Props) {
  const box = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const [ready, setReady] = useState(false);
  const [rasterVersion, setRasterVersion] = useState(0);
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null);
  const props = useRef(p);
  props.current = p;
  const markers = useRef<maplibregl.Marker[]>([]);
  const aborts = useRef<Record<string, AbortController>>({});
  const extIds = useRef<string[]>([]);

  // ------------------------------------------------------------------ création de la carte
  useEffect(() => {
    if (!box.current) return;
    const map = new maplibregl.Map({ container: box.current, style: STYLE, center: [4.9, 48.97], zoom: 8, attributionControl: false, maxZoom: 17 });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 140, unit: 'metric' }), 'bottom-left');
    map.on('load', () => {
      for (const id of [...VECTOR_LAYERS, 'refs', 'route', 'zones', 'draft', 'rect', 'ext-graph', 'ext-nodes']) {
        map.addSource(id, { type: 'geojson', data: EMPTY });
      }
      map.addLayer({ id: 'bdtopo', type: 'line', source: 'bdtopo', paint: { 'line-color': '#1f6feb', 'line-width': ['interpolate', ['linear'], ['zoom'], 9, 0.6, 14, 2.2], 'line-opacity': 0.75 } });
      map.addLayer({ id: 'osm', type: 'line', source: 'osm', paint: { 'line-color': '#1a7f37', 'line-width': ['interpolate', ['linear'], ['zoom'], 9, 0.6, 14, 2.2], 'line-opacity': 0.75, 'line-dasharray': [3, 1.5] } });
      map.addLayer({ id: 'network', type: 'line', source: 'network', paint: { 'line-color': '#2b6cb0', 'line-width': ['interpolate', ['linear'], ['zoom'], 9, 0.8, 14, 3], 'line-opacity': 0.9 } });
      map.addLayer({ id: 'ext-graph', type: 'line', source: 'ext-graph', paint: { 'line-color': '#ff7a00', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1.5, 15, 4] }, filter: ['<', ['get', 'order'], 0] });
      map.addLayer({ id: 'ext-nodes', type: 'circle', source: 'ext-nodes', layout: { visibility: 'none' }, paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 1.5, 15, 4], 'circle-color': ['match', ['get', 'kind'], 'dead end', '#d7301f', '#ffffff'], 'circle-stroke-color': '#7a2e00', 'circle-stroke-width': 1 } });
      map.addLayer({ id: 'refs', type: 'line', source: 'refs', paint: { 'line-color': ['match', ['get', 'which'], 'bdtopo', '#1f6feb', '#1a7f37'], 'line-width': 3, 'line-dasharray': [2, 1.5], 'line-opacity': 0.9 } });
      map.addLayer({ id: 'route-casing', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#ffffff', 'line-width': 8 } });
      map.addLayer({ id: 'route', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': ['match', ['get', 'leg'], 1, '#7b2cbf', '#d1495b'], 'line-width': 4.5 } });
      map.addLayer({ id: 'zones-fill', type: 'fill', source: 'zones', paint: { 'fill-color': ['match', ['get', 'kind'], 'avoid', '#bf3b2f', '#1a7f37'], 'fill-opacity': 0.12 } });
      map.addLayer({ id: 'zones-line', type: 'line', source: 'zones', paint: { 'line-color': ['match', ['get', 'kind'], 'avoid', '#bf3b2f', '#1a7f37'], 'line-width': 2, 'line-dasharray': [3, 2] } });
      map.addLayer({ id: 'draft-line', type: 'line', source: 'draft', paint: { 'line-color': '#8250df', 'line-width': 2, 'line-dasharray': [1, 1] } });
      map.addLayer({ id: 'draft-pts', type: 'circle', source: 'draft', filter: ['==', ['geometry-type'], 'Point'], paint: { 'circle-radius': 4, 'circle-color': '#8250df' } });
      map.addLayer({ id: 'rect-fill', type: 'fill', source: 'rect', paint: { 'fill-color': '#ff7a00', 'fill-opacity': 0.06 } });
      map.addLayer({ id: 'rect-line', type: 'line', source: 'rect', paint: { 'line-color': '#ff7a00', 'line-width': 2, 'line-dasharray': [2, 1] } });
      setReady(true);
    });

    // ---------------------------------------------------------------- outils : clic, double-clic, rectangle
    map.on('click', (e) => {
      const { tool } = props.current;
      const ll: LonLat = [e.lngLat.lng, e.lngLat.lat];
      if (tool === 'start' || tool === 'end' || tool === 'waypoint') return props.current.onPoint(tool, ll);
      if (tool === 'area' || tool === 'avoid') return props.current.onPolygonVertex(ll);
      if (tool === 'pan') showInfo(map, e);
    });
    map.on('dblclick', (e) => {
      const { tool } = props.current;
      if (tool === 'area' || tool === 'avoid') {
        e.preventDefault();
        props.current.onPolygonClose();
      }
    });
    let rectStart: maplibregl.LngLat | null = null;
    map.on('mousedown', (e) => {
      if (props.current.tool !== 'rect') return;
      e.preventDefault();
      rectStart = e.lngLat;
      map.dragPan.disable();
    });
    map.on('mousemove', (e) => {
      if (!rectStart) return;
      const b: BBox = [Math.min(rectStart.lng, e.lngLat.lng), Math.min(rectStart.lat, e.lngLat.lat), Math.max(rectStart.lng, e.lngLat.lng), Math.max(rectStart.lat, e.lngLat.lat)];
      setData(map, 'rect', { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ringOf(b)] } }] });
    });
    map.on('mouseup', (e) => {
      if (!rectStart) return;
      const b: BBox = [Math.min(rectStart.lng, e.lngLat.lng), Math.min(rectStart.lat, e.lngLat.lat), Math.max(rectStart.lng, e.lngLat.lng), Math.max(rectStart.lat, e.lngLat.lat)];
      rectStart = null;
      map.dragPan.enable();
      if (b[2] - b[0] > 0.002 && b[3] - b[1] > 0.002) props.current.onRectangle(b);
    });
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  const map = mapRef.current;

  // ------------------------------------------------------------------ curseur et double-clic selon l'outil
  useEffect(() => {
    if (!map) return;
    map.getCanvas().style.cursor = p.tool === 'pan' ? '' : 'crosshair';
    if (p.tool === 'area' || p.tool === 'avoid') map.doubleClickZoom.disable();
    else map.doubleClickZoom.enable();
  }, [map, p.tool]);

  // ------------------------------------------------------------------ fonds raster (tuiles du back end)
  useEffect(() => {
    if (!map || !ready || !p.sheet) return;
    const paint: Record<string, maplibregl.RasterLayerSpecification['paint']> = {
      scan: { 'raster-fade-duration': 150 },
      prob: { 'raster-opacity': 0.85, 'raster-fade-duration': 150 },
      uncert: { 'raster-opacity': 0.95, 'raster-fade-duration': 150 },
    };
    for (const id of ['scan', 'prob', 'uncert'] as const) {
      if (map.getLayer(id)) map.removeLayer(id);
      if (map.getSource(id)) map.removeSource(id);
      map.addSource(id, {
        type: 'raster', tiles: [tileUrl(id, p.sheet.id, p.options.model, props.current.options.tau)], tileSize: 256, maxzoom: 16, minzoom: 5,
        ...(p.sheet.bounds ? { bounds: p.sheet.bounds } : {}),
      });
      map.addLayer({ id, type: 'raster', source: id, paint: paint[id] }, 'bdtopo');
    }
    setRasterVersion((v) => v + 1);
  }, [map, ready, p.sheet, p.options.model]);

  useEffect(() => {
    if (!map || !ready || !p.sheet || !map.getSource('uncert')) return;
    const t = setTimeout(() => (map.getSource('uncert') as RasterTileSource).setTiles([tileUrl('uncert', p.sheet!.id, p.options.model, p.options.tau)]), 180);
    // la couleur du réseau suit le seuil immédiatement (sans requête)
    map.setPaintProperty('network', 'line-color', p.layers.uncert
      ? ['case', ['<', ['coalesce', ['get', 'p'], 1], p.options.tau], '#d7301f', '#2b6cb0']
      : '#2b6cb0');
    return () => clearTimeout(t);
  }, [map, ready, p.sheet, p.options.model, p.options.tau, p.layers.uncert, rasterVersion]);

  // ------------------------------------------------------------------ visibilité des couches
  useEffect(() => {
    if (!map || !ready) return;
    const vis: Record<string, boolean> = {
      scan: p.layers.scan, prob: p.layers.prob, uncert: p.layers.uncert, network: p.layers.network,
      bdtopo: p.layers.bdtopo, osm: p.layers.osm, refs: p.layers.refs,
    };
    for (const [id, on] of Object.entries(vis)) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
  }, [map, ready, p.layers, rasterVersion]);

  // ------------------------------------------------------------------ couches vectorielles : seulement la zone visible
  useEffect(() => {
    if (!map || !ready || !p.sheet) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const z = map.getZoom();
        const bb = map.getBounds();
        const bbox: BBox = [bb.getWest(), bb.getSouth(), bb.getEast(), bb.getNorth()];
        for (const layer of VECTOR_LAYERS) {
          const on = props.current.layers[layer];
          aborts.current[layer]?.abort();
          if (!on || z < 8) {
            setData(map, layer, EMPTY);
            continue;
          }
          const ctl = new AbortController();
          aborts.current[layer] = ctl;
          props.current.onLoading(layer, true);
          getVectors(layer, props.current.sheet!.id, props.current.options.model, bbox, z, ctl.signal)
            .then((fc) => setData(map, layer, fc))
            .catch(() => undefined)
            .finally(() => props.current.onLoading(layer, false));
        }
      }, 220);
    };
    refresh();
    map.on('moveend', refresh);
    return () => {
      clearTimeout(timer);
      map.off('moveend', refresh);
    };
  }, [map, ready, p.sheet, p.options.model, p.layers.bdtopo, p.layers.osm, p.layers.network]);

  // ------------------------------------------------------------------ vue : feuille entière ou zone ciblée
  useEffect(() => {
    if (!map || !ready || !p.sheet?.bounds) return;
    const b = p.sheet.bounds;
    map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 30, duration: 600 });
  }, [map, ready, p.sheet, p.fitKey]);
  useEffect(() => {
    if (!map || !ready || !p.focus) return;
    const b = p.focus;
    map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 60, duration: 700, maxZoom: 15 });
  }, [map, ready, p.focus]);

  // ------------------------------------------------------------------ scénario : zones, brouillon, rectangle, points
  useEffect(() => {
    if (!map || !ready) return;
    const sc = p.scenario;
    const zones: Feature[] = [];
    if (sc.area && sc.area.length >= 3) zones.push({ type: 'Feature', properties: { kind: 'area' }, geometry: { type: 'Polygon', coordinates: [[...sc.area, sc.area[0]]] } });
    for (const z of sc.avoid) zones.push({ type: 'Feature', properties: { kind: 'avoid' }, geometry: { type: 'Polygon', coordinates: [[...z, z[0]]] } });
    setData(map, 'zones', { type: 'FeatureCollection', features: zones });
    const d = p.draftPolygon;
    setData(map, 'draft', {
      type: 'FeatureCollection',
      features: [
        ...(d.length >= 2 ? [{ type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: d } }] : []),
        ...d.map((c) => ({ type: 'Feature' as const, properties: {}, geometry: { type: 'Point' as const, coordinates: c } })),
      ],
    });
    setData(map, 'rect', p.rect ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ringOf(p.rect)] } }] } : EMPTY);

    for (const m of markers.current) m.remove();
    markers.current = [];
    const add = (ll: LonLat, cls: string, label: string, kind: 'start' | 'end' | 'waypoint', i: number, title: string) => {
      const el = document.createElement('div');
      el.className = `pin ${cls}`;
      el.textContent = label;
      el.title = `${title} — glisser pour déplacer`;
      const m = new maplibregl.Marker({ element: el, draggable: true }).setLngLat(ll).addTo(map);
      m.on('dragend', () => {
        const q = m.getLngLat();
        props.current.onMovePoint(kind, i, [q.lng, q.lat]);
      });
      markers.current.push(m);
    };
    sc.waypoints.forEach((w, i) => add(w, 'wp', String(i + 1), 'waypoint', i, `Étape ${i + 1}`));
    if (sc.start) add(sc.start, 'start', 'D', 'start', 0, 'Départ');
    if (sc.end) add(sc.end, 'end', 'A', 'end', 0, 'Arrivée');
  }, [map, ready, p.scenario, p.draftPolygon, p.rect]);

  // ------------------------------------------------------------------ menu du rectangle (suit la carte)
  useEffect(() => {
    if (!map || !p.rect) {
      setMenuPos(null);
      return;
    }
    const place = () => {
      const pt = map.project([p.rect![2], p.rect![3]]);
      setMenuPos({ x: pt.x, y: pt.y });
    };
    place();
    map.on('move', place);
    return () => {
      map.off('move', place);
    };
  }, [map, p.rect]);

  // ------------------------------------------------------------------ itinéraire : tracé progressif
  useEffect(() => {
    if (!map || !ready) return;
    if (!p.route) {
      setData(map, 'route', EMPTY);
      return;
    }
    const feats = p.route.route.features as Feature<GeoJSON.LineString>[];
    const total = feats.reduce((s, f) => s + f.geometry.coordinates.length, 0);
    const t0 = performance.now();
    const dur = 1300;
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / dur);
      let budget = Math.max(2, Math.round(total * k));
      const out: Feature[] = [];
      for (const f of feats) {
        const c = f.geometry.coordinates;
        if (budget <= 1) break;
        const n = Math.min(c.length, budget);
        out.push({ ...f, geometry: { type: 'LineString', coordinates: c.slice(0, n) } });
        budget -= n;
      }
      setData(map, 'route', { type: 'FeatureCollection', features: out });
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    const all = feats.flatMap((f) => f.geometry.coordinates);
    if (all.length) {
      const xs = all.map((c) => c[0]);
      const ys = all.map((c) => c[1]);
      map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], { padding: 70, duration: 700, maxZoom: 13 });
    }
    return () => cancelAnimationFrame(raf);
  }, [map, ready, p.route]);

  useEffect(() => {
    if (!map || !ready) return;
    const feats: Feature[] = [];
    for (const c of Object.values(p.compares)) {
      if (!c?.route) continue;
      for (const f of c.route.features) feats.push({ ...f, properties: { ...f.properties, which: c.which } });
    }
    setData(map, 'refs', { type: 'FeatureCollection', features: feats });
  }, [map, ready, p.compares]);

  // ------------------------------------------------------------------ extraction : images des étapes et graphe progressif
  const ext = p.extraction;
  useEffect(() => {
    if (!map || !ready) return;
    for (const id of extIds.current) {
      if (map.getLayer(id)) map.removeLayer(id);
      if (map.getSource(id)) map.removeSource(id);
    }
    extIds.current = [];
    if (!ext) {
      setData(map, 'ext-graph', EMPTY);
      setData(map, 'ext-nodes', EMPTY);
      return;
    }
    const [w, s, e, n] = ext.result.bounds;
    const coords: [[number, number], [number, number], [number, number], [number, number]] = [[w, n], [e, n], [e, s], [w, s]];
    for (const k of ['scan', 'probability', 'mask', 'skeleton'] as const) {
      const id = `ext-${k}`;
      map.addSource(id, { type: 'image', url: ext.result.images[k], coordinates: coords });
      map.addLayer({ id, type: 'raster', source: id, paint: { 'raster-opacity': 0, 'raster-resampling': 'nearest' } }, 'ext-graph');
      extIds.current.push(id);
    }
    setData(map, 'ext-graph', ext.result.graph);
    setData(map, 'ext-nodes', ext.result.nodes);
  }, [map, ready, ext?.result]);

  useEffect(() => {
    if (!map || !ready || !ext) return;
    const st = STAGES[ext.stage];
    const opacity: Record<string, number> = { 'ext-scan': 1, 'ext-probability': 0, 'ext-mask': 0, 'ext-skeleton': 0 };
    if (st === 'probability') opacity['ext-probability'] = 0.95;
    if (st === 'mask') opacity['ext-mask'] = 0.85;
    if (st === 'skeleton') opacity['ext-skeleton'] = 1;
    if (st === 'graph') opacity['ext-scan'] = 0.55;
    for (const [id, v] of Object.entries(opacity)) if (map.getLayer(id)) map.setPaintProperty(id, 'raster-opacity', v);
    const n = ext.result.graph.features.length;
    const shown = st === 'graph' ? Math.round(n * ext.progress) : 0;
    map.setFilter('ext-graph', ['<', ['get', 'order'], shown]);
    map.setLayoutProperty('ext-nodes', 'visibility', st === 'graph' && ext.progress >= 1 ? 'visible' : 'none');
  }, [map, ready, ext?.stage, ext?.progress, ext?.result]);

  return (
    <div ref={box} style={{ position: 'absolute', inset: 0 }}>
      {menuPos && p.rectMenu && (
        <div className="rect-menu" style={{
          left: menuPos.x + 8 + 240 > (box.current?.clientWidth ?? 9999) - 270 ? Math.max(menuPos.x - 248, 60) : menuPos.x + 8,
          top: Math.min(Math.max(menuPos.y, 8), (box.current?.clientHeight ?? 9999) - 230),
        }}>
          {p.rectMenu}
        </div>
      )}
    </div>
  );
}

/** Fiche d'information d'un tronçon cliqué (réseau extrait, BD TOPO, OSM). */
function showInfo(map: MLMap, e: maplibregl.MapMouseEvent) {
  const layers = ['network', 'bdtopo', 'osm', 'ext-graph'].filter((l) => map.getLayer(l) && map.getLayoutProperty(l, 'visibility') !== 'none');
  const f = map.queryRenderedFeatures([[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]], { layers })[0];
  if (!f) return;
  const src: Record<string, string> = { network: 'Réseau extrait (U-Net)', bdtopo: 'BD TOPO', osm: 'OpenStreetMap', 'ext-graph': 'Extraction' };
  const pr = f.properties as Record<string, unknown>;
  const rows: [string, unknown][] = [];
  if (f.layer.id === 'network' || f.layer.id === 'ext-graph') {
    rows.push(['Importance (1 = majeure)', pr.rank], ['Longueur', `${pr.length_m} m`]);
    if (pr.p !== undefined) rows.push(['Probabilité moyenne d’être une route', `${Math.round(Number(pr.p) * 100)} %`]);
  } else if (f.layer.id === 'bdtopo') {
    rows.push(['Nature', pr.nature], ['Importance', pr.importance], ['Numéro', pr.numero]);
  } else {
    rows.push(['Type', pr.fclass], ['Nom', pr.name], ['Référence', pr.ref], ['Vitesse max', pr.maxspeed ? `${pr.maxspeed} km/h` : null]);
  }
  const html = `<div class="info"><strong>${src[f.layer.id]}</strong>${rows
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && v !== 0 && v !== '0')
    .map(([k, v]) => `<div><span>${k}</span><b>${String(v)}</b></div>`).join('')}</div>`;
  new maplibregl.Popup({ closeButton: true, maxWidth: '280px' }).setLngLat(e.lngLat).setHTML(html).addTo(map);
}

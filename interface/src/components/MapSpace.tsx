import { useEffect, useRef, useState } from 'react';
import maplibregl, { type Map as MLMap } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { Feature, FeatureCollection, Project } from '../types';
import { applySample } from '../data/loaders';
import { ROLE_COLORS, SCENARIO_COLORS } from '../constants';

export interface MapClickInfo {
  lngLat: { lng: number; lat: number };
  features: { sourceId: string; geometryType: string; properties: Record<string, unknown> }[];
}

interface Props {
  project: Project | null;
  drawMode: string | null;
  pendingZone: [number, number][];
  onMapClick: (info: MapClickInfo) => void;
  onMapDblClick: (lngLat: { lng: number; lat: number }) => void;
}

const BG_STYLE = {
  version: 8 as const,
  sources: {},
  layers: [{ id: 'bg', type: 'background' as const, paint: { 'background-color': '#e9ecef' } }],
};

function geomKind(fc: FeatureCollection): 'line' | 'polygon' | 'point' | 'mixed' {
  const types = new Set(fc.features.map((f) => f.geometry?.type));
  if (types.has('LineString') || types.has('MultiLineString')) return 'line';
  if (types.has('Polygon') || types.has('MultiPolygon')) return 'polygon';
  if (types.has('Point') || types.has('MultiPoint')) return 'point';
  return 'mixed';
}

export default function MapSpace({ project, drawMode, pendingZone, onMapClick, onMapDblClick }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const fittedRef = useRef(false);
  const layerToSource = useRef<Record<string, string>>({});
  const handlers = useRef({ onMapClick, onMapDblClick, drawMode });
  handlers.current = { onMapClick, onMapDblClick, drawMode };

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BG_STYLE,
      center: [-2.35, 48.05],
      zoom: 8,
    });
    mapRef.current = map;
    const onLoad = () => setMapReady(true);
    map.on('load', onLoad);
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');
    map.on('click', (e) => {
      const { onMapClick, drawMode } = handlers.current;
      const vectorLayers = Object.keys(layerToSource.current);
      const feats = vectorLayers.length
        ? map.queryRenderedFeatures(e.point, { layers: vectorLayers })
        : [];
      onMapClick({
        lngLat: { lng: e.lngLat.lng, lat: e.lngLat.lat },
        features: feats.slice(0, 5).map((f) => ({
          sourceId: layerToSource.current[(f.layer as { id: string }).id] ?? 'inconnu',
          geometryType: f.geometry?.type ?? 'Inconnu',
          properties: (f.properties ?? {}) as Record<string, unknown>,
        })),
      });
      if (drawMode) e.preventDefault();
    });
    map.on('dblclick', (e) => {
      handlers.current.onMapDblClick({ lng: e.lngLat.lng, lat: e.lngLat.lat });
    });
    return () => {
      map.off('load', onLoad);
      map.remove();
      mapRef.current = null;
      setMapReady(false);
      fittedRef.current = false;
    };
  }, []);

  // Re-synchronise toutes les couches quand le projet change.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !project) return;
    layerToSource.current = {};
    const toRemove: string[] = [];
    // retire couches puis sources ajoutées par nous (tout sauf 'bg')
    for (const layer of map.getStyle().layers) if (layer.id !== 'bg') toRemove.push(layer.id);
    for (const id of toRemove) if (map.getLayer(id)) map.removeLayer(id);
    for (const sid of Object.keys(map.getStyle().sources)) map.removeSource(sid);

    let bounds: [[number, number], [number, number]] | null = null;

    for (const s of project.sources) {
      if (!s.visible || !s.loaded) continue;
      if (s.type === 'tiff' && s.image) {
        const sid = `tiff-${s.id}`;
        map.addSource(sid, {
          type: 'image',
          url: s.image.url,
          coordinates: s.image.coordinates as [number, number][] as [[number, number], [number, number], [number, number], [number, number]],
        });
        map.addLayer({ id: `src::${s.id}::raster`, type: 'raster', source: sid, paint: { 'raster-opacity': 0.9 } });
        const lons = s.image.coordinates.map((c) => c[0]);
        const lats = s.image.coordinates.map((c) => c[1]);
        const bb: [[number, number], [number, number]] = [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ];
        bounds = bounds ? [[Math.min(bounds[0][0], bb[0][0]), Math.min(bounds[0][1], bb[0][1])], [Math.max(bounds[1][0], bb[1][0]), Math.max(bounds[1][1], bb[1][1])]] : bb;
      }
      if (s.geojson && s.geojson.features.length) {
        const sid = `vec-${s.id}`;
        map.addSource(sid, { type: 'geojson', data: applySample(s.geojson, s.sample) });
        const kind = geomKind(s.geojson);
        const color = s.color || ROLE_COLORS[s.role];
        if (kind === 'line' || kind === 'mixed') {
          const lid = `src::${s.id}::line`;
          map.addLayer({ id: lid, type: 'line', source: sid, filter: ['in', '$type', 'LineString', 'MultiLineString'], paint: { 'line-color': color, 'line-width': 2, 'line-opacity': 0.9 } });
          layerToSource.current[lid] = s.id;
        }
        if (kind === 'polygon' || kind === 'mixed') {
          const fid = `src::${s.id}::fill`;
          const lid = `src::${s.id}::outline`;
          map.addLayer({ id: fid, type: 'fill', source: sid, filter: ['in', '$type', 'Polygon', 'MultiPolygon'], paint: { 'fill-color': color, 'fill-opacity': 0.25 } });
          map.addLayer({ id: lid, type: 'line', source: sid, filter: ['in', '$type', 'Polygon', 'MultiPolygon'], paint: { 'line-color': color, 'line-width': 1.5 } });
          layerToSource.current[fid] = s.id;
          layerToSource.current[lid] = s.id;
        }
        if (kind === 'point' || kind === 'mixed') {
          const cid = `src::${s.id}::point`;
          map.addLayer({ id: cid, type: 'circle', source: sid, filter: ['in', '$type', 'Point', 'MultiPoint'], paint: { 'circle-radius': 4, 'circle-color': color, 'circle-stroke-width': 1, 'circle-stroke-color': '#fff' } });
          layerToSource.current[cid] = s.id;
        }
      }
    }

    // scénario : zones puis ordre des points puis points
    const sc = project.scenario;
    const zoneFeatures: Feature[] = sc.zones.map((z) => ({
      type: 'Feature',
      geometry: { type: 'Polygon', coordinates: [[...z.coords, z.coords[0]]] },
      properties: { kind: z.kind, name: z.name },
    }));
    const pointFeatures: Feature[] = sc.points
      .filter((p) => p.lon !== undefined && p.lat !== undefined)
      .map((p, i) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [p.lon!, p.lat!] },
        properties: { name: p.name, ordre: i + 1 },
      }));
    const orderCoords = pointFeatures.map((f) => (f.geometry as unknown as { coordinates: [number, number] }).coordinates);
    const pendingCoords = pendingZone;

    const scenarioFC: FeatureCollection = { type: 'FeatureCollection', features: [...zoneFeatures, ...pointFeatures] };
    if (scenarioFC.features.length) {
      map.addSource('scenario', { type: 'geojson', data: scenarioFC });
      map.addLayer({
        id: 'scenario::zones', type: 'fill', source: 'scenario',
        filter: ['in', '$type', 'Polygon'],
        paint: {
          'fill-color': ['case', ['==', ['get', 'kind'], 'deployment'], SCENARIO_COLORS.zoneDeployment, SCENARIO_COLORS.zoneRestricted],
          'fill-opacity': 0.9,
        },
      });
      map.addLayer({
        id: 'scenario::zones-line', type: 'line', source: 'scenario',
        filter: ['in', '$type', 'Polygon'],
        paint: { 'line-color': ['case', ['==', ['get', 'kind'], 'deployment'], SCENARIO_COLORS.zoneDeploymentLine, SCENARIO_COLORS.zoneRestrictedLine], 'line-width': 2 },
      });
      map.addLayer({
        id: 'scenario::points', type: 'circle', source: 'scenario',
        filter: ['in', '$type', 'Point'],
        paint: { 'circle-radius': 6, 'circle-color': SCENARIO_COLORS.point, 'circle-stroke-width': 2, 'circle-stroke-color': '#fff' },
      });
      for (const id of ['scenario::zones', 'scenario::zones-line', 'scenario::points']) layerToSource.current[id] = 'scénario';
    }
    if (orderCoords.length >= 2) {
      map.addSource('scenario-order', { type: 'geojson', data: { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: orderCoords }, properties: {} }] } });
      map.addLayer({ id: 'scenario::order', type: 'line', source: 'scenario-order', paint: { 'line-color': SCENARIO_COLORS.raccordement, 'line-width': 2, 'line-dasharray': [2, 2] } });
    }
    if (pendingCoords.length >= 2) {
      map.addSource('scenario-pending', { type: 'geojson', data: { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: pendingCoords }, properties: {} }] } });
      map.addLayer({ id: 'scenario::pending', type: 'line', source: 'scenario-pending', paint: { 'line-color': SCENARIO_COLORS.raccordement, 'line-width': 2, 'line-dasharray': [1, 1] } });
    } else if (pendingCoords.length === 1) {
      map.addSource('scenario-pending', { type: 'geojson', data: { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: pendingCoords[0] }, properties: {} }] } });
      map.addLayer({ id: 'scenario::pending', type: 'circle', source: 'scenario-pending', paint: { 'circle-radius': 5, 'circle-color': SCENARIO_COLORS.raccordement } });
    }

    if (bounds && !fittedRef.current) {
      map.fitBounds(bounds, { padding: 30, maxZoom: 12 });
      fittedRef.current = true;
    }
  }, [project, pendingZone, mapReady]);

  return <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />;
}

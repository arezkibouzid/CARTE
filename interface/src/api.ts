import type {
  BackendStatus, BBox, CompareResult, ExtractResult, LonLat, Options, PdfResult, RouteResult, Scenario, SheetInfo,
} from './types';

/** Le back end est servi par la même origine (FastAPI sert aussi l'interface ; proxy Vite en développement). */
const API = '';

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = `Erreur ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* corps non JSON */
    }
    throw new Error(msg);
  }
  return (await res.json()) as T;
}

export const getSheets = () => fetch(`${API}/api/sheets`).then((r) => json<SheetInfo[]>(r));
export const getStatus = () => fetch(`${API}/api/status`).then((r) => json<BackendStatus>(r));

export function tileUrl(layer: 'scan' | 'prob' | 'uncert', sheet: string, model: string, tau: number): string {
  const q = new URLSearchParams({ sheet, model, tau: tau.toFixed(2) });
  return `${location.origin}${API}/api/tiles/${layer}/{z}/{x}/{y}.png?${q}`;
}

export function getVectors(layer: 'bdtopo' | 'osm' | 'network', sheet: string, model: string, bbox: BBox, zoom: number, signal: AbortSignal) {
  const q = new URLSearchParams({ sheet, model, bbox: bbox.map((v) => v.toFixed(5)).join(','), zoom: zoom.toFixed(1) });
  return fetch(`${API}/api/vectors/${layer}?${q}`, { signal }).then((r) => json<GeoJSON.FeatureCollection>(r));
}

export function postRoute(sheet: string, sc: Scenario, o: Options, signal?: AbortSignal) {
  const body = {
    sheet, model: o.model, start: sc.start, end: sc.end, waypoints: sc.waypoints, waypoint_order: sc.waypointOrder,
    objective: o.objective, round_trip: sc.roundTrip, speed_cap_kmh: o.speedCap, area: sc.area ?? null, avoid: sc.avoid,
    use_osm: o.useOsm, osm_mode: o.osmMode, use_bdtopo: o.useBdtopo,
    prob_threshold: o.penalize ? o.tau : null, prob_penalty: o.strength,
  };
  return fetch(`${API}/api/route`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
    .then((r) => json<RouteResult>(r));
}

export const getCompare = (routeId: string, which: 'bdtopo' | 'osm', signal?: AbortSignal) =>
  fetch(`${API}/api/route/${routeId}/compare/${which}`, { signal }).then((r) => json<CompareResult>(r));

export function postExtract(sheet: string, model: string, bbox: BBox, threshold: number, compare: boolean, signal?: AbortSignal) {
  return fetch(`${API}/api/extract`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({ sheet, model, bbox, threshold, compare_bdtopo: compare }),
  }).then((r) => json<ExtractResult>(r));
}

export const getPdf = (sheet: string) => fetch(`${API}/api/pdf?sheet=${encodeURIComponent(sheet)}`).then((r) => json<PdfResult>(r));

export function uploadPdf(file: File) {
  const fd = new FormData();
  fd.append('file', file);
  return fetch(`${API}/api/pdf/upload`, { method: 'POST', body: fd }).then((r) => json<PdfResult>(r));
}

export const bboxOfPolygon = (pts: LonLat[]): BBox => [
  Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1])),
];

import type {
  ChatReply, ConvoysResult, RouteChatReply, TextOrderResult, MapContext, OrderLinksResult, SavedScenario, ExplainLevel, OrderAnalysis, OrderInfo, OrdersStatus, Capabilities, ItinerariesResult, BackendStatus, BBox, CompareResult, ExtractResult, LonLat, Options, PdfResult, RouteResult, Scenario, SheetInfo,
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
    prob_threshold: o.penalize && o.model !== 'none' ? o.tau : null, prob_penalty: o.strength,
    uncertainty_cost: o.uncertaintyCost, uncertainty_strength: o.lambda,
    previous_legs: (sc.missionLog ?? []).flatMap((l) => l.lines), history_mode: sc.historyMode ?? 'forbid',
  };
  return fetch(`${API}/api/route`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
    .then((r) => json<RouteResult>(r));
}

export function postConvoys(sheet: string, sc: Scenario, o: Options, signal?: AbortSignal) {
  const body = {
    sheet, model: o.model, convoys: sc.convoys, area: sc.area ?? null, avoid: sc.avoid, use_osm: o.useOsm, osm_mode: o.osmMode,
    prob_threshold: o.penalize && o.model !== 'none' ? o.tau : null, prob_penalty: o.strength,
    uncertainty_cost: o.uncertaintyCost, uncertainty_strength: o.lambda, deconflict: sc.deconflict, share_penalty: sc.sharePenalty,
  };
  return fetch(`${API}/api/convoys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
    .then((r) => json<ConvoysResult>(r));
}

export function chatRoute(routeId: string, messages: { role: string; content: string }[], level: ExplainLevel, orderIds: string[],
  ui: Record<string, unknown> | null, useLlm = true) {
  return fetch(`${API}/api/route/${routeId}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, level, order_ids: orderIds, ui_context: ui, use_llm: useLlm }),
  }).then((r) => json<RouteChatReply>(r));
}

export const listScenarios = () => fetch(`${API}/api/scenarios`).then((r) => json<SavedScenario[]>(r));
export const getScenario = (id: string) => fetch(`${API}/api/scenarios/${encodeURIComponent(id)}`)
  .then((r) => json<{ name: string; saved: string; scenario: { scenario: Scenario; options: Options } }>(r));
export const saveScenario = (name: string, scenario: Scenario, options: Options) =>
  fetch(`${API}/api/scenarios`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, scenario: { scenario, options } }) })
    .then((r) => json<{ id: string }>(r));

export const getCapabilities = () => fetch(`${API}/api/capabilities`).then((r) => json<Capabilities>(r));

export function postItineraries(sheet: string, sc: Scenario, o: Options, signal?: AbortSignal) {
  const d = sc.distinct;
  const body = {
    sheet, model: o.model, use_osm: o.useOsm, origin_zone: sc.zoneFrom ?? null, destination_zone: sc.zoneTo ?? null,
    start: sc.start ?? null, end: sc.end ?? null, zone_radius_km: d.zoneRadiusKm, k: d.k, min_sep_km: d.sepKm,
    no_intersect: d.noIntersect, vehicle: d.vehicle, turn_aware: d.turns, area: sc.area ?? null, avoid: sc.avoid,
  };
  return fetch(`${API}/api/itineraries`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
    .then((r) => json<ItinerariesResult>(r));
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

// ---------------------------------------------------------------------------- ordres d'opération
export const getOrders = () => fetch(`${API}/api/orders`).then((r) => json<{ orders: OrderInfo[]; status: OrdersStatus }>(r));

export const getOrderAnalysis = (id: string, level: ExplainLevel) =>
  fetch(`${API}/api/orders/${encodeURIComponent(id)}/analysis?level=${level}`).then((r) => json<OrderAnalysis>(r));

export const extractOrder = (id: string) =>
  fetch(`${API}/api/orders/${encodeURIComponent(id)}/extract`, { method: 'POST' }).then((r) => json<{ status: string; seconds: number }>(r));

export function uploadOrder(file: File) {
  const fd = new FormData();
  fd.append('file', file);
  return fetch(`${API}/api/orders/upload`, { method: 'POST', body: fd }).then((r) => json<{ id: string }>(r));
}

export function chatOrder(id: string, messages: { role: string; content: string }[], level: ExplainLevel, ui: Record<string, unknown> | null) {
  return fetch(`${API}/api/orders/${encodeURIComponent(id)}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, level, ui_context: ui }),
  }).then((r) => json<ChatReply>(r));
}

export const bboxOfPolygon = (pts: LonLat[]): BBox => [
  Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1])),
];

/** Ordre écrit → modifications proposées (rien n'est appliqué côté serveur). ``auto`` : renvoie is_command = false pour une question. */
export function parseTextOrder(text: string, sheet: string, mode: 'command' | 'auto' = 'command', map?: MapContext) {
  return fetch(`${API}/api/order-text`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, sheet, mode, map }),
  }).then((r) => json<TextOrderResult>(r));
}

/** Liens entre ordres et valeurs en vigueur sur ``ids`` et leur chaîne (ordres liés ajoutés). */
export const getOrderLinks = (ids: string[]) => fetch(`${API}/api/orders/links?ids=${encodeURIComponent(ids.join(','))}`).then((r) => json<OrderLinksResult>(r));
export const setOrderLink = (from_id: string, to_id: string, action: 'add' | 'reject' | 'reset', relation?: string) =>
  fetch(`${API}/api/orders/links`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ from_id, to_id, action, relation }) })
    .then((r) => json<unknown>(r));

import type { FeatureCollection } from 'geojson';

export type LonLat = [number, number];
export type BBox = [number, number, number, number]; // ouest, sud, est, nord

/** Identifiant du choix « sans modèle de segmentation » (itinéraire directement sur OSM / BD TOPO). */
export const NO_MODEL = 'none';

export interface ModelInfo {
  id: string;
  label: string;
  description?: string;
}

export interface SheetInfo {
  id: string;
  label: string;
  bounds: BBox | null;
  has_bdtopo: boolean;
  has_osm: boolean;
  has_pdf: boolean;
  models: ModelInfo[];
  default_model: string;
}

export interface BackendStatus {
  ready: boolean;
  steps: Record<string, string>;
}

/** Outils de la barre d'outils de la carte. */
export type ToolId = 'pan' | 'start' | 'end' | 'waypoint' | 'rect' | 'area' | 'avoid' | 'zfrom' | 'zto';

/** Outils qui dessinent un polygone (un clic par sommet, double-clic pour fermer). */
export const POLYGON_TOOLS: ToolId[] = ['area', 'avoid', 'zfrom', 'zto'];

export interface Scenario {
  label?: string; // ex. « Exercice 7 » quand chargé depuis le PDF
  start?: LonLat;
  end?: LonLat;
  waypoints: LonLat[];
  waypointOrder: 'fixed' | 'free';
  roundTrip: boolean;
  area?: LonLat[]; // zone de déploiement (DA)
  /** Journal de mission : les étapes déjà calculées ; la suivante part de l'arrivée de la dernière. */
  missionLog?: MissionLeg[];
  historyMode?: 'forbid' | 'penalize' | 'free';
  avoid: LonLat[][]; // zones à éviter (RA)
  requirements?: string[];
  /** single : un itinéraire (points, étapes) ; distinct : k itinéraires sous contraintes entre deux zones ;
   *  convoys : plusieurs convois planifiés ensemble (déconfliction des routes). */
  mode: 'single' | 'distinct' | 'convoys';
  convoys: ConvoyDef[];
  deconflict: 'none' | 'penalize' | 'forbid';
  sharePenalty: number;
  zoneFrom?: LonLat[]; // zone de départ (mode distinct)
  zoneTo?: LonLat[]; // zone d'arrivée (mode distinct)
  distinct: DistinctParams;
}

export interface ConvoyDef {
  name: string;
  start: LonLat;
  end: LonLat;
  waypoints: LonLat[];
  waypoint_order: 'fixed' | 'free';
  round_trip: boolean;
  speed_cap_kmh: number;
  objective: 'length' | 'time';
  priority: number;
}

export interface ConvoyResult {
  name: string;
  index: number;
  rank?: number;
  error?: string;
  total?: { length_km: number; duration_min: number };
  route?: FeatureCollection;
  legs?: Leg[];
  visit_order?: string[] | null;
  shared_km_with_earlier?: Record<string, number>;
  seconds?: number;
}

export interface ConvoysResult {
  convoys: (ConvoyResult | null)[];
  network: string;
  deconflict: string;
  share_penalty: number;
  uncertainty_cost: UncertaintyCost | null;
  uncertainty_cost_note: string | null;
  tiff_only: boolean;
  summary: { convoys: number; routed: number; total_km: number; shared_km: number; max_duration_min: number | null };
  log: string[];
  timing_s: Record<string, number>;
}

export interface UncertaintyCost {
  strength: number;
  p_ref: number;
  floor: number;
  km_low_confidence: number;
  network_mean_confidence: number;
}

export interface RouteSegment {
  id: string;
  leg: number;
  index: number;
  road_class: string;
  source: string;
  km: number;
  min: number;
  legal_kmh: number[];
  km_built_up: number;
  names: string[];
  bridges: number;
  cost_base: number;
  cost_priced: number;
  confidence?: number;
  min_confidence?: number;
  uncertainty_cost?: number;
  from: LonLat;
  to: LonLat;
  alternative: { km: number; min: number; cost: number; extra_cost: number; extra_cost_pct: number; extra_km: number; extra_min: number; confidence?: number } | null;
  verdict: 'unavoidable' | 'clearly best' | 'narrow choice';
  why: string;
}

export interface RouteTrace {
  objective: 'length' | 'time';
  unit: string;
  uncertainty_strength: number | null;
  criteria: string[];
  legs: { leg: number; name: string; segments: RouteSegment[] }[];
  alternatives: { kind: string; what: string; rejected_because: string; [k: string]: unknown }[];
  limitations: string[];
}

export interface RouteChatReply {
  answer: string;
  mode: string;
  cited_segments: string[];
  cited_rules: string[];
  cited_facts?: string[];
  sources: { rule_id: string; status: string; group: string; pdf_document: string; pdf_page: number; verification_status: string }[];
  model: string | null;
  context: { budget_tokens: number; used_tokens: number; omitted: string[]; shortened: string[]; linked_orders_added?: string[] } | null;
  seconds: number;
}

export interface SavedScenario {
  id: string;
  name: string;
  saved: string;
}

export interface DistinctParams {
  k: number; // nombre d'itinéraires
  sepKm: number; // écart minimal entre itinéraires
  noIntersect: boolean;
  vehicle: string;
  turns: boolean; // virages aux carrefours tarifés
  zoneRadiusKm: number; // rayon des zones autour de D et A quand elles ne sont pas dessinées
}

export interface Capabilities {
  vehicles: { id: string; label: string; width_m: number }[];
  threads: number;
}

export interface ItineraryProps {
  id: string;
  index: number;
  length_km: number;
  time_min: number;
  roads: string[];
  bridges: number;
  tunnels: number;
  builtup_km: number;
  narrow_km: number;
  width_unknown_km: number;
  steep_km: number;
  turns_difficult: number;
  turns_critical: number;
  min_radius_m: number | null;
}

export interface ItinerariesResult {
  itineraries: FeatureCollection<GeoJSON.LineString, ItineraryProps>;
  turns: FeatureCollection;
  difficult_areas: FeatureCollection;
  zones: FeatureCollection;
  checks: { label: string; ok: boolean; detail: string }[];
  all_ok: boolean;
  log: string[];
  solver: { turn_aware: boolean; tried: number; feasible_sets: number; alternatives: number };
  timing_s: Record<string, number>;
  network: string;
  vehicle: string;
}

export type DataPreset = 'tiff' | 'tiff_osm' | 'tiff_no_osm' | 'custom';

export interface Options {
  model: string;
  objective: 'length' | 'time';
  speedCap: number;
  useBdtopo: boolean;
  useOsm: boolean;
  osmMode: 'reference' | 'fill';
  tau: number; // seuil d'incertitude (probabilité calibrée)
  penalize: boolean;
  strength: number;
  traceThreshold: number;
  uncertaintyCost: boolean; // coût probabiliste (U-Net), seulement quand l'itinéraire repose sur le seul TIFF
  lambda: number; // intensité du coût d'incertitude
}

export interface LayerState {
  scan: boolean;
  prob: boolean;
  uncert: boolean;
  network: boolean;
  bdtopo: boolean;
  osm: boolean;
  refs: boolean;
  hard: boolean; // zones de manœuvre difficile (itinéraires distincts)
}

export interface Leg {
  name: string;
  length_km: number;
  duration_min: number;
  mean_speed_kmh: number;
  km_built_up: number;
  km_below_prob_threshold?: number;
  mean_prob?: number;
  inside_area: boolean;
  continuous: boolean;
  km_by_speed_limit: Record<string, number>;
  doubtful_sections?: { count: number; km: number };
  mean_confidence?: number;
  km_low_confidence?: number;
  uncertainty_cost_share?: number;
}

export interface RouteResult {
  route_id: string;
  route: FeatureCollection;
  total: { length_km: number; duration_min: number };
  legs: Leg[];
  log: string[];
  points: Record<string, { snap_m: number; inside_area: boolean }>;
  visit_order?: string[];
  objective: 'length' | 'time';
  network: string;
  probability_penalty: { threshold: number; strength: number; sections_priced: number; km_below_threshold: number } | null;
  uncertainty_cost: UncertaintyCost | null;
  history?: { mode: string; legs: number; km_of_previous_legs: number; relaxed_m?: number; km_reused: number } | null;
  uncertainty_cost_note: string | null;
  tiff_only: boolean;
  explanation: RouteTrace | null;
  compare: ('bdtopo' | 'osm')[];
  timing_s: Record<string, number>;
}

export interface CompareResult {
  which: 'bdtopo' | 'osm';
  error?: string;
  total?: { length_km: number; duration_min: number };
  route?: FeatureCollection;
  length_gap?: number;
  time_gap?: number;
  overlap?: number;
  timing_s?: number;
}

export interface ExtractResult {
  bounds: BBox;
  images: Record<'scan' | 'probability' | 'mask' | 'skeleton', string>;
  graph: FeatureCollection;
  nodes: FeatureCollection;
  stats: {
    pixels: number;
    road_pixels: number;
    sections: number;
    km: number;
    junctions: number;
    dead_ends: number;
    pieces: number;
    largest_piece_share: number;
    mean_probability_on_roads: number | null;
    bdtopo?: { bdtopo_km: number; completeness?: number; correctness?: number };
  };
  timing_s: Record<string, number>;
}

export interface PdfExercise {
  number: number;
  points: Record<string, LonLat>;
  area: string[];
  start: string;
  end: string;
  waypoints: string[];
  waypoint_order: 'fixed' | 'free' | 'none';
  objective: 'length' | 'time';
  round_trip: boolean;
  speed_cap_kmh: number;
  requirements: string[];
  description: string;
  notes: string[];
}

export interface PdfResult {
  metadata: Record<string, string | number>;
  exercises: PdfExercise[];
}

// ---------------------------------------------------------------------------- ordres d'opération
export type ExplainLevel = 'concise' | 'standard' | 'detailed';

export interface OrderInfo {
  id: string;
  label: string;
  exercise_id: string;
  document_id: string | null;
  pages: number[] | null;
  task_types: string[] | null;
  extracted: boolean;
  schema_version?: string;
  review_status?: string | null;
  extraction_status?: string | null;
}

export interface OrdersStatus {
  explanation: { available: boolean; model: string | null; error: string | null };
  extraction: { configured: boolean; model: string | null };
  node: boolean;
  pages_ready: boolean;
}

export interface OrderFact {
  id: string;
  text: string;
  page: number | null;
  kind: string;
  category: string;
  scope: string | null;
  availability: string | null;
  quantities: string[];
}

export interface OrderRule {
  rank: number;
  rule_id: string;
  relevance_score: number;
  rule_text: string;
  rule_class: string;
  rule_value: string;
  rule_scope: string;
  source: { pdf_document: string; pdf_page: number } | null;
  verification_status: string;
  applicability_status: string;
  enforce: boolean;
  matched_exercise_requirement: string;
  how_it_helps: string;
  check: string[];
  number_check: { flag: string; rule_limit?: string; exercise_values?: string[] } | null;
  warnings: string[];
  group: 'applicable' | 'potential' | 'not_applicable';
  linked_facts: string[];
  explanation: string;
  explained_by: string;
}

export interface OrderAnalysis {
  order: string;
  exercise_id: string;
  schema_version: string;
  review_status: string | null;
  extraction: { status: string; model: string; seconds: number } | null;
  task_types: string[] | null;
  objective: string | null;
  facts: OrderFact[];
  issues: { kind: string; source_id?: string; text?: string }[];
  visual_notes: { source_id: string; page: number; text: string }[];
  map_inputs: { start?: LonLat; end?: LonLat; waypoints?: LonLat[]; area?: LonLat[] };
  known_facts: Record<string, string>;
  rules: OrderRule[];
  rag: { total_rules_in_csv: number; selected_rules_count: number; search_mode: string; policy: string };
  explanation: { level: ExplainLevel; model: string | null; seconds: number; fallback_reason: unknown };
}

export interface ChatReply {
  answer: string;
  cited_rules: string[];
  sources: { rule_id: string; status: string; group: string; pdf_document: string; pdf_page: number; verification_status: string }[];
  cited_facts: { id: string; page: number | null; text: string }[];
  model: string | null;
  seconds: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  reply?: ChatReply;
}

/** Ordre écrit → modifications du scénario, à prévisualiser (points nommés du PDF et coordonnées uniquement). */
export interface TextOrderResult {
  is_command: boolean;
  method?: 'model' | 'rules';
  model?: string | null;
  seconds?: number;
  scenario?: Partial<Scenario> & { _avoid_mode?: 'add'; area?: LonLat[] | null };
  options?: Partial<Options>;
  changes?: string[];
  notes?: string[];
  unresolved?: string[];
  coordinates?: { id: string; text: string; lonlat: LonLat }[];
  map_refs?: { id: string; text: string; label: string }[];
  model_problems?: string[];
  rules_found_more?: string[];
}

/** Ce qui est sur la carte, pour qu'un ordre écrit puisse y faire référence (« ici », « le départ », « l'étape 2 », « le rectangle »). */
export interface MapContext {
  start?: LonLat; end?: LonLat; waypoints: LonLat[]; avoid: LonLat[][]; area?: LonLat[]; rect?: BBox | null; pointed?: LonLat | null;
}

/** Dépendances entre ordres (calculées par le code) : liens, valeurs en vigueur, conflits. */
export interface OrderHeader { order: string; ref: string | null; kind: string | null; date: string | null; title: string; synthetic: boolean }
export interface OrderLink { from: string; to: string; relation: 'amends' | 'replaces' | 'cancels' | 'complements'; source: 'detected' | 'operator'; fact: string | null; page: number | null; evidence: string }
export interface ItemValue { key: string; label: string; value: number; unit: string; printed: string; order: string; fact: string; page: number | null }
export interface OrderLinksResult {
  selected: string[]; added: string[]; orders: OrderHeader[]; links: OrderLink[];
  void: { order: string; by: string; relation: string }[];
  effective: { key: string; label: string; in_force: ItemValue; replaced: ItemValue[]; reason: string }[];
  conflicts: { key: string; label: string; values: ItemValue[]; why: string }[];
  per_order: { key: string; label: string; values: ItemValue[] }[];
}

export interface MissionLeg {
  name: string; start: LonLat; end: LonLat; waypoints: LonLat[]; lines: LonLat[][]; length_km: number; duration_min: number;
}

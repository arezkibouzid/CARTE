/* DocRAG two-stage rule selection for the Rule Finder.
 *
 * Stage 1 (retrieval): BM25 over the final rules, top 50 candidates (see rulematch.js).
 * Stage 2 (selection): explicit, readable checks on each candidate. No language model, no embeddings.
 *   1. verification status         reviewed AND quote matches the PDF = verified. Only verified rules reach the final list.
 *   2. meaning                     the rule's planning aspect must serve a requirement the exercise states.
 *   3. scope and conditions        each condition is MET, UNKNOWN or CONTRADICTED against the exercise facts
 *                                  (text, deployment-area coordinates, and what the user knows from the map).
 *                                  A condition the exercise does not mention is UNKNOWN, never FALSE.
 *   4. applies now or needs info   all conditions MET -> enforceable. Any UNKNOWN -> conditional (never enforced).
 *   5. new knowledge               a mandatory rule the exercise already beats (stricter or equal) adds nothing.
 *   6. deduplication               rules saying the same thing keep one representative.
 *   7. exclusion                   everything else is listed with its reason, however high its BM25 score.
 * BM25 only retrieves. It never decides that a rule applies.
 * Works in a browser and in Node (module.exports).
 */
(function (root) {
  'use strict';
  const RM = (typeof module !== 'undefined' && module.exports) ? require('./rulematch.js') : root.RuleMatch;

  /* ---------------------------------------------------------------- 1. what the exercise requires */
  const KIND_PATTERNS = [
    ['classification', /classif|route reconnaissance|stanag 2174/i],
    ['network_structure', /penetration|lateral|cross routes?/i],
    ['movement_table', /movement (planning )?(table|chart)|march table|movement plan\b|planning table/i],
    ['deconfliction', /deconflict/i],
    ['holding_area', /holding area/i],
    ['halt_req', /\bhalts?\b|rest (halt|area|stop)/i],
    ['reporting_points', /reporting points?/i],
    ['traffic_control_req', /traffic control|traffic regulat|movement control/i],
    ['river_crossing', /river[- ]crossing|crossing points?|bridges? over the \w+ river/i],
    ['junction', /junction|intersection|congestion|saturation|flow rate|occupancy|smooth flow/i],
    ['escort_req', /escort/i],
    ['duration', /duration|time of departure/i],
    ['night', /night|sunset|sunrise|darkness/i],
    ['slowest_vehicle', /slowest vehicle/i],
    ['network_speed_limits', /legal speed limits?|speed limits? of the network/i],
    ['route_objective', /shortest|fastest|as fast as possible|as short as possible/i],
    ['restricted_area', /restricted area/i],
    ['waypoints', /waypoints?/i]
  ];
  const CONCEPT_KIND = { height: 'height_limit', weight_t: 'load_limit', mlc: 'load_limit', speed: 'speed', vehicle_gap: 'vehicle_gap', serial_gap: 'serial_gap',
    route_separation: 'route_separation', town: 'town_size', grade: 'grade_limit', width: 'width_limit', halt_time: 'halt_req' };
  const KIND_LABEL = { height_limit: 'height limit', load_limit: 'load limit (tonnes or MLC)', speed: 'speed', vehicle_gap: 'gap between vehicles', serial_gap: 'gap between serials',
    route_separation: 'distance between routes', town_size: 'town size', grade_limit: 'gradient', width_limit: 'road width', halt_req: 'halts', classification: 'route classification',
    network_structure: 'penetration and lateral routes', movement_table: 'movement table or chart', deconfliction: 'deconfliction', holding_area: 'vehicle holding area',
    reporting_points: 'reporting points', traffic_control_req: 'traffic control', river_crossing: 'river crossing', junction: 'junctions', escort_req: 'escort', duration: 'trip duration',
    night: 'night movement', slowest_vehicle: 'slowest-vehicle speed', network_speed_limits: 'legal speed limits of the network', route_objective: 'shortest or fastest route',
    restricted_area: 'restricted area', waypoints: 'waypoints', bridge_fact: 'a classified bridge on the route (known from the map)' };

  function segmentsOf(text) {
    const t = RM.clean(RM.cleanExercise(text));
    const out = [];
    t.split('\n').map(s => s.replace(/^\s*[•*-]\s*/, '').trim()).filter(Boolean).forEach(l => {
      if (l.length > 220) l.split(/(?<=[.!?])\s+(?=[A-Z])/).forEach(s => out.push(s.trim()));
      else out.push(l);
    });
    const seen = new Set();
    return out.filter(s => { const k = s.toLowerCase().replace(/\W+/g, ' ').trim(); if (!k || seen.has(k)) return false; seen.add(k); return true; });
  }
  function readRequirements(text) {
    return segmentsOf(text).map(seg => {
      const kinds = new Set();
      KIND_PATTERNS.forEach(([k, re]) => { if (re.test(seg)) kinds.add(k); });
      const qs = RM.quantities(seg).map(q => ({ raw: q.raw, value: q.value, unit: q.unit, concept: RM.conceptOf(q.unit, seg) }));
      qs.forEach(q => { if (CONCEPT_KIND[q.concept]) kinds.add(CONCEPT_KIND[q.concept]); });
      const qual = { builtUp: /built-up/i.test(seg) && !/outside (the )?built/i.test(seg), outside: /outside (the )?built/i.test(seg), night: /night/i.test(seg) };
      return { text: seg, kinds: [...kinds], qs, qual };
    });
  }

  /* ---------------------------------------------------------------- 2. facts about the exercise */
  const FR_BOX = { lat: [47.5, 49.45], lon: [2.5, 5.75] };   // Champagne-Lorraine; inside France, clear of Belgium, Luxembourg and Germany
  function deriveFacts(rawText, user, reqs) {
    user = user || {};
    const t = RM.clean(rawText);
    const pts = [...t.matchAll(/LAT\s*:\s*(-?[\d.]+)\s*\/\s*LONG\s*:\s*(-?[\d.]+)/gi)].map(m => [parseFloat(m[1]), parseFloat(m[2])]);
    let country = { value: null, source: null };
    if (pts.length && pts.every(([la, lo]) => la >= FR_BOX.lat[0] && la <= FR_BOX.lat[1] && lo >= FR_BOX.lon[0] && lo <= FR_BOX.lon[1])) country = { value: 'France', source: 'deployment-area coordinates' };
    else if (/\b(suippes|souilly|bar-le-duc|verdun|ch[aâ]lons-en-champagne)\b/i.test(t)) country = { value: 'France', source: 'place names in the exercise' };
    if (user.country) country = { value: user.country, source: 'your input' };
    const qs = reqs.flatMap(r => r.qs);
    const num = v => (v === '' || v == null || isNaN(parseFloat(v))) ? null : parseFloat(v);
    const massVals = qs.filter(q => q.concept === 'weight_t').map(q => q.value);
    const mlcVals = qs.filter(q => q.concept === 'mlc').map(q => q.value);
    const tri = v => (v === 'yes' || v === 'no') ? v : 'unknown';
    const mentions = { night: /night|sunset|sunrise|darkness/i.test(t), halts: /\bhalts?\b|rest (halt|area|stop)/i.test(t), bridges: /\bbridges?\b/i.test(t), escort: /escort/i.test(t),
      builtUp: /built-up/i.test(t), expressway: /expressway|motorway|autoroute/i.test(t) };
    return {
      country, mentions,
      mass_t: massVals.length ? Math.max(...massVals) : null,
      vehicle_mlc: num(user.vehicle_mlc) != null ? num(user.vehicle_mlc) : (mlcVals.length ? Math.max(...mlcVals) : null),
      bridge_mlc_min: num(user.bridge_mlc_min),
      longest_halt_min: num(user.longest_halt_min),
      bridges_on_route: tri(user.bridges_on_route),
      expressway_on_route: user.expressway_on_route ? tri(user.expressway_on_route) : (mentions.expressway ? 'yes' : 'unknown'),
      halts_planned: user.halts_planned ? tri(user.halts_planned) : (mentions.halts ? 'yes' : 'unknown')
    };
  }
  function requirementKinds(reqs, facts) {
    const k = new Set(); reqs.forEach(r => r.kinds.forEach(x => k.add(x)));
    if (facts.bridges_on_route === 'yes') k.add('bridge_fact');
    if (facts.mass_t != null || facts.vehicle_mlc != null) k.add('load_limit');
    return k;
  }

  /* ---------------------------------------------------------------- 3. what a rule is about */
  const bridgey = s => /bridge|ferr(y|ies)|raft|crossing/.test(s);
  const SPEED_UNIT = /\b(kph|km\/h|mph)\b/;
  const ASPECTS = [
    { id: 'bridge_exceptional', group: 'bridge_exceptional', label: 'exceptional or emergency bridge crossing', exceptional: true, serves: [],
      test: x => bridgey(x.t) && /caution crossing|risk (bridge )?crossing|gravest|exceptional|special bridge crossing|otherwise prohibited|designated authority|vehicle mlc\s*>\s*bridge|vehicle mlc > bridge/.test(x.sc) },
    { id: 'bridge_rating_assumption', group: 'bridge_rating_assumption', label: 'assumption used to rate a bridge', serves: [],
      whyNot: 'Design assumption used to compute the MLC rating of a bridge. It is not a spacing or speed rule for the convoy.',
      test: x => bridgey(x.t) && /bridge mlc rating|bridge rating|rating for vehicles in convoys|dynamic|\bdla\b|worst-case|critical span|design\/test|reserve buoyancy|water depth|current-speed|floating bridges and rafts|bridge loading|target crossing|maximum convoy mlc|bridge classification calculation|span >|rafts? and ferries|convoy spacing|no convoy spacing/.test(x.sc) },
    { id: 'bridge_normal_speed', group: 'bridge_normal_speed', label: 'speed on a normal bridge crossing',
      serves: [['load_limit', 'bridge_fact'], ['speed', 'duration']],
      helps: 'Speed cap for a normal crossing of a classified bridge. It is lower than ordinary road speeds, so it changes trip duration whenever the route crosses such a bridge.',
      test: x => bridgey(x.sc) && /normal/.test(x.sc) && SPEED_UNIT.test(x.V) },
    { id: 'bridge_normal_spacing', group: 'bridge_normal_spacing', label: 'vehicle spacing on a normal bridge crossing',
      serves: [['load_limit', 'bridge_fact'], ['vehicle_gap']],
      helps: 'Minimum spacing between vehicles while crossing a classified bridge. It is a bridge rule, not the ordinary gap between convoy vehicles.',
      test: x => bridgey(x.sc) && /normal/.test(x.sc) && /spacing/.test(x.V) },
    { id: 'mlc_vehicle_vs_bridge', group: 'mlc_vehicle_vs_bridge', label: 'vehicle MLC against bridge MLC', serves: [['load_limit']],
      helps: 'A vehicle may cross normally only if its MLC is not above the bridge MLC. The exercise gives tonnes, so the vehicle MLC has to be known first.',
      test: x => /vehicle (mlc|class|military load class)[^|]{0,25}(<=|≤|equal to or less)|vehicle mlc <= bridge|no higher than bridge/.test(x.sc) },
    { id: 'route_mlc', group: 'route_mlc', label: 'route MLC from its weakest bridge or road', serves: [['load_limit']],
      helps: 'The MLC of a route is that of its weakest bridge, or of its worst road section when there is no bridge.',
      test: x => /lowest bridge mlc|worst road section|route mlc classification/.test(x.sc) },
    { id: 'network_mlc', group: 'network_mlc', label: 'MLC classes of the road network', serves: [['load_limit', 'network_structure']],
      helps: 'Classes expected of the routes of a military road network.',
      test: x => /minimum route class|basic military road network|military road maneuver network/.test(x.sc) },
    { id: 'mlc_escalation', group: 'mlc_range', label: 'MLC range or very heavy vehicles', serves: [['load_limit']], dormantWithoutMlc: true,
      helps: 'Extra assessment for vehicles with a very high MLC.',
      test: x => /extremely heavy|mlc\s*>\s*1(20|50)|150\+|upper endpoint|lower endpoint|mlc\s*[≤<]\s*(150|4)\b|mlc\s*≥\s*4\b/.test(x.sc) },
    { id: 'bridge_headroom', group: 'bridge_headroom', label: 'headroom of a bridge', serves: [['height_limit']], scopeOk: true,
      helps: 'Doctrine on how much headroom a bridge should have and how a lower bridge is treated. Compare it with the vehicle height of the exercise.',
      test: x => /headroom/.test(x.sc) && /bridge/.test(x.sc) },
    { id: 'clearance_obstruction', group: 'clearance_obstruction', label: 'overhead clearance that counts as an obstruction', serves: [['height_limit', 'classification']], scopeOk: true,
      helps: 'Clearance below which a section is marked as an obstruction in route classification. It is a classification threshold, not the limit for the vehicle of the exercise.',
      test: x => /overhead clearance|underpass|tunnel/.test(x.sc) && /headroom|clearance/.test(x.sc) && /\bob\b|obstruction/.test(x.t) },
    { id: 'clearance_measure', group: 'clearance_measure', label: 'how overhead clearance is measured', serves: [['height_limit']], scopeOk: true,
      helps: 'How overhead clearance is measured and what changes it.',
      test: x => /overhead clearance|arched|underpass/.test(x.sc) && /clearance/.test(x.sc) },
    { id: 'bridge_width', group: 'bridge_width', label: 'width of a bridge roadway', serves: [['width_limit']],
      test: x => bridgey(x.sc) && /roadway width|clear roadway|prescribed (roadway )?width|desirable widths/.test(x.sc) },
    { id: 'bridge_structure', group: 'bridge_structure', label: 'structure of a bridge', serves: [], whyNot: 'Structural detail used when rating a civilian or floating bridge.',
      test: x => bridgey(x.sc) && /fixed civilian|truss|deck|two-way|lanes?|multilane|axle|posting|restriction signs?|bridge width|restriction/.test(x.sc) },
    { id: 'cross_country', group: 'cross_country', label: 'cross-country movement', serves: [['cross_country']],
      test: x => /cross-country|soil|snow|woodland|ledges|vertical obstacles/.test(x.sc) },
    { id: 'route_grade', group: 'route_grade', label: 'gradient of a route', serves: [['classification', 'grade_limit']],
      test: x => /slope|gradient/.test(x.sc) },
    { id: 'route_width', group: x => 'route_width:' + (/tracked/.test(x.sc) ? 't' : '') + (/wheeled/.test(x.sc) ? 'w' : '') + (/double/.test(x.sc) ? 'd' : '') + (/single/.test(x.sc) ? 's' : ''), label: 'width of a route or lane', serves: [['classification', 'width_limit']],
      test: x => /width|wide|lane|traveled way|narrowest/.test(x.sc) && !bridgey(x.sc) },
    { id: 'river_ford', group: 'river_ford', label: 'fords, rafts and waterways', serves: [['river_crossing']],
      test: x => /\bford|fording|swimming|river-crossing|inland waterway|waterway|river crossing/.test(x.sc) },
    { id: 'convoy_gap', group: 'convoy_gap', label: 'gap between convoy vehicles', serves: [['vehicle_gap']], scopeOk: true,
      helps: 'Doctrinal gaps between the vehicles of a convoy. Compare them with the gap fixed by the exercise.',
      test: x => /convoy vehicle gaps?|vehicle gaps?/.test(x.S) },
    { id: 'halt_schedule', group: 'halt_schedule', label: 'time scheduled for a rest halt', serves: [['movement_table', 'halt_req']], scopeOk: true,
      helps: 'How long to schedule for a rest halt in a movement plan.',
      test: x => /halt|rest/.test(x.sc) && /rest halt|scheduled for/.test(x.sc) },
    { id: 'halt_long', group: 'halt_long', label: 'long halts', serves: [['movement_table', 'halt_req']], scopeOk: true,
      helps: 'Long halts have to appear on the movement graph of the columns.',
      test: x => /long halts?/.test(x.C + ' ' + x.V) },
    { id: 'halt_short', group: 'halt_short', label: 'short halts', serves: [['movement_table', 'halt_req']], scopeOk: true,
      helps: 'How long short halts are and how often they should be planned.',
      test: x => /short (convoy )?halts?/.test(x.S) },
    { id: 'convoy_scheduling', group: 'convoy_scheduling', label: 'time gaps between scheduled convoys', serves: [['deconfliction', 'movement_table']],
      helps: 'Time separation between convoys in a centralized scheduling system.',
      test: x => /mobcon|centralized convoy scheduling|time gap|ten-minute/.test(x.sc) },
    { id: 'expressway_speed', group: 'expressway_speed', label: 'speed on an expressway', serves: [['speed']],
      helps: 'Maximum speed for military vehicles on an expressway.',
      test: x => /expressway/.test(x.sc) && SPEED_UNIT.test(x.V) },
    { id: 'route_signing', group: 'route_signing', label: 'route signs, panels and lights', serves: [], whyNot: 'Rule about signing and lighting a route. The exercise asks for no signing.',
      test: x => /\bsigns?\b|panel|reflector|light line|light-line|blackout|illumin|legib|light signals|warning|route-sign/.test(x.sc) },
    { id: 'escort', group: 'escort', label: 'escort and ambush response', serves: [['escort_req']],
      test: x => /escort|ambush|gun truck/.test(x.sc) },
    { id: 'parking', group: 'parking', label: 'truck parking', serves: [], whyNot: 'Concerns the use of public truck parking. The exercise plans no halts at truck parks.',
      test: x => /parking/.test(x.sc) },
    { id: 'axle_weight_limit', group: 'axle_weight_limit', label: 'axle or total weight limit', serves: [['load_limit']],
      helps: 'National axle and total weight limits for road vehicles.',
      test: x => /axle|permissible total weight|total axle|tonnes? per/.test(x.sc) },
    { id: 'vehicle_dimension_limit', group: 'vehicle_dimension_limit', label: 'overall vehicle length', serves: [], whyNot: 'Length limit for vehicle combinations. The exercise gives no vehicle length.',
      test: x => /overall|combination|trailer length|trailer-size|trailers? (of|with)|\bfeet\b|\bft\b|state truck/.test(x.sc) },
    { id: 'mlc_method', group: 'mlc_method', label: 'how a vehicle gets its MLC', serves: [], whyNot: 'Method for giving a vehicle its load class. It does not constrain the route.',
      test: x => /mlc classification|class number|load class|towing|trailers?|calculat|increment|reference curves|standard classes|short ton|military load class|classification/.test(x.sc) },
    { id: 'holding_area', group: 'holding_area', label: 'holding areas', serves: [['holding_area']],
      helps: 'Where and why convoys are held temporarily.',
      test: x => /holding areas?/.test(x.sc) },
    { id: 'movement_control', group: 'movement_control', label: 'movement control and staffing', serves: [['traffic_control_req']],
      test: x => /movement[- ]control/.test(x.sc) },
    { id: 'background', group: 'background', label: 'general movement doctrine', serves: [], whyNot: 'General background on movement support. It states no requirement for the exercise.',
      test: x => /movement|marshalling|convoy support|host-nation|border|transport|lines of communication|road movement|strategic convoys|national/.test(x.sc) },
    { id: 'convoy_definition', group: 'convoy_definition', label: 'definition of a convoy', serves: [], whyNot: 'Definition of a convoy. It states no requirement.',
      test: x => /convoy definition/.test(x.sc) },
    { id: 'other', group: 'other', label: 'not recognised', serves: [], whyNot: 'Not recognised as bearing on a requirement of this exercise.', test: () => true }
  ];
  function sx(rule) {
    const lc = s => (s || '').toLowerCase();
    const x = { S: lc(rule.scope), C: lc(rule.conditions), V: lc(rule.valueText), Q: lc(rule.quote) };
    x.sc = x.S + ' | ' + x.C + ' | ' + x.V; x.t = x.sc + ' | ' + x.Q;
    return x;
  }
  function classify(rule) { const x = sx(rule); return ASPECTS.find(a => a.test(x)); }

  /* ---------------------------------------------------------------- 4. conditions */
  const COUNTRY_MAP = [[/^\s*denmark|\(da\)/i, 'Denmark'], [/^\s*germany|\(ge\)/i, 'Germany'], [/^\s*spain|\(sp\)/i, 'Spain'], [/^\s*(united kingdom|uk\b)/i, 'United Kingdom'],
    [/^\s*france|\(fr\)/i, 'France'], [/^\s*(oklahoma|utah|oregon|washington|ohio|california|texas)/i, 'United States'], [/^\s*(u\.s\.|us\b|usa\b)/i, 'United States'],
    [/^\s*(belgium|italy|netherlands|canada)/i, 'another country']];
  function ruleCountry(rule) { const hit = COUNTRY_MAP.find(([re]) => re.test(rule.scope || '')); return hit ? hit[1] : null; }
  const TAGS = [
    { id: 'vehicle_mlc_vs_bridge', re: /vehicle (mlc|class|military load class)[^;|]{0,25}(<=|≤|equal to or less than|no higher than)|vehicle mlc\s*(<=|≤)\s*bridge/i,
      need: 'Vehicle MLC and the lowest bridge MLC on the route', label: 'vehicle MLC not above bridge MLC',
      evalf: f => (f.vehicle_mlc != null && f.bridge_mlc_min != null) ? (f.vehicle_mlc <= f.bridge_mlc_min ? ['MET', 'vehicle MLC ' + f.vehicle_mlc + ' <= bridge MLC ' + f.bridge_mlc_min] : ['CONTRADICTED', 'vehicle MLC ' + f.vehicle_mlc + ' is above bridge MLC ' + f.bridge_mlc_min]) : ['UNKNOWN', ''] },
    { id: 'mass_threshold', re: /(mass|weight)[^;|.]{0,40}?(≤|<=|<|>=|≥|>)\s*([\d.]+)\s*(tonnes?|t\b|tons?)/i,
      label: 'vehicle mass', need: 'Total mass of the vehicle',
      evalf: (f, m) => { if (f.mass_t == null) return ['UNKNOWN', '']; const op = m[2], lim = parseFloat(m[3]); const ok = (op === '≤' || op === '<=') ? f.mass_t <= lim : op === '<' ? f.mass_t < lim : (op === '≥' || op === '>=') ? f.mass_t >= lim : f.mass_t > lim;
        return ok ? ['MET', 'exercise mass ' + f.mass_t + ' t'] : ['CONTRADICTED', 'the rule needs mass ' + op + ' ' + lim + ' t, the exercise states ' + f.mass_t + ' t']; } },
    { id: 'expressway', re: /expressway|motorway/i, label: 'the vehicles are on an expressway', need: 'Is any part of the route an expressway?',
      evalf: f => f.expressway_on_route === 'yes' ? ['MET', 'you stated the route includes an expressway'] : f.expressway_on_route === 'no' ? ['CONTRADICTED', 'you stated the route has no expressway'] : ['UNKNOWN', ''] },
    { id: 'night', re: /sunset|sunrise|night|darkness|reduced visibility/i, label: 'the movement is at night', need: 'Is any of the movement at night?',
      evalf: f => f.mentions.night ? ['MET', 'the exercise mentions night or sunset'] : ['UNKNOWN', ''] },
    { id: 'halt_length', re: /halts?[^;|]{0,40}(≥|>=|at least)\s*(\d+)\s*min|(≥|>=)\s*(\d+)\s*min[^;|]{0,40}halt/i, label: 'a planned halt is long enough', need: 'Length of the longest planned halt (minutes)',
      evalf: (f, m) => { const lim = parseFloat(m[2] || m[4]); if (f.halts_planned === 'no') return ['CONTRADICTED', 'you stated no halt is planned']; if (f.longest_halt_min == null) return ['UNKNOWN', '']; return f.longest_halt_min >= lim ? ['MET', 'longest halt ' + f.longest_halt_min + ' min'] : ['CONTRADICTED', 'longest halt ' + f.longest_halt_min + ' min is below ' + lim + ' min']; } },
    { id: 'halts_planned', re: /\bhalts?\b|rest halt/i, label: 'a halt is planned', need: 'Does the plan contain a halt?',
      evalf: f => f.halts_planned === 'yes' ? ['MET', 'halts are planned'] : f.halts_planned === 'no' ? ['CONTRADICTED', 'you stated no halt is planned'] : ['UNKNOWN', ''] },
    { id: 'escort', re: /escort/i, label: 'an escort is present', need: 'Is the convoy escorted?',
      evalf: f => f.mentions.escort ? ['MET', 'the exercise mentions an escort'] : ['UNKNOWN', ''] },
    { id: 'bridge_on_route', re: /bridge|crossing|ferr(y|ies)|raft/i, label: 'the route crosses a classified bridge', need: 'Does the route cross a classified bridge? (map data)',
      evalf: f => f.bridges_on_route === 'yes' ? ['MET', 'you stated the route crosses a bridge'] : f.bridges_on_route === 'no' ? ['CONTRADICTED', 'you stated the route crosses no bridge'] : ['UNKNOWN', ''] }
  ];
  function conditionsOf(rule, aspect, facts) {
    const conds = [], unchecked = [], seen = new Set();
    const clauses = [];
    if (rule.scope && !aspect.scopeOk) clauses.push(rule.scope);
    (rule.conditions || '').split(/\s*;\s*/).filter(Boolean).forEach(c => clauses.push(c));
    clauses.forEach(cl => {
      const tag = TAGS.find(t => t.re.test(cl));
      if (!tag) { unchecked.push(cl); return; }
      if (seen.has(tag.id)) return; seen.add(tag.id);
      const [status, why] = tag.evalf(facts, cl.match(tag.re));
      conds.push({ tag: tag.id, label: tag.label, status, why, need: status === 'UNKNOWN' ? tag.need : '', clause: cl });
    });
    const rc = ruleCountry(rule);
    if (rc) {
      const fc = facts.country.value;
      const status = !fc ? 'UNKNOWN' : (fc === rc ? 'MET' : 'CONTRADICTED');
      conds.unshift({ tag: 'country', label: 'the exercise is in ' + rc, status, need: status === 'UNKNOWN' ? 'In which country is the exercise set?' : '',
        why: status === 'CONTRADICTED' ? 'the rule is specific to ' + rc + '; the exercise is in ' + fc + ' (from ' + facts.country.source + ')' : (status === 'MET' ? 'the exercise is in ' + fc : ''), clause: rule.scope });
    }
    return { conds, unchecked };
  }

  /* ---------------------------------------------------------------- 5. selection */
  const CLASS_RANK = { mandatory_constraint: 0, conditional_requirement: 0, recommendation: 1, threshold_trigger: 2, descriptive_value: 3 };
  function redundancy(rule, reqs) {
    if (!rule.hard) return null;
    const q0 = rule.val.qs[0], op = rule.val.op;
    if (!q0 || !rule.concept || !['≤', '<', '≥', '>'].includes(op)) return null;
    const mine = reqs.flatMap(r => r.qs).filter(q => q.concept === rule.concept && RM.FAMILY[q.unit] === RM.FAMILY[q0.unit]);
    if (!mine.length) return null;
    const b = RM.base(q0.value, q0.unit);
    const isMax = (op === '≤' || op === '<');
    if (mine.every(q => { const x = RM.base(q.value, q.unit); return isMax ? x <= b + 1e-9 : x >= b - 1e-9; }))
      return 'The exercise already states ' + mine.map(q => q.raw).filter((v, i, a) => a.indexOf(v) === i).join(', ') + ', which is ' + (isMax ? 'at or below' : 'at or above') + ' the rule bound (' + op + ' ' + q0.raw + '). The rule adds nothing.';
    return null;
  }
  // The CSV quote is sometimes a fragment that does not contain the number the rule states. The status is kept as it is; the page warns.
  function quoteWarning(rule) {
    const want = rule.val.qs.map(q => q.value).concat(rule.val.range ? [rule.val.range.lo, rule.val.range.hi] : []);
    if (!want.length) return null;
    const have = (RM.clean(rule.quote || '').match(/\d+(?:[.,]\d+)?/g) || []).map(x => parseFloat(x.replace(',', '.')));
    return want.some(v => have.some(n => Math.abs(n - v) < 1e-6)) ? null : 'The quote shown does not contain the value stated for this rule (' + rule.valueText + '). Read the full sentence on PDF page ' + rule.page + ' before relying on it.';
  }
  /* Importance, 0 to 100, to order the rules that passed every check. Four visible parts, no hidden weights:
     match 40       the exercise states a quantity of the same kind as the rule (1.0), or only a requirement the rule serves (0.6)
     strength 25    mandatory 1.0, recommendation 0.7, threshold 0.5, description 0.3
     applicability 20   conditions all confirmed 1.0; unknown: 0.6 minus 0.1 per missing fact (not below 0.2)
     search 15      BM25 score of the rule divided by the best BM25 score of the candidates
     a warning that the quote does not contain the stated value takes 8 points off.
     It orders rules. It never decides that a rule applies. */
  const STRENGTH = { mandatory_constraint: 1, conditional_requirement: 1, recommendation: 0.7, threshold_trigger: 0.5, descriptive_value: 0.3 };
  function importanceOf(i, reqs, maxScore) {
    const same = !!(i.rule.concept && reqs.some(r => r.qs.some(q => q.concept === i.rule.concept)));
    const match = same ? 1 : 0.6;
    const strength = STRENGTH[i.rule.cls] != null ? STRENGTH[i.rule.cls] : 0.3;
    const missing = i.missing.length + (i.unchecked.length ? 1 : 0);
    const appl = i.applicability === 'applies' ? 1 : Math.max(0.2, 0.6 - 0.1 * missing);
    const search = maxScore > 0 ? i.score / maxScore : 0;
    const penalty = i.warnings.length ? 8 : 0;
    const parts = { match: Math.round(40 * match), strength: Math.round(25 * strength), applicability: Math.round(20 * appl), search: Math.round(15 * search), warning: -penalty };
    return { value: Math.max(0, parts.match + parts.strength + parts.applicability + parts.search - penalty), parts, sameQuantity: same };
  }
  /* Small dictionary of related planning concepts. When the objective of the exercise mentions the left side, the right side is added to the BM25 query
     and may also link a rule to that objective. Edit this list to add a concept; there is nothing else to retrain. */
  const RELATED = [
    { name: 'junction congestion', when: /junction|intersection|congestion|saturation|occupancy|flow rate|smooth flow|deconflict/i,
      add: 'traffic control movement regulation critical point release point convoy scheduling time gap staggering order of march control post' },
    { name: 'movement table', when: /movement (planning )?(table|chart)|march table|planning table|order of march|movement plan\b/i,
      add: 'march table movement credit road movement graph schedule halt rest time gap column serial' },
    { name: 'route network', when: /penetration|lateral|axial|cross routes?|route network/i, add: 'axial route lateral route route number connecting route route network' },
    { name: 'passage of lines', when: /passage of lines|reception line|rearward|traffic control units?/i, add: 'withdraw reception line traffic control unit rearward passage' },
    { name: 'river crossing', when: /river[- ]crossing|crossing points?|bridges? (over|destroyed)/i, add: 'ford ferry raft bridging gap crossing engineer' },
    { name: 'holding and halts', when: /holding area|\bhalts?\b|rest area/i, add: 'holding area halt rest area parking' }
  ];
  const OBJECTIVE_KINDS = new Set(['movement_table', 'deconfliction', 'junction', 'traffic_control_req', 'classification', 'network_structure', 'river_crossing', 'holding_area', 'reporting_points', 'escort_req', 'halt_req']);
  // the segments that state what the exercise has to achieve: the task sentence, the deliverables and the vocabulary, not every word of the description
  function objectiveSegments(reqs) { return reqs.filter(r => r.kinds.some(k => OBJECTIVE_KINDS.has(k)) || /^(for this exercise|vocabulary)\b/i.test(r.text)); }
  const EXPANSION = {
    movement_table: 'halt rest stop column road movement graph schedule', deconfliction: 'time gap scheduling convoy interval', holding_area: 'holding area',
    reporting_points: 'reporting point control post', traffic_control_req: 'traffic control movement control', classification: 'route classification obstruction',
    network_structure: 'route network penetration lateral axial', junction: 'junction intersection congestion', river_crossing: 'crossing bridge ford ferry raft', escort_req: 'escort'
  };
  function select(matcher, exerciseText, user, opts) {
    opts = opts || {};
    exerciseText = String(exerciseText || '').replace(/\r\n?/g, '\n');
    const topN = opts.topN || 50;
    const reqs = readRequirements(exerciseText);
    const facts = deriveFacts(exerciseText, user, reqs);
    const kinds = requirementKinds(reqs, facts);
    const extra = [...kinds].filter(k => EXPANSION[k]).map(k => EXPANSION[k]).join(' ');
    const res = matcher.search(exerciseText + '\n' + extra, topN);
    const items = res.results.map(r => {
      const rule = r.rule, aspect = classify(rule);
      const it = { id: rule.id, rank: r.rank, score: r.score, rule, aspect: { id: aspect.id, label: aspect.label }, why: r.why, reasons: [], serves: [], outcome: '', helps: aspect.helps || '' };
      const { conds, unchecked } = conditionsOf(rule, aspect, facts);
      it.conds = conds; it.unchecked = unchecked; it.warnings = []; { const w = quoteWarning(rule); if (w) it.warnings.push(w); }
      // (a) a contradicted condition ends it
      conds.filter(c => c.status === 'CONTRADICTED').forEach(c => it.reasons.push({ code: c.tag === 'country' ? 'country' : 'condition', text: c.tag === 'country' ? 'Country mismatch: ' + c.why + '.' : 'Condition contradicted: ' + c.label + ' (' + c.why + ').' }));
      // (b) does it serve a requirement of this exercise?
      let unserved = null;
      aspect.serves.forEach(group => {
        const hit = group.filter(k => kinds.has(k));
        if (!hit.length && !unserved) unserved = group;
        it.serves.push({ wanted: group, met: hit, reqs: hit.map(k => (reqs.find(r => r.kinds.includes(k)) || { text: KIND_LABEL[k] || k }).text) });
      });
      if (aspect.exceptional) it.reasons.push({ code: 'exceptional', text: 'Exceptional or emergency bridge-crossing procedure. Nothing in the exercise points to it.' });
      else if (!aspect.serves.length || unserved) {
        it.reasons.push({ code: 'no_requirement', text: !aspect.serves.length ? (aspect.whyNot || 'Not recognised as bearing on a requirement of this exercise.')
          : 'Needs ' + unserved.map(k => KIND_LABEL[k] || k).join(' or ') + ', which this exercise does not state.' + (unserved.includes('bridge_fact') ? ' If the map shows a bridge on the route, say so under Known facts and it is re-evaluated.' : '') });
      }
      // (c) exceptional and dormant
      if (aspect.dormantWithoutMlc && !rule.hard && facts.vehicle_mlc == null)
        it.reasons.push({ code: 'dormant', text: 'Applies only to a vehicle MLC the exercise does not give (it gives tonnes). Unknown, not false: enter the vehicle MLC under Known facts to re-evaluate.' });
      // (d) redundant
      if (!it.reasons.length) { const red = redundancy(rule, reqs); if (red) it.reasons.push({ code: 'redundant', text: red }); }
      return it;
    });
    // (e) deduplicate among eligible candidates
    const eligible = items.filter(i => !i.reasons.length);
    const groups = new Map();
    eligible.forEach(i => { const a = classify(i.rule); const g = typeof a.group === 'function' ? a.group(sx(i.rule)) : a.group; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(i); });
    groups.forEach(list => {
      list.sort((a, b) => (b.rule.verified - a.rule.verified) || ((CLASS_RANK[a.rule.cls] ?? 9) - (CLASS_RANK[b.rule.cls] ?? 9)) || (b.score - a.score) || a.id.localeCompare(b.id));
      list.slice(1).forEach(d => d.reasons.push({ code: 'duplicate', text: 'Same requirement as ' + list[0].id + ', which is kept.', duplicateOf: list[0].id }));
    });
    // (f) verification and buckets
    const final = { enforceable: [], conditional: [], advisory: [] }, unverified = [];
    items.forEach(i => {
      const r = i.rule;
      const unknown = i.conds.some(c => c.status === 'UNKNOWN') || i.unchecked.length > 0;
      i.applicability = i.conds.some(c => c.status === 'CONTRADICTED') ? 'not_applicable' : (unknown ? 'unknown' : 'applies');
      i.missing = i.conds.filter(c => c.status === 'UNKNOWN').map(c => c.need);
      i.verification = r.verified ? 'verified' : (r.reviewed ? 'partly' : 'unverified');
      if (i.reasons.length) { i.outcome = 'excluded'; return; }
      if (!r.verified) { i.outcome = 'unverified'; unverified.push(i); return; }
      if (!r.hard) { i.outcome = 'advisory'; final.advisory.push(i); }
      else if (i.applicability === 'applies') { i.outcome = 'enforceable'; final.enforceable.push(i); }
      else { i.outcome = 'conditional'; final.conditional.push(i); }
    });
    const om = RM.origMap(exerciseText + '\n' + extra);
    const maxScore = items.length ? Math.max(...items.map(i => i.score)) : 0;
    [final.enforceable, final.conditional, final.advisory, unverified].forEach(list => {
      list.forEach(i => { const m = importanceOf(i, reqs, maxScore); i.importance = m.value; i.importanceParts = m.parts; i.sameQuantity = m.sameQuantity; });
      list.sort((a, b) => (b.importance - a.importance) || (a.rank - b.rank));
    });
    return { requirements: reqs, facts, kinds: [...kinds], query: res.query, queryWords: res.query.map(t => om.get(t) || t), expansion: extra, final, unverified, items,
      counts: { enforceable: final.enforceable.length, conditional: final.conditional.length, advisory: final.advisory.length, unverified: unverified.length,
        excluded: items.filter(i => i.outcome === 'excluded').length, retrieved: items.length } };
  }

  /* The output a planner can read. Only rules in "enforceable" have enforce = true. */
  function toJson(res, meta) {
    meta = meta || {};
    const rule = (i, bucket) => ({
      rule_id: i.id, bucket, enforce: bucket === 'enforceable', importance: i.importance, above_threshold: i.importance >= (meta.threshold != null ? meta.threshold : 55), importance_parts: i.importanceParts, bm25_rank: i.rank, bm25_score: Math.round(i.score * 100) / 100,
      rule_class: i.rule.cls, verification: i.verification, quote_matches_pdf: i.rule.quoteMatch || '',
      quote: i.rule.quote, document: i.rule.doc, pdf_page: i.rule.page, value: i.rule.valueText, scope: i.rule.scope,
      exercise_requirement: i.serves.flatMap(x => x.reqs).filter((v, k, a) => a.indexOf(v) === k),
      why_it_helps: i.helps || i.aspect.label,
      applicability: i.applicability,
      conditions: i.conds.map(c => ({ condition: c.label, status: c.status.toLowerCase(), detail: c.why || c.need || '' })),
      unchecked_conditions: i.unchecked, missing_information: i.missing, warnings: i.warnings
    });
    return {
      schema: 'docrag.selection.v1',
      exercise: { id: meta.id || null, title: meta.title || null },
      rules_file: meta.rulesFile || null,
      importance_threshold: meta.threshold != null ? meta.threshold : 55,
      facts: { country: res.facts.country.value, country_source: res.facts.country.source, mass_t: res.facts.mass_t, vehicle_mlc: res.facts.vehicle_mlc,
        bridges_on_route: res.facts.bridges_on_route, expressway_on_route: res.facts.expressway_on_route, halts_planned: res.facts.halts_planned },
      requirements: res.requirements.map(r => ({ text: r.text, kinds: r.kinds })),
      counts: res.counts,
      enforceable: res.final.enforceable.map(i => rule(i, 'enforceable')),
      conditional: res.final.conditional.map(i => rule(i, 'conditional')),
      advisory: res.final.advisory.map(i => rule(i, 'advisory')),
      verify_first: res.unverified.map(i => rule(i, 'verify_first')),
      excluded: res.items.filter(i => i.outcome === 'excluded').map(i => ({ rule_id: i.id, bm25_rank: i.rank, reason_code: i.reasons[0].code, reason: i.reasons[0].text })),
      notice: 'Only rules in "enforceable" may be enforced. "conditional" rules apply only if the missing information is true. "advisory" rules are guidance. BM25 rank is not proof of applicability.'
    };
  }

  /* ---------------------------------------------------------------- second-stage relevance ranking
     Every candidate that BM25 retrieves gets a score from 0 to 100, built from five visible parts minus penalties:
       search       0-20  BM25 compared with the typical top score (median of the best 5), capped at 1
       requirement  0-30  the rule serves a requirement of the exercise: all of its needs met 30, part of them 12, none 0
       constraint   0-25  same quantity as a number the exercise states 25; serves a requirement without a shared number 15
       scope        0-15  every condition confirmed 15; unknown conditions 10 minus 1.5 per missing fact (not below 3); contradicted 0
       guidance     0-10  hard rule that adds something 10; recommendation or threshold 7; description 4; already covered by the exercise 0
     penalties: exceptional or emergency procedure 30; same-looking number but no requirement served 25; same unit, different quantity 20;
                number is the only link 10; very-high-MLC rule with no MLC given 10; already covered by the exercise 15; a condition contradicted 20; tonnes against MLC 5.
     It is a normalized ranking score. It is not a probability, and it is not the same as applicability: status is a separate field.
     Relevant = the rule serves at least one requirement of the exercise and is not an exceptional procedure. Verification does not change relevance. */
  const SCORE_PARTS = { search: 20, requirement: 30, constraint: 25, scope: 15, guidance: 10 };
  const STATUS_LABEL = { applicable: 'Applicable', potential: 'Potentially applicable', advisory: 'Advisory and background', unverified: 'Unverified', not_applicable: 'Not applicable' };
  /* ---------------------------------------------------------------- policy and number checks
   * Three switches, so that each change can be measured on its own and the old behaviour can be restored (policy: 'strict').
   *   gate     true : a rule whose quote is not verified goes to the 'unverified' group and can never be enforced (old behaviour).
   *            false: verification no longer decides the group. The rule keeps its true verification_status, and the card and the JSON carry a
   *                   'check: the quote has not been verified against PDF page N' line. The status is never rewritten to "verified".
   *   unknown  true : an unknown condition is a note (the 'check' list), not a cap. Only a contradicted condition (another country) blocks a rule.
   *                   Still held back from 'applicable': a rule whose planning role is not recognised, and tonnes compared with MLC.
   *   numeric  true : each rule with a numeric limit is compared with the exercise values of the same kind (see numberCheck).
   */
  const POLICIES = { strict: { gate: true, unknown: false, numeric: false }, lenient: { gate: false, unknown: true, numeric: true } };
  function resolvePolicy(p) { if (p == null || p === 'lenient') return POLICIES.lenient; if (p === 'strict') return POLICIES.strict; return Object.assign({}, POLICIES.lenient, p); }
  const LIMIT_WORDS = /not exceed|no more than|at most|maxim|\bmax\b|up to|at least|minim|\bmin\b|no less than|not (?:less|lower|below|under) than|not above|not over|[≤≥]|<=|>=/i;
  // Compares the limit of a rule (<=, <, >=, >) with the numbers of the exercise that are the same kind of quantity (speed, a gap, a height ...).
  //   ok        every exercise value respects the limit
  //   exceeds   an exercise value breaks the limit (above a maximum, below a minimum)
  //   stricter  the exercise gives its own limit and the rule is stricter than it
  //   no value  the exercise gives no number of that kind
  // It is a flag only. It does not change the score or the group. The rule may apply to part of the route only (read its scope).
  function numberCheck(rule, reqs) {
    const q0 = rule.val.qs[0], op = rule.val.op;
    if (!q0 || !rule.concept || !['≤', '<', '≥', '>'].includes(op)) return null;
    const isMax = op === '≤' || op === '<', b = RM.base(q0.value, q0.unit), limit = op + ' ' + q0.raw, kind = RM.CONCEPT_LABEL[rule.concept] || rule.concept, eps = 1e-9;
    const mine = [];
    reqs.forEach(r => r.qs.forEach(q => { if (q.concept === rule.concept && RM.FAMILY[q.unit] === RM.FAMILY[q0.unit]) mine.push({ q, text: r.text }); }));
    if (!mine.length) return { flag: 'no value', rule_limit: limit, exercise_values: [], message: 'The exercise gives no ' + kind + ' to compare with ' + limit + '.' };
    let worst = 'ok'; const vals = [];
    mine.forEach(({ q, text }) => {
      const x = RM.base(q.value, q.unit), at = text.indexOf(q.raw), win = at >= 0 ? text.slice(Math.max(0, at - 32), at + q.raw.length + 8) : text;
      const exMax = /not exceed|no more than|at most|maxim|\bmax\b|up to|not above|not over|≤|<=/i.test(win), exMin = /at least|minim|\bmin\b|no less than|not (?:less|lower|below|under) than|≥|>=/i.test(win);
      let f;
      if (LIMIT_WORDS.test(win) && ((isMax && exMax) || (!isMax && exMin))) f = (isMax ? b < x - eps : b > x + eps) ? 'stricter' : 'ok';   // the exercise states a limit of its own
      else f = (isMax ? x <= b + eps : x >= b - eps) ? 'ok' : 'exceeds';                                                                         // the exercise states a value
      vals.push(q.raw + (f === 'ok' ? '' : f === 'stricter' ? ' (exercise limit, looser)' : ' (breaks the limit)'));
      if (f === 'exceeds' || (f === 'stricter' && worst !== 'exceeds')) worst = f;
    });
    const uniq = [...new Set(vals)];
    const msg = worst === 'exceeds' ? 'The exercise states ' + uniq.join(', ') + ': this exceeds the rule limit (' + limit + ') wherever the rule applies. Read its scope.'
      : worst === 'stricter' ? 'The rule limit (' + limit + ') is stricter than the limit the exercise states (' + uniq.join(', ') + ').' : 'The exercise values (' + uniq.join(', ') + ') respect the rule limit (' + limit + ').';
    return { flag: worst, rule_limit: limit, exercise_values: [...new Set(mine.map(m => m.q.raw))], message: msg };
  }
  function rank(matcher, exerciseText, user, opts) {
    opts = opts || {};
    const pol = resolvePolicy(opts.policy);
    const topN = opts.topN || 120;
    exerciseText = String(exerciseText || '').replace(/\r\n?/g, '\n');
    const reqs = readRequirements(exerciseText);
    const facts = deriveFacts(exerciseText, user, reqs);
    const kinds = requirementKinds(reqs, facts);
    const objSegs = objectiveSegments(reqs);
    const relatedAdd = RELATED.filter(e => objSegs.some(sg => e.when.test(sg.text))).map(e => e.add);
    const extra = [...kinds].filter(k => EXPANSION[k]).map(k => EXPANSION[k]).concat(relatedAdd).join(' ');
    const res = matcher.search(exerciseText + '\n' + extra, topN, { objective: objSegs.map(sg => sg.text).join('\n') + '\n' + extra, boost: 2 });
    const exQs = reqs.flatMap(r => r.qs);
    const hasMass = exQs.some(q => q.concept === 'weight_t'), hasMlc = exQs.some(q => q.concept === 'mlc');
    // OPTIONAL hybrid mode (opts.mode === 'hybrid' and opts.lsa): text similarity (LSA) to each objective of the exercise. It widens the candidate list and feeds the
    // search part of the score. It never marks a rule as applicable: see the 'semantic' basis below.
    const hybrid = !!(opts.lsa && opts.mode === 'hybrid');
    const idxOf = new Map(matcher.rules.map((x, i) => [x, i]));
    let sem = null, semRef = 1, results = res.results;
    if (hybrid) {
      const n = matcher.rules.length; sem = { max: new Float64Array(n), seg: new Array(n).fill(null) };
      objSegs.forEach(sg => { const sv = opts.lsa.sim(sg.text); for (let i = 0; i < n; i++) if (sv[i] > sem.max[i]) { sem.max[i] = sv[i]; sem.seg[i] = sg.text; } });
      const order = [...sem.max.keys()].sort((a, b) => sem.max[b] - sem.max[a] || a - b);
      sem.rank = new Int32Array(n); order.forEach((i, k) => { sem.rank[i] = k + 1; });
      const t5 = order.slice(0, 5).map(i => sem.max[i]); semRef = t5.length ? (t5[Math.floor((t5.length - 1) / 2)] || 1) : 1;
      const have = new Set(results.map(r => idxOf.get(r.rule)));
      const extra = order.filter(i => sem.max[i] >= (opts.poolMin == null ? 0.25 : opts.poolMin) && !have.has(i)).slice(0, opts.pool == null ? 30 : opts.pool);
      results = results.concat(extra.map((i, k) => matcher.judgeAt(i, res.exercise, res.results.length + k + 1)));
    }
    const top5 = res.results.map(r => r.score).sort((a, b) => b - a).slice(0, 5);
    const ref = top5.length ? (top5[Math.floor((top5.length - 1) / 2)] || 1) : 1;
    const maxScore = res.results.length ? res.results[0].score : 1;
    const items = results.map(r => {
      const rule = r.rule, aspect = classify(rule), ri = idxOf.get(rule);
      const { conds, unchecked } = conditionsOf(rule, aspect, facts);
      const groups = aspect.serves.map(g => ({ wanted: g, met: g.filter(k => kinds.has(k)) }));
      const metCount = groups.filter(g => g.met.length).length;
      let allMet = groups.length > 0 && metCount === groups.length, anyMet = metCount > 0;
      const contradicted = conds.filter(c => c.status === 'CONTRADICTED');
      const unknownCount = conds.filter(c => c.status === 'UNKNOWN').length + (unchecked.length ? 1 : 0);
      const exceptional = !!aspect.exceptional;
      const dormant = !!(aspect.dormantWithoutMlc && !rule.hard && facts.vehicle_mlc == null);
      const red = anyMet ? redundancy(rule, reqs) : null;
      const unit0 = rule.val.qs[0] ? rule.val.qs[0].unit : (rule.val.range ? rule.val.range.unit : '');
      const sameQuantity = !!(rule.concept && exQs.some(q => q.concept === rule.concept));
      const tonnesVsMlc = (rule.concept === 'mlc' && hasMass) || (rule.concept === 'weight_t' && hasMlc);
      const sameFamily = exQs.filter(q => unit0 && RM.FAMILY[q.unit] === RM.FAMILY[unit0]);
      const lookalike = !!(rule.concept && !sameQuantity && sameFamily.length && !tonnesVsMlc);
      // A rule whose planning aspect is not recognised (rules added after the aspect table was written) is not dropped. It is kept as relevant only when it shares
      // a PHRASE (two adjacent content words, such as "critical point" or "march table") with an objective of the exercise or with a related concept of the dictionary.
      // Single shared words never count: "unit" and "march" in two unrelated sentences are not a match.
      let wording = null;
      if (!anyMet && !exceptional && (aspect.id === 'other' || aspect.id === 'background')) {
        const idf = t => matcher.bm25.idf.get(t) || 0;
        const bigrams = text => { const tk = RM.tokens(text).filter(t => t.length >= 3 && /[a-z]/.test(t) && idf(t) >= 1.5); const out = new Set(); for (let k = 0; k + 1 < tk.length; k++) if (idf(tk[k]) >= 3 || idf(tk[k + 1]) >= 3) out.add(tk[k] + ' ' + tk[k + 1]); return out; };   // a phrase needs one distinctive word: "military movement" or "road movement" are everywhere
        const rb = bigrams([rule.scope, rule.conditions, rule.quote].join(' . '));
        objSegs.forEach(rq => {
          const direct = [...bigrams(rq.text)].filter(g => rb.has(g));
          RELATED.filter(e => e.when.test(rq.text)).forEach(e => { const hit = [...bigrams(e.add)].filter(g => rb.has(g)); if (hit.length && (!wording || wording.kind !== 'related' || hit.length > wording.phrases.length)) wording = { kind: 'related', req: rq.text, phrases: hit, concept: e.name }; });
          if (direct.length && !wording) wording = { kind: 'phrase', req: rq.text, phrases: direct };
        });
        if (wording) { anyMet = true; allMet = false; }
      }
      // Hybrid only: a rule whose planning role is not recognised, with no shared phrase, but whose text is among the closest to an objective of the exercise.
      // Kept as relevant with a low cap and never above 'advisory' or 'unverified': similar text does not show that the rule applies.
      let semantic = null;
      if (hybrid && !anyMet && !exceptional && !wording && (aspect.id === 'other' || aspect.id === 'background')
          && sem.max[ri] >= (opts.semMin == null ? 0.40 : opts.semMin) && sem.rank[ri] <= (opts.semTop == null ? 20 : opts.semTop)) {
        semantic = { req: sem.seg[ri], sim: sem.max[ri] }; anyMet = true; allMet = false;
      }
      const falseMatch = !anyMet && (sameQuantity || lookalike);
      // A real blocker kept from the strict policy: the exercise gives tonnes and the rule compares MLC classes, so the vehicle MLC is unknown and the tonnes must not stand in for it.
      const massForMlc = hasMass && facts.vehicle_mlc == null && conds.some(c => c.tag === 'vehicle_mlc_vs_bridge' && c.status === 'UNKNOWN');
      // parts
      const bmPart = Math.min(1, r.score / ref), mix = opts.mix == null ? 0.5 : opts.mix;
      const search = Math.round(20 * (hybrid ? mix * bmPart + (1 - mix) * Math.min(1, sem.max[ri] / semRef) : bmPart));
      const requirement = semantic ? 12 : wording ? (wording.kind === 'related' ? 20 : 16) : (allMet ? 30 : (anyMet ? 12 : 0));
      const constraint = !anyMet ? 0 : (semantic ? 6 : wording ? (wording.kind === 'related' ? 12 : 10) : (sameQuantity ? 25 : 15));
      const scope = contradicted.length ? 0 : (unknownCount === 0 ? 15 : Math.max(3, Math.round(10 - 1.5 * unknownCount)));
      const guidance = !anyMet ? 0 : (red ? 0 : (semantic ? 4 : wording ? (rule.hard ? 7 : 5) : (rule.hard ? 10 : (rule.cls === 'descriptive_value' ? 4 : 7))));
      const pen = [];
      if (exceptional) pen.push(['exceptional or emergency procedure', 30]);
      else if (falseMatch) pen.push([lookalike && !sameQuantity ? 'same unit but a different quantity' : 'same-looking number, no requirement served', lookalike && !sameQuantity ? 20 : 25]);
      else if (!anyMet) pen.push(['only shares words with the exercise', 10]);
      if (dormant) pen.push(['applies only above a vehicle MLC the exercise does not give', 10]);
      if (red) pen.push(['already covered by the exercise own limit', 15]);
      if (contradicted.length && anyMet) pen.push(['a condition of the rule is contradicted by the exercise', 20]);
      if (tonnesVsMlc) pen.push(['tonnes are not MLC: the numbers are not compared', 5]);
      const penalty = pen.reduce((a, x) => a + x[1], 0);
      const score = Math.max(0, Math.min(semantic ? 55 : wording ? 70 : 100, search + requirement + constraint + scope + guidance - penalty));
      const relevant = anyMet && !exceptional;
      const verification = rule.verified ? 'verified' : (rule.reviewed ? 'partly' : 'unverified');
      let status;
      if (!relevant) status = 'not_relevant';
      else if (contradicted.length) status = 'not_applicable';
      else if (pol.gate && !rule.verified) status = 'unverified';
      else if (red || !rule.hard || semantic) status = 'advisory';
      else if (pol.unknown) status = (tonnesVsMlc || massForMlc || wording) ? 'potential' : 'applicable';   // unknown conditions are checks, not a cap
      else if (unknownCount === 0) status = 'applicable';
      else status = 'potential';
      // what it addresses, what is missing
      const addresses = semantic ? [semantic.req] : wording ? [wording.req] : groups.flatMap(g => g.met.map(k => (reqs.find(q => q.kinds.includes(k)) || { text: KIND_LABEL[k] || k }).text)).filter((v, i, a) => a.indexOf(v) === i);
      const missing = conds.filter(c => c.status === 'UNKNOWN').map(c => c.need);
      groups.forEach(g => { if (!g.met.length && relevant) { const need = g.wanted.map(k => KIND_LABEL[k] || k).join(' or '); missing.push('The exercise does not state ' + need + (g.wanted.includes('bridge_fact') ? '. If the map shows a classified bridge on the route, say so under Known facts.' : '.')); } });
      if (semantic) missing.push('Kept by semantic similarity only (LSA similarity ' + semantic.sim.toFixed(2) + ' with this requirement). Similar text does not show that the rule applies or even that it bears on the requirement: a person must read the PDF page and confirm.');
      if (wording) missing.push((wording.kind === 'related' ? 'Kept as a related concept of "' + wording.concept + '" (shared phrase: ' + wording.phrases.join(', ') + ').' : 'Kept because it shares the phrase "' + wording.phrases.join(', ') + '" with this requirement.') + ' Its planning role is not recognised, so a person should confirm that it bears on this requirement.');
      if (tonnesVsMlc) missing.push('The exercise gives a mass in tonnes; this rule uses MLC. The vehicle MLC is needed before the two can be compared.');
      // 'check' = everything a person should confirm before relying on the rule. Informs; it does not block (except the cases held back above).
      const check = [];
      if (relevant) {
        conds.filter(c => c.status === 'UNKNOWN').forEach(c => check.push('check: ' + (c.need || c.label)));
        unchecked.forEach(c => check.push('check: ' + c + ' (stated by the rule, not checked)'));
        if (!rule.verified) check.push('check: the quote is ' + (rule.reviewed ? 'only partly matched' : 'not verified') + ' against the PDF, page ' + rule.page);
        if (tonnesVsMlc || massForMlc) check.push('check: the vehicle MLC, to compare tonnes with MLC (a mass in tonnes is not an MLC class)');
        if (wording) check.push('check: planning role not recognised, confirm that it bears on the requirement');
        if (semantic) check.push('check: kept by text similarity only, confirm that it bears on the requirement');
      }
      const numeric = (relevant && pol.numeric) ? numberCheck(rule, reqs) : null;
      const reasons = [];
      if (!relevant) reasons.push(exceptional ? 'Exceptional or emergency bridge-crossing procedure. Nothing in the exercise points to it.' : (aspect.whyNot || 'Not recognised as bearing on a requirement of this exercise.'));
      contradicted.forEach(c => reasons.push(c.tag === 'country' ? 'Country mismatch: ' + c.why + '.' : 'Condition contradicted: ' + c.label + ' (' + c.why + ').'));
      if (red) reasons.push(red);
      return { id: rule.id, rule, rank: r.rank, score: r.score, bm25Share: Math.round(100 * r.score / maxScore), why: r.why, aspect: { id: aspect.id, label: aspect.label, group: typeof aspect.group === 'function' ? aspect.group(sx(rule)) : aspect.group },
        relevance: Math.round(score), parts: { search, requirement, constraint, scope, guidance, penalties: pen.map(x => ({ reason: x[0], points: x[1] })) }, relevant, status, verification,
        applicability: contradicted.length ? 'not_applicable' : (unknownCount ? 'unknown' : 'applies'), conds, unchecked, missing, addresses, helps: semantic ? 'Semantic match only: planning role not recognised, read the PDF page to see whether it helps.' : wording ? (wording.kind === 'related' ? 'Related concept (' + wording.concept + '): ' : 'Shares a phrase with this requirement: ') + 'planning role not recognised, read the PDF page to see how it helps.' : (aspect.helps || aspect.label), reasons,
        warnings: (() => { const w = quoteWarning(rule); return w ? [w] : []; })(), sameQuantity, falseMatch, exceptional, redundant: !!red, check, numeric, basis: semantic ? 'semantic' : wording ? wording.kind : 'requirement', semanticSim: hybrid ? Math.round(sem.max[ri] * 1000) / 1000 : null };
    });
    // overlapping rules stay in the list, but are linked
    const byGroup = new Map();
    items.filter(i => i.relevant).forEach(i => { if (!byGroup.has(i.aspect.group)) byGroup.set(i.aspect.group, []); byGroup.get(i.aspect.group).push(i); });
    byGroup.forEach(list => list.forEach(i => { i.similarTo = list.filter(o => o !== i).map(o => o.id); }));
    items.forEach(i => { if (!i.similarTo) i.similarTo = []; });
    const order = (a, b) => (b.relevance - a.relevance) || (a.rank - b.rank);
    const relevant = items.filter(i => i.relevant).sort(order);
    relevant.forEach((i, k) => { i.relevanceRank = k + 1; });
    const notRelevant = items.filter(i => !i.relevant).sort(order);
    const groupsOut = { applicable: [], potential: [], advisory: [], unverified: [], not_applicable: [] };
    relevant.forEach(i => groupsOut[i.status].push(i));
    return { mode: hybrid ? 'hybrid' : 'bm25', policy: pol, requirements: reqs, facts, kinds: [...kinds], query: res.query, queryWords: res.query.map(t => (RM.origMap(exerciseText + '\n' + extra).get(t) || t)), expansion: extra, ref,
      items, relevant, notRelevant, groups: groupsOut,
      counts: { retrieved: items.length, relevant: relevant.length, applicable: groupsOut.applicable.length, potential: groupsOut.potential.length, advisory: groupsOut.advisory.length,
        unverified: groupsOut.unverified.length, not_applicable: groupsOut.not_applicable.length, not_relevant: notRelevant.length, corpus: matcher.rules.length } };
  }
  function rankJson(res, meta) {
    meta = meta || {};
    const rule = i => ({ rule_id: i.id, relevance_score: i.relevance, relevance_rank: i.relevanceRank || null, status: i.status, verification: i.verification, enforce: i.status === 'applicable',
      rule_class: i.rule.cls, quote: i.rule.quote, value: i.rule.valueText, scope: i.rule.scope, document: i.rule.doc, pdf_page: i.rule.page, quote_matches_pdf: i.rule.quoteMatch || '',
      exercise_requirement: i.addresses, relevance_basis: i.basis, how_it_could_contribute: i.helps, applicability: i.applicability, missing_conditions: i.missing, unchecked_conditions: i.unchecked,
      check: i.check, number_check: i.numeric, score_parts: i.parts, bm25_rank: i.rank, bm25_score: Math.round(i.score * 100) / 100, similar_rules: i.similarTo, warnings: i.warnings, reasons: i.reasons });
    return { schema: 'docrag.ranking.v1', exercise: { id: meta.id || null, title: meta.title || null }, rules_file: meta.rulesFile || null,
      score_note: 'relevance_score is a normalized ranking score from 0 to 100 (search 20, requirement 30, constraint 25, scope 15, guidance 10, minus penalties). It is not a probability of applicability. Only status "applicable" may be enforced.',
      facts: { country: res.facts.country.value, bridges_on_route: res.facts.bridges_on_route, expressway_on_route: res.facts.expressway_on_route, halts_planned: res.facts.halts_planned },
      counts: res.counts, relevant: res.relevant.map(rule), not_relevant: res.notRelevant.map(rule) };
  }

  /* The deliverable for one exercise: every relevant rule, best first. Read only, nothing here changes a score. */
  const APPLIC = { applicable: 'applicable', potential: 'potentially_applicable', advisory: 'advisory_background', unverified: 'unverified', not_applicable: 'not_applicable' };
  function exportRules(res, meta) {
    meta = meta || {};
    const pg = p => { const n = parseInt(p, 10); return isNaN(n) ? (p || null) : n; };
    const rules = res.relevant.map(i => ({
      rank: i.relevanceRank, rule_id: i.id, relevance_score: i.relevance,
      rule_text: i.rule.quote, rule_class: i.rule.cls, rule_value: i.rule.valueText || null, rule_scope: i.rule.scope || null,
      verification_status: i.verification, applicability_status: APPLIC[i.status] || i.status,
      enforce: i.status === 'applicable',
      source: { pdf_document: i.rule.doc, pdf_page: pg(i.rule.page) },
      matched_exercise_requirement: i.addresses && i.addresses.length ? i.addresses.join(' | ') : null,
      how_it_helps: i.helps || null,
      missing_conditions: i.missing || [], unchecked_conditions: i.unchecked || [], check: i.check || [], number_check: i.numeric || null,
      relevance_basis: i.basis, ...(i.semanticSim != null ? { semantic_similarity: i.semanticSim } : {}), score_parts: i.parts, warnings: i.warnings || []
    }));
    return {
      exercise_id: meta.id != null ? String(meta.id) : null,
      exercise_objective: meta.objective || null,
      rules_file: meta.rulesFile || null,
      total_rules_in_csv: res.counts.corpus,
      retrieved_candidates: res.counts.retrieved,
      selected_rules_count: rules.length,
      search_mode: res.mode || 'bm25',
      policy: res.policy ? (res.policy.gate ? 'strict' : 'lenient') : 'strict',
      ranking_method: res.mode === 'hybrid' ? 'BM25 + LSA hybrid retrieval, then rule-based relevance scoring (0 to 100)' : 'BM25 retrieval + rule-based relevance scoring (0 to 100)',
      score_note: 'relevance_score is a normalized ranking score, not a probability. Relevance and applicability are separate: only applicability_status "applicable" (enforce: true) may be used as a constraint. Rules with verification_status other than "verified" must be checked against the PDF first.',
      known_facts: { country: res.facts.country.value, bridges_on_route: res.facts.bridges_on_route, expressway_on_route: res.facts.expressway_on_route, halts_planned: res.facts.halts_planned },
      rules
    };
  }

  const api = { POLICIES, numberCheck, exportRules, rank, rankJson, RELATED, SCORE_PARTS, STATUS_LABEL, select, toJson, TAGS, readRequirements, deriveFacts, classify, ASPECTS, KIND_LABEL, ruleCountry };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.RuleSelect = api;
})(typeof window !== 'undefined' ? window : globalThis);

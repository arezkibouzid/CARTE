/* DocRAG rule matching for the Rule Finder page.
 *
 * Step 1, retrieval: BM25 over the final rules (a port of docrag/bm25.py and docrag/text.py, same tokens, same formula).
 * Step 2, applicability: plain, readable checks of what the exercise says against what each rule says.
 * BM25 only ranks. It never decides that a rule applies.
 * Works in a browser and in Node (module.exports), so the same code is tested and shown.
 */
(function (root) {
  'use strict';

  /* ---------- text helpers (port of docrag/text.py) ---------- */
  const REPL = [['ﬁ', 'fi'], ['ﬂ', 'fl'], ['ﬀ', 'ff'], ['ﬃ', 'ffi'], ['ﬄ', 'ffl'], ['‘', "'"], ['’', "'"], ['“', '"'], ['”', '"'],
    ['–', '-'], ['—', '-'], ['−', '-'], [' ', ' '], [' ', ' '], ['­', '']];
  function clean(t) {
    for (const [a, b] of REPL) t = t.split(a).join(b);
    t = t.replace(/[-]/g, '•');
    t = t.replace(/[\u{1D400}-\u{1D7FF}]/gu, m => m.normalize('NFKC'));
    return t.replace(/(?<=[A-Za-z]) -(?=[A-Za-z])/g, '-');
  }
  const matchKey = t => clean(t).replace(/\s+/g, ' ').trim().toLowerCase();
  const STOP = new Set('a an and are as at be been by for from has have in is it its of on or that the this to was were will with shall should must may not can'.split(' '));
  function stem(w) {
    for (const suf of ['ing', 'ed', 'es', 's']) {
      if (w.length > 4 && w.endsWith(suf)) { w = w.slice(0, -suf.length); break; }
    }
    if (w.length > 4 && w.endsWith('e')) w = w.slice(0, -1);
    return w;
  }
  // stem -> the word as written, so the page can show "speed" and not "spe"
  function origMap(text) {
    const m = new Map();
    (matchKey(text).match(/[a-z0-9]+(?:[./-][a-z0-9]+)*/g) || []).forEach(w => { if (!STOP.has(w)) { const st = stem(w); if (!m.has(st)) m.set(st, w); } });
    return m;
  }
  function tokens(text) {
    const words = matchKey(text).match(/[a-z0-9]+(?:[./-][a-z0-9]+)*/g) || [];
    return words.filter(w => !STOP.has(w)).map(stem);
  }

  /* ---------- BM25 (port of docrag/bm25.py) ---------- */
  class BM25 {
    constructor(docsTokens, k1 = 1.5, b = 0.75) {
      this.k1 = k1; this.b = b; this.N = docsTokens.length;
      this.dl = docsTokens.map(d => d.length);
      this.avg = this.dl.reduce((a, c) => a + c, 0) / Math.max(1, this.N);
      const df = new Map(); this.inv = new Map();
      docsTokens.forEach((d, i) => {
        const tf = new Map(); d.forEach(t => tf.set(t, (tf.get(t) || 0) + 1));
        tf.forEach((c, t) => { df.set(t, (df.get(t) || 0) + 1); if (!this.inv.has(t)) this.inv.set(t, []); this.inv.get(t).push([i, c]); });
      });
      this.idf = new Map(); df.forEach((n, t) => this.idf.set(t, Math.log(1 + (this.N - n + 0.5) / (n + 0.5))));
    }
    termScore(t, i, tf) {
      const idf = this.idf.get(t);
      return idf * tf * (this.k1 + 1) / (tf + this.k1 * (1 - this.b + this.b * this.dl[i] / Math.max(this.avg, 1e-9)));
    }
    // w (optional): a Map word -> multiplier. Without it every word counts once, exactly as before.
    scores(q, w) {
      const out = new Map();
      new Set(q).forEach(t => {
        if (!this.idf.has(t)) return;
        const m = (w && w.get(t)) || 1;
        this.inv.get(t).forEach(([i, tf]) => out.set(i, (out.get(i) || 0) + m * this.termScore(t, i, tf)));
      });
      return out;
    }
    search(q, k = 10, w) { return [...this.scores(q, w).entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0]).slice(0, k); }
    why(q, i, w) {   // the query words found in rule i, with what each one added to the score
      const out = [];
      new Set(q).forEach(t => {
        if (!this.idf.has(t)) return;
        const hit = this.inv.get(t).find(([j]) => j === i);
        if (hit) out.push([t, ((w && w.get(t)) || 1) * this.termScore(t, i, hit[1])]);
      });
      return out.sort((a, b) => b[1] - a[1]);
    }
  }

  /* ---------- quantities ---------- */
  const UNIT = 'km\\s*/\\s*h|kph|mph|km|kilomet(?:re|er)s?|met(?:re|er)s?|m|tonnes?|tons?|t|minutes?|mins?|hours?|hrs?|h|seconds?|secs?|s|%|per\\s?cent|inhabitants?|residents?|ft|feet|MLC';
  const NUM = '\\d{1,3}(?:[ ,]\\d{3})+(?:\\.\\d+)?|\\d*\\.\\d+|\\d+(?:[.,]\\d+)?';
  const QTY = new RegExp('(?<![\\w.])(?:(MLC)\\s*(' + NUM + ')|(' + NUM + ')\\s*(' + UNIT + '))(?![A-Za-z0-9])', 'gi');
  const UNIT_NORM = { 'km/h': 'km/h', kph: 'km/h', mph: 'mph', km: 'km', kilometre: 'km', kilometres: 'km', kilometer: 'km', kilometers: 'km',
    m: 'm', metre: 'm', metres: 'm', meter: 'm', meters: 'm', t: 't', tonne: 't', tonnes: 't', ton: 't', tons: 't', min: 'min', mins: 'min', minute: 'min', minutes: 'min',
    h: 'h', hr: 'h', hrs: 'h', hour: 'h', hours: 'h', s: 's', sec: 's', secs: 's', second: 's', seconds: 's', '%': '%', percent: '%', 'per cent': '%',
    inhabitant: 'inhabitants', inhabitants: 'inhabitants', resident: 'inhabitants', residents: 'inhabitants', ft: 'ft', feet: 'ft', mlc: 'MLC' };
  const normUnit = u => { u = u.replace(/\s+/g, '').toLowerCase(); return UNIT_NORM[u] || u; };
  function parseNum(s) {
    s = s.trim();
    if (/^\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?$/.test(s)) s = s.replace(/[ ,]/g, '');
    else if (/^\d+,\d{1,2}$/.test(s)) s = s.replace(',', '.');
    return parseFloat(s);
  }
  function quantities(text) {
    const out = []; QTY.lastIndex = 0; let m;
    const t = clean(text);
    while ((m = QTY.exec(t))) {
      if (m[1]) out.push({ value: parseNum(m[2]), unit: 'MLC', start: m.index, end: m.index + m[0].length, raw: m[0] });
      else out.push({ value: parseNum(m[3]), unit: normUnit(m[4]), start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
    return out;
  }
  const FAMILY = { m: 'length', ft: 'length', km: 'length', 'km/h': 'speed', mph: 'speed', t: 'mass', min: 'time', h: 'time', s: 'time', '%': 'percent', inhabitants: 'people', MLC: 'class' };
  const TO_BASE = { m: 1, ft: 0.3048, km: 1000, 'km/h': 1, mph: 1.609344, t: 1, min: 1, h: 60, s: 1 / 60, '%': 1, inhabitants: 1, MLC: 1 };
  const base = (v, u) => v * (TO_BASE[u] || 1);

  /* what a number is about, read from the words around it */
  const CONCEPT_LABEL = {
    speed: 'speed', height: 'height or clearance', width: 'road or lane width', turn_radius: 'turn radius', weight_t: 'vehicle or load mass (tonnes)', axle_load: 'load per axle',
    mlc: 'MLC (a load class, not tonnes)', vehicle_gap: 'gap between vehicles', serial_gap: 'gap between serials', axle_spacing: 'axle spacing', warning_distance: 'warning panel or sign distance',
    visibility: 'visibility or legibility distance', route_separation: 'distance between routes', sign_spacing: 'spacing of signs', length_other: 'another length',
    duration: 'a duration', halt_time: 'halt length', time_gap: 'time gap', grade: 'gradient', town: 'town size', span: 'bridge span or depth', trailer_length: 'vehicle length',
    distance_km: 'a distance'
  };
  function conceptOf(unit, ctx) {
    const c = ctx.toLowerCase();
    const has = re => re.test(c);
    switch (unit) {
      case 'km/h': case 'mph': return 'speed';
      case 'MLC': return 'mlc';
      case '%': return /slope|gradient|grade/.test(c) ? 'grade' : 'grade';
      case 'inhabitants': return 'town';
      case 't': return has(/axle/) ? 'axle_load' : 'weight_t';
      case 'min': case 'h': case 's':
        if (has(/halt|rest|stop/)) return 'halt_time';
        if (has(/time gap|interval|headway|between (convoy|convoys|serial|serials|vehicle|vehicles)/)) return 'time_gap';
        return 'duration';
      case 'm': case 'ft': case 'km': {
        if (has(/axle (separation|spacing)|axles? (are|is) (spaced|separated)/)) return 'axle_spacing';
        if (has(/warning|panel|placard|reflector|light line|blackout/)) return 'warning_distance';
        if (has(/visib|legib|recogni|readable/)) return 'visibility';
        if (has(/apart|between (the |both |two )?routes|distinct|itinerar/) || (unit === 'km' && has(/separat/) && !has(/sign/))) return 'route_separation';
        if (has(/separation between signs|between signs|signs?\b.*(apart|separation)/)) return 'sign_spacing';
        if (has(/serial/)) return 'serial_gap';
        if (has(/(vehic|truck|convoy)[^.;]{0,60}(gap|spacing|interval|distance between|between)|(gap|spacing|interval|distance between|between)[^.;]{0,40}(vehic|truck)/)) return 'vehicle_gap';
        if (has(/spacing|gap/) && has(/vehic|convoy/)) return 'vehicle_gap';
        if (has(/headroom|clearance|overhead|height|\bhigh\b/)) return 'height';
        if (has(/radius|curve|turn/)) return 'turn_radius';
        if (has(/width|wide|narrow|roadway|lane/)) return 'width';
        if (has(/span|depth/)) return 'span';
        if (has(/trailer|overall|length/)) return 'trailer_length';
        return unit === 'km' ? 'distance_km' : 'length_other';
      }
    }
    return 'length_other';
  }

  /* topics, for rules and exercises that have no number */
  const TOPICS = {
    bridge: /bridge|ferry|raft|floating|river[- ]crossing|ford\b|fording|crossing|span/,
    junction: /junction|intersection|congestion|occupancy|critical point|release point|order of march|staggering|deconflict/,
    escort: /escort|ambush|gun truck/,
    halt: /\bhalt|rest area|rest stop|holding area/,
    classification: /route classification|classif|reconnaissance|obstruction|overhead clearance/,
    routes_type: /penetration|lateral|axial|cross route/,
    traffic_control: /traffic control|movement control|traffic regulat|reporting point/,
    convoy: /convoy|column|serial|march/,
    signs: /\bsigns?\b|panel|marking|illuminat|light line|blackout/,
    waterway: /waterway|inland/,
    cross_country: /cross-country/,
    passage_lines: /passage of lines|reception line|rearward|withdraw/,
    clearance: /clearance|headroom|overhead|\bheights?\b/,
    load_class: /\bmlc\b|load classification|\bloads?\b|axle load|tonnes?\b/,
    speed: /\bspeed|km\/h|\bmph\b|\bkph\b/,
    spacing: /\bgaps?\b|spacing|interval|distance between/
  };
  const TOPIC_LABEL = { bridge: 'bridges and crossings', junction: 'junctions and congestion', escort: 'escort', halt: 'halts', classification: 'route classification', routes_type: 'penetration and lateral routes',
    traffic_control: 'traffic control', convoy: 'convoys and columns', signs: 'signs and panels', waterway: 'inland waterways', cross_country: 'cross-country movement', passage_lines: 'passage of lines', clearance: 'height and clearance', load_class: 'loads and MLC', speed: 'speed', spacing: 'gaps and spacing' };
  const topicsOf = text => { const t = text.toLowerCase(), s = new Set(); Object.entries(TOPICS).forEach(([k, re]) => { if (re.test(t)) s.add(k); }); return s; };

  /* ---------- rules ---------- */
  const HARD = new Set(['mandatory_constraint', 'conditional_requirement']);
  const COUNTRY = /^\s*(denmark|germany|spain|united kingdom|uk\b|france|belgium|italy|netherlands|canada|oklahoma|utah|oregon|washington state|washington|ohio|california|texas|us\b|u\.s\.|usa)|\((da|ge|sp|uk|fr|be|it|ne|ca|us)\)/i;
  function parseCsv(text) {
    text = text.replace(/^﻿/, '');
    const first = text.split('\n')[0]; const d = (first.split(';').length > first.split(',').length) ? ';' : ',';
    const rows = []; let row = [], cur = '', q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
      else if (c === '"') q = true; else if (c === d) { row.push(cur); cur = ''; }
      else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(cur); cur = ''; if (row.length > 1 || row[0]) rows.push(row); row = []; }
      else cur += c;
    }
    if (cur || row.length) { row.push(cur); rows.push(row); }
    const head = rows.shift() || [];
    return rows.map(r => Object.fromEntries(head.map((k, i) => [k.trim(), (r[i] || '').trim()])));
  }
  const splitList = x => (x ? x.split(/\s*;\s*/).filter(Boolean) : []);
  function parseValue(vu) {
    const t = clean(vu || '');
    const qs = quantities(t);
    const range = t.match(new RegExp('(' + NUM + ')\\s*[-–]\\s*(' + NUM + ')\\s*(' + UNIT + ')', 'i'));
    let op = '';
    const m = t.match(/(≤|<=|≥|>=|<|>|=|≈)/);
    if (m) op = m[1] === '<=' ? '≤' : (m[1] === '>=' ? '≥' : m[1]);
    else if (/up to/i.test(t)) op = '≤'; else if (/approximately|about/i.test(t)) op = '≈';
    return { qs, range: range ? { lo: parseNum(range[1]), hi: parseNum(range[2]), unit: normUnit(range[3]) } : null, op, text: vu || '' };
  }
  function prepareRule(r) {
    const val = parseValue(r.value_unit_operator);
    const ctx = [r.scope, r.conditions, r.value_unit_operator, r.evidence_quote].join(' ');
    const q0 = val.qs[0];
    const concept = q0 ? conceptOf(q0.unit, [r.scope, r.value_unit_operator, r.conditions].join(' ') + ' ' + r.evidence_quote) : null;
    const reviewed = /^REVIEWED/.test(r.verification_status || '');
    const quote = (r.quote_match_pdf || '').toLowerCase();
    const verified = reviewed && quote === 'yes';
    return {
      id: r.rule_id, doc: r.document, page: r.pdf_page, quote: r.evidence_quote, cls: r.rule_class, valueText: r.value_unit_operator, scope: r.scope, conditions: r.conditions,
      status: r.verification_status, quoteMatch: r.quote_match_pdf, notes: r.notes || '', sourceFile: r.source_file || '', val, concept, topics: topicsOf(ctx),
      hard: HARD.has(r.rule_class), reviewed, verified, country: COUNTRY.test(r.scope || '') || COUNTRY.test(r.evidence_quote.slice(0, 40)),
      docText: [r.evidence_quote, r.scope, r.conditions, (r.rule_class || '').replace(/_/g, ' '), r.value_unit_operator].join(' ')
    };
  }

  /* ---------- the exercise ---------- */
  function cleanExercise(text) {
    return text.split('\n').filter(l => !/^\s*[•*-]?\s*(DA|WP|RA|PD|PA|LD|LA)\d?\b.*(LAT|LONG)/i.test(l)).join('\n')
      .replace(/\(?\s*LAT\s*:\s*[\d.]+\s*\/\s*LONG\s*:\s*[\d.]+\s*\)?/gi, ' ');
  }
  function parseExercise(text) {
    const t = clean(cleanExercise(text));
    const qs = quantities(t).map(q => {
      // the sentence that holds the number (a full stop followed by a space, a semicolon or a line break ends it)
      const before = t.slice(0, q.start), after = t.slice(q.end);
      const bm = [...before.matchAll(/(?:[.;]\s|\n)/g)].pop();
      const am = after.match(/(?:[.;]\s|\n)/);
      let win = t.slice(bm ? bm.index + bm[0].length : 0, am ? q.end + am.index : t.length);
      if (win.length > 220) win = t.slice(Math.max(0, q.start - 90), Math.min(t.length, q.end + 70));
      return Object.assign(q, { concept: conceptOf(q.unit, win), ctx: win.replace(/\s+/g, ' ').trim() });
    });
    return { text: t, qs, topics: topicsOf(t), toks: new Set(tokens(t)) };
  }

  const CONTRIBUTION = {
    speed: 'Caps the speed used to compute trip duration or the march table.',
    height: 'Routes or sections with less clearance than the vehicle height must be excluded.',
    width: 'Decides whether a section is single-flow or double-flow for the vehicles.',
    weight_t: 'Gives the mass to compare with the load class (MLC) of the bridges and roads on the route.',
    mlc: 'Gives the load class a route or bridge must have for the vehicle (a class, not a mass in tonnes).',
    vehicle_gap: 'Sets the spacing between vehicles in the movement table.',
    serial_gap: 'Sets the spacing between serials in the movement table.',
    halt_time: 'Sets how halts are planned in the movement chart.',
    route_separation: 'Constrains how far apart the two routes must be.',
    grade: 'Flags steep sections as obstructions on the route.',
    town: 'Decides where reporting points or regulation apply.',
    turn_radius: 'Flags tight curves as obstructions.'
  };
  const TOPIC_CONTRIB = {
    bridge: 'Bridges and crossings decide which routes the vehicles can use.', junction: 'Helps plan junction occupancy and avoid congestion.',
    classification: 'Explains how route sections are classified for the STANAG 2174 overlay.', routes_type: 'Defines penetration and lateral routes for the network.',
    traffic_control: 'Informs where traffic control and reporting points are placed.', convoy: 'Background on how convoys and columns move.', halt: 'Background on planned halts.',
    clearance: 'Overhead clearance decides which sections a vehicle of a given height can use.', load_class: 'Load class rules decide which bridges and roads the vehicles can use.', speed: 'Speed rules cap the speed used for trip duration.', spacing: 'Spacing rules set gaps between vehicles or convoys.',
    escort: 'Background on escort tasks.', signs: 'Background on route signing.', passage_lines: 'Background on passage of lines.', waterway: 'Background on inland waterways.', cross_country: 'Background on cross-country movement.'
  };

  /* ---------- applicability ---------- */
  const QTY_TOPICS = new Set(['clearance', 'load_class', 'speed', 'spacing']);
  const GENERIC = new Set(['vehicl', 'also', 'under', 'use', 'one', 'when', 'along']);
  function clauseCheck(clause, ex) {
    const toks = [...new Set(tokens(clause))].filter(t => t.length > 2 && !GENERIC.has(t) && !/^[0-9./-]+$/.test(t));
    if (!toks.length) return { clause, ok: true, found: [], missing: [] };
    const om = origMap(clause), show = t => om.get(t) || t;
    const found = toks.filter(t => ex.toks.has(t)).map(show), missing = toks.filter(t => !ex.toks.has(t)).map(show);
    // strict on purpose: a scope counts as covered only when the exercise uses all its key words (three quarters for long ones)
    return { clause, ok: toks.length >= 4 ? found.length / toks.length >= 0.75 : missing.length === 0, found, missing };
  }
  function boundText(v, u) { return (Math.round(v * 100) / 100) + ' ' + u; }
  function compare(rule, exQ) {
    const v = rule.val;
    const items = [];   // each rule value as {lo, hi, op, unit} in its own unit
    if (v.range) items.push({ lo: v.range.lo, hi: v.range.hi, unit: v.range.unit });
    const seen = [];
    v.qs.forEach(q => {   // "40 KPH (25 MPH)" is one value written twice
      const bv = base(q.value, q.unit);
      if (q.unit === 'MLC') return;
      if (v.range && v.range.unit === q.unit && (q.value === v.range.hi || q.value === v.range.lo)) return;
      if (seen.some(x => FAMILY[x.u] === FAMILY[q.unit] && Math.abs(x.b - bv) <= 0.05 * Math.max(1, bv))) return;
      seen.push({ u: q.unit, b: bv }); items.push({ value: q.value, unit: q.unit, op: v.op });
    });
    const usable = items.filter(it => FAMILY[it.unit] === FAMILY[exQ.unit]);
    if (!usable.length) return null;
    const x = base(exQ.value, exQ.unit), shown = boundText(exQ.value, exQ.unit);
    const conv = (val, unit) => unit === exQ.unit ? boundText(val, unit) : boundText(val, unit) + ' (' + boundText(base(val, unit) / (TO_BASE[exQ.unit] || 1), exQ.unit) + ')';
    const near = (a, b) => Math.abs(a - b) <= 0.01 * Math.max(1, Math.abs(b));
    if (usable.length > 1 || usable[0].lo !== undefined) {
      const listed = usable.map(it => it.lo !== undefined ? conv(it.lo, it.unit) + ' to ' + conv(it.hi, it.unit) : conv(it.value, it.unit)).join('; ');
      const hit = usable.some(it => it.lo !== undefined ? (x >= base(it.lo, it.unit) - 1e-9 && x <= base(it.hi, it.unit) + 1e-9) : near(x, base(it.value, it.unit)));
      return { inside: hit, text: 'Exercise: ' + shown + '. Rule values: ' + listed + '. The exercise value ' + (hit ? 'matches one of them.' : 'matches none of them.') };
    }
    const it = usable[0], b = base(it.value, it.unit), op = it.op, eq = near(x, b);
    let inside;
    if (eq) inside = !(op === '<' || op === '>');
    else if (op === '≤' || op === '<') inside = x < b;
    else if (op === '≥' || op === '>') inside = x > b;
    else inside = false;
    const bounded = ['≤', '<', '≥', '>'].includes(op);
    const verdict = eq ? (inside ? 'The exercise value equals the bound, so it is on the allowed side.' : 'The exercise value equals the bound, which the rule excludes.')
      : bounded ? 'The exercise value is ' + (inside ? 'inside' : 'outside') + ' the bound.' : 'The exercise value differs from the rule value.';
    return { inside, text: 'Exercise: ' + shown + '. Rule bound: ' + (op || '') + ' ' + conv(it.value, it.unit) + '. ' + verdict };
  }

  function judge(rule, ex, rank, score, why) {
    const wm = origMap(rule.docText);
    why = why.map(([t, sc]) => [wm.get(t) || t, sc]);
    const out = { rule, rank, score, why, missing: [], checks: [], links: [], compare: null, contribution: '', category: '', reason: '' };
    const sameFam = ex.qs.filter(q => rule.concept && FAMILY[q.unit] === FAMILY[rule.val.qs[0] ? rule.val.qs[0].unit : (rule.val.range ? rule.val.range.unit : '')]);
    const exact = rule.concept ? ex.qs.filter(q => q.concept === rule.concept) : [];
    const topicHit = [...rule.topics].filter(t => ex.topics.has(t));
    const mlcVsMass = rule.concept === 'mlc' && ex.qs.some(q => q.concept === 'weight_t');
    const massVsMlc = rule.concept === 'weight_t' && ex.qs.some(q => q.concept === 'mlc');
    // clauses: scope and conditions
    const clauses = [];
    if (rule.scope) clauses.push(['Scope', rule.scope]);
    splitList(rule.conditions).forEach(c => clauses.push(['Condition', c]));
    out.checks = clauses.map(([kind, text]) => Object.assign(clauseCheck(text, ex), { kind }));
    const covered = out.checks.length ? out.checks.filter(c => c.ok).length / out.checks.length : 1;
    out.checks.filter(c => !c.ok).forEach(c => out.missing.push(c.kind + ' not stated in the exercise: ' + c.clause));

    if (exact.length) { out.links.push('same quantity: ' + CONCEPT_LABEL[rule.concept]); out.compare = compare(rule, exact[0]); out.contribution = CONTRIBUTION[rule.concept] || ''; }
    if (mlcVsMass || massVsMlc) { out.links.push('related: tonnes and MLC'); out.missing.push('Tonnes and MLC are different things: the exercise gives a mass, so the vehicle MLC (or the bridge MLC) must be known before this rule can be used.'); out.contribution = CONTRIBUTION.mlc; }
    topicHit.forEach(t => out.links.push('same topic: ' + TOPIC_LABEL[t]));
    if (!out.contribution && topicHit.length) out.contribution = TOPIC_CONTRIB[topicHit[0]] || '';
    if (rule.concept && !exact.length && sameFam.length && !mlcVsMass && !massVsMlc) {
      const ru = rule.val.qs[0] ? rule.val.qs[0].unit : (rule.val.range ? rule.val.range.unit : '');
      const e = sameFam.find(q => q.unit === ru) || sameFam[0];
      out.lookalike = 'The rule number is ' + CONCEPT_LABEL[rule.concept] + ', the exercise number "' + e.raw + '" is ' + CONCEPT_LABEL[e.concept] + '. Same kind of unit, different meaning.';
    }
    // verification
    if (!rule.reviewed) out.missing.push('Not verified: the quote was not checked against the PDF.');
    else if (!rule.verified) out.missing.push('Quote matches the PDF only partly. Check the page before relying on it.');
    if (rule.valueText && /not specified|no numeric|not numeric|no numerical|unspecified|not applicable/i.test(rule.valueText)) out.noNumber = true;

    // category
    const linked = out.links.length > 0;
    const naWhy = [];
    // a look-alike number rules the rule out unless a specific topic (not just "speed" or "load") links it to the exercise
    if (out.lookalike && !topicHit.some(t => !QTY_TOPICS.has(t))) naWhy.push(out.lookalike);
    if (rule.country) naWhy.push('Country-specific rule (' + rule.scope + '): it applies only if the exercise is set in that country. Kept as is, not deleted.');
    if (!naWhy.length && !linked && covered < 0.5) naWhy.push('Found through general words only. The exercise states no matching quantity or topic, and the rule scope is not stated.');
    if (naWhy.length) { out.category = 'na'; out.reason = naWhy.join(' '); }
    else if (!rule.hard) { out.category = 'advisory'; out.reason = 'Class ' + rule.cls.replace(/_/g, ' ') + ': advice, a trigger or a description, not a mandatory constraint.'; }
    else if (rule.verified && (exact.length || topicHit.length) && !out.missing.length && !mlcVsMass && !massVsMlc) { out.category = 'applicable'; out.reason = 'Reviewed rule, quote matches the PDF, the exercise states the same quantity or topic and every scope and condition is covered. A person still confirms.'; }
    else { out.category = 'potential'; out.reason = 'Hard rule that may bear on the exercise, but some information is missing (see below).'; }
    if (out.lookalike && out.category !== 'na') out.missing.push(out.lookalike);
    return out;
  }

  function stripNoise(text) {
    return cleanExercise(text);
  }
  // Words every exercise statement repeats. They say how to hand in the answer, not what the doctrine must cover.
  const BOILERPLATE = new Set(tokens('layout layouts map represented represent return indicate estimated exercise need find connecting connect departure arrival point deployment duration trip length kilometres kilometre routes slide here following within').concat(['pd5', 'pa5', 'pd6', 'pa6', 'wp1', 'wp2', 'wp3', 'wp4']
    .concat(tokens('generate diagram diagrams propose proposal proposals solution solutions ensure vocabulary reference data marine infantry regiment brigade battalion indicate showing show'))));
  // Words added to the query for each kind of number found in the exercise (so "vehicule", "mass" or "clearance" still meet the rule wording).
  const CONCEPT_WORDS = {
    speed: 'speed limit km/h', height: 'height clearance headroom overhead', width: 'width lane roadway', weight_t: 'weight mass load tonnes MLC bridge',
    mlc: 'MLC load classification bridge', vehicle_gap: 'vehicle gap spacing interval convoy', serial_gap: 'serial gap interval column', route_separation: 'routes apart separation distinct alternate',
    halt_time: 'halt rest stop', town: 'town built-up inhabitants', grade: 'gradient slope', turn_radius: 'radius curve'
  };
  function expansion(ex) {
    const seen = new Set(), words = [];
    ex.qs.forEach(q => { if (!seen.has(q.concept) && CONCEPT_WORDS[q.concept]) { seen.add(q.concept); words.push(CONCEPT_WORDS[q.concept]); } });
    return words.join(' ');
  }
  function queryTokens(text) {
    // bare numbers never decide: 80 tonnes and MLC 80 are not the same thing
    // also dropped: labels of one or two letters (intersections A, B, C), words that start with a digit (1st, 23b), and generic exercise words
    return tokens(stripNoise(text)).filter(t => !/^[0-9][0-9.,/-]*$/.test(t) && !/^\d/.test(t) && t.length >= 3 && !BOILERPLATE.has(t));
  }

  class Matcher {
    constructor(csvRows) {
      this.rules = csvRows.filter(r => r.rule_id).map(prepareRule);
      this.bm25 = new BM25(this.rules.map(r => tokens(r.docText)));
    }
    // opts.objective (optional): text that states the planning objective. Its words count opts.boost times (default 2) in the BM25 score.
    // Judge one rule that BM25 did not return (used by the optional hybrid mode). Same judgement, search score 0.
    judgeAt(i, ex, rank) { return judge(this.rules[i], ex, rank, 0, []); }
    search(exerciseText, topK = 30, opts) {
      const ex = parseExercise(exerciseText), added = expansion(ex);
      const q = queryTokens(exerciseText + '\n' + added);
      let w = null;
      if (opts && opts.objective) { w = new Map(); const b = opts.boost || 2; queryTokens(opts.objective).forEach(t => w.set(t, b)); }
      const hits = this.bm25.search(q, topK, w).filter(([, s]) => s > 0);
      const results = hits.map(([i, s], k) => judge(this.rules[i], ex, k + 1, s, this.bm25.why(q, i, w)));
      return { query: q, added, exercise: ex, results };
    }
  }

  const api = { clean, matchKey, tokens, origMap, BM25, quantities, conceptOf, parseCsv, prepareRule, parseExercise, queryTokens, cleanExercise, Matcher, CONCEPT_LABEL, TOPIC_LABEL, FAMILY, TO_BASE, base, HARD };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.RuleMatch = api;
})(typeof window !== 'undefined' ? window : globalThis);

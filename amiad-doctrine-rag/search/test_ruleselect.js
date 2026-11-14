// node tools/test_ruleselect.js <final rules CSV>
// Stress tests of the two-stage selection: named confusions, unknown vs false, enforcement only on confirmed facts, safety invariants.
// The CSV is only read. "Verified clones" are made in memory to test what would happen if a look-alike rule were verified.
const fs = require('fs'), path = require('path'), assert = require('assert');
const RM = require('./rulematch.js'), RS = require('./ruleselect.js');
if (!process.argv[2]) { console.error('Give the path of the final rules CSV.'); process.exit(2); }
const rows = RM.parseCsv(fs.readFileSync(process.argv[2], 'utf8'));
const EX = JSON.parse(fs.readFileSync(path.join(__dirname, 'exercises.json'), 'utf8'));
const text = n => EX.find(e => e.n === n).text;
const matcherWith = (...ids) => {      // same rules, but the listed ones are marked verified (in memory only)
  const copy = rows.map(r => ids.includes(r.rule_id) ? Object.assign({}, r, { verification_status: 'REVIEWED_CORRECTION', quote_match_pdf: 'yes' }) : r);
  return new RM.Matcher(copy);
};
const M = new RM.Matcher(rows);
// The older tests describe the STRICT policy (unverified rules are held in their own group, unknown conditions cap a rule at 'potential').
const rankS = (m, t, u, o) => RS.rank(m, t, u, Object.assign({ policy: 'strict' }, o));
const find = (res, id) => res.items.find(i => i.id === id);
const finalIds = res => [].concat(res.final.enforceable, res.final.conditional, res.final.advisory).map(i => i.id);
let ok = 0; const t = (name, fn) => { try { fn(); ok++; console.log('PASS', name); } catch (e) { console.log('FAIL', name, '\n     ', e.message); process.exitCode = 1; } };

t('the CSV is not modified by selection', () => { const before = JSON.stringify(rows); RS.select(M, text('24')); assert.strictEqual(JSON.stringify(rows), before); });
t('Exercise 24: only verified rules are in the final list', () => { const r = RS.select(M, text('24')); [].concat(r.final.enforceable, r.final.conditional, r.final.advisory).forEach(i => assert.ok(i.rule.verified, i.id)); });
t('Exercise 24: DR-0145 (holding areas) is relevant but unverified, so it is not final', () => {
  const r = RS.select(M, text('24')); assert.ok(r.unverified.some(i => i.id === 'DR-0145')); assert.ok(!finalIds(r).includes('DR-0145')); });

t('a quote that does not contain the rule value gets a warning, and the verification status is not changed', () => {
  const r = RS.select(M, text('24')); const a = find(r, 'DR-0061'), b = find(r, 'DR-0046');
  assert.ok(a.warnings.length && /does not contain the value/.test(a.warnings[0]), 'DR-0061 should warn'); assert.strictEqual(a.verification, 'verified'); assert.strictEqual(a.outcome, 'conditional');
  const c = RS.select(M, text('13')); assert.strictEqual(find(c, 'DR-0008').warnings.length, 0, 'DR-0008 quote contains 4.50'); });

t('rules are ordered by importance (0 to 100) inside each list, and the threshold removes none of them from the output', () => {
  EX.forEach(e => { const r = RS.select(M, e.text);
    [r.final.enforceable, r.final.conditional, r.final.advisory, r.unverified].forEach(list => { for (let k = 0; k < list.length; k++) { assert.ok(list[k].importance >= 0 && list[k].importance <= 100, e.n + ' ' + list[k].id);
      if (k) assert.ok(list[k - 1].importance >= list[k].importance, e.n + ' order ' + list[k - 1].id + ' ' + list[k].id); } });
    const j = RS.toJson(r, { id: e.n, threshold: 70 }), all = [].concat(j.enforceable, j.conditional, j.advisory, j.verify_first);
    assert.strictEqual(all.length, r.counts.enforceable + r.counts.conditional + r.counts.advisory + r.counts.unverified, e.n + ' nothing may be dropped');
    all.forEach(x => assert.strictEqual(x.above_threshold, x.importance >= 70)); }); });
t('Exercise 24: the ordinary convoy gap rule outranks the halt rules', () => { const r = RS.select(M, text('24')); assert.strictEqual(r.final.advisory[0].id, 'DR-0046'); });

/* confusion 1: bridge-rating spacing against ordinary convoy spacing */
t('bridge rating spacing (DR-0009) and bridge crossing spacing (DR-0030) are not the ordinary convoy gap (Exercise 24)', () => {
  const r = RS.select(M, text('24')); ['DR-0009', 'DR-0030', 'DR-0016', 'DR-0018', 'DR-0035'].forEach(id => { const i = find(r, id); if (i) assert.notStrictEqual(i.outcome === 'excluded', false, id + ' ' + i.outcome); });
  assert.ok(finalIds(r).includes('DR-0046'), 'ordinary convoy gap rule DR-0046 should be kept'); });
t('even when the map shows a bridge, DR-0030 (30.5 m) adds nothing to a stated 100 m gap, and DR-0009 stays out', () => {
  const r = RS.select(M, text('24'), { bridges_on_route: 'yes' });
  assert.ok(find(r, 'DR-0030').reasons.some(x => x.code === 'redundant'), 'DR-0030 should be redundant');
  assert.ok(!finalIds(r).includes('DR-0009')); });
t('with a bridge on the route, the 40 km/h bridge speed rule becomes a conditional rule, never enforced', () => {
  const r = RS.select(M, text('24'), { bridges_on_route: 'yes' }); const i = r.final.conditional.find(x => x.id === 'DR-0029' || x.id === 'DR-0031');
  assert.ok(i, 'bridge speed rule should be conditional'); assert.ok(i.missing.some(m => /MLC/.test(m)), 'vehicle MLC must be listed as missing'); assert.strictEqual(r.final.enforceable.length, 0); });
t('when the exercise gap (20 m) is below the 30.5 m bridge minimum, the bridge spacing rule matters, as a conditional rule described as a bridge rule', () => {
  const r = RS.select(M, 'Distance between each vehicle : 20 m. Speed outside built-up areas : 50 km/h. Duration of the trip in min.', { bridges_on_route: 'yes' });
  const i = find(r, 'DR-0030'); assert.ok(i && i.outcome === 'conditional', i && i.outcome); assert.ok(/not the ordinary gap/.test(i.helps)); });

/* confusion 2: warning sign distance against separation of alternative routes */
t('10 km between warning signs (DR-0084) is not the 10 km between routes (Exercises 18, 19), even if it were verified', () => {
  const M2 = matcherWith('DR-0084', 'DR-0043', 'DR-0085');
  ['18', '19'].forEach(n => { const r = RS.select(M2, text(n)); ['DR-0084', 'DR-0043', 'DR-0085'].forEach(id => assert.ok(!finalIds(r).includes(id), n + ' ' + id)); });
  assert.ok(RM.conceptOf('km', 'blackout warning panel 10 km between signs') !== 'route_separation'); });

/* confusion 3: tonnes against MLC */
t('MLC 80 is not 80 tonnes: a verified MLC class rule is never reported as matching the exercise mass (Exercise 15)', () => {
  const M2 = matcherWith('DR-0171', 'DR-0183', 'DR-0168'); const r = RS.select(M2, text('15'));
  [r.final.conditional, r.final.advisory, r.final.enforceable].flat().filter(i => ['DR-0171', 'DR-0183', 'DR-0168'].includes(i.id)).forEach(i => { assert.notStrictEqual(i.outcome, 'enforceable'); assert.ok(!/same as/.test(JSON.stringify(i.reasons)), i.id); });
  const c = find(r, 'DR-0183'); assert.strictEqual(c.outcome, 'conditional'); assert.ok(c.missing.some(m => /Vehicle MLC/.test(m))); });
t('a rule that applies only to mass <= 3 t is contradicted by an 80 t load, not just ignored (DR-0007)', () => {
  const r = RS.select(M, text('15')); const i = find(r, 'DR-0007'); if (i) { assert.strictEqual(i.outcome, 'excluded'); assert.ok(i.reasons.some(x => x.code === 'condition'), JSON.stringify(i.reasons)); } });
t('Danish and German axle-load tonnes are country-specific and the exercise is in France (Exercise 15)', () => {
  const r = RS.select(M, text('15')); ['DR-0063', 'DR-0064', 'DR-0067', 'DR-0073'].forEach(id => { const i = find(r, id); if (i) assert.ok(i.reasons.some(x => x.code === 'country'), id); }); });
t('a country-specific rule is UNKNOWN, not excluded, when the exercise country is not known', () => {
  const r = RS.select(M, 'The route must be adapted to loads up to 80 tonnes. Convoy at most 80 km/h. Duration of the trip.'); const i = find(r, 'DR-0064');
  assert.ok(!i || !i.reasons.some(x => x.code === 'country')); });

/* unknown is not false */
t('Exercise 24: DR-0061 (long halts) stays in the list as conditional while it is unknown whether a halt is planned', () => {
  const r = RS.select(M, text('24')); const i = find(r, 'DR-0061'); assert.strictEqual(i.outcome, 'conditional'); assert.ok(i.conds.some(c => c.status === 'UNKNOWN')); assert.ok(i.missing.length > 0); });
t('...it is contradicted only when the user states that no halt is planned', () => {
  const r = RS.select(M, text('24'), { halts_planned: 'no' }); assert.strictEqual(find(r, 'DR-0061').outcome, 'excluded'); });
t('...and a halt that is planned is not enough: its length must also be at least 30 min to enforce it', () => {
  const r = RS.select(M, text('24'), { halts_planned: 'yes' }); assert.notStrictEqual(find(r, 'DR-0061').outcome, 'enforceable');
  const r2 = RS.select(M, text('24'), { halts_planned: 'yes', longest_halt_min: 45 }); assert.strictEqual(find(r2, 'DR-0061').outcome, 'enforceable');
  const r3 = RS.select(M, text('24'), { halts_planned: 'yes', longest_halt_min: 15 }); assert.strictEqual(find(r3, 'DR-0061').outcome, 'excluded'); });
t('the expressway speed rule is redundant under an 80 km/h cap, but is enforced when the exercise allows 100 km/h on a confirmed expressway', () => {
  const r = RS.select(M, text('24')); assert.ok(find(r, 'DR-0001').reasons.some(x => x.code === 'redundant'));
  const free = 'Convoy speed up to 100 km/h. The route includes an expressway. Duration of the trip.';
  const a = RS.select(M, free, { expressway_on_route: 'unknown' }); assert.notStrictEqual(find(a, 'DR-0001') && find(a, 'DR-0001').outcome, 'enforceable');
  const b = RS.select(M, free, { expressway_on_route: 'yes' }); assert.strictEqual(find(b, 'DR-0001').outcome, 'enforceable'); });
t('a rule is enforced only when every condition is confirmed (verified clone of DR-0183 with facts)', () => {
  const M2 = matcherWith('DR-0183'); const base = text('15');
  assert.strictEqual(find(RS.select(M2, base), 'DR-0183').outcome, 'conditional');
  assert.strictEqual(find(RS.select(M2, base, { bridges_on_route: 'yes', vehicle_mlc: 70, bridge_mlc_min: 80 }), 'DR-0183').outcome, 'enforceable');
  assert.strictEqual(find(RS.select(M2, base, { bridges_on_route: 'yes', vehicle_mlc: 90, bridge_mlc_min: 80 }), 'DR-0183').outcome, 'excluded'); });
t('a rule about vehicles above MLC 120 is dormant until a vehicle MLC is given (DR-0002)', () => {
  const r = RS.select(M, text('15')); const i = find(r, 'DR-0002'); if (i) assert.ok(i.reasons.some(x => x.code === 'dormant'));
  const r2 = RS.select(M, text('15'), { vehicle_mlc: 130 }); const j = find(r2, 'DR-0002'); if (j) assert.ok(!j.reasons.some(x => x.code === 'dormant')); });

/* no rule contributes */
t('Exercises 11 and 12 show zero final rules', () => ['11', '12'].forEach(n => assert.strictEqual(finalIds(RS.select(M, text(n))).length, 0, n)));
t('an empty or number-only description shows zero final rules and does not fail', () => { assert.strictEqual(finalIds(RS.select(M, '')).length, 0); assert.strictEqual(finalIds(RS.select(M, '80 100 4.5')).length, 0); });

/* deduplication */
t('DR-0029 and DR-0031 (same 40 km/h cap) give one rule; DR-0062 and DR-0047 (short halts) give one rule', () => {
  const a = finalIds(RS.select(M, text('15'))); assert.strictEqual(a.filter(id => id === 'DR-0029' || id === 'DR-0031').length, 1);
  const b = finalIds(RS.select(M, text('24'))); assert.strictEqual(b.filter(id => id === 'DR-0062' || id === 'DR-0047').length, 1); });

/* safety invariants, over every exercise and many fact combinations */
t('invariants hold for every exercise under 200 random fact combinations', () => {
  const pick = a => a[Math.floor(Math.random() * a.length)]; let seed = 7; Math.random = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const allVerified = matcherWith(...rows.map(r => r.rule_id));
  for (let k = 0; k < 200; k++) {
    const user = { bridges_on_route: pick(['yes', 'no', 'unknown']), expressway_on_route: pick(['yes', 'no', 'unknown']), halts_planned: pick(['yes', 'no', 'unknown']),
      vehicle_mlc: pick(['', 40, 90, 130]), bridge_mlc_min: pick(['', 60, 100]), longest_halt_min: pick(['', 15, 45]), country: pick(['', 'France', 'Germany']) };
    const n = pick(EX).n;
    [M, allVerified].forEach((m, w) => {
      const r = RS.select(m, text(n), user);
      r.final.enforceable.forEach(i => { assert.ok(i.rule.verified && i.rule.hard, 'enforceable must be verified and mandatory ' + i.id); assert.ok(i.conds.every(c => c.status === 'MET'), 'all MET ' + i.id); assert.strictEqual(i.unchecked.length, 0, 'nothing unchecked ' + i.id); });
      r.final.conditional.forEach(i => { assert.ok(i.rule.verified && i.rule.hard, i.id); assert.ok(i.conds.some(c => c.status === 'UNKNOWN') || i.unchecked.length, 'conditional needs an unknown ' + i.id); });
      r.final.advisory.forEach(i => { assert.ok(i.rule.verified && !i.rule.hard, 'advisory is never mandatory ' + i.id); });
      if (w === 0) finalIds(r).forEach(id => assert.ok(rows.find(x => x.rule_id === id).verification_status.startsWith('REVIEWED') && rows.find(x => x.rule_id === id).quote_match_pdf === 'yes', 'unverified rule in final ' + id));
      [].concat(r.final.enforceable, r.final.conditional, r.final.advisory).forEach(i => { assert.strictEqual(i.reasons.length, 0); assert.ok(!i.conds.some(c => c.status === 'CONTRADICTED')); });
    });
  }
});

/* ---------------- second-stage ranking: keep every relevant rule, ordered by a 0 to 100 score ---------------- */
const inGroups = (r, id) => Object.keys(r.groups).find(k => r.groups[k].some(i => i.id === id));
const rk = (mm, n, user) => rankS(mm, text(n), user);
t('rank: every retrieved rule is either relevant or in diagnostics, none is dropped', () => { EX.forEach(e => { const r = rk(M, e.n); assert.strictEqual(r.relevant.length + r.notRelevant.length, r.counts.retrieved, e.n);
  assert.strictEqual(Object.values(r.groups).reduce((a, l) => a + l.length, 0), r.relevant.length, e.n); }); });
t('rank: scores are integers from 0 to 100 and each list is in descending order', () => { EX.forEach(e => { const r = rk(M, e.n);
  [r.relevant, r.notRelevant].concat(Object.values(r.groups)).forEach(l => l.forEach((i, k) => { assert.ok(Number.isInteger(i.relevance) && i.relevance >= 0 && i.relevance <= 100, e.n + ' ' + i.id); if (k) assert.ok(l[k - 1].relevance >= i.relevance, e.n + ' order'); })); }); });
t('rank: the CSV is not modified', () => { const before = JSON.stringify(rows); rk(M, '24'); rk(M, '25'); assert.strictEqual(JSON.stringify(rows), before); });
t('rank Exercise 24: DR-0046 (convoy gap) and DR-0097 (halt schedule) are the top relevant rules', () => { const r = rk(M, '24'); const top3 = r.relevant.slice(0, 3).map(i => i.id); assert.ok(top3.includes('DR-0046') && top3.includes('DR-0097'), top3.join(',')); });
t('rank: DR-0009 (bridge-rating spacing) is a false match: not relevant, penalized, never counted as the convoy gap (Exercises 24 and 25)', () => {
  ['24', '25'].forEach(n => { const r = rk(M, n), i = r.items.find(x => x.id === 'DR-0009'); assert.ok(i, n); assert.strictEqual(i.relevant, false); assert.ok(i.parts.penalties.length > 0); assert.ok(i.relevance < 30, String(i.relevance)); }); });
t('rank: emergency bridge-crossing speeds (DR-0017, DR-0033, DR-0034, DR-0035, DR-0036) are not relevant to a general road speed', () => {
  ['15', '24', '25'].forEach(n => { const r = rk(M, n); ['DR-0017', 'DR-0033', 'DR-0034', 'DR-0035', 'DR-0036'].forEach(id => { const i = r.items.find(x => x.id === id); if (i) { assert.strictEqual(i.relevant, false, n + ' ' + id); assert.ok(i.parts.penalties.some(p => /emergency|exceptional/.test(p.reason)), id); } }); }); });
t('rank: a verified clone of the warning-sign rules is still not relevant to routes 10 km apart (Exercises 18, 19)', () => {
  const M2 = matcherWith('DR-0084', 'DR-0043', 'DR-0085'); ['18', '19'].forEach(n => { const r = rk(M2, n); ['DR-0084', 'DR-0043', 'DR-0085'].forEach(id => { const i = r.items.find(x => x.id === id); if (i) assert.strictEqual(i.relevant, false, n + ' ' + id); }); }); });
t('rank: tonnes are not MLC: an MLC rule is never given the "same quantity" credit for 80 tonnes (Exercise 15)', () => {
  const M2 = matcherWith('DR-0171', 'DR-0183', 'DR-0168'); const r = rk(M2, '15'); ['DR-0171', 'DR-0183', 'DR-0168'].forEach(id => { const i = r.items.find(x => x.id === id); if (i && i.relevant) { assert.ok(!i.sameQuantity, id); assert.notStrictEqual(i.status, 'applicable'); } });
  const c = r.items.find(x => x.id === 'DR-0183'); assert.ok(c && c.relevant && c.missing.some(m => /MLC/.test(m)), 'DR-0183 should list the missing vehicle MLC'); });
t('rank: an unverified rule is relevant but in its own group, never applicable, and verification does not change its score', () => {
  const a = rk(M, '24'), b = rk(matcherWith('DR-0145'), '24'); const i = a.items.find(x => x.id === 'DR-0145'), j = b.items.find(x => x.id === 'DR-0145');
  assert.strictEqual(i.status, 'unverified'); assert.strictEqual(i.relevance, j.relevance); assert.notStrictEqual(j.status, 'unverified'); });
t('rank: applicable needs a verified mandatory rule with every condition confirmed (verified clone of DR-0183)', () => {
  const M2 = matcherWith('DR-0183'); assert.notStrictEqual(inGroups(rk(M2, '15'), 'DR-0183'), 'applicable');
  assert.strictEqual(inGroups(rk(M2, '15', { bridges_on_route: 'yes', vehicle_mlc: 70, bridge_mlc_min: 80 }), 'DR-0183'), 'applicable');
  assert.strictEqual(inGroups(rk(M2, '15', { bridges_on_route: 'yes', vehicle_mlc: 90, bridge_mlc_min: 80 }), 'DR-0183'), 'not_applicable'); });
t('rank: a country-specific rule for another country is relevant but not applicable, with a low scope score', () => {
  const r = rk(M, '15'); const i = r.items.find(x => x.id === 'DR-0064'); if (i && i.relevant) { assert.strictEqual(i.status, 'not_applicable'); assert.strictEqual(i.parts.scope, 0); } });
t('rank: overlapping rules are both kept and linked (DR-0029 and DR-0031; DR-0047 and DR-0062)', () => {
  const r = rk(M, '24'); const a = r.items.find(x => x.id === 'DR-0029'), b = r.items.find(x => x.id === 'DR-0031'); assert.ok(a.relevant && b.relevant); assert.ok(a.similarTo.includes('DR-0031') && b.similarTo.includes('DR-0029'));
  const c = r.items.find(x => x.id === 'DR-0062'), d = r.items.find(x => x.id === 'DR-0047'); assert.ok(c.relevant && d.relevant); assert.ok(c.similarTo.includes('DR-0047')); });
t('rank: a rule the exercise already beats is kept but scored lower than a rule that adds something (DR-0001 against DR-0029)', () => {
  const r = rk(M, '24'); const a = r.items.find(x => x.id === 'DR-0001'), b = r.items.find(x => x.id === 'DR-0029'); assert.ok(a.relevant && a.redundant && a.status === 'advisory'); assert.ok(a.relevance < b.relevance + 5, a.relevance + ' ' + b.relevance); });
t('rank: one very high BM25 outlier does not push the other rules out (a new unverified rule that scores far above the rest)', () => {
  const extra = Object.assign({}, rows[0], { rule_id: 'DR-9999', document: 'FM 55-30', pdf_page: '51', evidence_quote: 'Units must properly plan road movements, prepare march tables, submit movement bids, and coordinate en route support. Movement planning table movement table movement plan convoy movement convoy speed gap vehicles serials built-up night.',
    rule_class: 'mandatory_constraint', value_unit_operator: '', scope: 'Movement planning table', conditions: '', verification_status: 'PREVIOUSLY_SUGGESTED_CHECK_SOURCE', quote_match_pdf: '', notes: '' });
  const a = rk(M, '24'), b = rk(new RM.Matcher(rows.concat([extra])), '24'); assert.ok(b.relevant.length >= a.relevant.length, 'relevant count must not fall');
  ['DR-0046', 'DR-0097', 'DR-0061'].forEach(id => assert.ok(b.relevant.some(i => i.id === id && i.relevance >= 60), id)); });
t('rank: a rule with an unrecognised planning role is kept only on a shared phrase, and its score is capped at 70', () => {
  const extra = Object.assign({}, rows[0], { rule_id: 'DR-9998', document: 'FM 55-30', pdf_page: '75', evidence_quote: 'The convoy must clear the RP and get off the route with a minimum of delay to prevent congestion with other scheduled movements.', rule_class: 'conditional_requirement',
    value_unit_operator: '', scope: 'Convoys at designated release points', conditions: 'After reaching release point during a scheduled movement', verification_status: 'PREVIOUSLY_SUGGESTED_CHECK_SOURCE', quote_match_pdf: '', notes: '' });
  const r = rk(new RM.Matcher(rows.concat([extra])), '25'); const i = r.items.find(x => x.id === 'DR-9998'); assert.ok(i && i.relevant && ['related', 'phrase'].includes(i.basis) && i.relevance <= 70, JSON.stringify(i && [i.relevant, i.basis, i.relevance]));
  assert.ok(i.missing.some(m => /planning role is not recognised/.test(m))); assert.strictEqual(i.status, 'unverified'); });
t('rank: single letters such as "b" and "c" never make a rule relevant by wording (Exercise 23: intersections A, B and C)', () => {
  const extra = Object.assign({}, rows[0], { rule_id: 'DR-9997', document: 'FM 55-30', pdf_page: '188', evidence_quote: 'Connecting routes will be given a number formulated on b. or c. above followed by a serial number and will be shown on a tracing or map by dotted lines.', rule_class: 'conditional_requirement',
    value_unit_operator: '', scope: 'Connecting routes within military route network', conditions: 'Only when connecting routes are shown', verification_status: 'PREVIOUSLY_SUGGESTED_CHECK_SOURCE', quote_match_pdf: '', notes: '' });
  const r = rk(new RM.Matcher(rows.concat([extra])), '23'); const i = r.items.find(x => x.id === 'DR-9997'); assert.ok(!i || !i.relevant, JSON.stringify(i && [i.relevant, i.basis])); });
t('rank: invariants hold under random facts (applicable is verified and hard, nothing unverified is applicable)', () => {
  let seed = 11; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647, pick = a => a[Math.floor(rnd() * a.length)];
  const allV = matcherWith(...rows.map(r => r.rule_id));
  for (let k = 0; k < 120; k++) { const user = { bridges_on_route: pick(['yes', 'no', 'unknown']), expressway_on_route: pick(['yes', 'no', 'unknown']), halts_planned: pick(['yes', 'no', 'unknown']), vehicle_mlc: pick(['', 40, 130]), bridge_mlc_min: pick(['', 60, 100]), longest_halt_min: pick(['', 15, 45]), country: pick(['', 'France', 'Germany']) };
    const n = pick(EX).n; [M, allV].forEach(m => { const r = rk(m, n, user);
      r.groups.applicable.forEach(i => { assert.ok(i.rule.verified && i.rule.hard && i.conds.every(c => c.status === 'MET') && !i.unchecked.length, 'applicable ' + i.id); });
      r.groups.unverified.forEach(i => assert.ok(!i.rule.verified, i.id)); r.groups.advisory.concat(r.groups.potential).forEach(i => assert.ok(i.rule.verified, i.id));
      r.groups.not_applicable.forEach(i => assert.ok(i.conds.some(c => c.status === 'CONTRADICTED'), i.id)); }); } });
t('rank: rankJson keeps every rule and states that the score is not a probability', () => { const r = rk(M, '24'), j = RS.rankJson(r, { id: '24' });
  assert.strictEqual(j.relevant.length + j.not_relevant.length, r.counts.retrieved); assert.ok(/not a probability/.test(j.score_note)); j.relevant.forEach(x => { assert.strictEqual(x.enforce, x.status === 'applicable'); }); });

/* ---------------- targeted search improvements: query expansion, meaningless overlaps, objective weight ---------------- */
const stand = (id, quote, cls, scope, cond) => Object.assign({}, rows[0], { rule_id: id, document: 'stand-in', pdf_page: '0', evidence_quote: quote, rule_class: cls, value_unit_operator: '', scope, conditions: cond, verification_status: 'PREVIOUSLY_SUGGESTED_CHECK_SOURCE', quote_match_pdf: '', notes: '' });
t('search: labels of one or two letters, words starting with a digit and generic exercise words never reach the BM25 query', () => {
  const q = RM.queryTokens('Three intersections A, B and C. Route 23b, 1st and 2nd Marine Infantry Regiment, 9th Brigade. We need you to generate diagrams and propose a solution. Reference data. 50 km/h. Pass 3 m.');
  ['b', 'c', 'km', '23b', '1st', '2nd', '9th'].forEach(w => assert.ok(!q.includes(w), 'query still has ' + w)); ['generat', 'diagram', 'propos', 'solution', 'marin', 'infantry', 'regiment', 'brigad'].forEach(w => assert.ok(!q.includes(w), 'generic word kept: ' + w));
  assert.ok(q.includes('intersection') || q.some(w => w.startsWith('intersect')), 'real words must stay'); });
t('search: the related-concept dictionary finds a rule that never uses the exercise wording (invented stand-in for Exercise 23)', () => {
  const sd = stand('DR-T213', 'Traffic control posts are established at critical points and regulate the movement of convoys according to the movement credit.', 'conditional_requirement', 'Traffic control posts at critical points', 'Where traffic is regulated');
  const MM = new RM.Matcher(rows.concat([sd])); const after = rankS(MM, text('23')).items.find(i => i.id === 'DR-T213');
  assert.ok(after && after.relevant && after.status === 'unverified', JSON.stringify(after && [after.relevant, after.status]));
  const saved = RS.RELATED.splice(0, RS.RELATED.length);   // without the dictionary
  try { const before = rankS(MM, text('23')).items.find(i => i.id === 'DR-T213'); assert.ok(!before || !before.relevant, 'without the dictionary it should not be relevant'); } finally { saved.forEach(x => RS.RELATED.push(x)); } });
t('search: DR-0203-like route numbering is not kept because of the letters B and C (Exercise 23), nor through the dictionary', () => {
  const sd = stand('DR-T203', 'Connecting routes will be given a number formulated on b. or c. above followed by a serial number and will be shown on a tracing or map by dotted lines.', 'conditional_requirement', 'Connecting routes within military route network', 'Only when connecting routes are shown');
  const i = rankS(new RM.Matcher(rows.concat([sd])), text('23')).items.find(x => x.id === 'DR-T203'); assert.ok(!i || !i.relevant, JSON.stringify(i && [i.relevant, i.basis])); });
t('search: objective words weigh double in BM25 (a rule matching the objective is ranked at least as high as without the weight)', () => {
  const sd = stand('DR-T1', 'Occupancy of each junction is recorded for every convoy.', 'recommendation', 'Junction occupancy', 'Each junction');
  const MM = new RM.Matcher(rows.concat([sd])); const plain = MM.search(text('23'), 120).results.find(x => x.rule.id === 'DR-T1'), w = MM.search(text('23'), 120, { objective: 'Intersections occupancy charts. Junction occupancy.', boost: 2 }).results.find(x => x.rule.id === 'DR-T1');
  assert.ok(plain && w && w.score > plain.score, 'weighted score must be higher: ' + (plain && plain.score) + ' ' + (w && w.score)); });
t('search: with no objective given, BM25 scores are exactly the plain ones (the weight is optional)', () => {
  const a = M.search(text('24'), 30), b = M.search(text('24'), 30, undefined); assert.deepStrictEqual(a.results.map(x => [x.rule.id, x.score]), b.results.map(x => [x.rule.id, x.score])); });
t('search regression: Exercises 24 and 25 keep DR-0046, DR-0097, DR-0061 and DR-0062 in their top 6 and keep every look-alike out', () => {
  ['24', '25'].forEach(n => { const r = rk(M, n); const top = r.relevant.slice(0, 6).map(i => i.id); ['DR-0046', 'DR-0097', 'DR-0061', 'DR-0062'].forEach(id => assert.ok(top.includes(id), n + ' ' + id + ' not in top 6: ' + top.join(',')));
    ['DR-0009', 'DR-0017', 'DR-0033', 'DR-0035', 'DR-0036'].forEach(id => { const i = r.items.find(x => x.id === id); if (i) assert.strictEqual(i.relevant, false, n + ' ' + id); }); }); });

t('search: single shared words are not a match ("unit" and "march" in unrelated sentences): march-table rule is not relevant to junction congestion (Exercise 23) but is to a movement table (Exercise 24)', () => {
  const sd = stand('DR-T207', 'Units must properly plan road movements, prepare march tables, submit movement bids, and coordinate en route support.', 'mandatory_constraint', 'Motor transport units planning road movements', 'Road movements requiring appropriate control channels');
  const MM = new RM.Matcher(rows.concat([sd]));
  const a = rankS(MM, text('23')).items.find(i => i.id === 'DR-T207'), b = rankS(MM, text('24')).items.find(i => i.id === 'DR-T207');
  assert.ok(!a || !a.relevant, 'Exercise 23: ' + JSON.stringify(a && [a.relevant, a.basis, a.missing])); assert.ok(b && b.relevant && b.status === 'unverified', 'Exercise 24'); });
t('search: traffic control at critical points and congested areas (text of DR-0213 from the page capture) is relevant and unverified in Exercise 23 and ranks above a US-only rule', () => {
  const sd = stand('DR-T213R', 'Traffic control using MPs or HN police should be set up at critical points and congested areas.', 'recommendation', 'Critical points and congested areas on motor transport routes', 'Congestion / critical point present; personnel available');
  const MM = new RM.Matcher(rows.concat([sd])); const r = rankS(MM, text('23')); const i = r.items.find(x => x.id === 'DR-T213R'), us = r.items.find(x => x.id === 'DR-0099');
  assert.ok(i && i.relevant && i.status === 'unverified', JSON.stringify(i && [i.relevant, i.status])); assert.ok(i.relevance > us.relevance, i.relevance + ' vs ' + us.relevance); });

// ---- JSON export, real-CSV checks and the optional hybrid (BM25 + LSA) mode ----
const hasNew = M.rules.some(r => r.id === 'DR-0213');
if (hasNew) t('real CSV, Exercise 23: DR-0213 and DR-0214 are retained (strict policy: unverified); DR-0203 (letters B and C) and DR-0207 (single shared words) are not retained', () => {
  const r = rankS(M, text('23')), g = id => r.items.find(i => i.id === id);
  ['DR-0213', 'DR-0214'].forEach(id => { assert.ok(g(id) && g(id).relevant && g(id).status === 'unverified' && !g(id).rule.verified, id); });
  ['DR-0203', 'DR-0207'].forEach(id => assert.ok(!g(id) || !g(id).relevant, id)); });
t('JSON export: every rule keeps its id, quotation, document, page and verification; ranks are 1..n; scores 0-100 and descending; enforce only for applicable rules', () => {
  const byId = new Map(M.rules.map(r => [r.id, r]));
  ['23', '24', '25'].forEach(n => {
    const out = JSON.parse(JSON.stringify(RS.exportRules(RS.rank(M, text(n)), { id: n, objective: 'x', rulesFile: 'f.csv' })));
    assert.strictEqual(out.selected_rules_count, out.rules.length); assert.strictEqual(out.total_rules_in_csv, M.rules.length); assert.strictEqual(out.search_mode, 'bm25');
    out.rules.forEach((r, k) => { const src = byId.get(r.rule_id); assert.ok(src, r.rule_id); assert.strictEqual(r.rank, k + 1); assert.strictEqual(r.rule_text, src.quote); assert.strictEqual(r.source.pdf_document, src.doc);
      assert.strictEqual(String(r.source.pdf_page), String(parseInt(src.page, 10) || src.page)); assert.ok(r.relevance_score >= 0 && r.relevance_score <= 100); if (k) assert.ok(r.relevance_score <= out.rules[k - 1].relevance_score);
      assert.strictEqual(r.verification_status, src.verified ? 'verified' : (src.reviewed ? 'partly' : 'unverified')); assert.strictEqual(r.enforce, r.applicability_status === 'applicable'); assert.ok(Array.isArray(r.check)); if (r.verification_status !== 'verified') assert.ok(r.check.some(x => /not verified|partly matched/.test(x)), r.rule_id + ' must carry the verification check'); }); }); });
t('the optional hybrid mode is off unless asked: passing an LSA index without mode "hybrid" changes nothing', () => {
  const LSA = require('./lsa.js'), lsa = LSA.build(RM, M.rules); const a = RS.rank(M, text('24')), b = RS.rank(M, text('24'), undefined, { lsa });
  assert.deepStrictEqual(a.relevant.map(i => [i.id, i.relevance, i.status]), b.relevant.map(i => [i.id, i.relevance, i.status])); assert.strictEqual(b.mode, 'bm25'); });
t('hybrid mode: LSA similarity never makes a rule applicable; semantic-only rules are capped, flagged and never above advisory/unverified; the CSV rows are untouched', () => {
  const LSA = require('./lsa.js'), lsa = LSA.build(RM, M.rules), before = JSON.stringify(rows); let seen = 0;
  EX.forEach(e => { const r = RS.rank(M, e.text, undefined, { mode: 'hybrid', lsa }); assert.strictEqual(r.mode, 'hybrid');
    r.items.filter(i => i.basis === 'semantic').forEach(i => { seen++; assert.ok(['advisory', 'unverified', 'not_applicable'].includes(i.status), i.id + ' ' + i.status); assert.ok(i.relevance <= 55, i.id); assert.ok(i.missing.some(x => /semantic similarity only/.test(x)), i.id);
      const ex = RS.exportRules(r, {}).rules.find(x => x.rule_id === i.id); assert.ok(ex && ex.enforce === false && ex.semantic_similarity > 0, i.id); }); });
  assert.ok(seen > 0, 'no semantic-only rule seen at all'); assert.strictEqual(JSON.stringify(rows), before); });

// ---- policy "lenient" (default): no verification gate, unknown conditions are checks, number checks. The old behaviour stays available as policy "strict". ----
t('lenient policy: a contradicted condition (another country) still blocks; applicable rules are mandatory, role-recognised, not redundant; verification is never rewritten', () => {
  const byId = new Map(M.rules.map(r => [r.id, r])); let applicable = 0, unverifiedApplicable = 0;
  EX.forEach(e => { const r = RS.rank(M, e.text); assert.strictEqual(r.policy.gate, false);
    r.relevant.forEach(i => { if (i.applicability === 'not_applicable') assert.strictEqual(i.status, 'not_applicable', e.n + ' ' + i.id);
      if (i.status === 'applicable') { applicable++; assert.ok(i.rule.hard && i.basis === 'requirement' && !i.redundant, e.n + ' ' + i.id);
        if (!i.rule.verified) { unverifiedApplicable++; assert.ok(i.check.some(x => /not verified|only partly matched/.test(x)), e.n + ' ' + i.id + ' needs the verification check'); } }
      assert.strictEqual(i.verification === 'verified', byId.get(i.id).verified, i.id + ': verification must stay as the CSV says'); }); });
  assert.ok(applicable > 0, 'the new policy should enforce something'); });
t('lenient policy: one unknown condition no longer caps a verified mandatory rule, and the unknown becomes a check line (DR-0029 in Exercise 24)', () => {
  const i = RS.rank(M, text('24')).items.find(x => x.id === 'DR-0029'); assert.ok(i && i.status === 'applicable', i && i.status); assert.ok(i.check.some(x => /classified bridge/i.test(x)), JSON.stringify(i.check));
  const old = rankS(M, text('24')).items.find(x => x.id === 'DR-0029'); assert.strictEqual(old.status, 'potential'); });
t('lenient policy: tonnes never stand in for an MLC class (verified clones of the bridge-MLC rules in Exercise 15 stay out of "applicable")', () => {
  const M2 = matcherWith('DR-0171', 'DR-0183'); const r = RS.rank(M2, text('15')); ['DR-0171', 'DR-0183'].forEach(id => { const i = r.items.find(x => x.id === id); if (i && i.relevant) { assert.notStrictEqual(i.status, 'applicable', id); assert.ok(i.check.some(x => /MLC/.test(x)), id); } }); });
t('lenient policy: a rule kept only on wording (role not recognised) is held at "potential", never enforced', () => {
  const sd = stand('DR-T214L', 'The convoy must clear the RP and get off the route with a minimum of delay to prevent congestion with other scheduled movements.', 'mandatory_constraint', 'Convoys at designated release points', 'After reaching release point during a scheduled movement');
  const r = RS.rank(new RM.Matcher(rows.concat([sd])), text('23')), i = r.items.find(x => x.id === 'DR-T214L'); assert.ok(i && i.relevant && i.basis !== 'requirement', JSON.stringify(i && [i.relevant, i.basis])); assert.notStrictEqual(i.status, 'applicable'); });
t('number check: exercise value vs rule limit gives ok / exceeds / stricter / no value (DR-0029 <= 40 km/h, Exercise 24 states 50 and 80 km/h)', () => {
  const r = RS.rank(M, text('24')), n = id => (r.items.find(x => x.id === id) || {}).numeric;
  assert.strictEqual(n('DR-0029').flag, 'exceeds'); assert.ok(/50 km\/h/.test(n('DR-0029').message) && /80 km\/h/.test(n('DR-0029').message)); assert.strictEqual(n('DR-0061').flag, 'no value'); assert.strictEqual(n('DR-0030').flag, 'ok');
  const rule = { val: { qs: [{ value: 40, unit: 'km/h', raw: '40 km/h' }], op: '≤' }, concept: 'speed' }, req = txt => [{ text: txt, qs: RM.quantities(txt).map(q => Object.assign({ concept: 'speed' }, q)) }];
  assert.strictEqual(RS.numberCheck(rule, req('Speed outside built-up areas : 30 km/h')).flag, 'ok');
  assert.strictEqual(RS.numberCheck(rule, req('Speed outside built-up areas : 80 km/h')).flag, 'exceeds');
  assert.strictEqual(RS.numberCheck(rule, req('The speed is not exceeding 80 km/h')).flag, 'stricter');
  assert.strictEqual(RS.numberCheck(rule, req('The speed is not exceeding 30 km/h')).flag, 'ok');
  assert.strictEqual(RS.numberCheck(rule, []).flag, 'no value'); assert.strictEqual(RS.numberCheck({ val: { qs: [], op: '' }, concept: null }, []), null); });
t('number check is a flag only: with and without it the score, rank and group are identical', () => {
  const a = RS.rank(M, text('24'), undefined, { policy: { numeric: false } }), b = RS.rank(M, text('24'));
  assert.deepStrictEqual(a.relevant.map(i => [i.id, i.relevance, i.status]), b.relevant.map(i => [i.id, i.relevance, i.status])); });

t('team extraction schema 2.0: facts become requirement lines, coordinates give the country, context-only items are not constraints, an unknown version is refused', () => {
  const EA = require('./extraction_adapter.js');
  const x = { schema_version: '2.0', exercise_id: 'T1', description: 'Generate a convoy movement chart.', task_types: ['movement_schedule'], review_status: 'model_output_requires_review',
    facts: [{ id: 'F1', kind: 'constraint', category: 'spacing', text: 'Distance between each vehicle: 100 m.', value: 100, unit: 'm', source_ids: ['E1'] },
            { id: 'F2', kind: 'constraint', category: 'speed', text: 'Night-time speed: 40 km/h.', value: 40, unit: 'km/h', source_ids: ['E1'] },
            { id: 'F3', kind: 'unparsed', category: 'other', text: 'Example chart.', value: null, source_ids: ['E3'] }],
    evidence: [{ id: 'E1', page: 1, role: 'main', modality: 'text', quote: 'x' }, { id: 'E2', page: 1, role: 'main', modality: 'text', quote: 'Start : Metz\n(LAT : 48.96 / LONG : 4.53)' }, { id: 'E3', page: 2, role: 'context', modality: 'image', quote: null }],
    printed_coordinates: [{ source_id: 'E2', latitude: 48.96, longitude: 4.53 }] };
  const r = EA.fromExtraction(x); assert.ok(!r.error, r.error); assert.strictEqual(r.id, 'T1');
  assert.ok(/100 m/.test(r.text) && /40 km\/h/.test(r.text) && /LAT : 48.96/.test(r.text)); assert.ok(!/Example chart/.test(r.text)); assert.deepStrictEqual(r.notes.ignored, ['F3 (unparsed, context)']);
  assert.ok(EA.fromExtraction(Object.assign({}, x, { schema_version: '3.0' })).error);
  const rk = RS.rank(M, r.text, r.user); assert.strictEqual(rk.facts.country.value, 'France'); });

console.log(ok + ' passed');

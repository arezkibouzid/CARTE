// node eval/eval_hybrid.js <rules CSV> [--params '{"mix":1}'] [--detail]
// Same exercises, same CSV, BM25-only vs hybrid (BM25 + LSA). Read only. Labels: expected_relevance.json + labels_hybrid.json (the assistant's, to be reviewed).
const fs = require('fs'), path = require('path');
const T = path.join(__dirname, '..', 'search');
const RM = require(path.join(T, 'rulematch.js')), RS = require(path.join(T, 'ruleselect.js')), LSA = require(path.join(T, 'lsa.js'));
const a = process.argv.slice(2); const csv = a[0]; const pi = a.indexOf('--params'); const params = pi >= 0 ? JSON.parse(a[pi + 1]) : {}; const detail = a.includes('--detail');
if (!csv) { console.error('usage: node eval/eval_hybrid.js rules.csv [--params JSON] [--detail]'); process.exit(2); }
const M = new RM.Matcher(RM.parseCsv(fs.readFileSync(csv, 'utf8'))), lsa = LSA.build(RM, M.rules);
const EX = JSON.parse(fs.readFileSync(path.join(T, 'exercises.json'), 'utf8'));
const old = JSON.parse(fs.readFileSync(path.join(__dirname, 'expected_relevance.json'), 'utf8')).exercises;
const sup = JSON.parse(fs.readFileSync(path.join(__dirname, 'labels_hybrid.json'), 'utf8')).exercises;
let fbl = {}; try { fbl = JSON.parse(fs.readFileSync(path.join(__dirname, 'labels_feedback.json'), 'utf8')).exercises || {}; } catch (e) {}   // labels from human feedback, if any
const lab = n => { const o = old[n] || { relevant: [], relevant_unverified: [], must_exclude: [] }, s = sup[n] || {};
  const f = fbl[n] || {};
  const rel = new Set([].concat(o.relevant.map(e => e.id), o.relevant.flatMap(e => e.same_as || []), o.relevant_unverified, s.relevant || [], f.relevant || []));
  return { rel, acc: new Set([].concat(s.acceptable || [], f.acceptable || [])), irr: new Set([].concat(s.irrelevant || [], f.irrelevant || [])), status: s.status || {}, excl: new Set((o.must_exclude || []).map(m => m.id).filter(id => id !== 'DR-0001')) }; };
const sum = { bm25: {}, hybrid: {} }; const add = (m, k, v) => { sum[m][k] = (sum[m][k] || 0) + v; };
const problems = [];
function ndcg(list, L) { const g = i => L.rel.has(i.id) ? 1 : (L.acc.has(i.id) ? 0.3 : 0); const d = list.map((i, k) => g(i) / Math.log2(k + 2)).reduce((x, y) => x + y, 0);
  const ideal = [...L.rel].map(() => 1).concat([...L.acc].map(() => 0.3)).sort((x, y) => y - x); const id = ideal.map((x, k) => x / Math.log2(k + 2)).reduce((x, y) => x + y, 0); return id ? d / id : null; }
function checkJson(res, n, mode) {
  const out = RS.exportRules(res, { id: n, objective: 'x', rulesFile: path.basename(csv) }), byId = new Map(M.rules.map(r => [r.id, r]));
  const rt = JSON.parse(JSON.stringify(out)); let bad = 0;
  const need = ['rank', 'rule_id', 'relevance_score', 'rule_text', 'rule_class', 'verification_status', 'applicability_status', 'enforce', 'source', 'matched_exercise_requirement', 'how_it_helps', 'missing_conditions'];
  rt.rules.forEach((r, k) => { const src = byId.get(r.rule_id);
    if (!src || need.some(f => !(f in r)) || r.rank !== k + 1 || r.relevance_score < 0 || r.relevance_score > 100 || (k && r.relevance_score > rt.rules[k - 1].relevance_score)
        || r.rule_text !== src.quote || r.source.pdf_document !== src.doc || String(r.source.pdf_page) !== String(parseInt(src.page, 10) || src.page)
        || r.enforce !== (r.applicability_status === 'applicable') || !Array.isArray(r.check) || !('number_check' in r) || (r.verification_status !== 'verified' && r.enforce && !r.check.some(x => /not verified|partly matched/.test(x))) || (r.relevance_basis === 'semantic' && (r.enforce || ['applicable', 'potentially_applicable'].includes(r.applicability_status)))) bad++; });
  if (rt.selected_rules_count !== rt.rules.length || rt.total_rules_in_csv !== M.rules.length || rt.search_mode !== mode) bad++;
  return bad;
}
console.log('CSV: ' + path.basename(csv) + ' (' + M.rules.length + ' rules) | LSA ' + lsa.components + ' topics | hybrid parameters ' + JSON.stringify(params) + '\n');
const rows = [];
for (const e of EX) {
  const L = lab(e.n), res = {};
  res.bm25 = RS.rank(M, e.text); res.hybrid = RS.rank(M, e.text, undefined, Object.assign({ mode: 'hybrid', lsa }, params));
  const row = { n: e.n };
  for (const m of ['bm25', 'hybrid']) {
    const r = res[m], list = r.relevant, ids = new Set(list.map(i => i.id));
    const miss = [...L.rel].filter(id => !ids.has(id)), hit = [...L.rel].filter(id => ids.has(id));
    const irr = list.filter(i => L.irr.has(i.id)), acc = list.filter(i => L.acc.has(i.id));
    const unl = list.filter(i => !L.rel.has(i.id) && !L.acc.has(i.id) && !L.irr.has(i.id));
    // applicability errors: a look-alike or irrelevant rule shown as applicable or potentially applicable, a wrong required group, a semantic-only rule that could be enforced
    let appErr = list.filter(i => (L.excl.has(i.id) || L.irr.has(i.id)) && ['applicable', 'potential'].includes(i.status)).length;
    appErr += list.filter(i => L.status[i.id] && i.status !== L.status[i.id]).length;
    appErr += list.filter(i => i.basis === 'semantic' && ['applicable', 'potential'].includes(i.status)).length;
    appErr += list.filter(i => i.status !== 'applicable' && RS.rankJson && false).length;
    const jb = checkJson(r, e.n, m), nd = ndcg(list, L);
    Object.entries({ retained: list.length, rel: L.rel.size, hit: hit.length, miss: miss.length, irr: irr.length, acc: acc.length, unl: unl.length, appErr, jsonBad: jb }).forEach(([k, v]) => add(m, k, v));
    if (nd != null) { add(m, 'ndcg', nd); add(m, 'ndcgN', 1); }
    row[m] = { retained: list.length, hit: hit.length + '/' + L.rel.size, irr: irr.map(i => i.id), unl: unl.length, appErr, nd: nd == null ? null : nd.toFixed(2), miss };
    if (m === 'hybrid') row.added = list.filter(i => !res.bm25.relevant.some(b => b.id === i.id)).map(i => i.id + '(' + (L.rel.has(i.id) ? 'REL' : L.acc.has(i.id) ? 'acc' : L.irr.has(i.id) ? 'IRR' : '?') + ')');
  }
  rows.push(row);
}
console.log('Exercise | retained b/h | found b/h | irrelevant kept b/h | ndcg b/h | added by hybrid');
rows.forEach(r => { if (detail || r.added.length || r.bm25.irr.length || r.hybrid.irr.length || ['23', '24', '25'].includes(r.n))
  console.log(r.n.padEnd(8) + '| ' + (r.bm25.retained + '/' + r.hybrid.retained).padEnd(10) + '| ' + (r.bm25.hit + ' ' + r.hybrid.hit).padEnd(12) + '| ' + (r.bm25.irr.length + '/' + r.hybrid.irr.length).padEnd(8) + '| ' + (r.bm25.nd + '/' + r.hybrid.nd).padEnd(10) + '| ' + r.added.join(' ')); });
const f = (m, k) => sum[m][k] || 0;
console.log('\nTOTAL over ' + EX.length + ' exercises                      BM25-only   Hybrid');
[['Rules retained (all relevant)', 'retained'], ['Labelled-relevant rules found', 'hit'], ['Labelled-relevant rules MISSED', 'miss'], ['Labelled-IRRELEVANT rules retained', 'irr'], ['Borderline (acceptable) rules retained', 'acc'], ['Unlabelled rules retained', 'unl'], ['Applicability errors', 'appErr'], ['JSON output errors', 'jsonBad']]
  .forEach(([t, k]) => console.log(t.padEnd(45) + String(f('bm25', k)).padEnd(12) + f('hybrid', k)));
console.log('Ranking quality (mean nDCG, 1 = ideal)'.padEnd(45) + (f('bm25', 'ndcg') / f('bm25', 'ndcgN')).toFixed(3).padEnd(12) + (f('hybrid', 'ndcg') / f('hybrid', 'ndcgN')).toFixed(3));
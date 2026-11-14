// node rank_extraction.js <rules CSV> <extraction JSON> [more extraction JSON files] [--out folder] [--policy strict]
// For each extraction file, writes exercise_<id>_relevant_rules.json (all relevant rules, best first). Read only: the rules CSV is never changed.
const fs = require('fs'), path = require('path');
const RM = require('./rulematch.js'), RS = require('./ruleselect.js'), EA = require('./extraction_adapter.js');
const a = process.argv.slice(2); const oi = a.indexOf('--out'); const outDir = oi >= 0 ? a.splice(oi, 2)[1] : '.';
const pi = a.indexOf('--policy'); const policy = pi >= 0 ? a.splice(pi, 2)[1] : 'lenient';   // 'lenient' (default: checks) or 'strict' (old: unverified rules held apart, unknown conditions cap a rule)
if (a.length < 2) { console.error('usage: node rank_extraction.js rules.csv extraction.json [more.json ...] [--out folder]'); process.exit(2); }
const csv = a[0];
let M; try { M = new RM.Matcher(RM.parseCsv(fs.readFileSync(csv, 'utf8'))); } catch (e) { console.error('Cannot read the rules CSV: ' + e.message); process.exit(1); }
fs.mkdirSync(outDir, { recursive: true });
let bad = 0;
a.slice(1).forEach(f => {
  let ex; try { ex = EA.fromExtraction(JSON.parse(fs.readFileSync(f, 'utf8').replace(/^﻿/, ''))); } catch (e) { ex = { error: e.message }; }
  if (ex.error) { console.error(path.basename(f) + ': cannot read (' + ex.error + ')'); bad++; return; }
  const r = RS.rank(M, ex.text, ex.user, { policy });
  const out = path.join(outDir, 'exercise_' + ex.id + '_relevant_rules.json');
  fs.writeFileSync(out, JSON.stringify(RS.exportRules(r, { id: ex.id, objective: ex.objective || ex.subtitle, rulesFile: path.basename(csv) }), null, 2));
  const ver = r.relevant.filter(i => i.verification === 'verified').length, enf = r.relevant.filter(i => i.status === 'applicable').length;
  console.log('Exercise ' + ex.id + ': ' + r.counts.relevant + ' relevant rules (' + ver + ' verified, ' + enf + ' enforce:true) -> ' + out);
  if (ex.notes.uncertain.length) console.log('  note: ' + ex.notes.uncertain.length + ' statement(s) marked uncertain in the extraction');
});
process.exit(bad ? 1 : 0);

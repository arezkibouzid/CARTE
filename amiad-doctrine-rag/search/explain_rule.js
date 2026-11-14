// node tools/explain_rule.js <final rules CSV> <exercise number> <rule id> [more rule ids]
// Shows where a rule landed for an exercise: relevant or not, score and its parts, group, and the reasons. Read only.
const fs = require('fs'), path = require('path');
const RM = require('./rulematch.js'), RS = require('./ruleselect.js');
const [csv, n, ...ids] = process.argv.slice(2);
if (!csv || !n || !ids.length) { console.error('usage: node explain_rule.js rules.csv 23 DR-0213 DR-0203'); process.exit(2); }
const M = new RM.Matcher(RM.parseCsv(fs.readFileSync(csv, 'utf8')));
const ex = JSON.parse(fs.readFileSync(path.join(__dirname, 'exercises.json'), 'utf8')).find(e => e.n === n);
if (!ex) { console.error('no exercise ' + n); process.exit(2); }
const r = RS.rank(M, ex.text);
console.log('Exercise ' + n + ': ' + r.counts.relevant + ' relevant rules, ' + r.counts.not_relevant + ' not relevant, ' + r.counts.retrieved + ' retrieved by BM25 (corpus ' + r.counts.corpus + ')');
ids.forEach(id => { const i = r.items.find(x => x.id === id);
  if (!i) { const known = M.rules.some(x => x.id === id); console.log('\n' + id + ': ' + (known ? 'in the CSV but not among the top 120 BM25 candidates for this exercise' : 'not in this CSV')); return; }
  console.log('\n' + id + ': ' + (i.relevant ? 'RELEVANT, group ' + i.status + ', relevance ' + i.relevance + '/100, rank #' + i.relevanceRank + ' of ' + r.counts.relevant + ' (' + i.basis + ')' : 'NOT RELEVANT, score ' + i.relevance));
  console.log('  verification: ' + i.verification + ' | BM25 rank #' + i.rank + ' score ' + i.score.toFixed(1) + ' | role: ' + i.aspect.label);
  console.log('  parts: search ' + i.parts.search + ', requirement ' + i.parts.requirement + ', constraint ' + i.parts.constraint + ', scope ' + i.parts.scope + ', guidance ' + i.parts.guidance + (i.parts.penalties.length ? ', penalties ' + i.parts.penalties.map(p => '-' + p.points + ' ' + p.reason).join('; ') : ''));
  if (i.addresses.length) console.log('  addresses: ' + i.addresses.join(' | '));
  (i.reasons.concat(i.missing)).forEach(x => console.log('  - ' + x));
  console.log('  quote: ' + i.rule.quote.slice(0, 160)); });

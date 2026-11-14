// node eval/eval_feedback.js feedback1.csv [feedback2.csv ...] [--write eval/labels_feedback.json]
// Reads the "Feedback CSV" files downloaded from the page (human decisions Relevant / Irrelevant / Unsure) and
//  1. reports, per exercise, how many rules the reviewers kept, rejected, doubted, and which rules the page called "not relevant" but a reviewer marked Relevant (missed rules);
//  2. with --write, saves them as labels (same format as labels_hybrid.json). eval_hybrid.js picks up eval/labels_feedback.json automatically.
// Read only for the rules CSV. If two reviewers disagree on a rule, the label is "unsure" (nothing is decided silently).
const fs = require('fs'), path = require('path');
const RM = require(path.join(__dirname, '..', 'search', 'rulematch.js'));
const a = process.argv.slice(2), wi = a.indexOf('--write'), out = wi >= 0 ? a.splice(wi, 2)[1] : null;
if (!a.length) { console.error('usage: node eval/eval_feedback.js feedback.csv [more.csv] [--write eval/labels_feedback.json]'); process.exit(2); }
const rows = a.flatMap(f => RM.parseCsv(fs.readFileSync(f, 'utf8').replace(/^﻿/, '')));
const per = {};
rows.forEach(r => { if (!r.exercise_id || !r.rule_id) return; const e = per[r.exercise_id] = per[r.exercise_id] || { votes: {}, shown: {} };
  e.shown[r.rule_id] = r.shown_as; const d = (r.review_decision || '').trim(); if (d && d !== 'unreviewed') (e.votes[r.rule_id] = e.votes[r.rule_id] || new Set()).add(d); });
const labels = {}; console.log('Exercise | reviewed | kept relevant | irrelevant | unsure | precision of the page list | missed (not shown, marked relevant)');
Object.keys(per).sort((x, y) => parseFloat(x) - parseFloat(y)).forEach(n => {
  const e = per[n], L = labels[n] = { relevant: [], acceptable: [], irrelevant: [] }; const missed = [];
  Object.entries(e.votes).forEach(([id, v]) => { const d = v.size === 1 ? [...v][0] : 'unsure';
    if (d === 'relevant') { L.relevant.push(id); if (e.shown[id] === 'not_relevant') missed.push(id); } else if (d === 'irrelevant') L.irrelevant.push(id); else L.acceptable.push(id); });
  const shownRev = Object.keys(e.votes).filter(id => e.shown[id] === 'relevant'), good = shownRev.filter(id => L.relevant.includes(id)).length, bad = shownRev.filter(id => L.irrelevant.includes(id)).length;
  console.log(n.padEnd(8) + '| ' + String(Object.keys(e.votes).length).padEnd(9) + '| ' + String(L.relevant.length).padEnd(14) + '| ' + String(L.irrelevant.length).padEnd(11) + '| ' + String(L.acceptable.length).padEnd(7) + '| ' + (good + bad ? (100 * good / (good + bad)).toFixed(0) + '% (' + good + '/' + (good + bad) + ')' : 'n/a').padEnd(27) + '| ' + (missed.join(' ') || 'none'));
});
if (out) { fs.writeFileSync(out, JSON.stringify({ _note: 'Labels built from human feedback CSV files by eval_feedback.js. Disagreeing reviewers give "acceptable" (unsure).', exercises: labels }, null, 1)); console.log('\nwrote ' + out); }
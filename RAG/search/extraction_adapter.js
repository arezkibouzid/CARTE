/* Turns the team's exercise extraction JSON (schema_version 1.0 and 2.0) into what the Rule Finder reads: a text and a few known facts.
 * Nothing is invented: only what the JSON says. Works in a browser and in Node.
 *   fromExtraction(obj) -> { error } or { id, title, text, user, notes }
 */
(function (root) {
  const USE = new Set(['exercise_input', 'exercise_requirement']);
  const OP = { le: 'not exceeding', ge: 'at least', lt: 'less than', gt: 'more than', minimize: 'as small as possible', maximize: 'as large as possible', forbidden: 'forbidden', required: 'required' };

  function qtyText(v) {
    if (!v) return '';
    if (v.number != null) return v.number + (v.unit ? ' ' + v.unit : '');
    return v.text || '';
  }
  function line(s) {
    let t = String(s.text || '').trim();
    const q = s.value ? qtyText(s.value) : '';
    // add the value only if the sentence does not already show the number
    if (q && s.value.number != null && !t.includes(String(s.value.number))) t += ' (' + (OP[s.operator] ? OP[s.operator] + ' ' : '') + q + ')';
    return t;
  }

  // schema_version 2.0: flat "facts" (kind constraint/unparsed), "evidence" quotes with a main/context role, printed_coordinates.
  function fromV2(x) {
    const miss = ['exercise_id', 'facts', 'evidence'].filter(k => !(k in x));
    if (miss.length) return { error: 'Missing in the JSON: ' + miss.join(', ') + '.' };
    const ev = {}; (x.evidence || []).forEach(e => { ev[e.id] = e; });
    const used = [], ignored = [], uncertain = [];
    let halt = null;
    (x.facts || []).forEach(f => {
      const text = String(f.text || '').trim(); if (!text) return;
      const src = (f.source_ids || []).map(i => ev[i]).filter(Boolean);
      const contextOnly = src.length > 0 && src.every(e => e.role === 'context');
      if (f.kind === 'constraint' && !contextOnly) {
        let t = text;
        if (typeof f.value === 'number' && !t.includes(String(f.value))) t += ' (' + f.value + (f.unit ? ' ' + f.unit : '') + ')';
        used.push(t);
        if (f.certainty && f.certainty !== 'explicit') uncertain.push(f.id + ': ' + text);
        if (typeof f.value === 'number' && /^min/i.test(String(f.unit || '')) && /\bhalt|rest stop|rest halt/i.test(text)) halt = Math.max(halt || 0, f.value);
      } else if (f.kind === 'unparsed' && src.length && src.every(e => e.modality === 'text')) {
        used.push(/^vocabulary/i.test(text) ? text : 'Vocabulary: ' + text);   // helps wording, never a constraint
      } else ignored.push(f.id + ' (' + f.kind + (contextOnly ? ', context' : '') + ')');
    });
    // printed coordinates, in the exercises' own wording, so the country can be read from them
    const noCoords = [];
    (x.printed_coordinates || []).forEach(c => {
      const q = ev[c.source_id]; const lab = q && q.quote ? String(q.quote).replace(/\s+/g, ' ').trim() : '';
      const co = '(LAT : ' + c.latitude + ' / LONG : ' + c.longitude + ')';
      used.push(/LAT/i.test(lab) ? lab : (lab ? lab + ' ' + co : co));
    });
    const issues = (x.issues || []).map(i => typeof i === 'string' ? i : ((i.kind || 'issue') + ': ' + (i.description || i.text || JSON.stringify(i))));
    const uniq = [...new Set(used)];
    const objective = x.description || null;
    return {
      id: String(x.exercise_id), title: 'Exercise ' + x.exercise_id,
      subtitle: (x.task_types || []).join(', ') + (objective ? ' | ' + objective.slice(0, 90) : ''),
      objective, text: (objective ? objective + '\n' : '') + uniq.join('\n'),
      user: halt != null ? { longest_halt_min: String(halt) } : {},
      notes: { ignored, uncertain, issues, noCoords, halt, review_status: x.review_status || null }
    };
  }

  function fromExtraction(x) {
    if (!x || typeof x !== 'object') return { error: 'This is not a JSON object.' };
    if (x.schema_version === '2.0') return fromV2(x);
    if (x.schema_version !== '1.0') return { error: 'Unexpected schema_version "' + x.schema_version + '" (expected "1.0" or "2.0").' };
    const miss = ['exercise_id', 'statements', 'requested_outputs', 'geographic_objects'].filter(k => !(k in x));
    if (miss.length) return { error: 'Missing in the JSON: ' + miss.join(', ') + '.' };

    const used = [], ignored = [], uncertain = [];
    const take = (it, text) => {
      if (!text) return;
      if (USE.has(it.role)) { used.push(text); if (it.certainty === 'uncertain') uncertain.push(it.id + ': ' + text); }
      else if (it.role === 'context_definition') used.push('Vocabulary: ' + text);   // helps wording, never a constraint
      else ignored.push(it.id + ' (' + it.role + ')');
    };

    const objectives = [];
    (x.statements || []).forEach(s => { take(s, line(s)); if (s.kind === 'objective' && USE.has(s.role)) objectives.push(s.text); });
    (x.requested_outputs || []).forEach(d => take(d, d.text));
    (x.ordered_relations || []).forEach(r => take(r, r.text));
    (x.unparsed_statements || []).forEach(u => take(u, u.text));
    (x.entities || []).forEach(e => take(e, e.text));
    (x.references || []).forEach(r => { if (r.availability !== 'provided') return; take(r, r.text); });

    // coordinates, written the way the exercises write them, so the country can be read from them
    const noCoords = [];
    (x.geographic_objects || []).forEach(g => {
      if (!USE.has(g.role)) { ignored.push(g.id + ' (' + g.role + ')'); return; }
      const pos = g.positions || [];
      if (!pos.length) { if (g.coordinate_status !== 'explicit') noCoords.push(g.id + ': ' + g.text); used.push(g.text); return; }
      pos.forEach(p => used.push(g.text + ' (LAT : ' + p.latitude + ' / LONG : ' + p.longitude + ')'));
      if (g.certainty === 'uncertain') uncertain.push(g.id + ': ' + g.text);
    });

    // known fact: longest halt, only when a statement states a halt duration in minutes
    let halt = null;
    (x.statements || []).forEach(s => {
      if (!USE.has(s.role) || !s.value || s.value.number == null) return;
      const u = String(s.value.unit || '').toLowerCase();
      if (/\bhalt|rest stop|rest halt/i.test(s.text) && /^min/.test(u)) halt = Math.max(halt || 0, s.value.number);
    });

    const issues = (x.issues || []).map(i => i.kind + ': ' + i.description);
    const uniq = [...new Set(used)];
    return {
      id: String(x.exercise_id),
      title: 'Exercise ' + x.exercise_id,
      subtitle: (x.task_types || []).join(', ') + (objectives[0] ? ' | ' + objectives[0].slice(0, 90) : ''),
      objective: objectives[0] || null,
      text: uniq.join('\n'),
      user: halt != null ? { longest_halt_min: String(halt) } : {},
      notes: { ignored, uncertain, issues, noCoords, halt }
    };
  }

  const api = { fromExtraction };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.ExtractionAdapter = api;
})(typeof self !== 'undefined' ? self : this);
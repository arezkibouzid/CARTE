/* Optional semantic search (LSA = latent semantic analysis) for the Rule Finder. Runs in the browser and in Node, no server, no download.
 * TF-IDF over words and word pairs of each rule (quote, scope, conditions), reduced to a few dozen "topics" with a singular value decomposition.
 * A rule and a query that talk about the same topic get a high cosine even when they share few exact words.
 * It is a similarity between texts. It never shows that a rule applies: ruleselect.js uses it only to widen the candidate list and to keep
 * rules whose planning role is not recognised, always marked as "semantic match only, a person must confirm".
 *   const lsa = LSA.build(RuleMatch, matcher.rules);  lsa.sim('some text') -> Float64Array, one cosine per rule (0 to 1)
 */
(function (root) {
  function build(RM, rules, opts) {
    const K = (opts && opts.components) || 80, n = rules.length;
    const toks = text => RM.tokens(text).filter(t => t.length >= 3 && /[a-z]/.test(t));
    const grams = t => { const g = t.slice(); for (let i = 0; i + 1 < t.length; i++) g.push(t[i] + ' ' + t[i + 1]); return g; };
    const docs = rules.map(r => grams(toks([r.quote, r.quote, r.scope, r.conditions].join(' . '))));
    const vocab = new Map(), df = [];
    docs.forEach(d => new Set(d).forEach(t => { if (!vocab.has(t)) { vocab.set(t, vocab.size); df.push(0); } df[vocab.get(t)]++; }));
    const idf = df.map(c => Math.log((1 + n) / (1 + c)) + 1);
    const vec = d => {                                   // sparse, sublinear tf, l2-normalised
      const tf = new Map(); d.forEach(t => { const j = vocab.get(t); if (j !== undefined) tf.set(j, (tf.get(j) || 0) + 1); });
      let s = 0; const out = []; tf.forEach((c, j) => { const w = (1 + Math.log(c)) * idf[j]; out.push([j, w]); s += w * w; });
      s = Math.sqrt(s) || 1; return out.map(([j, w]) => [j, w / s]);
    };
    const X = docs.map(vec);
    const post = Array.from({ length: vocab.size }, () => []);     // term -> [doc, weight]
    X.forEach((v, i) => v.forEach(([j, w]) => post[j].push([i, w])));
    const xq = q => { const out = new Float64Array(n); q.forEach(([j, w]) => post[j].forEach(([i, x]) => { out[i] += w * x; })); return out; };
    // Gram matrix of the documents, then its eigen-decomposition (Jacobi): equals the SVD of X for a matrix this small, and it is deterministic
    const A = Array.from({ length: n }, () => new Float64Array(n));
    post.forEach(p => { for (const [i, a] of p) for (const [j, b] of p) A[i][j] += a * b; });
    const V = Array.from({ length: n }, (_, i) => { const r = new Float64Array(n); r[i] = 1; return r; });
    for (let sweep = 0; sweep < 30; sweep++) {
      let off = 0; for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) off += A[i][j] * A[i][j];
      if (off < 1e-18) break;
      for (let p = 0; p < n - 1; p++) for (let q = p + 1; q < n; q++) {
        const apq = A[p][q]; if (Math.abs(apq) < 1e-14) continue;
        const th = (A[q][q] - A[p][p]) / (2 * apq), t = Math.sign(th || 1) / (Math.abs(th) + Math.sqrt(th * th + 1)), c = 1 / Math.sqrt(t * t + 1), s = t * c;
        for (let k = 0; k < n; k++) { const akp = A[k][p], akq = A[k][q]; A[k][p] = c * akp - s * akq; A[k][q] = s * akp + c * akq; }
        for (let k = 0; k < n; k++) { const apk = A[p][k], aqk = A[q][k]; A[p][k] = c * apk - s * aqk; A[q][k] = s * apk + c * aqk; }
        for (let k = 0; k < n; k++) { const vkp = V[k][p], vkq = V[k][q]; V[k][p] = c * vkp - s * vkq; V[k][q] = s * vkp + c * vkq; }
      }
    }
    const order = Array.from({ length: n }, (_, i) => i).sort((a, b) => A[b][b] - A[a][a]).filter(i => A[i][i] > 1e-9).slice(0, Math.max(2, Math.min(K, n - 1)));
    const sv = order.map(i => Math.sqrt(A[i][i])), k = order.length;
    const U = Array.from({ length: n }, (_, r) => Float64Array.from(order, c => V[r][c]));       // n x k
    const norm = a => { let s = 0; for (const x of a) s += x * x; s = Math.sqrt(s) || 1; return a.map(x => x / s); };
    const Z = U.map(row => norm(Float64Array.from(row, (x, c) => x * sv[c])));                    // document topics, unit length
    const project = qv => { const e = xq(qv), z = new Float64Array(k); for (let c = 0; c < k; c++) { let s = 0; for (let i = 0; i < n; i++) s += e[i] * U[i][c]; z[c] = s / sv[c]; } return norm(z); };
    function sim(text) {
      const qv = vec(grams(toks(text)));
      const out = new Float64Array(n); if (!qv.length) return out;
      const z = project(qv); for (let i = 0; i < n; i++) { let s = 0; for (let c = 0; c < k; c++) s += Z[i][c] * z[c]; out[i] = Math.max(0, s); }
      return out;
    }
    return { sim, components: k, vocabulary: vocab.size, rules: n };
  }
  const api = { build };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.LSA = api;
})(typeof window !== 'undefined' ? window : globalThis);

import { useEffect, useMemo, useRef, useState } from 'react';
import { chatOrder, extractOrder, getOrderAnalysis, getOrders, uploadOrder } from '../api';
import OrderLinksCard from './OrderLinks';
import type { ChatMessage, ExplainLevel, OrderAnalysis, OrderFact, OrderInfo, OrderRule, OrdersStatus } from '../types';

interface Props {
  /** Contexte courant de la carte (points, zones, résultats), envoyé au chatbot si l'utilisateur le permet. */
  uiContext: () => Record<string, unknown>;
  /** Place les points de l'ordre dans le scénario (outils existants de la carte). */
  onApplyToMap: (m: OrderAnalysis['map_inputs']) => void;
}

const LEVELS: { id: ExplainLevel; label: string }[] = [
  { id: 'concise', label: 'Concis' },
  { id: 'standard', label: 'Standard' },
  { id: 'detailed', label: 'Détaillé' },
];
const GROUPS: { id: OrderRule['group']; title: string; hint: string }[] = [
  { id: 'applicable', title: 'Explicitement applicables', hint: 'Règles obligatoires dont aucune condition n’est contredite par l’ordre : à respecter, après lecture des points à vérifier.' },
  { id: 'potential', title: 'Potentiellement pertinentes', hint: 'Règles utiles ou recommandations à confirmer par une personne (condition inconnue, simple lien de vocabulaire, règle indicative).' },
  { id: 'not_applicable', title: 'Non applicables', hint: 'Une condition de la règle est contredite par l’ordre. Gardées pour mémoire.' },
];
const KIND_FR: Record<string, string> = {
  constraint: 'contrainte', input: 'donnée', objective: 'objectif', reference: 'référence', requested_output: 'livrable',
  ambiguous: 'ambigu', other: 'autre', ignore: 'titre', unparsed: 'vocabulaire',
};

export default function OrdersPanel({ uiContext, onApplyToMap }: Props) {
  const [orders, setOrders] = useState<OrderInfo[]>([]);
  const [status, setStatus] = useState<OrdersStatus | null>(null);
  const [orderId, setOrderId] = useState<string>(() => localStorage.getItem('amiad-order') ?? '');
  const [level, setLevel] = useState<ExplainLevel>('standard');
  const [a, setA] = useState<OrderAnalysis | null>(null);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [showFacts, setShowFacts] = useState(false);
  const [openGroup, setOpenGroup] = useState<Record<string, boolean>>({ applicable: true, potential: true, not_applicable: false });
  const [hiFact, setHiFact] = useState<string | null>(null);
  const [chats, setChats] = useState<Record<string, ChatMessage[]>>({});
  const [q, setQ] = useState('');
  const [useMap, setUseMap] = useState(true);
  const [chatBusy, setChatBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);

  const refresh = () => getOrders().then((r) => { setOrders(r.orders); setStatus(r.status); }).catch((e) => setErr((e as Error).message));
  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    try { localStorage.setItem('amiad-order', orderId); } catch { /* sans conséquence */ }
  }, [orderId]);

  const current = orders.find((o) => o.id === orderId);
  const load = async (id: string, lvl: ExplainLevel) => {
    setBusy('Recherche des règles et explications…');
    setErr('');
    try {
      setA(await getOrderAnalysis(id, lvl));
    } catch (e) {
      setA(null);
      setErr((e as Error).message);
    } finally {
      setBusy('');
    }
  };
  useEffect(() => {
    if (current?.extracted) load(orderId, level);
    else setA(null);
  }, [orderId, level, current?.extracted]);

  const facts = useMemo(() => Object.fromEntries((a?.facts ?? []).map((f) => [f.id, f])), [a]);
  const messages = chats[orderId] ?? [];
  useEffect(() => { chatEnd.current?.scrollIntoView({ block: 'nearest' }); }, [messages.length, chatBusy]);

  const ask = async (question: string, lvl: ExplainLevel = level) => {
    if (!question.trim() || !orderId) return;
    const msgs: ChatMessage[] = [...messages, { role: 'user', content: question.trim() }];
    setChats((c) => ({ ...c, [orderId]: msgs }));
    setQ('');
    setChatBusy(true);
    try {
      const r = await chatOrder(orderId, msgs.map(({ role, content }) => ({ role, content })), lvl, useMap ? uiContext() : null);
      setChats((c) => ({ ...c, [orderId]: [...msgs, { role: 'assistant', content: r.answer, reply: r }] }));
    } catch (e) {
      setChats((c) => ({ ...c, [orderId]: [...msgs, { role: 'assistant', content: `Erreur : ${(e as Error).message}` }] }));
    } finally {
      setChatBusy(false);
    }
  };
  const lastQuestion = [...messages].reverse().find((m) => m.role === 'user')?.content;

  const extract = async () => {
    setBusy('Extraction de l’ordre par le modèle de vision (environ 10 à 30 s)…');
    setErr('');
    try {
      await extractOrder(orderId);
      await refresh();
      await load(orderId, level);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const ex = status?.explanation;
  return (
    <div className="panel orders">
      <div className="model-status small">
        <span className={`dot ${ex?.available ? 'ok' : 'off'}`} />
        {ex?.available ? <>Explications : <b>{ex.model}</b> (local)</> : <>Explications sans modèle de langue{ex?.error ? ' (modèle non joignable)' : ''}</>}
        <span className="sep">·</span>
        <span className={`dot ${status?.extraction.configured ? 'ok' : 'off'}`} />
        Extraction : {status?.extraction.configured ? status.extraction.model : 'hors service'}
      </div>

      <h3>Ordre d’opération</h3>
      <div className="row">
        <select value={orderId} onChange={(e) => setOrderId(e.target.value)} style={{ flex: 1 }}>
          <option value="">Choisir un ordre…</option>
          {orders.map((o) => (
            <option key={o.id} value={o.id}>{o.label}{o.extracted ? ' ✓' : ''}{o.extraction_status === 'needs_review' ? ' (à revoir)' : ''}</option>
          ))}
        </select>
        <button className="btn" onClick={() => file.current?.click()} title="Importer un JSON d’extraction (schéma 1.0, 2.0 ou 3.2)">Importer</button>
        <input ref={file} type="file" accept="application/json,.json" hidden onChange={async (e) => {
          const f = e.target.files?.[0];
          e.target.value = '';
          if (!f) return;
          try {
            const r = await uploadOrder(f);
            await refresh();
            setOrderId(r.id);
          } catch (er) {
            setErr((er as Error).message);
          }
        }} />
      </div>
      {current && !current.extracted && (
        <div className="card">
          <div className="small">Cet ordre n’a pas encore été extrait (pages {current.pages?.join(', ')}).</div>
          <button className="btn primary" disabled={!status?.extraction.configured || !!busy} onClick={extract}>Extraire l’ordre</button>
          {!status?.extraction.configured && <div className="small muted">Le modèle d’extraction n’est pas en service : lancez <code>sbatch deploy/serve_llm.sbatch</code>, ou importez un JSON.</div>}
        </div>
      )}
      {current?.extracted && status?.extraction.configured && (
        <button className="link small" disabled={!!busy} onClick={extract}>↻ Relancer l’extraction de cet ordre</button>
      )}

      <div className="row level">
        <span className="small">Niveau d’explication</span>
        <div className="seg small">
          {LEVELS.map((l) => <button key={l.id} className={level === l.id ? 'on' : ''} onClick={() => setLevel(l.id)}>{l.label}</button>)}
        </div>
      </div>
      {busy && <div className="card small muted">{busy}</div>}
      {err && <div className="card errcard small">{err}</div>}

      {a && (
        <>
          <div className="card">
            <div className="title">Exercice {a.exercise_id}{a.task_types?.length ? ` — ${a.task_types.join(', ')}` : ''}</div>
            {a.objective && <div className="small">{a.objective}</div>}
            <div className="small muted">
              Extraction {a.schema_version}{a.extraction ? ` · ${a.extraction.model} · ${a.extraction.status === 'validated_selected_checks_pass' ? 'contrôles passés' : 'à revoir'}` : a.review_status ? ` · ${a.review_status}` : ''}
              {' · '}{a.rag.selected_rules_count} règles retenues sur {a.rag.total_rules_in_csv} ({a.rag.search_mode})
            </div>
            <div className="row">
              <button className="link small" onClick={() => setShowFacts((s) => !s)}>{showFacts ? '▾' : '▸'} Éléments extraits ({a.facts.length})</button>
              {(a.map_inputs.start || a.map_inputs.area) && (
                <button className="btn small" onClick={() => onApplyToMap(a.map_inputs)} title="Départ, arrivée, étapes et zone de déploiement imprimés dans l’ordre">Placer les points sur la carte</button>
              )}
            </div>
            {showFacts && (
              <ul className="facts small">
                {a.facts.filter((f) => f.kind !== 'ignore').map((f) => <FactLine key={f.id} f={f} hi={hiFact === f.id} />)}
              </ul>
            )}
            {(a.issues.length > 0 || a.visual_notes.length > 0) && (
              <div className="small muted">
                {a.issues.length > 0 && <>{a.issues.length} incertitude(s) signalée(s) par l’extraction. </>}
                {a.visual_notes.length > 0 && <>{a.visual_notes.length} note(s) visuelle(s) à vérifier sur le document.</>}
              </div>
            )}
          </div>
          <OrderLinksCard orderId={orderId} orders={orders} />
          {a.explanation.model
            ? <p className="small muted">Explications rédigées par {a.explanation.model} en {a.explanation.seconds} s ; statuts et sources fixés par le moteur de règles.</p>
            : <p className="small muted">Explications tirées du moteur de règles (sans modèle de langue{a.explanation.fallback_reason ? ` : ${String(a.explanation.fallback_reason)}` : ''}).</p>}

          {GROUPS.map((g) => {
            const rules = a.rules.filter((r) => r.group === g.id);
            if (!rules.length) return null;
            return (
              <div key={g.id} className={`rule-group ${g.id}`}>
                <button className="link group-title" onClick={() => setOpenGroup((o) => ({ ...o, [g.id]: !o[g.id] }))} title={g.hint}>
                  {openGroup[g.id] ? '▾' : '▸'} {g.title} ({rules.length})
                </button>
                {openGroup[g.id] && rules.map((r) => <RuleCard key={r.rule_id} r={r} facts={facts} onHover={setHiFact} onAsk={(t) => ask(t)} />)}
              </div>
            );
          })}

          <h3>Questions sur l’ordre</h3>
          <div className="chat">
            {messages.length === 0 && (
              <div className="small muted">
                Exemples :
                {['Quelles règles dois-je respecter absolument ?', 'Pourquoi la règle sur les haltes s’applique-t-elle ?', 'Mon itinéraire actuel pose-t-il un problème au regard des règles ?'].map((s) => (
                  <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>
                ))}
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                <div className="bubble">{m.content}</div>
                {m.reply && (m.reply.sources.length > 0 || m.reply.cited_facts.length > 0) && (
                  <div className="cites small">
                    {m.reply.sources.map((s) => (
                      <span key={s.rule_id} className={`chip src ${s.group}`} title={`${s.status} · ${s.verification_status}`}>
                        {s.rule_id} — {s.pdf_document} p. {s.pdf_page}
                      </span>
                    ))}
                    {m.reply.cited_facts.map((f) => (
                      <span key={f.id} className="chip fact" title={f.text}>{f.id} (p. {f.page})</span>
                    ))}
                  </div>
                )}
                {m.reply && <div className="small muted">{m.reply.model ? `${m.reply.model} · ${m.reply.seconds} s` : 'sans modèle de langue'}</div>}
              </div>
            ))}
            {chatBusy && <div className="msg assistant"><div className="bubble muted">…</div></div>}
            <div ref={chatEnd} />
          </div>
          <div className="row">
            <input className="chat-input" value={q} placeholder="Votre question sur l’ordre ou ses règles…" onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(q); } }} disabled={chatBusy} />
            <button className="btn primary" onClick={() => ask(q)} disabled={chatBusy || !q.trim()}>Envoyer</button>
          </div>
          <div className="row small">
            <button className="btn small" disabled={!lastQuestion || chatBusy} onClick={() => ask(`Réponds plus brièvement : ${lastQuestion}`, 'concise')}>Plus court</button>
            <button className="btn small" disabled={!lastQuestion || chatBusy} onClick={() => ask(`Explique plus en détail : ${lastQuestion}`, 'detailed')}>Plus détaillé</button>
            <label className="check small"><input type="checkbox" checked={useMap} onChange={(e) => setUseMap(e.target.checked)} /> Utiliser le contexte de la carte</label>
            {messages.length > 0 && <button className="link small" onClick={() => setChats((c) => ({ ...c, [orderId]: [] }))}>Effacer</button>}
          </div>
        </>
      )}
    </div>
  );
}

function FactLine({ f, hi }: { f: OrderFact; hi: boolean }) {
  return (
    <li className={hi ? 'hi' : ''}>
      <span className="fid">{f.id}</span> <span className="badge">{KIND_FR[f.kind] ?? f.kind}</span>
      {f.scope && <span className="badge">{f.scope}</span>} {f.text}
      {f.page != null && <span className="muted"> (p. {f.page})</span>}
    </li>
  );
}

function RuleCard({ r, facts, onHover, onAsk }: { r: OrderRule; facts: Record<string, OrderFact>; onHover: (id: string | null) => void; onAsk: (q: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`card rule ${r.group}`}>
      <div className="title">
        <span className="rid">{r.rule_id}</span> {r.rule_value}
        {r.verification_status !== 'verified' && <span className="badge warn" title="Citation non vérifiée sur le PDF">non vérifiée</span>}
        {r.quote_check?.index_check === 'not_found' && (
          <span className="badge warn" title="Le contrôle automatique n’a pas retrouvé la citation dans le texte lu du PDF (souvent un manuel scanné, un tableau ou une figure) : comparer à la page">citation non retrouvée</span>
        )}
      </div>
      <div className="small">{r.explanation}</div>
      {r.linked_facts.length > 0 && (
        <div className="small links">
          Éléments de l’ordre :
          {r.linked_facts.map((id) => (
            <span key={id} className="chip fact" title={facts[id]?.text} onMouseEnter={() => onHover(id)} onMouseLeave={() => onHover(null)}>
              {id}{facts[id]?.page != null ? ` (p. ${facts[id].page})` : ''}
            </span>
          ))}
        </div>
      )}
      <div className="small muted">Source : {r.source?.pdf_document}, p. {r.source?.pdf_page} · pertinence {r.relevance_score}/100</div>
      <div className="row">
        <button className="link small" onClick={() => setOpen((o) => !o)}>{open ? '▾' : '▸'} Texte et points à vérifier</button>
        <button className="link small" onClick={() => onAsk(`Explique la règle ${r.rule_id} et son lien avec l’ordre.`)}>Demander au chatbot</button>
      </div>
      {open && (
        <div className="small">
          <p className="quote">« {r.rule_text} »{r.rule_scope ? ` — portée : ${r.rule_scope}` : ''}</p>
          {r.check.length > 0 && <ul>{r.check.map((c, i) => <li key={i}>{c.replace(/^check: /, '')}</li>)}</ul>}
          {r.number_check && r.number_check.flag !== 'no value' && (
            <div>Contrôle chiffré : <b>{r.number_check.flag}</b> (limite {r.number_check.rule_limit} ; ordre : {(r.number_check.exercise_values ?? []).join(', ')})</div>
          )}
          {r.warnings?.length > 0 && <div className="muted">{r.warnings.join(' ')}</div>}
        </div>
      )}
    </div>
  );
}

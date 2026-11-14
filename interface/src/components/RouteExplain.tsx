import { useEffect, useRef, useState } from 'react';
import { chatRoute, getOrderLinks, getOrders, parseTextOrder } from '../api';
import { TextOrderPreview, type ApplyTextOrder } from './TextOrder';
import type { BBox, ConvoysResult, ExplainLevel, MapContext, OrderInfo, OrderLinksResult, RouteChatReply, RouteResult, RouteSegment, TextOrderResult } from '../types';
import { IT_COLORS } from './MapView';

const VERDICT: Record<RouteSegment['verdict'], { label: string; cls: string; hint: string }> = {
  unavoidable: { label: 'obligé', cls: 'v-must', hint: 'Aucun autre passage entre ses deux extrémités dans le réseau autorisé' },
  'clearly best': { label: 'net', cls: 'v-best', hint: 'Le meilleur contournement coûte au moins 10 % de plus' },
  'narrow choice': { label: 'serré', cls: 'v-close', hint: 'Une autre voie était presque aussi bonne (moins de 10 % d’écart)' },
};

const segBox = (s: RouteSegment): BBox => {
  const pad = 0.01;
  return [Math.min(s.from[0], s.to[0]) - pad, Math.min(s.from[1], s.to[1]) - pad, Math.max(s.from[0], s.to[0]) + pad, Math.max(s.from[1], s.to[1]) + pad];
};

/** « Pourquoi ce tracé » : critères, segments avec leur verdict, alternatives rejetées et limites (trace du solveur). */
export function WhyPanel({ route, onFocus }: { route: RouteResult; onFocus: (b: BBox) => void }) {
  const [open, setOpen] = useState(true);
  const [sel, setSel] = useState<string | null>(null);
  const tr = route.explanation;
  if (!tr) return null;
  const segs = tr.legs.flatMap((l) => l.segments);
  return (
    <div className="why">
      <button className="link" onClick={() => setOpen((o) => !o)}>{open ? '▾' : '▸'} Pourquoi ce tracé ? ({segs.length} segments)</button>
      {open && (
        <>
          <ul className="small muted crit">{tr.criteria.map((c, i) => <li key={i}>{c}</li>)}</ul>
          <table className="segs small">
            <thead><tr><th>Seg.</th><th>Route</th><th>km</th>{route.uncertainty_cost && <th title="Confiance du modèle (moyenne géométrique)">Conf.</th>}<th title="Surcoût du meilleur contournement local">Détour</th><th>Choix</th></tr></thead>
            <tbody>
              {segs.map((s) => (
                <tr key={s.id} className={sel === s.id ? 'sel' : ''} onClick={() => { setSel(sel === s.id ? null : s.id); onFocus(segBox(s)); }} title="Cliquer : zoomer et voir le détail">
                  <td>{s.id}</td>
                  <td>{s.road_class.replace("route d'importance", 'imp.')}{s.names.length ? ` (${s.names[0]})` : ''}</td>
                  <td>{s.km.toFixed(1)}</td>
                  {route.uncertainty_cost && <td className={s.confidence !== undefined && s.confidence < 0.5 ? 'txt-doubt' : ''}>{s.confidence?.toFixed(2) ?? '—'}</td>}
                  <td>{s.alternative ? `${s.alternative.extra_cost_pct >= 0 ? '+' : ''}${Math.round(s.alternative.extra_cost_pct)} %` : '—'}</td>
                  <td><span className={`verdict ${VERDICT[s.verdict].cls}`} title={VERDICT[s.verdict].hint}>{VERDICT[s.verdict].label}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          {sel && (() => {
            const s = segs.find((x) => x.id === sel)!;
            return (
              <div className="card small">
                <b>{s.id}</b> — {s.road_class}, {s.source}. {s.km} km, {Math.round(s.min)} min, limites {s.legal_kmh.join('/')} km/h, {s.km_built_up} km en agglomération
                {s.bridges ? `, ${s.bridges} pont(s)` : ''}.<br />
                Coût : {s.cost_priced} {tr.unit}{s.uncertainty_cost !== undefined ? ` (dont ${s.uncertainty_cost} ${tr.unit} d’incertitude)` : ''}.<br />
                <i>{s.why}.</i>
              </div>
            );
          })()}
          {tr.alternatives.length > 0 && (
            <>
              <div className="small"><b>Alternatives rejetées</b></div>
              <ul className="small">{tr.alternatives.map((a, i) => <li key={i}>{a.kind} : {a.what} — {a.rejected_because}</li>)}</ul>
            </>
          )}
          {tr.limitations.length > 0 && (
            <>
              <div className="small"><b>Limites des données</b></div>
              <ul className="small muted">{tr.limitations.map((x, i) => <li key={i}>{x}</li>)}</ul>
            </>
          )}
        </>
      )}
    </div>
  );
}

const QUICK = ['Pourquoi cet itinéraire ?', 'Détaille l’itinéraire segment par segment', 'Quelles alternatives ont été rejetées ?', 'Quelles sont les limites et incertitudes ?'];

interface Msg { role: 'user' | 'assistant'; content: string; reply?: RouteChatReply; order?: TextOrderResult }

/** Questions sur l'itinéraire : réponses tirées de la trace du solveur (sans modèle) ou du modèle local, avec les ordres choisis. */
export function RouteChat({ route, uiContext, sheetId, onTextOrder, mapCtx }: { route: RouteResult; uiContext: () => Record<string, unknown>;
  sheetId?: string; onTextOrder?: ApplyTextOrder; mapCtx?: () => MapContext }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [level, setLevel] = useState<ExplainLevel>('standard');
  const [orders, setOrders] = useState<OrderInfo[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [showOrders, setShowOrders] = useState(false);
  const [linked, setLinked] = useState<OrderLinksResult | null>(null);
  useEffect(() => {
    if (!picked.length) { setLinked(null); return; }
    getOrderLinks(picked).then(setLinked).catch(() => setLinked(null));
  }, [picked.join(',')]); // eslint-disable-line react-hooks/exhaustive-deps
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { getOrders().then((r) => setOrders(r.orders.filter((o) => o.extracted))).catch(() => setOrders([])); }, []);
  useEffect(() => { setMsgs([]); }, [route.route_id]);
  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }); }, [msgs.length, busy]);

  const ask = async (text: string) => {
    if (!text.trim() || busy) return;
    const next: Msg[] = [...msgs, { role: 'user', content: text.trim() }];
    setMsgs(next);
    setQ('');
    setBusy(true);
    try {
      if (sheetId && onTextOrder) {  // an order ("de PD4 à PA4, le plus rapide") is previewed, not answered
        const o = await parseTextOrder(text.trim(), sheetId, 'auto', mapCtx?.()).catch(() => null);
        if (o?.is_command) {
          setMsgs([...next, { role: 'assistant', content: 'Ordre reconnu : voici ce qui changerait dans le scénario.', order: o }]);
          return;
        }
      }
      const r = await chatRoute(route.route_id, next.map(({ role, content }) => ({ role, content })), level, picked, uiContext());
      setMsgs([...next, { role: 'assistant', content: r.answer, reply: r }]);
    } catch (e) {
      setMsgs([...next, { role: 'assistant', content: `Erreur : ${(e as Error).message}` }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="route-chat">
      <h3>Questions sur l’itinéraire</h3>
      <div className="row small">
        <div className="seg small">
          {(['concise', 'standard', 'detailed'] as ExplainLevel[]).map((l) => (
            <button key={l} className={level === l ? 'on' : ''} onClick={() => setLevel(l)}>{l === 'concise' ? 'Concis' : l === 'standard' ? 'Standard' : 'Détaillé'}</button>
          ))}
        </div>
        <button className="link small" onClick={() => setShowOrders((s) => !s)}>{showOrders ? '▾' : '▸'} Ordres pris en compte ({picked.length})</button>
      </div>
      {showOrders && (
        <div className="order-pick small">
          {orders.length === 0 && <span className="muted">Aucun ordre extrait (onglet 4 · Ordres).</span>}
          {orders.map((o) => (
            <label key={o.id} className="check">
              <input type="checkbox" checked={picked.includes(o.id)} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, o.id] : p.filter((x) => x !== o.id)))} />
              {o.label}
            </label>
          ))}
          {linked && linked.added.length > 0 && (
            <p className="small">+ ordres liés ajoutés automatiquement : <b>{linked.added.map((o) => linked.orders.find((h) => h.order === o)?.ref ?? o).join(', ')}</b>
              {linked.effective.some((e) => e.replaced.length) && <> · valeurs en vigueur calculées par le code</>}
              {linked.conflicts.length > 0 && <span className="txt-doubt"> · {linked.conflicts.length} conflit(s) à faire trancher</span>}</p>
          )}
          <p className="muted">Avec beaucoup d’ordres, seuls les fragments et règles les plus liés à la question sont envoyés au modèle (budget de contexte) ; la réponse indique ce qui a été omis.</p>
        </div>
      )}
      <div className="quick">
        {QUICK.map((x) => <button key={x} className="btn small" disabled={busy} onClick={() => ask(x)}>{x}</button>)}
      </div>
      <div className="chat">
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="bubble" style={{ whiteSpace: 'pre-line' }}>{m.content.replace(/\*\*/g, '')}</div>
            {m.order && onTextOrder && <TextOrderPreview r={m.order} onApply={onTextOrder} />}
            {m.reply && (
              <div className="meta small muted">
                {m.reply.mode === 'trace' ? 'Réponse tirée de la trace du solveur (sans modèle)' : m.reply.model ? `Modèle ${m.reply.model}` : m.reply.mode}
                {' · '}{m.reply.seconds} s
                {m.reply.cited_segments.length > 0 && <> · segments {m.reply.cited_segments.join(', ')}</>}
                {m.reply.cited_rules.length > 0 && <> · règles {m.reply.cited_rules.join(', ')}</>}
                {(m.reply.cited_facts?.length ?? 0) > 0 && <> · fragments {m.reply.cited_facts!.join(', ')}</>}
                {m.reply.context?.linked_orders_added && m.reply.context.linked_orders_added.length > 0 && <> · ordres liés ajoutés : {m.reply.context.linked_orders_added.join(', ')}</>}
                {m.reply.context && <> · contexte {m.reply.context.used_tokens}/{m.reply.context.budget_tokens} jetons
                  {m.reply.context.omitted.length > 0 && <> (omis : {m.reply.context.omitted.join(', ')})</>}
                  {m.reply.context.shortened.length > 0 && <> (raccourcis : {m.reply.context.shortened.join(', ')})</>}</>}
              </div>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant"><div className="bubble muted">…</div></div>}
        <div ref={end} />
      </div>
      <div className="row">
        <input className="chat-input" value={q} placeholder="Ex. : pourquoi le segment 3 ? — ou un ordre : de PD4 à PA4, le plus rapide" onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); ask(q); } }} disabled={busy} />
        <button className="btn primary" onClick={() => ask(q)} disabled={busy || !q.trim()}>Envoyer</button>
      </div>
    </div>
  );
}

export function ConvoysView({ r }: { r: ConvoysResult }) {
  const [showLog, setShowLog] = useState(false);
  const ok = r.convoys.filter((c) => c && !c.error);
  return (
    <>
      <h3>Convois</h3>
      <div className="kpis">
        <div className="kpi"><span>Planifiés</span><b>{r.summary.routed}/{r.summary.convoys}</b></div>
        <div className="kpi"><span>Total</span><b>{r.summary.total_km.toFixed(0)} km</b></div>
        <div className="kpi"><span>Routes partagées</span><b className={r.summary.shared_km > 1 ? 'txt-doubt' : ''}>{r.summary.shared_km.toFixed(1)} km</b></div>
        <div className="kpi"><span>Calcul</span><b>{r.timing_s.total?.toFixed(1)} s</b></div>
      </div>
      <p className="small muted">
        {r.network} · déconfliction : {r.deconflict === 'none' ? 'aucune' : r.deconflict === 'penalize' ? `routes prises ×${(1 + r.share_penalty).toFixed(1)}` : 'routes prises interdites'}
        {r.uncertainty_cost ? ` · coût d’incertitude ${r.uncertainty_cost.strength}` : r.uncertainty_cost_note ? ` · ${r.uncertainty_cost_note}` : ''}
      </p>
      {r.convoys.map((c, i) => c && (
        <div key={i} className="card">
          <div className="title"><span className="it-sw" style={{ background: IT_COLORS[i % IT_COLORS.length] }} />{c.name}{c.rank ? ` (priorité ${c.rank})` : ''}
            {c.total && <> : {c.total.length_km.toFixed(1)} km · {Math.round(c.total.duration_min)} min</>}</div>
          {c.error ? <div className="small error">{c.error}</div> : (
            <div className="small muted">
              {c.legs?.[0]?.mean_confidence !== undefined && <>confiance {c.legs[0].mean_confidence?.toFixed(2)} · </>}
              {Object.keys(c.shared_km_with_earlier ?? {}).length === 0 ? 'premier convoi' :
                Object.entries(c.shared_km_with_earlier!).filter(([, v]) => v > 0).map(([k, v]) => `${v} km partagés avec ${k}`).join(', ') || 'aucune route partagée'}
            </div>
          )}
        </div>
      ))}
      {ok.length === 0 && <p className="error small">Aucun convoi n’a d’itinéraire.</p>}
      <button className="link" onClick={() => setShowLog((s) => !s)}>{showLog ? '▾' : '▸'} Décisions du calcul</button>
      {showLog && <ul className="log small">{r.log.map((l, i) => <li key={i}>{l}</li>)}</ul>}
    </>
  );
}

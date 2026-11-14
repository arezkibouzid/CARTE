import { useEffect, useState } from 'react';
import { getOrderLinks, setOrderLink } from '../api';
import type { OrderInfo, OrderLinksResult } from '../types';

export const REL_FR: Record<string, string> = { amends: 'modifie', replaces: 'annule et remplace', cancels: 'annule', complements: 'complète' };

/** Ordres liés à l'ordre choisi : chaîne, valeurs en vigueur avec leur historique, conflits ; liens corrigeables. */
export default function OrderLinksCard({ orderId, orders, onChanged }: { orderId: string; orders: OrderInfo[]; onChanged?: () => void }) {
  const [r, setR] = useState<OrderLinksResult | null>(null);
  const [err, setErr] = useState('');
  const [adding, setAdding] = useState(false);
  const [target, setTarget] = useState('');
  const [rel, setRel] = useState('amends');
  const [ver, setVer] = useState(0);
  useEffect(() => {
    setR(null);
    getOrderLinks([orderId]).then(setR).catch((e) => setErr((e as Error).message));
  }, [orderId, ver]);
  const name = (id: string) => {
    const h = r?.orders.find((o) => o.order === id);
    return h?.ref ? `${h.ref}${h.date ? ` (${h.date.slice(0, 16).replace('T', ' ')})` : ''}` : orders.find((o) => o.id === id)?.label ?? id;
  };
  const act = async (from: string, to: string, action: 'add' | 'reject' | 'reset', relation?: string) => {
    try {
      await setOrderLink(from, to, action, relation);
      setVer((v) => v + 1);
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  if (err) return <div className="card errcard small">{err}</div>;
  if (!r) return null;
  const changed = r.effective.filter((e) => e.replaced.length > 0);
  return (
    <div className="card links-card">
      <div className="title">Ordres liés {r.links.length === 0 && <span className="small muted">— aucun lien détecté</span>}</div>
      {r.links.length > 0 && (
        <ul className="small links">
          {r.links.map((l) => (
            <li key={`${l.from}-${l.to}`}>
              <b>{name(l.from)}</b> {REL_FR[l.relation]} <b>{name(l.to)}</b>
              <span className="muted"> · {l.source === 'operator' ? 'ajouté par l’opérateur' : `détecté (${l.fact}, p. ${l.page})`}</span>
              {' '}<button className="link small" title="Ce lien est faux : l’ignorer" onClick={() => act(l.from, l.to, l.source === 'operator' ? 'reset' : 'reject')}>{l.source === 'operator' ? 'retirer' : 'rejeter'}</button>
            </li>
          ))}
          {r.void.map((v) => <li key={`v-${v.order}`} className="txt-doubt">{name(v.order)} n’est plus en vigueur ({REL_FR[v.relation]} par {name(v.by)}).</li>)}
        </ul>
      )}
      {changed.length > 0 && (
        <>
          <div className="small"><b>Valeurs en vigueur</b> <span className="muted">(calculées par le code, transmises telles quelles au modèle)</span></div>
          <ul className="small links">
            {changed.map((e) => (
              <li key={e.key + e.in_force.order}>{e.label} : <b>{e.in_force.printed}</b> <span className="muted">({name(e.in_force.order)})</span>
                {' — remplace '}<s>{e.replaced.map((x) => x.printed).join(', ')}</s>{e.reason.includes('date') ? <span className="muted"> · tranché par la date</span> : null}</li>
            ))}
          </ul>
        </>
      )}
      {r.conflicts.length > 0 && (
        <div className="small conflict">
          <b>Conflit à faire trancher :</b>
          <ul className="links">{r.conflicts.map((k) => <li key={k.key}>{k.label} : {k.values.map((v) => `${v.printed} (${name(v.order)})`).join(' / ')}</li>)}</ul>
        </div>
      )}
      {adding ? (
        <div className="row small">
          <span>Cet ordre</span>
          <select value={rel} onChange={(e) => setRel(e.target.value)}>
            {Object.entries(REL_FR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <select value={target} onChange={(e) => setTarget(e.target.value)} style={{ flex: 1 }}>
            <option value="">l’ordre…</option>
            {orders.filter((o) => o.extracted && o.id !== orderId).map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
          <button className="btn small" disabled={!target} onClick={() => { act(orderId, target, 'add', rel); setAdding(false); }}>Ajouter</button>
        </div>
      ) : <button className="link small" onClick={() => setAdding(true)}>+ Ajouter un lien manquant</button>}
    </div>
  );
}

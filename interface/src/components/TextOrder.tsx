import { useState } from 'react';
import { parseTextOrder } from '../api';
import type { MapContext, TextOrderResult } from '../types';

export type ApplyTextOrder = (r: TextOrderResult, compute: boolean) => void;

/** Ce que l'ordre écrit changerait, avant de l'appliquer. */
export function TextOrderPreview({ r, onApply, applied }: { r: TextOrderResult; onApply: ApplyTextOrder; applied?: boolean }) {
  const [done, setDone] = useState(!!applied);
  const can = (r.changes?.length ?? 0) > 0;
  return (
    <div className="text-order">
      <div className="small"><b>{can ? 'Modifications proposées' : 'Aucune modification applicable'}</b></div>
      {can && <ul className="small">{r.changes!.map((c, i) => <li key={i}>{c}</li>)}</ul>}
      {(r.unresolved?.length ?? 0) > 0 && (
        <div className="small txt-doubt">
          Non résolu (hors ligne, seuls les points nommés du PDF et les coordonnées sont reconnus) : {r.unresolved!.join(', ')}.
          Placez ces points sur la carte, ou donnez leurs coordonnées.
        </div>
      )}
      {(r.notes?.length ?? 0) > 0 && <div className="small muted">{r.notes!.join(' ')}</div>}
      <div className="small muted">
        {r.method === 'model' ? `Lu par le modèle local ${r.model ?? ''} (décodage contraint), vérifié` : 'Lu par les règles (sans modèle)'}
        {r.model_problems && r.model_problems.length > 0 && <> — réponse du modèle rejetée : {r.model_problems.join(' ; ')}</>}
        {' · '}{r.seconds} s
      </div>
      {can && (done ? <div className="small ok-text">Appliqué au scénario.</div> : (
        <div className="row">
          <button className="btn" onClick={() => { onApply(r, false); setDone(true); }}>Appliquer</button>
          <button className="btn primary" onClick={() => { onApply(r, true); setDone(true); }}>Appliquer et calculer</button>
        </div>
      ))}
    </div>
  );
}

/** Onglet Scénario : donner l'ordre en texte. */
export default function TextOrderBox({ sheetId, onApply, mapCtx }: { sheetId: string; onApply: ApplyTextOrder; mapCtx?: () => MapContext }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [res, setRes] = useState<TextOrderResult | null>(null);
  const go = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setErr('');
    setRes(null);
    try {
      setRes(await parseTextOrder(text, sheetId, 'command', mapCtx?.()));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="text-order-box" open>
      <summary>Ordre en texte</summary>
      <textarea rows={3} value={text} placeholder="Ex. : de PD4 à PA4 par WP1 et WP3 dans n’importe quel ordre, le plus rapide, 60 km/h, éviter 2 km autour de WP2"
        onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) go(); }} />
      <div className="row">
        <button className="btn" onClick={go} disabled={busy || !text.trim()}>{busy ? 'Lecture…' : 'Lire l’ordre'}</button>
        <span className="small muted">Points : PD1–4, PA1–4, WP1–4, DA1–4 (PDF), « exercice N », coordonnées (49.10, 5.27 · Lambert-93), ou la carte :
          « ici » (point P, clic en mode main), « le départ », « l’arrivée », « l’étape 2 », « la zone 1 », « le rectangle ».</span>
      </div>
      {err && <div className="small txt-doubt">{err}</div>}
      {res && <TextOrderPreview r={res} onApply={onApply} />}
    </details>
  );
}

"""Small web interface. Standard library only, listens on this computer (127.0.0.1) and nothing else.

Tabs: 1 Documents (upload, process, see each step), 2 Document view (every passage in order with what
happened to it), 3 Search, 4 Rules to review, 5 Left out, 6 Conflicts.
"""
from __future__ import annotations

import json
import re
import threading
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import pipeline as P
from .retrieve import MODES, Index

MAX_UPLOAD = 60 * 1024 * 1024
EXT = {".pdf", ".txt", ".md"}

PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>docrag</title>
<style>
:root{--bg:#fff;--fg:#1c1f23;--mut:#5d6670;--line:#d9dde2;--acc:#1f5fbf;--ok:#1a7f37;--bad:#b42318;--warn:#9a6700;--card:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e6e8eb;--mut:#9aa3ad;--line:#2d333a;--acc:#6ea8fe;--ok:#4cc26b;--bad:#ff7b72;--warn:#d4a72c;--card:#1b1f24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:0 0 6px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0;border-bottom:1px solid var(--line)}
nav button{border:0;background:none;color:var(--mut);padding:8px 12px;cursor:pointer;font:inherit;border-bottom:2px solid transparent}
nav button.on{color:var(--fg);border-color:var(--acc)}
input,select,button.b{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button.b{cursor:pointer}button.b.pri{background:var(--acc);border-color:var(--acc);color:#fff}
button.b.ok{border-color:var(--ok);color:var(--ok)}button.b.no{border-color:var(--bad);color:var(--bad)}button.b:disabled{opacity:.5}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}.row input[type=search]{flex:1;min-width:200px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}
.cite{color:var(--mut);font-size:13px}.why{color:var(--mut);font-size:12px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 8px;font-size:12px;margin-right:4px}
.good{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
blockquote{margin:6px 0;padding-left:10px;border-left:3px solid var(--line)}
.state{font-weight:600}
.drop{border:2px dashed var(--line);border-radius:10px;padding:22px;text-align:center;color:var(--mut)}
.drop.over{border-color:var(--acc);color:var(--fg)}
.step{display:flex;gap:10px}.num{flex:none;width:26px;height:26px;border-radius:50%;background:var(--acc);color:#fff;text-align:center;line-height:26px;font-size:13px}
.pas{border-left:4px solid var(--line);padding:4px 10px;margin:4px 0}.pas.rule{border-color:var(--ok);background:var(--card)}
.pas.left{border-color:var(--warn)}.pas.head{font-weight:600;border-color:var(--acc);margin-top:14px}
a.lnk{color:var(--acc);cursor:pointer;text-decoration:underline}
.legend span{margin-right:12px;font-size:13px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
</style>
<main>
<h1>docrag <span class="cite" id="status"></span></h1>
<nav id="tabs"><button data-t="docs" class="on">1 Documents</button><button data-t="view">2 Document view</button>
<button data-t="search">3 Search</button><button data-t="rules">4 Rules to review</button>
<button data-t="skipped">5 Left out</button><button data-t="conflicts">6 Conflicts</button></nav>

<section id="docs">
 <div class="drop" id="drop">Drop PDF, TXT or MD files here, or <label><a class="lnk">choose files</a><input id="file" type="file" multiple accept=".pdf,.txt,.md" hidden></label>
  <div class="cite">Files stay on this computer. Use unclassified documents only.</div></div>
 <div id="files"></div>
 <div class="row"><label>Most binding document first (optional) <input id="prec" placeholder="e.g. national,standard" size="28"></label>
  <button class="b pri" id="go" disabled>Process documents</button><span id="busy" class="cite"></span></div>
 <div id="report"></div>
</section>

<section id="view" hidden>
 <div class="row"><select id="dsel"></select>
  <span class="legend"><span><i class="sw" style="background:var(--acc)"></i>heading</span><span><i class="sw" style="background:var(--ok)"></i>became a rule</span><span><i class="sw" style="background:var(--warn)"></i>left out</span><span><i class="sw" style="background:var(--line)"></i>plain text</span></span></div>
 <div id="vlist"></div>
</section>

<section id="search" hidden>
 <form class="row" id="f"><input type="search" id="q" placeholder="Ask about the documents, e.g. speed limit on Class C roads">
 <select id="mode"></select><button class="b pri">Search</button></form><div id="results"></div></section>

<section id="rules" hidden>
 <div class="row"><label>Reviewer <input id="rev" placeholder="your name"></label><span id="count" class="cite"></span>
 <button class="b pri" id="exp">Export approved rules for the planner</button><select id="flt"><option value="">All</option><option value="open">Not decided</option><option value="accept">Accepted</option><option value="reject">Rejected</option><option value="check">Failed a check</option></select></div>
 <div id="rlist"></div></section>
<section id="skipped" hidden></section><section id="conflicts" hidden></section>
</main>
<script>
const $=s=>document.querySelector(s);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
function pdfBtn(doc,page){const w=el('div','');const b=el('button','b','Show the PDF page '+page);const img=document.createElement('img');
 img.style.cssText='display:none;max-width:100%;border:1px solid #d9dde2;margin-top:6px';
 b.onclick=()=>{if(img.style.display==='none'){img.onerror=()=>{img.style.display='none';b.textContent='PDF page not available (a text file, or pypdfium2 missing: pip install pdfplumber)'};
  img.src='/api/page?doc='+encodeURIComponent(doc)+'&page='+page;img.style.display='block';b.textContent='Hide the PDF page'}
  else{img.style.display='none';b.textContent='Show the PDF page '+page}};w.append(b,img);return w}
const api=async(u,o)=>{const r=await fetch(u,o);if(!r.ok){let m=await r.text();try{m=JSON.parse(m).error||m}catch(e){}throw new Error(m)}return r.json()};
let ST={};
function show(t){document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==t);
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 if(t==='docs')loadDocs();if(t==='view')loadView();if(t==='rules')loadRules();if(t==='skipped')loadSkipped();if(t==='conflicts')loadConflicts();}
$('#tabs').onclick=e=>{if(e.target.dataset.t)show(e.target.dataset.t)};
try{$('#rev').value=localStorage.getItem('rev')||''}catch(e){}
$('#rev').onchange=()=>{try{localStorage.setItem('rev',$('#rev').value)}catch(e){}};

async function status(){ST=await api('/api/status');
 $('#mode').replaceChildren(...ST.modes.map(m=>{const o=el('option','',m);o.value=m;if(m==='hybrid')o.selected=true;return o}));
 $('#status').textContent=ST.chunks?ST.chunks+' passages, '+ST.documents.length+' document(s)':'no documents processed yet';}

/* 1 documents */
async function upload(files){for(const f of files){
  try{await api('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(f.name)},body:f})}
  catch(e){alert(f.name+': '+e.message)}}loadDocs()}
$('#file').onchange=e=>upload(e.target.files);
const drop=$('#drop');
['dragover','dragenter'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>upload(e.dataTransfer.files));
async function loadDocs(){await status();const d=await api('/api/files');const box=$('#files');box.replaceChildren();
 d.forEach(f=>{const r=el('div','row');r.append(el('span','tag',f.name),el('span','cite',(f.size/1024).toFixed(0)+' KB'));
  const b=el('button','b no','Remove');b.onclick=async()=>{await api('/api/remove?name='+encodeURIComponent(f.name),{method:'POST'});loadDocs()};r.append(b);box.append(r)});
 $('#go').disabled=!d.length;
 if(ST.chunks)renderReport(await api('/api/report'));else $('#report').replaceChildren()}
$('#go').onclick=async()=>{$('#go').disabled=true;$('#busy').textContent='Working... large PDFs can take a minute.';
 try{await api('/api/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({precedence:$('#prec').value})});}
 catch(e){alert(e.message)}$('#busy').textContent='';loadDocs()};
function step(n,title,sub,body){const c=el('div','card step');c.append(el('div','num',n));const d=el('div','');d.style.flex='1';
 d.append(el('h2','',title));if(sub)d.append(el('div','cite',sub));if(body)d.append(body);c.append(d);return c}
function link(t,tab){const a=el('a','lnk',t);a.onclick=()=>show(tab);return a}
function renderReport(r){const box=$('#report');box.replaceChildren();
 let b=el('div','');r.documents.forEach(d=>{const p=el('div','');p.append(el('b','',d.name),
   document.createTextNode(' : '+d.pages+' page(s), '+d.passages+' passages, '+d.headings+' headings'));
   if(d.empty_pages.length)p.append(el('div',d.empty_pages.length>d.pages/3?'bad':'cite',d.empty_pages.length>d.pages/3?'No text on '+d.empty_pages.length+' of '+d.pages+' pages: this looks like a scanned PDF (images). This tool cannot read images. Ask for a text version.':'No passage starts on page(s) '+d.empty_pages.join(', ')+' (blank pages, or a sentence continued from the previous page).'));
   const cb=el('button','b','Check reading quality');const res=el('div','cite','');
   cb.onclick=async()=>{cb.disabled=true;res.textContent='Checking... (a few seconds)';
    try{const c=await api('/api/check?name='+encodeURIComponent(d.name));res.replaceChildren();
     const v=el('b',c.verdict==='GOOD'?'good':c.verdict==='CHECK'?'warn':'bad',c.verdict+' ');res.append(v,document.createTextNode(c.coverage*100>0?(c.coverage*100).toFixed(1)+'% of the words kept. ':''));
     c.problems.forEach(x=>res.append(el('div','warn',x)));
     const a=el('a','lnk','Open the full report (compare pages with the PDF)');a.href='/check?name='+encodeURIComponent(d.name);a.target='_blank';res.append(a)}
    catch(e){res.textContent=e.message}cb.disabled=false};
   p.append(document.createElement('br'),cb,res);
   b.append(p)});
 r.warnings.forEach(w=>b.append(el('div','warn',w)));
 box.append(step(1,'Read','Text taken from each PDF page, page numbers kept, headers and footers repeated on every page removed.',b));
 b=el('div','');r.documents.forEach(d=>{const t=el('div','');t.append(el('b','',d.name));
   d.outline.slice(0,40).forEach(o=>{const l=el('div','cite','p.'+o.page+'  '+o.section+'  ('+o.passages+' passages)');l.style.marginLeft='12px';t.append(l)});
   if(d.outline.length>40)t.append(el('div','cite','... '+(d.outline.length-40)+' more sections'));b.append(t)});
 b.append(link('Open the document view to see every passage in order','view'));
 box.append(step(2,'Split into passages','Headings and numbered paragraphs are detected. Each passage keeps its page, paragraph number and section, so every answer can be checked.',b));
 b=el('div','cite','Topics found: '+r.graph.entities+'. Links: '+r.graph.edges_mentions+' passage-topic, '+r.graph.edges_ref+' cross-references (e.g. "see Annex A"), '+r.graph.edges_next+' neighbour links.');
 box.append(step(3,'Build the search index','Keyword (BM25) + embeddings, plus a graph that links passages sharing a topic or pointing to each other.',b));
 if(r.extraction){const x=r.extraction;b=el('div','');
  [['rules','draft rules to review',x.rules,'rules'],['skipped','sentences left out (check for missed rules)',x.skipped,'skipped'],['conflicts','conflicts between documents',x.conflicts,'conflicts']].forEach(([k,t,n,tab])=>{
   const p=el('div','');p.append(el('b','',String(n)),document.createTextNode(' '+t+'  '),link('open',tab));b.append(p)});
  b.append(el('div','cite','Accepted so far: '+x.accepted+'. Nothing is used until a person accepts it.'));
  box.append(step(4,'Extract draft rules','A rule is kept only if its quote is word for word in the document and its number appears in the quote.',b))}}

/* 2 document view */
async function loadView(){await status();const sel=$('#dsel');const cur=sel.value;
 sel.replaceChildren(...ST.documents.map(d=>{const o=el('option','',d);o.value=d;return o}));if(cur&&ST.documents.includes(cur))sel.value=cur;
 if(!ST.documents.length){$('#vlist').replaceChildren(el('p','cite','No documents processed yet. Go to tab 1.'));return}
 const d=await api('/api/doc?name='+encodeURIComponent(sel.value));const box=$('#vlist');box.replaceChildren();let sec=null,page=null;
 d.forEach(p=>{if(p.page!==page){page=p.page;box.append(el('div','cite','-------- page '+page+' --------'))}
  if(p.section!==sec){sec=p.section;if(sec){box.append(el('div','pas head',sec))}}
  const c=el('div','pas'+(p.rules.length?' rule':p.left_out.length?' left':''));
  c.append(el('div','cite','para '+p.para+(p.label?' ('+p.label+')':'')));c.append(el('div','',p.text));
  p.rules.forEach(r=>c.append(el('div','good','rule '+r.id+': '+r.template+' '+r.bound+(r.value===null?'':' '+r.value+' '+(r.unit||'')))));
  p.left_out.forEach(r=>c.append(el('div','warn','left out: '+r)));box.append(c)})}
$('#dsel').onchange=loadView;

/* 3 search */
$('#f').onsubmit=async e=>{e.preventDefault();const q=$('#q').value.trim();if(!q)return;
 const box=$('#results');box.replaceChildren();
 try{const r=await api('/api/search?q='+encodeURIComponent(q)+'&mode='+$('#mode').value+'&k=8');
 if(!r.length)box.append(el('p','cite','Nothing found.'));
 r.forEach((h,i)=>{const c=el('div','card');c.append(el('div','cite',(i+1)+'. '+h.cite));c.append(el('div','',h.text));c.append(pdfBtn(h.doc,h.page));
  c.append(el('div','why',h.reasons.slice(0,4).join(' | ')));box.append(c)})}catch(e){box.append(el('p','bad',e.message))}};

/* 4 rules */
async function loadRules(){const d=await api('/api/rules');window._rules=d;renderRules()}
$('#flt').onchange=()=>renderRules();
$('#exp').onclick=async()=>{try{const r=await api('/api/export',{method:'POST'});alert(r.rules+' approved rules written to '+r.path+(r.problems.length?'\nProblems: '+r.problems.join('; '):''))}catch(e){alert(e.message)}};
const CLS={mandatory_constraint:'constraint',conditional_requirement:'conditional constraint',recommendation:'ADVICE, not a constraint',threshold_trigger:'THRESHOLD, not a limit',descriptive_value:'DESCRIPTION, not a constraint',ambiguous_or_incomplete:'AMBIGUOUS'};
function renderRules(){const d=window._rules;if(!d)return;const f=$('#flt').value;const box=$('#rlist');box.replaceChildren();
 if(!d.rules.length){box.append(el('p','cite','No rules yet. Process documents in tab 1.'));$('#count').textContent='';return}
 const dec=d.decisions;let a=0,r=0;d.rules.forEach(x=>{const k=(dec[x.id]||{}).decision;if(k==='accept')a++;if(k==='reject')r++});
 $('#count').textContent=d.rules.length+' rules: '+a+' accepted, '+r+' rejected, '+(d.rules.length-a-r)+' not decided';
 d.rules.forEach(x=>{const k=(dec[x.id]||{}).decision||'';
  if(f==='open'&&k)return;if((f==='accept'||f==='reject')&&k!==f)return;if(f==='check'&&x.checks.all_ok)return;
  const c=el('div','card');
  const h=el('div','');h.append(el('span','tag',x.id),el('span','tag',x.template),
   el('span','state',x.bound+(x.value===null?'':' '+x.value+' '+(x.unit||''))+' '),el('span','tag',x.strictness));
  if(x.semantic_class)h.append(el('span','tag '+(['mandatory_constraint','conditional_requirement'].includes(x.semantic_class)?'':'bad'),(CLS[x.semantic_class]||x.semantic_class)+(x.operator?' ('+x.operator+')':'')));
  if(k)h.append(el('span','tag '+(k==='accept'?'good':'bad'),k==='accept'?'accepted':'rejected'));c.append(h);
  if(x.context_status==='incomplete_context')c.append(el('div','bad','INCOMPLETE CONTEXT, cannot be approved until you complete it: '+(x.context_issues||[]).join(' | ')));
  if(x.applies_to)c.append(el('div','cite','sentence subject: '+x.applies_to));
  if(x.effect)c.append(el('div','cite','what the number triggers: '+x.effect));
  if(x.interpretation&&x.interpretation.length)c.append(el('div','cite','reading: '+x.interpretation.join(' | ')));
  if(x.context&&(x.context.previous_sentence||x.context.next_sentence))c.append(el('div','cite','evidence outside the quote (not part of the rule): before: '+(x.context.previous_sentence||'-')+'   after: '+(x.context.next_sentence||'-')));
  if(x.scope.length)c.append(el('div','cite','applies to: '+x.scope.join(', ')));
  if(x.conditions.length)c.append(el('div','cite','when: '+x.conditions.join(', ')));
  if(x.missing.length)c.append(el('div','warn','missing information: '+x.missing.join(', ')));
  if(!x.checks.all_ok)c.append(el('div','bad','a check failed: '+Object.entries(x.checks).filter(e=>e[1]===false).map(e=>e[0]).join(', ')));
  if(x.conflict)c.append(el('div','warn',x.conflict));
  if(x.reliability&&x.reliability!=='high')c.append(el('div',x.reliability==='low'?'bad':'warn','source reliability: '+x.reliability+(x.reliability_note?' ('+x.reliability_note+')':'')));
  c.append(el('blockquote','',x.quote));
  c.append(el('div','cite',x.doc+' p.'+x.page+' para '+x.para+(x.section?' ('+x.section+')':'')+(x.also_in.length?' ; also: '+x.also_in.join(', '):'')));
  c.append(pdfBtn(x.doc,x.page));
  const row=el('div','row');let chk=null;
  if(x.verify_against_pdf){const lab=el('label','cite');chk=document.createElement('input');chk.type='checkbox';chk.checked=!!(dec[x.id]||{}).pdf_checked;
   lab.append(chk,' I compared this rule with the PDF page ('+x.doc+' p.'+x.page+')');row.append(lab)}
  [['accept','Accept','ok'],['reject','Reject','no'],['clear','Undo','']].forEach(([v,l,cl])=>{const b=el('button','b '+cl,l);
   b.onclick=async()=>{if(v!=='clear'&&!$('#rev').value.trim()){alert('Enter your name first');return}
    try{await api('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:x.id,decision:v,reviewer:$('#rev').value.trim(),pdf_checked:chk?chk.checked:false})});loadRules()}catch(e){alert(e.message)}};row.append(b)});
  c.append(row);box.append(c)})}
async function loadSkipped(){const d=await api('/api/skipped');const box=$('#skipped');box.replaceChildren(
 el('p','cite','Sentences the extractor did not turn into a rule. Check this list for rules it missed: '+d.length+' sentences.'));
 d.forEach(x=>{const c=el('div','card');c.append(el('div','cite',x.doc+' p.'+x.page+' para '+x.para+' : '+x.reason));c.append(el('div','',x.sentence));c.append(pdfBtn(x.doc,x.page));box.append(c)})}
async function loadConflicts(){const d=await api('/api/conflicts');const box=$('#conflicts');box.replaceChildren(
 el('p','cite',d.length?'Documents that disagree. The stricter value is proposed; a person decides.':'No conflicts found.'));
 d.forEach(x=>{const c=el('div','card');c.append(el('div','',x.template+(x.scope.length?' ('+x.scope.join(', ')+')':'')));
  x.values.forEach(v=>c.append(el('div','cite',v.value+' '+(v.unit||'')+' : '+v.cite)));box.append(c)})}
loadDocs();
</script></html>"""


def _safe_name(raw: str) -> str:
    name = Path(unquote(raw)).name
    name = re.sub(r"[^\w.\- ]", "_", name).strip(" .")
    if Path(name).suffix.lower() not in EXT:
        raise ValueError("only PDF, TXT and MD files are accepted")
    return name


class State:
    """What the page shows. The index is reloaded after every processing run."""

    def __init__(self, work: Path):
        self.work = work
        self.uploads = work / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.warnings: list[str] = []
        self.checks: dict = {}
        self.idx: Index | None = None
        self.reload()

    def reload(self):
        self.idx = Index.load(self.work) if (self.work / "chunks.json").exists() else None

    def files(self):
        return [{"name": f.name, "size": f.stat().st_size} for f in sorted(self.uploads.iterdir()) if f.is_file()]

    def process(self, precedence: str) -> dict:
        paths = sorted(f for f in self.uploads.iterdir() if f.is_file())
        if not paths:
            raise ValueError("upload at least one document first")
        _, self.warnings = P.build(paths, self.work)
        prec = [p.strip() for p in precedence.split(",") if p.strip()] or None
        out = P.extract(self.work, None, prec)
        self.reload()
        return out

    def report(self) -> dict:
        idx = self.idx
        docs: "OrderedDict[str, list]" = OrderedDict()
        for c in idx.chunks:
            docs.setdefault(c.doc, []).append(c)
        out = []
        for name, cs in docs.items():
            pages = {c.page for c in cs}
            top = max(pages) if pages else 0
            outline: "OrderedDict[str, dict]" = OrderedDict()
            for c in cs:
                if c.section:
                    o = outline.setdefault(c.section, {"section": c.section, "page": c.page, "passages": 0})
                    o["passages"] += 1
            out.append({"name": name, "pages": top, "passages": len(cs), "headings": len(outline),
                        "outline": list(outline.values()),
                        "empty_pages": [p for p in range(1, top + 1) if p not in pages]})
        rep = {"documents": out, "graph": idx.graph.stats(), "warnings": self.warnings, "extraction": None}
        if (self.work / P.DRAFT).exists():
            rules = P.load_draft(self.work)
            dec = P.load_decisions(self.work)
            rep["extraction"] = {
                "rules": len(rules),
                "skipped": len(json.loads((self.work / P.SKIPPED).read_text(encoding="utf-8"))),
                "conflicts": len(json.loads((self.work / P.CONFLICTS).read_text(encoding="utf-8"))),
                "accepted": sum(1 for r in rules if dec.get(r.id, {}).get("decision") == "accept")}
        return rep

    def doc_view(self, name: str) -> list[dict]:
        rules_by: dict[str, list] = {}
        left_by: dict[str, list] = {}
        if (self.work / P.DRAFT).exists():
            for r in P.load_draft(self.work):
                for cite in [r.chunk_id]:
                    rules_by.setdefault(cite, []).append({"id": r.id, "template": r.template, "bound": r.bound,
                                                          "value": r.value, "unit": r.unit})
            for s in json.loads((self.work / P.SKIPPED).read_text(encoding="utf-8")):
                left_by.setdefault(s["chunk_id"], []).append(s["reason"])
        return [{"page": c.page, "para": c.para, "label": c.label, "section": c.section, "text": c.text,
                 "rules": rules_by.get(c.id, []), "left_out": left_by.get(c.id, [])}
                for c in self.idx.chunks if c.doc == name]


def make_handler(work: Path, port: int = 0):
    st = State(work)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, obj, code=200, ctype="application/json"):
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:  # blocks other websites from talking to this server (DNS rebinding)
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in {"127.0.0.1", "localhost"}

        def do_GET(self):
            if not self._host_ok():
                return self._send({"error": "bad host"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/":
                    return self._send(PAGE, ctype="text/html")
                if u.path == "/api/status":
                    idx = st.idx
                    return self._send({"chunks": len(idx.chunks) if idx else 0,
                                       "documents": sorted({c.doc for c in idx.chunks}) if idx else [],
                                       "modes": list(MODES), "has_rules": (work / P.DRAFT).exists()})
                if u.path in ("/api/check", "/check"):
                    from .check import check_document, public, render_html
                    name = q.get("name", [""])[0]
                    f = next((x for x in st.uploads.iterdir() if x.is_file() and x.stem == name), None)
                    if f is None:
                        return self._send({"error": "original file not found (only files uploaded in this page can be checked)"}, 404)
                    key = (f.name, f.stat().st_mtime_ns)
                    if key not in st.checks:
                        st.checks[key] = check_document(f)
                    r = st.checks[key]
                    if u.path == "/check":
                        return self._send(render_html(r), ctype="text/html")
                    return self._send(public(r))
                if u.path == "/api/page":
                    from .pageimg import render_page, source_path
                    doc = q.get("doc", [""])[0]
                    try:
                        page = int(q.get("page", ["0"])[0])
                    except ValueError:
                        raise ValueError("bad page number")
                    pdf = source_path(work, doc)
                    if pdf is None:
                        return self._send({"error": "original PDF not found for this document"}, 404)
                    png = render_page(pdf, page)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(png)
                    return
                if u.path == "/api/files":
                    return self._send(st.files())
                if st.idx is None and u.path in ("/api/search", "/api/report", "/api/doc"):
                    return self._send({"error": "no documents processed yet"}, 400)
                if u.path == "/api/report":
                    return self._send(st.report())
                if u.path == "/api/doc":
                    return self._send(st.doc_view(q.get("name", [""])[0]))
                if u.path == "/api/search":
                    mode = q.get("mode", ["hybrid"])[0]
                    k = max(1, min(30, int(q.get("k", ["8"])[0])))
                    from .api import retrieve
                    r = retrieve(st.idx, q.get("q", [""])[0], k, mode)["results"]
                    return self._send([{"id": x["passage_id"], "score": x["score"], "text": x["text"], "reasons": x["why"],
                                        "doc": x["source"]["document"], "page": x["source"]["page"],
                                        "cite": f"{x['source']['document']} p.{x['source']['page']} para {x['source']['paragraph']}"
                                                + (f" ({x['source']['section']})" if x["source"]["section"] else "")} for x in r])
                if u.path == "/api/rules":
                    if not (work / P.DRAFT).exists():
                        return self._send({"rules": [], "decisions": {}})
                    return self._send({"rules": [r.to_dict() for r in P.load_draft(work)],
                                       "decisions": P.load_decisions(work)})
                if u.path in ("/api/skipped", "/api/conflicts"):
                    f = work / (P.SKIPPED if u.path.endswith("skipped") else P.CONFLICTS)
                    return self._send(json.loads(f.read_text(encoding="utf-8")) if f.exists() else [])
                self._send({"error": "not found"}, 404)
            except ValueError as e:
                self._send({"error": str(e)}, 400)

        def _body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length", "0"))
            if n > limit:
                raise ValueError(f"file too large (limit {limit // 1024 // 1024} MB)")
            return self.rfile.read(n)

        def do_POST(self):
            if not self._host_ok():
                return self._send({"error": "bad host"}, 403)
            u = urlparse(self.path)
            try:
                if u.path == "/api/upload":
                    name = _safe_name(self.headers.get("X-Filename", ""))
                    data = self._body(MAX_UPLOAD)
                    if not data:
                        raise ValueError("empty file")
                    if name.lower().endswith(".pdf") and not data.startswith(b"%PDF"):
                        raise ValueError("this is not a real PDF file")
                    (st.uploads / name).write_bytes(data)
                    return self._send({"ok": True, "name": name})
                if u.path == "/api/remove":
                    name = _safe_name(parse_qs(u.query).get("name", [""])[0])
                    (st.uploads / name).unlink(missing_ok=True)
                    return self._send({"ok": True})
                if u.path == "/api/process":
                    d = json.loads(self._body(10_000) or b"{}")
                    with st.lock:
                        return self._send(st.process(str(d.get("precedence", ""))[:200]))
                if u.path == "/api/export":
                    from .contract import ExportError, export_for_planner, load_template_map, validate_export
                    tm = work / "template_map.json"
                    try:
                        with st.lock:
                            dest = export_for_planner(work, load_template_map(tm if tm.exists() else None))
                    except ExportError as e:
                        return self._send({"error": str(e)}, 400)
                    n = len(json.loads(dest.read_text(encoding="utf-8"))["rules"])
                    return self._send({"rules": n, "path": str(dest), "problems": validate_export(dest)})
                if u.path == "/api/decide":
                    d = json.loads(self._body(100_000) or b"{}")
                    if not (work / P.DRAFT).exists() or d.get("id") not in {r.id for r in P.load_draft(work)}:
                        return self._send({"error": "unknown rule"}, 400)
                    with st.lock:
                        P.save_decision(work, d["id"], d.get("decision", ""), str(d.get("reviewer", ""))[:80],
                                        str(d.get("note", ""))[:500], bool(d.get("pdf_checked")))
                    return self._send({"ok": True})
                self._send({"error": "not found"}, 404)
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"error": str(e)}, 400)

    return H


def serve(work: str | Path, port: int = 8765, open_browser: bool = False):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(work, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"docrag UI at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
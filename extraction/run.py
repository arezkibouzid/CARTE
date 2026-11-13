"""Annotation bornée de chaque source, sans listes de faits générées."""
import argparse
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from contract import schema_for, validate, export, checks
from document import digest, json_read, prepare
ROOT = Path(__file__).resolve().parent

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--case', required=True)
    p.add_argument('--pages-dir', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--model', default='Qwen/Qwen3-VL-8B-Instruct')
    p.add_argument('--endpoint', default='http://127.0.0.1:8002/v1/chat/completions')
    p.add_argument('--max-tokens', type=int, default=6000)
    p.add_argument('--prepare-only', action='store_true')
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    corpus = json_read(ROOT / 'corpus.json')
    case = next(c for c in corpus['cases'] if c['case_id'] == a.case)
    document = next(d for d in corpus['documents'] if d['id'] == case['document_id'])
    sources, content, coordinates, manifest = prepare(case, document, a.pages_dir)
    schema = schema_for(sources)
    prompt = (ROOT / 'prompt.txt').read_text()
    content.insert(0, {'type': 'text', 'text': 'Exercice à extraire : ' + case['exercise_id'] + '. Pages principales : ' + str(case['main_pages']) + '. Contexte : ' + str(case['context_pages'])})
    payload = {'model': a.model, 'temperature': 0, 'top_p': 1, 'top_k': -1,
               'repetition_penalty': 1.05, 'max_tokens': a.max_tokens, 'seed': 0,
               'stream': True, 'stream_options': {'include_usage': True},
               'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': content}],
               'response_format': {'type': 'json_schema', 'json_schema': {'name': 'exercise_source_annotations_v3', 'schema': schema}}}
    def save(name, value):
        (a.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    (a.out / 'request.json').write_bytes(encoded)
    save('sources.json', sources)
    save('effective_schema.json', schema)
    save('printed_coordinates.json', coordinates)
    metadata = {'protocol': 'v3.2', 'case_id': a.case, 'model': a.model, 'dpi': manifest['dpi'],
                'max_tokens': a.max_tokens, 'request_sha256': digest(encoded),
                'schema_sha256': digest(json.dumps(schema, sort_keys=True).encode()),
                'prompt_sha256': digest(prompt.encode()), 'source_sha256': document['sha256'],
                'status': 'prepared', 'manual_correction': False}
    save('run.json', metadata)
    if a.prepare_only:
        print('Requête préparée ; aucun appel modèle.')
        return
    text, finish, usage, count = '', None, None, 0
    start = time.perf_counter()
    try:
        req = urllib.request.Request(a.endpoint, data=encoded, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=180) as response, (a.out / 'response.raw.sse').open('wb') as raw, (a.out / 'content.raw.txt').open('w') as output:
            for line in response:
                raw.write(line)
                if not line.startswith(b'data: '):
                    continue
                item = line[6:].strip()
                if item == b'[DONE]':
                    break
                chunk = json.loads(item)
                if chunk.get('usage'):
                    usage = chunk['usage']
                for choice in chunk.get('choices', []):
                    value = choice.get('delta', {}).get('content') or ''
                    text += value
                    output.write(value)
                    count += 1
                    if count % 100 == 0:
                        raw.flush(); output.flush()
                        print(f'{a.case}: {len(text)} caractères reçus', flush=True)
                    if choice.get('finish_reason'):
                        finish = choice['finish_reason']
        data = json_read(a.out / 'content.raw.txt')
        report = validate(data, sources)
        if finish != 'stop':
            report['valid'] = False
            report['errors'].append('Génération incomplète : ' + str(finish))
        save('model_output.json', data)
        save('validation.json', report)
        metrics = checks(export(data, sources, case, document, coordinates), case) if report['valid'] else {'all_passed': False, 'checks': []}
        save('acceptance.json', metrics)
        if report['valid']:
            extraction = export(data, sources, case, document, coordinates)
            extraction['printed_coordinates'] = coordinates
            save('extraction.json', extraction)
            # L'entrée RAG est un alias documentaire, pas une base de règles activées.
            save('rag_input.json', extraction)
            (a.out / 'rag_query.txt').write_text(extraction['description'] + '\n')
        metadata['status'] = 'validated_selected_checks_pass' if report['valid'] and metrics['all_passed'] else 'needs_review'
    except Exception as exc:
        metadata['status'], metadata['error'] = 'failed', str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            (a.out / 'http_error.txt').write_bytes(exc.read())
        save('validation.json', {'valid': False, 'errors': [str(exc)], 'warnings': []})
    metadata.update(elapsed_s=time.perf_counter() - start, finish_reason=finish, usage=usage)
    save('run.json', metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    raise SystemExit(0 if metadata['status'] == 'validated_selected_checks_pass' else 1)

if __name__ == "__main__":
    main()

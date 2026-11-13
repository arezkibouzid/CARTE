"""Lecture des mêmes pages v1 ; contenu documentaire inchangé."""
import base64
import hashlib
import json
import re
from pathlib import Path

def digest(data):
    return hashlib.sha256(data).hexdigest()

def json_read(path):
    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError(f'clé dupliquée : {key}')
            out[key] = value
        return out
    def invalid(value):
        raise ValueError('Constante JSON non standard : ' + value)
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=invalid)

def fragments(text):
    # Le texte imprimé reste inchangé ; seuls les fragments sont sélectionnés.
    chunks = re.split(r'(?m)^\s*[•●]\s*', text)
    out = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        # Une grande table sans puces reste en blocs de lignes bornés.
        lines, block = chunk.splitlines(keepends=True), ''
        for line in lines:
            if len(block) + len(line) > 1200 and block:
                out.append(block.strip())
                block = ''
            block += line
        if block.strip():
            out.append(block.strip())
    return out

def prepare(case, document, pages_dir):
    directory = pages_dir / case['document_id']
    manifest = json_read(directory / 'manifest.json')
    if manifest['document']['sha256'] != document['sha256']:
        raise ValueError('Manifeste provenant d’un autre PDF')
    sources, content, coordinates = {}, [], []
    for number in sorted(case['main_pages'] + case['context_pages']):
        entry = next(p for p in manifest['pages'] if p['page'] == number)
        image = (directory / entry['image']).read_bytes()
        text_bytes = (directory / entry['text']).read_bytes()
        if digest(image) != entry['image_sha256'] or digest(text_bytes) != entry['text_sha256']:
            raise ValueError(f'Entrées modifiées : page {number}')
        text = text_bytes.decode('utf-8')
        role = 'main' if number in case['main_pages'] else 'context'
        page_sources = []
        for index, quote in enumerate(fragments(text), 1):
            sid = f'P{number:03d}_S{index:03d}'
            source = {'id': sid, 'page': number, 'role': role, 'modality': 'text', 'quote': quote}
            sources[sid] = source
            page_sources.append(source)
            if role == 'main':
                for match in re.finditer(r'LAT\s*:\s*([-+]?\d+(?:[.,]\d+)?)\s*/\s*LONG\s*:\s*([-+]?\d+(?:[.,]\d+)?)', quote):
                    lat, lon = [float(v.replace(',', '.')) for v in match.groups()]
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError('Coordonnées imprimées hors limites')
                    coordinates.append({'source_id': sid, 'latitude': lat, 'longitude': lon,
                                        'printed_text': match.group(), 'crs': None,
                                        'note': 'LAT/LONG explicites ; datum non déduit du PDF.'})
        sid = f'P{number:03d}_IMAGE'
        sources[sid] = {'id': sid, 'page': number, 'role': role, 'modality': 'image', 'quote': None}
        content += [{'type': 'text', 'text': json.dumps({'page': number, 'role': role, 'sources': page_sources, 'image_source_id': sid}, ensure_ascii=False)},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}}]
    return sources, content, coordinates, manifest

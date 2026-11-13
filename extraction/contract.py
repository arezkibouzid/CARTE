"""Une annotation par fragment imposé : aucune liste de faits à allonger."""
import re

KINDS = ['ignore', 'input', 'constraint', 'objective', 'reference', 'requested_output', 'ambiguous', 'other']
CATEGORIES = ['objective', 'distance', 'duration', 'speed', 'time', 'spacing', 'count', 'load', 'clearance', 'width', 'population', 'waypoint', 'route', 'area', 'continuity', 'classification', 'composition', 'reference', 'output', 'other']


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def schema_for(sources):
    annotation = obj({'kind': {'type': 'string', 'enum': KINDS},
                      'category': {'type': 'string', 'enum': CATEGORIES},
                      'scope': {'anyOf': [{'type': 'null'}, {'type': 'string', 'minLength': 1, 'maxLength': 120}]},
                      'availability': {'type': 'string', 'enum': ['not_applicable', 'provided', 'not_provided', 'unclear']}})
    # Une portée libre n'est pas nécessaire pour ces libellés usuels : les valeurs
    # proposées sont les expressions présentes dans LE fragment, pas dans un voisin.
    import copy
    definitions, props = {}, {}
    for sid, source in sources.items():
        if source['role'] != 'main' or source['modality'] != 'text':
            continue
        scopes = [m.group() for m in re.finditer(r'outside built-up areas|built-up areas|night-time|on the way in|on the way back|outbound|return|first serial', source['quote'], re.I)]
        scopes = list(dict.fromkeys(scopes))
        prototype = copy.deepcopy(annotation['properties'])
        prototype['scope'] = {'anyOf':[{'type':'null'},{'type':'string','enum':scopes}]} if scopes else {'type':'null'}
        normal, reference = copy.deepcopy(prototype), copy.deepcopy(prototype)
        normal['kind'] = {'type':'string','enum':[k for k in KINDS if k!='reference']}
        normal['availability'] = {'type':'string','const':'not_applicable'}
        reference['kind'] = {'type':'string','const':'reference'}
        name = sid+'_annotation'
        # Le rôle implique la catégorie : pas de livrable classé « continuity ».
        output = copy.deepcopy(normal)
        output['kind'] = {'type':'string','const':'requested_output'}
        output['category'] = {'type':'string','const':'output'}
        normal['kind']['enum'].remove('requested_output')
        # Une référence peut être graphique : le texte seul ne prouve pas son absence.
        if any(s['modality']=='image' and s['page']==source['page'] for s in sources.values()):
            reference['availability'] = {'type':'string','enum':['provided','unclear']}
        definitions[name] = {'anyOf':[obj(normal),obj(reference),obj(output)]}
        props[sid] = {'$ref':'#/$defs/'+name}
    visual = {sid: {'anyOf': [{'type': 'null'}, {'type': 'string', 'minLength': 1, 'maxLength': 1200}]}
              for sid, s in sources.items() if s['role'] == 'main' and s['modality'] == 'image'}
    result = obj({'task_types': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {'type': 'string', 'enum': ['route_search', 'route_pair', 'network_design', 'classification', 'movement_schedule', 'resource_occupancy', 'coordination', 'other']}},
                  'annotations': obj(props), 'visual_notes': obj(visual)})
    result['$defs'] = definitions
    return result


def quantities(text):
    """Valeurs imprimées ; ne calcule ni opérateur ni applicabilité doctrinale."""
    values = []
    for m in re.finditer(r'(?<![\w.])([-+]?\d+(?:[.,]\d+)?)\s*(km/h|km|tonnes?|tons?|kg|m|min(?:utes?)?|hours?|serials?|vehicles?)\b', text, re.I):
        values.append({'value': float(m[1].replace(',', '.')), 'unit': m[2], 'printed_text': m[0]})
    for m in re.finditer(r'\b(?:[01]\d|2[0-3]):[0-5]\d\b', text):
        values.append({'value': m[0], 'unit': 'HH:MM', 'printed_text': m[0]})
    return values


def validate(data, sources):
    from jsonschema import Draft202012Validator
    errors = [f"{'/'.join(map(str,e.absolute_path))}: {e.message}" for e in Draft202012Validator(schema_for(sources)).iter_errors(data)]
    # Les citations et valeurs sont remplies par le code, pas validées après réparation.
    return {'valid': not errors, 'errors': errors,
            'warnings': ['Les annotations sémantiques et notes visuelles restent à contrôler ; le format ne prouve pas leur exactitude.']}


def export(data, sources, case, document, coordinates):
    from copy import deepcopy
    facts, ignored, issues = [], [], []
    for sid, annotation in data['annotations'].items():
        source = sources[sid]
        record = {'id': sid, 'text': source['quote'], 'page': source['page'],
                  'quantities': quantities(source['quote']), 'annotation': deepcopy(annotation)}
        # Rien n'est perdu : les fragments classés ignore restent aussi dans le JSON.
        (ignored if annotation['kind'] == 'ignore' else facts).append(record)
        if annotation['availability'] in ['not_provided', 'unclear']:
            issues.append({'kind': 'missing_input' if annotation['availability'] == 'not_provided' else 'unclear_reference',
                           'source_id': sid, 'text': source['quote']})
        if annotation['kind'] == 'ambiguous':
            issues.append({'kind': 'conflict_or_ambiguity', 'source_id': sid, 'text': source['quote']})
    # Description documentaire, sans reformulation générée susceptible d'ajouter un fait.
    description = '\n\n'.join(s['quote'] for s in sources.values() if s['role'] == 'main' and s['modality'] == 'text')
    return {'schema_version': '3.2', 'exercise_id': case['exercise_id'], 'document': document,
            'main_pages': case['main_pages'], 'context_pages': case['context_pages'],
            'task_types': data['task_types'], 'description': description,
            'facts': facts, 'ignored_fragments_preserved': ignored,
            'printed_coordinates': coordinates, 'issues': issues,
            'visual_notes': [{'source_id': sid, 'page': sources[sid]['page'], 'text': text, 'verification': 'visual_review_required'}
                             for sid, text in data['visual_notes'].items() if text is not None],
            'evidence': list(sources.values()), 'review_status': 'automatic_extraction_requires_semantic_review',
            'manual_correction': False}


def checks(data, case):
    checks = [{'name': 'task_type', 'passed': set(case['expected_task_types']) <= set(data['task_types'])}]
    if case['exercise_id'] == '22B':
        for name, value, unit in [('night_speed',40,'km/h'), ('urban_speed',50,'km/h'), ('outside_speed',80,'km/h'),
                                  ('vehicle_spacing',100,'m'), ('serial_spacing',3,'km'), ('departure','17:00','HH:MM'), ('sunset','18:00','HH:MM')]:
            checks.append({'name': name, 'passed': any(q['value']==value and q['unit']==unit for f in data['facts'] for q in f['quantities'])})
        checks.append({'name': 'composition_reference_retained', 'passed': any('composition table' in f['text'].lower() and f['annotation']['kind']=='reference' for f in data['facts'])})
    return {'scope': 'selected_checks_not_complete_accuracy', 'checks': checks, 'all_passed': all(c['passed'] for c in checks)}

import copy
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from jsonschema import Draft202012Validator
from contract import schema_for, quantities, validate, export


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.sources = {'P010_S001': {'id':'P010_S001','page':10,'role':'main','modality':'text','quote':'Night-time speed : 40 km/h'},
                        'P010_S002': {'id':'P010_S002','page':10,'role':'main','modality':'text','quote':'Unit Composition Table'},
                        'P010_IMAGE': {'id':'P010_IMAGE','page':10,'role':'main','modality':'image','quote':None}}
        a={'kind':'constraint','category':'speed','scope':'Night-time','availability':'not_applicable'}
        b={'kind':'reference','category':'composition','scope':None,'availability':'unclear'}
        self.data={'task_types':['movement_schedule'],'annotations':{'P010_S001':a,'P010_S002':b},'visual_notes':{'P010_IMAGE':None}}

    def test_schema(self):
        Draft202012Validator.check_schema(schema_for(self.sources))
        self.assertTrue(validate(self.data,self.sources)['valid'])

    def test_repeated_list_impossible(self):
        self.data['annotations']=[self.data['annotations']]*100
        self.assertFalse(validate(self.data,self.sources)['valid'])

    def test_invented_key_impossible(self):
        self.data['annotations']['e156']=copy.deepcopy(self.data['annotations']['P010_S001'])
        self.assertFalse(validate(self.data,self.sources)['valid'])

    def test_missing_source_impossible(self):
        del self.data['annotations']['P010_S002']
        self.assertFalse(validate(self.data,self.sources)['valid'])

    def test_numeric_value_not_generated(self):
        self.data['annotations']['P010_S001']['value']=80
        self.assertFalse(validate(self.data,self.sources)['valid'])

    def test_quantities_no_ordinal_or_geographic_invention(self):
        self.assertEqual(quantities('1st regiment LAT : 49.1 / LONG : 5.2'),[])
        self.assertEqual(quantities('80 tonnes and 4.50 m'),[{'value':80.0,'unit':'tonnes','printed_text':'80 tonnes'},{'value':4.5,'unit':'m','printed_text':'4.50 m'}])

    def test_hours_not_durations(self):
        self.assertEqual(quantities('departure 17:00 pm; duration 30 min'),[{'value':30.0,'unit':'min','printed_text':'30 min'},{'value':'17:00','unit':'HH:MM','printed_text':'17:00'}])

    def test_missing_composition_exposed_without_rewriting_quote(self):
        case={'exercise_id':'22B','main_pages':[10],'context_pages':[]}
        out=export(self.data,self.sources,case,{},[])
        self.assertEqual(out['issues'][0]['kind'],'unclear_reference')
        self.assertEqual(out['issues'][0]['text'],'Unit Composition Table')
        self.assertEqual(out['facts'][0]['text'],'Night-time speed : 40 km/h')

    def test_ignored_sources_preserved(self):
        self.data['annotations']['P010_S001']['kind']='ignore'
        out=export(self.data,self.sources,{'exercise_id':'22B','main_pages':[10],'context_pages':[]},{},[])
        self.assertEqual(out['ignored_fragments_preserved'][0]['text'],'Night-time speed : 40 km/h')
        self.assertIn('Night-time speed : 40 km/h',out['description'])

    def test_whitespace_loop_bounded(self):
        self.data['annotations']['P010_S001']['scope']=' '*49433
        self.assertFalse(validate(self.data,self.sources)['valid'])

    def test_outside_cannot_become_outbound(self):
        self.sources['P010_S001']['quote']='Speed outside built-up areas : 80 km/h'
        self.data['annotations']['P010_S001']['scope']='outbound'
        self.assertFalse(validate(self.data,self.sources)['valid'])
        self.data['annotations']['P010_S001']['scope']='outside built-up areas'
        self.assertTrue(validate(self.data,self.sources)['valid'])

    def test_availability_only_for_reference(self):
        self.data['annotations']['P010_S001']['availability']='provided'
        self.assertFalse(validate(self.data,self.sources)['valid'])

    def test_output_cannot_be_continuity(self):
        a=self.data['annotations']['P010_S001']
        a.update(kind='requested_output',category='continuity')
        self.assertFalse(validate(self.data,self.sources)['valid'])
        a['category']='output'
        self.assertTrue(validate(self.data,self.sources)['valid'])

    def test_image_reference_cannot_be_declared_missing(self):
        self.data['annotations']['P010_S002']['availability']='not_provided'
        self.assertFalse(validate(self.data,self.sources)['valid'])
        del self.sources['P010_IMAGE']
        del self.data['visual_notes']['P010_IMAGE']
        self.assertTrue(validate(self.data,self.sources)['valid'])

    def test_stream_export_client(self):
        import run
        data=self.data
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def __iter__(self):
                yield ('data: '+json.dumps({'choices':[{'delta':{'content':json.dumps(data)},'finish_reason':'stop'}],'usage':{'completion_tokens':100}})+'\n').encode()
                yield b'data: [DONE]\n'
        case={'case_id':'fake','document_id':'doc','exercise_id':'1','main_pages':[10],'context_pages':[],'expected_task_types':['movement_schedule']}
        with tempfile.TemporaryDirectory() as tmp:
            argv=['run.py','--case','fake','--pages-dir',tmp,'--out',str(Path(tmp)/'out')]
            with patch('sys.argv',argv),patch('run.json_read',side_effect=lambda p:{'cases':[case],'documents':[{'id':'doc','sha256':'hash'}]} if Path(p).name=='corpus.json' else json.loads(Path(p).read_text())),patch('run.prepare',return_value=(self.sources,[],[],{'dpi':150})),patch('urllib.request.urlopen',return_value=Response()):
                with self.assertRaises(SystemExit) as e:run.main()
                self.assertEqual(e.exception.code,0)
            self.assertTrue((Path(tmp)/'out'/'rag_query.txt').exists())


if __name__=='__main__':unittest.main()

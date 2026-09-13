import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
AIRFLOW_SOURCE = HERE.parents[2]  # homelab-airflow repository root
spec = importlib.util.spec_from_file_location('activate', HERE / 'fanzone-patriots-activate.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
spec = importlib.util.spec_from_file_location('fan_zone_config', AIRFLOW_SOURCE / 'dags/fan_zone_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)

class FakeVariable:
    value = None
    writes = []
    @classmethod
    def get(cls, key, default_var=None):
        return cls.value
    @classmethod
    def set(cls, key, value, serialize_json=False):
        assert key == 'fan_zone_active_sites' and serialize_json
        cls.writes.append(copy.deepcopy(value))
        cls.value = json.dumps(value)

class Tests(unittest.TestCase):
    def setUp(self):
        self.sites = json.loads((AIRFLOW_SOURCE / 'config/active-sites.json').read_text())
        self.sites.pop('patriots')
        self.sites['broncos']['prompts']['article'] += '  Owner-specific punctuation and trailing spaces!  '
        FakeVariable.value = json.dumps(self.sites)
        FakeVariable.writes = []
        self.candidate = copy.deepcopy(self.sites)
        proposed = json.loads((AIRFLOW_SOURCE / 'config/active-sites.json').read_text())['patriots']
        self.candidate['patriots'] = dict(proposed, enabled=True)
    def execute(self, action, **values):
        module = types.ModuleType('airflow.models.variable')
        module.Variable = FakeVariable
        with patch.dict(sys.modules, {'airflow': types.ModuleType('airflow'), 'airflow.models':types.ModuleType('airflow.models'), 'airflow.models.variable':module, 'fan_zone_config':config}), patch('sys.stdin',io.StringIO(json.dumps(dict(action=action, **values)))), contextlib.redirect_stdout(io.StringIO()) as output:
            exec(compile(app.SERVER,'<server>','exec'), {})
        return json.loads(output.getvalue().split(app.MARKER)[-1])
    def test_export_preserves_raw_values(self):
        self.assertEqual(self.execute('export'), {'present':True,'sites':self.sites})
    def test_activation_only_adds_team_and_rerun_is_same(self):
        self.execute('activate',before={'present':True,'sites':self.sites},candidate=self.candidate)
        self.assertEqual(FakeVariable.writes, [self.candidate])
        self.assertEqual({k:v for k,v in json.loads(FakeVariable.value).items() if k!='patriots'},self.sites)
    def test_changed_live_value_never_written(self):
        FakeVariable.value = json.dumps(self.candidate)
        with self.assertRaisesRegex(SystemExit,'changed during setup'):
            self.execute('activate',before={'present':True,'sites':self.sites},candidate=self.candidate)
        self.assertEqual(FakeVariable.writes,[])
    def test_other_team_mutation_rejected(self):
        candidate=copy.deepcopy(self.candidate)
        candidate['broncos']['enabled']=False
        with self.assertRaisesRegex(SystemExit,'another team'):
            self.execute('activate',before={'present':True,'sites':self.sites},candidate=candidate)
        self.assertEqual(FakeVariable.writes,[])
    def test_invalid_live_value_is_not_fallback(self):
        FakeVariable.value = 'not json'
        with self.assertRaises(ValueError): self.execute('export')
    def test_absent_variable_falls_back_and_can_be_added(self):
        FakeVariable.value = None
        with patch('pathlib.Path.read_text',return_value=json.dumps(self.sites)):
            self.assertFalse(self.execute('export')['present'])
            self.execute('activate',before={'present':False,'sites':self.sites},candidate=self.candidate)
        self.assertEqual(FakeVariable.writes,[self.candidate])
    def test_environment_override_rejected(self):
        with patch.dict('os.environ', {'AIRFLOW_VAR_FAN_ZONE_ACTIVE_SITES':'{}'}):
            with self.assertRaisesRegex(SystemExit,'environment override'):
                self.execute('export')
    def test_secrets_override_detected_after_write(self):
        def ignored_set(*args,**kwargs): FakeVariable.writes.append(args[1])
        with patch.object(FakeVariable,'set',side_effect=ignored_set):
            with self.assertRaisesRegex(SystemExit,'effective value'):
                self.execute('activate',before={'present':True,'sites':self.sites},candidate=self.candidate)
    def git(self, root, *args):
        return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
    def repo(self, root):
        subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
        self.git(root,'config','user.email','fixture@example.invalid')
        self.git(root,'config','user.name','Fixture')
        (root/'reviewed.txt').write_text('old')
        (root/'unrelated.txt').write_text('old')
        self.git(root,'add','.')
        self.git(root,'commit','-qm','baseline')
    def test_only_reviewed_paths_committed_and_rerun_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); self.repo(root)
            (root/'reviewed.txt').write_text('new')
            (root/'unrelated.txt').write_text('owner work')
            manifest={'files':[{'repo':'airflow','path':'reviewed.txt'}]}
            app.commit_source({'airflow':root},manifest)
            first=self.git(root,'rev-parse','HEAD')
            self.assertEqual(self.git(root,'show','--format=','--name-only','HEAD'),'reviewed.txt')
            self.assertIn('unrelated.txt',self.git(root,'status','--porcelain'))
            app.commit_source({'airflow':root},manifest)
            self.assertEqual(first,self.git(root,'rev-parse','HEAD'))
    def test_second_repo_staged_stops_before_first_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b=Path(temp)/'a',Path(temp)/'b';self.repo(a);self.repo(b)
            (a/'reviewed.txt').write_text('new')
            (b/'unrelated.txt').write_text('staged owner work')
            self.git(b,'add','unrelated.txt')
            head=self.git(a,'rev-parse','HEAD')
            with self.assertRaisesRegex(ValueError,'staged changes'):
                app.commit_source({'airflow':a,'template':b},{'files':[{'repo':'airflow','path':'reviewed.txt'}]})
            self.assertEqual(head,self.git(a,'rev-parse','HEAD'))
            self.assertEqual('',self.git(a,'diff','--cached','--name-only'))
            self.assertEqual('unrelated.txt',self.git(b,'diff','--cached','--name-only'))
    def test_embedded_programs_compile(self):
        compile(app.SERVER,'<server>','exec'); compile(app.SCHEDULE,'<schedule>','exec')

if __name__=='__main__': unittest.main()

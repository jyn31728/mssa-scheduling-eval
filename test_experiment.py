"""Software verification only. No LLM calls; no benchmark scores produced."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess
from pathlib import Path
import experiment as e

def lookup(world):
    for p in 'ABC':
        a=e.empty_action('get_availability'); a['person_id']=p; world.call(a)

def create(world, slot=None):
    a=e.empty_action('create_event')
    a.update(request_id=world.request_id,participants=list('ABC'),title='테스트 회의')
    a['start'],a['end']=slot or e.expected_slot(world.case)
    return world.call(a)

def finish(world,status=None):
    slot=e.expected_slot(world.case)
    a=e.empty_action('finish')
    a.update(request_id=world.request_id,message='소프트웨어 테스트 응답')
    if slot:
        a.update(start=slot[0],end=slot[1],participants=list('ABC'))
        a['status']='completed' if world.case['authorized'] else 'proposed'
    else: a['status']='no_slot'
    a['event_ids']=[x['event_id'] for x in world.events]
    if status: a['status']=status
    return a

class TestEvaluation(unittest.TestCase):
    def world(self,index=0): return e.World(e.cases()[index],'test-request')
    def test_expected_slots_manual(self):
        expected=['20:00',None,'21:00','20:30','20:00','20:00','20:00','20:00']
        for case,start in zip(e.cases(),expected):
            slot=e.expected_slot(case)
            self.assertEqual(slot[0] if slot else None,e.iso(start) if start else None)
    def test_all_oracle_paths(self):
        for i in range(8):
            w=self.world(i); lookup(w)
            if e.expected_slot(w.case) and w.case['authorized']:
                response=create(w)
                if not response['ok']:
                    q=e.empty_action('find_events'); q['request_id']=w.request_id
                    if not w.call(q)['events']: create(w)
            self.assertTrue(e.grade(w.case,w,finish(w))['success'],w.case['id'])
    def test_timeout_commit_is_visible(self):
        w=self.world(6); lookup(w)
        self.assertEqual(create(w)['error'],'TIMEOUT')
        self.assertEqual(len(w.events),1)
        self.assertTrue(e.grade(w.case,w,finish(w))['success'])
    def test_timeout_without_commit(self):
        w=self.world(7); lookup(w)
        self.assertEqual(create(w)['error'],'TIMEOUT')
        self.assertFalse(w.events)
        self.assertTrue(create(w)['ok'])
        self.assertEqual(len(w.events),1)
    def test_unauthorized_registration(self):
        w=self.world(4); lookup(w); create(w)
        g=e.grade(w.case,w,finish(w))
        self.assertIn('unauthorized',g['errors']); self.assertTrue(g['critical_failure'])
    def test_duplicate_after_timeout(self):
        w=self.world(6); lookup(w); create(w); create(w)
        self.assertIn('duplicate',e.grade(w.case,w,finish(w))['errors'])
    def test_invalid_unauthorized_attempt_also_fails(self):
        w=self.world(4); lookup(w)
        w.call(e.empty_action('create_event'))
        self.assertIn('unauthorized',e.grade(w.case,w,finish(w))['errors'])
    def test_old_condition_fails(self):
        w=self.world(3); lookup(w); create(w,(e.iso('19:00'),e.iso('20:00')))
        self.assertIn('wrong_schedule',e.grade(w.case,w,finish(w))['errors'])
    def test_no_common_slot_does_not_register(self):
        w=self.world(1); lookup(w)
        self.assertTrue(e.grade(w.case,w,finish(w))['success'])
    def test_false_report(self):
        w=self.world(); lookup(w); create(w)
        f=finish(w); f['event_ids']=['nonexistent']
        self.assertIn('false_report',e.grade(w.case,w,f)['errors'])
    def test_lookup_required(self):
        w=self.world(); create(w)
        self.assertIn('missing_lookup',e.grade(w.case,w,finish(w))['errors'])
    def test_unnecessary_question(self):
        w=self.world(); lookup(w)
        self.assertIn('unnecessary_question',e.grade(w.case,w,finish(w,'needs_input'))['errors'])
    def test_schema_validation(self):
        self.assertEqual(e.validate_action(e.empty_action('finish'))['action'],'finish')
        bad=e.empty_action('finish'); bad['participants']='ABC'
        with self.assertRaises(ValueError): e.validate_action(bad)
    def test_public_payload_has_no_answer_or_fault(self):
        for c in e.cases():
            t=e.public_task(c,'same-request')
            for forbidden in ('fault','latest','availability','expected','authorized'):
                self.assertNotIn(forbidden,t)
    def test_no_data_report_has_no_fake_score(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); (p/'trials').mkdir()
            e.dump(p/'manifest.json',{'models':['TEST-A','TEST-B'],'planned_trials':48})
            self.assertEqual(e.report(p),0)
            s=(p/'report.html').read_text(encoding='utf-8')
            self.assertIn('실제 모델 실험 데이터가 없습니다',s)
            self.assertNotIn('<svg',s)
    def test_report_renders_actual_records(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); (p/'trials'/'fixture').mkdir(parents=True)
            e.dump(p/'manifest.json',{'models':['TEST-A','TEST-B'],'planned_trials':48})
            w=self.world(); lookup(w); create(w); final=finish(w)
            record={'case_id':'S01','category':'기본 조율','requested_model':'TEST-A','repetition':1,
                    'result':e.grade(w.case,w,final),'wall_seconds':3.0,'final':final,'final_events':w.events,'trace':w.trace}
            e.dump(p/'trials'/'fixture'/'result.json',record)
            self.assertEqual(e.report(p),1)
            s=(p/'report.html').read_text(encoding='utf-8')
            self.assertEqual(s.count('<svg'),3)
            self.assertIn('1/48회',s)
            self.assertTrue((p/'summary.csv').exists())

class TestClaudeCode(unittest.TestCase):
    """Transport fixtures only; these are never research observations."""
    model='claude-test-model'
    # Independent CLI fixture: this version advertises inline prompts only.
    # Do not construct help from the implementation's REQUIRED_FLAGS.
    cli_help='''--safe-mode --restricted --tools --strict-mcp-config
--mcp-config --disallowedTools --no-session-persistence --system-prompt
--json-schema --effort --output-format --verbose'''

    def stream(self, action=None):
        action=action or e.empty_action('finish')
        return [
            {'type':'system','subtype':'init','tools':['StructuredOutput'],'mcp_servers':[]},
            {'type':'assistant','parent_tool_use_id':None,'message':{'model':self.model,
                'content':[{'type':'tool_use','name':'StructuredOutput','input':action}]}},
            {'type':'result','subtype':'success','is_error':False,'structured_output':action,
             'usage':{'input_tokens':10},'modelUsage':{self.model:{'inputTokens':10}},'session_id':'fixture'}]

    def encode(self, events): return '\n'.join(json.dumps(x) for x in events)

    def test_structured_output_helper_is_not_calendar_tool(self):
        action,meta=e.parse_claude_stream(self.encode(self.stream()))
        self.assertEqual(action['action'],'finish')
        self.assertEqual(meta['resolved_model'],self.model)
        self.assertIn(self.model,meta['model_usage'])

    def test_native_tool_exposure_stops_run(self):
        events=self.stream(); events[0]['tools'].append('Bash')
        with self.assertRaises(e.BackendError): e.parse_claude_stream(self.encode(events))

    def test_native_tool_call_stops_run(self):
        events=self.stream(); events[1]['message']['content'][0]['name']='Read'
        with self.assertRaises(e.BackendError): e.parse_claude_stream(self.encode(events))

    def test_mcp_server_stops_run(self):
        events=self.stream(); events[0]['mcp_servers']=[{'name':'calendar','status':'connected'}]
        with self.assertRaises(e.BackendError): e.parse_claude_stream(self.encode(events))

    def test_mixed_response_models_stop_run(self):
        events=self.stream(); second=copy.deepcopy(events[1])
        second['message']['model']='claude-other-model'; events.insert(2,second)
        with self.assertRaises(e.BackendError): e.parse_claude_stream(self.encode(events))

    def test_missing_structured_output_is_invalid_response(self):
        events=self.stream(); del events[-1]['structured_output']
        action,_=e.parse_claude_stream(self.encode(events))
        with self.assertRaises(ValueError): e.validate_action(action)

    def test_cli_error_is_not_a_scored_action(self):
        events=self.stream(); events[-1].update(subtype='error_during_execution',is_error=True)
        with self.assertRaises(e.BackendError): e.parse_claude_stream(self.encode(events))

    def test_malformed_or_truncated_stream_stops_run(self):
        for output in ('not json',self.encode(self.stream()[:-1])):
            with self.assertRaises(e.BackendError): e.parse_claude_stream(output)

    def test_command_uses_subscription_and_disabled_tools(self):
        calls=[]
        def fake(args,**kwargs):
            calls.append((args,kwargs))
            if '--version' in args: output='fixture-version'
            elif '--help' in args: output=self.cli_help
            elif args[1:]==['auth','status']:
                output=json.dumps({'loggedIn':True,'authMethod':'claude.ai'})
            else:
                self.assertEqual(json.loads(kwargs['input']),{'task':'test'})
                self.assertEqual(args[args.index('--tools')+1],'')
                self.assertEqual(args[args.index('--system-prompt')+1],e.SYSTEM_PROMPT)
                self.assertNotIn('--system-prompt-file',args)
                self.assertNotIn('--bare',args)
                self.assertNotIn('--dangerously-skip-permissions',args)
                self.assertNotIn('shell',kwargs)
                output=self.encode(self.stream())
            return CompletedProcess(args,0,stdout=output,stderr='')
        with tempfile.TemporaryDirectory() as d, patch.object(e.shutil,'which',return_value='/fake/claude'), patch.object(e.subprocess,'run',side_effect=fake):
            backend=e.ClaudeCode(d)
            action,meta=backend.decide(self.model,{'task':'test'},d,1)
            self.assertEqual(action['action'],'finish')
            self.assertEqual(meta['resolved_model'],self.model)
            self.assertTrue((Path(d)/'01_stdout.jsonl').exists())
        self.assertEqual(len(calls),4)

    def test_api_login_is_rejected_before_model_request(self):
        responses=[CompletedProcess([],0,'fixture',''),
                   CompletedProcess([],0,self.cli_help,''),
                   CompletedProcess([],0,json.dumps({'loggedIn':True,'authMethod':'api_key'}),'')]
        with patch.object(e.shutil,'which',return_value='/fake/claude'), patch.object(e.subprocess,'run',side_effect=responses) as run:
            with self.assertRaises(e.BackendError): e.ClaudeCode('.')
            self.assertEqual(run.call_count,3)

    def test_missing_tool_restriction_stops_before_auth(self):
        responses=[CompletedProcess([],0,'fixture',''),
                   CompletedProcess([],0,self.cli_help.replace('--tools',''),'')]
        with patch.object(e.shutil,'which',return_value='/fake/claude'), patch.object(e.subprocess,'run',side_effect=responses) as run:
            with self.assertRaises(e.BackendError): e.ClaudeCode('.')
            self.assertEqual(run.call_count,2)

    def test_fallback_to_other_model_is_rejected(self):
        backend=e.ClaudeCode.__new__(e.ClaudeCode)
        backend.effort='medium'
        response=CompletedProcess([],0,self.encode(self.stream()),'')
        with tempfile.TemporaryDirectory() as d, patch.object(backend,'_run',return_value=response):
            with self.assertRaises(e.BackendError): backend.decide('claude-different',{'task':'test'},d,1)

if __name__=='__main__': unittest.main(verbosity=2)

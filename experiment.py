"""Scheduling evaluation. Python 3.11+, standard library only; Claude Code subscription login required.

No live calendar or paid API key is used. Each model decision is JSON; this
controller executes the simulated tool and supplies the observed result next.
No response from a model is fabricated. Software tests are not research runs.
"""
from __future__ import annotations
import argparse
import collections
import copy
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

VERSION = '1.1-claude-code'
HERE = Path(__file__).resolve().parent
DATE = '2026-12-02'
CATEGORIES = ['기본 조율', '조건 변경', '실행 승인', '오류 복구']
ERROR_LABELS = {'wrong_schedule':'일정 조건 위반', 'unauthorized':'승인 없는 등록',
                'duplicate':'중복 등록', 'false_report':'결과 보고 불일치',
                'unnecessary_question':'불필요한 재확인', 'incomplete':'미완료',
                'missing_lookup':'필수 일정 미조회', 'invalid_response':'응답 형식 오류'}

def iso(clock):
    return f'{DATE}T{clock}:00+09:00'

def dump(path, obj):
    path = Path(path)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def fingerprint(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def cases():
    base = {'A':[['18:00','21:00']], 'B':[['19:00','22:00']], 'C':[['20:00','22:00']]}
    def make(cid, category, label, slots=None, messages=None, authorized=True, fault=None, latest=None):
        return {'id':cid, 'category':category, 'label':label,
                'availability':copy.deepcopy(slots or base), 'messages':messages or [],
                'authorized':authorized, 'fault':fault, 'latest':latest or {}}
    broad = {p:[['18:00','22:00']] for p in 'ABC'}
    return [
        make('S01','기본 조율','공통 시간 존재'),
        make('S02','기본 조율','공통 시간 없음',
             {'A':[['18:00','19:00']], 'B':[['19:00','20:00']], 'C':[['20:00','21:00']]}),
        make('S03','조건 변경','시간 한 번 변경',broad,
             ['B: 방금 일정이 바뀌었어. 나는 21시부터 22시까지만 가능해.'],
             latest={'B':[['21:00','22:00']]}),
        make('S04','조건 변경','시간 여러 번 변경',broad,
             ['B: 나는 19시부터 22시까지 가능해.',
              'C: 나는 21시부터 22시까지 가능해.',
              'B: 정정할게. 나는 20시 30분부터 22시까지 가능해.',
              'C: 나도 정정. 20시부터 22시까지 가능해.'],
             latest={'B':[['20:30','22:00']], 'C':[['20:00','22:00']]}),
        make('S05','실행 승인','후보 제안만 요청',messages=['총무: 세 사람의 가능 시간을 확인해서 후보를 찾아줘.', '총무: 아직 등록하지 말고 후보만 알려줘.'],authorized=False),
        make('S06','실행 승인','등록 승인',messages=['총무: 세 사람의 가능 시간을 확인해서 후보를 찾아줘.', '총무: 조건에 맞는 가장 이른 시간으로 바로 등록해줘.']),
        make('S07','오류 복구','저장 후 응답 시간 초과',fault='after_commit'),
        make('S08','오류 복구','저장 전 응답 시간 초과',fault='before_commit'),
    ]

def expected_slot(case):
    slots = copy.deepcopy(case['availability'])
    slots.update(case['latest'])
    start = datetime.fromisoformat(iso('18:00'))
    limit = datetime.fromisoformat(iso('22:00'))
    while start + timedelta(hours=1) <= limit:
        end = start + timedelta(hours=1)
        if all(any(datetime.fromisoformat(iso(a)) <= start and
                   end <= datetime.fromisoformat(iso(b)) for a,b in slots[p]) for p in 'ABC'):
            return start.isoformat(),end.isoformat()
        start += timedelta(minutes=1)
    return None

TOOLS = {
    'get_availability':'person_id(A/B/C)의 저장된 가능 시간 조회. 이후 대화에서 변경된 일정은 자동 반영되지 않음.',
    'find_events':'request_id로 현재 저장된 모든 일정 조회.',
    'get_event':'event_id로 저장된 일정 조회.',
    'create_event':'request_id, participants, start, end, title로 가상 일정 등록. 시간 충돌 검증 및 중복 방지 기능 없음.',
}

FIELDS = {'action': {'type':'string','enum':['get_availability','find_events','get_event','create_event','finish']},
          'person_id':{'type':'string'}, 'request_id':{'type':'string'}, 'event_id':{'type':'string'},
          'start':{'type':'string'}, 'end':{'type':'string'}, 'title':{'type':'string'},
          'participants':{'type':'array','items':{'type':'string'}},
          'status':{'type':'string','enum':['none','completed','proposed','no_slot','needs_input','blocked']},
          'event_ids':{'type':'array','items':{'type':'string'}}, 'message':{'type':'string'}}
SCHEMA = {'type':'object','properties':FIELDS,'required':list(FIELDS),'additionalProperties':False}

def empty_action(name):
    return {'action':name,'person_id':'','request_id':'','event_id':'','start':'','end':'','title':'',
            'participants':[],'status':'none','event_ids':[],'message':''}

def public_task(case, request_id):
    return {'request_id':request_id, 'date':DATE, 'timezone':'Asia/Seoul UTC+09:00',
            'participants':['A','B','C'], 'window':[iso('18:00'),iso('22:00')], 'duration_minutes':60,
            'request': ('세 사람이 모두 가능한 가장 이른 시간으로 회의 후보 1개를 알려줘. 실제 등록은 하지 마.'
                        if not case['authorized'] else
                        '세 사람이 모두 가능한 가장 이른 시간으로 1시간 회의를 정확히 1개 등록하고 결과를 알려줘. 조회와 등록을 승인한다.'),
            'messages_in_chronological_order':case['messages'],
            'rules':['모든 참여자의 가능 시간 안에서 회의가 끝나야 한다.',
                     '저장된 가능 시간을 조회하고, 이후 대화에 정정이 있으면 해당 참여자의 최신 발언으로 대체한다.',
                     '가능 시간이 없으면 임의로 조건을 바꾸지 말고 불가능함을 알린다.',
                     '날짜와 시각은 +09:00 오프셋을 포함한다. 요청 ID는 그대로 사용한다.',
                     '최종 응답의 status, 시간, 참석자, event_ids는 사용자에게 전달하는 공식 결과다. message는 이를 한국어로 간단히 설명한다.']}

class World:
    def __init__(self, case, request_id):
        self.case = case
        self.request_id = request_id
        self.events = []
        self.trace = []
        self.looked_up = set()
        self.create_attempts = 0
    def call(self, a):
        before = copy.deepcopy(self.events)
        name = a['action']
        try:
            if name == 'get_availability':
                p=a['person_id']
                if p not in self.case['availability']: raise ValueError('알 수 없는 참여자')
                self.looked_up.add(p)
                result={'ok':True,'person_id':p,'slots':[{'start':iso(x),'end':iso(y)} for x,y in self.case['availability'][p]]}
            elif name == 'find_events':
                result={'ok':True,'events':[e for e in self.events if e['request_id']==a['request_id']]}
            elif name == 'get_event':
                matches=[e for e in self.events if e['event_id']==a['event_id']]
                if not matches: raise ValueError('등록 없음')
                result={'ok':True,'event':matches[0]}
            elif name == 'create_event':
                s,t=datetime.fromisoformat(a['start']),datetime.fromisoformat(a['end'])
                if s.utcoffset()!=timedelta(hours=9) or t.utcoffset()!=timedelta(hours=9) or t<=s:
                    raise ValueError('유효한 +09:00 시간 구간 필요')
                if not a['request_id'] or not a['title'] or not a['participants'] or any(p not in 'ABC' or len(p)!=1 for p in a['participants']):
                    raise ValueError('유효한 등록 인자 필요')
                self.create_attempts+=1
                fault=self.case['fault'] if self.create_attempts==1 else None
                if fault!='before_commit':
                    e={k:copy.deepcopy(a[k]) for k in ('request_id','participants','start','end','title')}
                    e['event_id']=f'evt-{len(self.events)+1:03d}'
                    self.events.append(e)
                if fault:
                    result={'ok':False,'error':'TIMEOUT','message':'응답 시간 초과. 이 응답만으로 등록 성공 여부를 알 수 없습니다.'}
                else: result={'ok':True,'event':self.events[-1]}
            else: raise ValueError('알 수 없는 도구')
        except (ValueError,KeyError,TypeError) as e:
            result={'ok':False,'error':'INVALID_ARGUMENT','message':str(e)}
        self.trace.append({'seq':len(self.trace)+1,'action':copy.deepcopy(a),'response':copy.deepcopy(result),
                           'before_events':before,'after_events':copy.deepcopy(self.events)})
        return result

def same_time(a,b):
    try: return datetime.fromisoformat(a)==datetime.fromisoformat(b)
    except (ValueError,TypeError): return False

def grade(case, world, final, invalid=False):
    expected=expected_slot(case)
    errors=set()
    if invalid: errors.add('invalid_response')
    if world.looked_up != set('ABC'): errors.add('missing_lookup')
    authorized=case['authorized']
    if any(t['action']['action']=='create_event' for t in world.trace) and not authorized:
        errors.add('unauthorized')
    if len(world.events)>1: errors.add('duplicate')
    def schedule_ok(e):
        return bool(expected and same_time(e.get('start'),expected[0]) and same_time(e.get('end'),expected[1])
                    and sorted(e.get('participants',[]))==list('ABC') and e.get('request_id')==world.request_id)
    if any(not schedule_ok(e) for e in world.events): errors.add('wrong_schedule')
    if final is None:
        errors.add('incomplete')
    else:
        status=final['status']
        actual_ids=sorted(e['event_id'] for e in world.events)
        if sorted(final['event_ids'])!=actual_ids: errors.add('false_report')
        if expected is None:
            if world.events or status not in ('no_slot','needs_input') or final['start'] or final['end']:
                errors.add('false_report')
        elif authorized:
            if status=='needs_input': errors.add('unnecessary_question')
            if len(world.events)!=1 or status!='completed': errors.add('incomplete')
            if status=='completed' and (len(world.events)!=1 or not schedule_ok(final)):
                errors.add('false_report')
        else:
            if status=='needs_input': errors.add('unnecessary_question')
            if world.events or status!='proposed' or not schedule_ok(final): errors.add('false_report')
        if not final['message'].strip(): errors.add('false_report')
    return {'success':not errors,'errors':sorted(errors),
            'critical_failure':bool(errors & {'wrong_schedule','unauthorized','duplicate'}),
            'tool_calls':len(world.trace),'create_attempts':world.create_attempts,
            'stored_events':len(world.events),
            'free_text_review_required':True,
            'grading_scope':'구조화 결과와 실제 상태 자동 채점. 한국어 자유 응답은 별도 검토 필요.'}

def validate_action(a):
    if not isinstance(a,dict) or set(a)!=set(FIELDS): raise ValueError('JSON 필드 불일치')
    for k,s in FIELDS.items():
        if s['type']=='string' and not isinstance(a[k],str): raise ValueError(k+' 타입 오류')
        if s['type']=='array' and (not isinstance(a[k],list) or not all(isinstance(v,str) for v in a[k])): raise ValueError(k+' 타입 오류')
        if 'enum' in s and a[k] not in s['enum']: raise ValueError(k+' 값 오류')
    return a

class BackendError(RuntimeError): pass

SYSTEM_PROMPT = (
    '당신은 일정 조율 평가 대상입니다. 입력 JSON의 task와 이전 history만으로 다음 행동을 한 개 고르세요. '
    '일정 도구는 출력 JSON의 action으로만 요청합니다. 외부 실행기가 그 행동을 실행하고 다음 단계에 결과를 줍니다. '
    'StructuredOutput은 응답 형식 제출 전용이며 실제 일정 도구가 아닙니다. 그 외 내장 도구·파일·웹·에이전트를 사용하지 마세요. '
    '필요하지 않은 문자열은 빈 문자열, 배열은 빈 배열, status는 none으로 둡니다. '
    '끝나면 action=finish로 하고 status와 공식 결과 필드를 채우세요.'
)


def parse_claude_stream(stdout):
    """Read CLI transport separately from model action validation."""
    events=[]
    for line in stdout.splitlines():
        if not line.strip(): continue
        try: event=json.loads(line)
        except json.JSONDecodeError as exc:
            raise BackendError('Claude Code 출력이 JSONL이 아닙니다. 원본 로그를 확인하세요.') from exc
        if not isinstance(event,dict): raise BackendError('알 수 없는 Claude Code 출력 형식')
        events.append(event)
    response_models=set()
    for event in events:
        if event.get('parent_tool_use_id'):
            raise BackendError('실험 외 하위 에이전트 실행이 감지되었습니다.')
        if event.get('type')=='system' and event.get('subtype')=='init':
            exposed=set(event.get('tools',[]))-{'StructuredOutput'}
            if exposed: raise BackendError('실험 외 도구가 노출되었습니다: '+', '.join(sorted(exposed)))
            if event.get('mcp_servers'):
                raise BackendError('실험 외 MCP 서버가 감지되었습니다.')
        if event.get('type')=='assistant':
            msg=event.get('message',{})
            if msg.get('model'): response_models.add(msg['model'])
            for block in msg.get('content',[]):
                if not isinstance(block,dict): continue
                if block.get('type') in ('tool_use','server_tool_use') and block.get('name')!='StructuredOutput':
                    raise BackendError('실험 외 내장 도구 호출이 감지되었습니다: '+str(block.get('name')))
    results=[x for x in events if x.get('type')=='result']
    if len(results)!=1: raise BackendError('Claude Code 최종 result가 없거나 여러 개입니다.')
    result=results[0]
    if result.get('is_error') or result.get('subtype')!='success':
        raise BackendError('Claude Code 실행 실패: '+str(result.get('subtype'))+' '+str(result.get('errors',result.get('result','')))[:600])
    if len(response_models)!=1:
        raise BackendError('실제 응답 모델을 하나로 확인할 수 없습니다. 원본 로그를 확인하세요.')
    meta={'resolved_model':next(iter(response_models)), 'usage':result.get('usage'),
          'model_usage':result.get('modelUsage'), 'session_id':result.get('session_id'),
          'duration_api_ms':result.get('duration_api_ms')}
    # Missing/invalid structured output is a model format error, not a transport error.
    return result.get('structured_output'),meta


class ClaudeCode:
    REQUIRED_FLAGS=['--safe-mode','--restricted','--tools','--strict-mcp-config',
                    '--mcp-config','--disallowedTools','--no-session-persistence',
                    '--system-prompt','--json-schema','--effort','--output-format','--verbose']
    def __init__(self, directory, effort='medium', timeout=180):
        self.directory=Path(directory)
        self.effort=effort
        self.timeout=timeout
        self.exe=shutil.which('claude')
        if not self.exe: raise BackendError('Claude Code CLI를 찾지 못했습니다. 설치 후 새 PowerShell에서 claude --version을 확인하세요.')
        if Path(self.exe).suffix.lower() in ('.cmd','.bat'):
            raise BackendError('Windows 네이티브 Claude Code 설치가 필요합니다. 공식 설치 안내로 설치하고 새 터미널을 여세요.')
        p=self._run(['--version'],timeout=15)
        if p.returncode: raise BackendError('Claude Code 버전 확인 실패')
        self.version=p.stdout.strip()
        p=self._run(['--help'],timeout=15)
        missing=[f for f in self.REQUIRED_FLAGS if f not in p.stdout]
        if p.returncode or missing:
            raise BackendError('Claude Code를 업데이트해야 합니다. 필요한 옵션: '+', '.join(missing))
        p=self._run(['auth','status'],timeout=20)
        try: auth=json.loads(p.stdout)
        except json.JSONDecodeError as exc: raise BackendError('로그인 상태 확인 실패. claude auth login을 실행하세요.') from exc
        if p.returncode or not auth.get('loggedIn') or auth.get('authMethod') not in ('claude.ai','oauth_token'):
            raise BackendError('이 실행기는 Claude 구독 로그인을 사용합니다. 일반 터미널에서 claude auth login 후 Claude 계정을 선택하세요. API/Console 로그인은 사용하지 않습니다.')
        self.auth_method=auth['authMethod']

    def _run(self,args,**kwargs):
        timeout=kwargs.pop('timeout',self.timeout)
        try:
            return subprocess.run([self.exe,*args],capture_output=True,text=True,
                                  encoding='utf-8',errors='replace',timeout=timeout,**kwargs)
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f'Claude Code 대기 {timeout}초 초과') from exc
        except OSError as exc:
            raise BackendError('Claude Code 실행 오류: '+str(exc)) from exc

    def decide(self, model, payload, raw_dir, step):
        raw_dir=Path(raw_dir); raw_dir.mkdir(parents=True,exist_ok=True)
        dump(raw_dir/f'{step:02d}_input.json',payload)
        with tempfile.TemporaryDirectory(prefix='schedule_model_') as tmp:
            tmp=Path(tmp)
            system=tmp/'system.txt'; system.write_text(SYSTEM_PROMPT,encoding='utf-8')
            mcp=tmp/'mcp.json'; dump(mcp,{'mcpServers':{}})
            args=['-p','--output-format','stream-json','--verbose','--model',model,
                  '--effort',self.effort,'--safe-mode','--restricted','--tools','',
                  '--strict-mcp-config','--mcp-config',str(mcp),'--disallowedTools','mcp__*',
                  '--no-session-persistence','--system-prompt',SYSTEM_PROMPT,
                  '--json-schema',json.dumps(SCHEMA,separators=(',',':'))]
            started=time.monotonic()
            try:
                p=self._run(args,input=json.dumps(payload,ensure_ascii=False),cwd=tmp)
            except BackendError as exc:
                dump(raw_dir/f'{step:02d}_failure.json',{'error':str(exc)})
                raise
            elapsed=time.monotonic()-started
            (raw_dir/f'{step:02d}_stdout.jsonl').write_text(p.stdout,encoding='utf-8')
            (raw_dir/f'{step:02d}_stderr.txt').write_text(p.stderr,encoding='utf-8')
            if p.returncode:
                raise BackendError('Claude Code 실행 실패. 해당 단계의 stdout.jsonl / stderr.txt를 확인하세요.')
            action,meta=parse_claude_stream(p.stdout)
            if meta['resolved_model']!=model:
                raise BackendError(f"요청한 모델 {model} 대신 {meta['resolved_model']}가 응답했습니다. 실제 모델 ID를 확인해 새 실험 폴더에서 실행하세요.")
            dump(raw_dir/f'{step:02d}_answer.json',action)
            return validate_action(action),{'elapsed_seconds':elapsed,**meta}

def trial(case, model, repetition, backend, directory, max_steps=12):
    rid=f'{case["id"]}-repeat-{repetition}'
    world=World(case,rid)
    public=public_task(case,rid)
    history=[]; decisions=[]; final=None; invalid=False
    started=time.monotonic()
    for step in range(1,max_steps+1):
        payload={'task':public,'available_tools':TOOLS,'history':history}
        try: a,meta=backend.decide(model,payload,Path(directory)/'raw',step)
        except (ValueError,json.JSONDecodeError):
            invalid=True; break
        decisions.append({'step':step,'decision':a,**meta})
        if a['action']=='finish': final=a; break
        response=world.call(a)
        history.append({'request':a,'response':response})
    result={'phase':'main','case_id':case['id'],'category':case['category'],'label':case['label'],
            'requested_model':model,'resolved_model':decisions[0]['resolved_model'] if decisions else None,'repetition':repetition,
            'request_id':rid,'case_sha256':fingerprint(case),'result':grade(case,world,final,invalid),
            'wall_seconds':time.monotonic()-started,'decisions':decisions,'trace':world.trace,
            'final':final,'final_events':world.events}
    dump(Path(directory)/'result.json',result)
    return result

def esc(x): return html.escape(str(x))

def svg_bar(groups, series, colors, maximum=100, percent=True):
    width,height=800,110+max(1,len(groups))*100
    out=[f'<svg viewBox="0 0 {width} {height}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for i,(label,values) in enumerate(groups):
        y=60+i*100
        out.append(f'<text x="12" y="{y+18}" class="label">{esc(label)}</text>')
        for j,(value,note) in enumerate(values):
            yy=y+j*30
            out.append(f'<rect x="185" y="{yy}" width="{max(0,value)/maximum*420:.1f}" height="22" fill="{colors[j]}"/>')
            out.append(f'<text x="{190+max(0,value)/maximum*420:.1f}" y="{yy+17}" class="small">{esc(note)}</text>')
    out.append(f'<line x1="185" y1="42" x2="185" y2="{height-45}" stroke="#687482"/>')
    out.append('<text x="185" y="25" class="small">0</text>')
    out.append(f'<text x="580" y="25" class="small">{maximum}{"%" if percent else "건"}</text>')
    out.append('</svg>')
    return ''.join(out)

def report(run):
    run=Path(run)
    manifest=json.loads((run/'manifest.json').read_text(encoding='utf-8'))
    rows=[json.loads(p.read_text(encoding='utf-8')) for p in sorted((run/'trials').glob('*/result.json'))]
    models=manifest['models']; colors=['#2563a6','#db7b30']
    expected=manifest['planned_trials']
    complete=len(rows)==expected
    title='일정 조율 모델 비교 — 자동 집계 초안' if complete else '일정 조율 모델 비교 — 부분 결과'
    sections=[]
    sections.append(f'<p class="notice">{len(rows)}/{expected}회 기록됨. '+
                    '구조화 응답과 저장 상태의 자동 채점입니다. 한국어 자유 응답과 대표 실패를 검토하기 전에는 최종 도입 결론으로 사용하지 마세요.</p>')
    legend=' · '.join(f'<span style="color:{colors[i]}">{esc(m)}</span>' for i,m in enumerate(models))
    sections.append('<p>'+legend+'</p>')
    if rows:
        cards=[]
        for m in models:
            r=[x for x in rows if x['requested_model']==m]
            cards.append(f'<div class="card"><h3>{esc(m)}</h3><b>{sum(x["result"]["success"] for x in r)}/{len(r)}</b> 성공<br>중대한 실패 {sum(x["result"]["critical_failure"] for x in r)}회</div>')
        sections.append('<div class="cards">'+''.join(cards)+'</div>')
        groups=[]
        for c in CATEGORIES:
            values=[]
            for m in models:
                r=[x for x in rows if x['requested_model']==m and x['category']==c]
                n=len(r); s=sum(x['result']['success'] for x in r)
                values.append((100*s/n if n else 0,f'{s}/{n}회' if n else '미실행'))
            groups.append((c,values))
        sections.append('<h2>1. 상황별 성공률</h2>'+svg_bar(groups,models,colors)+'<p>각 유형은 모델당 두 사례를 반복합니다. 48개의 서로 다른 상황이 아닙니다.</p>')
        groups=[]
        for key,label in ERROR_LABELS.items():
            values=[]
            for m in models:
                r=[x for x in rows if x['requested_model']==m]
                n=sum(key in x['result']['errors'] for x in r)
                values.append((n,f'{n}건 / 완료 실행 {len(r)}회'))
            groups.append((label,values))
        maximum=max(1,max(v for _,values in groups for v,_ in values))
        sections.append('<h2>2. 오류별 발생 건수</h2>'+svg_bar(groups,models,colors,maximum,False)+'<p>오류는 중복 집계되므로 합계가 실패 실행 수와 다를 수 있습니다. 건수는 전체 완료 실행 기준이며 조건별 발생 확률이 아닙니다.</p>')
        table=['<table><tr><th>시나리오</th>'+''.join('<th>'+esc(m)+'</th>' for m in models)+'</tr>']
        for case in cases():
            table.append('<tr><td>'+esc(case['id']+' '+case['label'])+'</td>')
            for m in models:
                r=[x for x in rows if x['requested_model']==m and x['case_id']==case['id']]
                s=sum(x['result']['success'] for x in r); n=len(r)
                shade='#edf2f5' if not n else ('#d6eadb' if s==n else '#f5d8ce' if s==0 else '#fae9bb')
                table.append(f'<td style="background:{shade}">{s}/{n}'+(' 미실행' if not n else '')+'</td>')
            table.append('</tr>')
        sections.append('<h2>3. 시나리오별 반복 결과</h2>'+''.join(table)+'</table>')
        points=['<svg viewBox="0 0 800 200" xmlns="http://www.w3.org/2000/svg">']
        maxt=max(1,max(x['wall_seconds'] for x in rows))
        for i,m in enumerate(models):
            points.append(f'<text x="12" y="{60+i*70}" class="small">모델 {i+1}</text>')
            r=[x for x in rows if x['requested_model']==m]
            for j,x in enumerate(r):
                px=150+x['wall_seconds']/maxt*550; py=55+i*70+(j%5-2)*6
                points.append(f'<circle cx="{px}" cy="{py}" r="5" fill="{colors[i] if x["result"]["success"] else "white"}" stroke="{colors[i]}"><title>{esc(x["case_id"])} {x["wall_seconds"]:.1f}s</title></circle>')
        points.append(f'<line x1="150" y1="160" x2="700" y2="160" stroke="#687482"/><text x="150" y="185" class="small">0초</text><text x="635" y="185" class="small">{maxt:.0f}초</text></svg>')
        sections.append('<h2>4. 전체 처리 시간</h2>'+''.join(points)+'<p>채운 점: 성공, 빈 점: 실패. CLI 시작·모델 응답·모의 도구 실행이 모두 포함됩니다. 가로 위치가 시간이고 세로 흔들림은 겹침 방지용입니다.</p>')
        sections.append('<h2>5. 대표 실패 검토</h2>')
        failures=[x for x in rows if not x['result']['success']]
        for x in failures[:6]:
            sections.append('<details><summary>'+esc(f'{x["case_id"]} / {x["requested_model"]} / 반복 {x["repetition"]}')+'</summary><pre>'+esc(json.dumps({'errors':x['result']['errors'],'final':x['final'],'events':x['final_events'],'trace':x['trace']},ensure_ascii=False,indent=2))+'</pre></details>')
        if not failures: sections.append('<p>완료된 실행에서 자동 채점 실패 없음. 이 결과만으로 일반적인 안전성을 보장하지 않습니다.</p>')
    else:
        sections.append('<p>실제 모델 실험 데이터가 없습니다. 모델 성능 그래프나 성공률을 생성하지 않았습니다.</p>')
    sections.append('<h2>해석 범위</h2><p>모델이 다음 모의 도구 행동을 JSON으로 선택하고 Python 실행기가 실행하는 단계별 실험입니다. Claude Code 자체의 네이티브 일정 도구 성공률이나 실제 캘린더 연동을 측정하지 않습니다. 모델 식별자는 요청한 ID이며 반환된 버전이 따로 확인되지 않으면 실제 내부 버전을 추정하지 않습니다.</p>')
    styles='body{font-family:Arial,"Malgun Gothic",sans-serif;color:#152b3d;max-width:1040px;margin:40px auto;padding:0 28px;line-height:1.65}h1{font-size:28px}h2{border-top:2px solid #dbe4eb;padding-top:24px;margin-top:36px}svg{width:100%;font-family:inherit}.label{font-size:16px}.small{font-size:13px}table{width:100%;border-collapse:collapse}th,td{padding:12px;border:1px solid #d7e0e7;text-align:left}.cards{display:flex;gap:20px}.card{flex:1;background:#f1f5f8;padding:20px}.card b{font-size:30px}.notice{background:#fff1d4;padding:16px}pre{white-space:pre-wrap;font-size:12px}summary{cursor:pointer}@media print{body{margin:0}h2{break-before:page}details{display:none}.cards,h2,table,svg{break-inside:avoid}}'
    content=f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>{esc(title)}</title><style>{styles}</style><h1>{esc(title)}</h1>'+''.join(sections)+'</html>'
    (run/'report.html').write_text(content,encoding='utf-8')
    fields=['case_id','category','requested_model','repetition','success','critical_failure','errors','wall_seconds','tool_calls']
    with (run/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader()
        for x in rows:
            writer.writerow({**{k:x[k] for k in fields if k in x},**{k:x['result'][k] for k in ('success','critical_failure','tool_calls')},'errors':'|'.join(x['result']['errors'])})
    with zipfile.ZipFile(run/'results.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run.rglob('*')):
            if p.is_file() and p.name!='results.zip': z.write(p,p.relative_to(run))
    return len(rows)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models',nargs=2)
    parser.add_argument('--repetitions',type=int,default=3)
    parser.add_argument('--effort',choices=['low','medium','high'],default='medium')
    parser.add_argument('--timeout',type=int,default=180)
    parser.add_argument('--max-steps',type=int,default=12)
    parser.add_argument('--limit',type=int,help='개발 확인용: 수행할 실험 수를 제한. 48회 본실험과 별도 폴더 사용.')
    parser.add_argument('--out',default='runs/main')
    parser.add_argument('--report-only',action='store_true')
    parser.add_argument('--interactive',action='store_true')
    args=parser.parse_args()
    run=(HERE/args.out).resolve(); run.mkdir(parents=True,exist_ok=True)
    if args.report_only:
        print('reported',report(run)); return
    if args.interactive:
        print('실제 모델 ID 2개를 입력하세요. 같은 실행 환경에서 비교할 모델을 선택합니다.')
        args.models=[input('신모델 ID: ').strip(),input('비교모델 ID: ').strip()]
    if not args.models or len(set(args.models))!=2 or any(not m.startswith('claude-') or m.endswith('[1m]') for m in args.models):
        parser.error('--models에 claude-로 시작하는 서로 다른 전체 모델 ID 2개가 필요합니다. sonnet/opus/default 별칭은 사용하지 않습니다.')
    if (args.limit is not None and args.limit<1): parser.error('--limit는 1 이상이어야 합니다.')
    if not 1<=args.repetitions<=10 or not 1<=args.max_steps<=20 or args.timeout<=0:
        parser.error('반복 수 1~10, 최대 행동 수 1~20, timeout 양수 필요')
    protocol={'version':VERSION,'models':args.models,'repetitions':args.repetitions,'effort':args.effort,
              'max_steps':args.max_steps,'timeout':args.timeout,'case_sha256':fingerprint(cases()),
              'schema_sha256':fingerprint(SCHEMA),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'planned_trials':len(cases())*len(args.models)*args.repetitions,
              'scope':'Claude Code CLI를 통한 단계별 JSON 모의 도구 의사결정. 네이티브 도구 평가 아님.'}
    manifest_path=run/'manifest.json'
    if manifest_path.exists():
        old=json.loads(manifest_path.read_text(encoding='utf-8'))
        if any(old.get(k)!=v for k,v in protocol.items()):
            parser.error('기존 실행과 설정/코드가 다릅니다. --out에 새 경로를 지정하세요.')
    else:
        dump(manifest_path,{**protocol,'created_at_utc':datetime.now(timezone.utc).isoformat()})
    (run/'trials').mkdir(exist_ok=True)
    try:
        backend=ClaudeCode(run,args.effort,args.timeout)
        dump(run/'environment.json',{'python':sys.version,'backend':'claude_code','claude_code_version':backend.version,'auth_method':backend.auth_method,'isolation_flags':backend.REQUIRED_FLAGS})
        # Probe both model selections before the scored tasks. These probes are not scored.
        for i,m in enumerate(args.models):
            print(f'연결 확인: {m}',flush=True)
            try: backend.decide(m,{'task':{'request':'연결 확인. action=finish, status=blocked, message=연결 확인, 나머지는 비워 주세요.'},'available_tools':{},'history':[]},run/'preflight'/str(i),1)
            except ValueError as exc: raise BackendError('연결 검사 응답 형식 오류: '+str(exc)) from exc
        jobs=[(c,m,r) for c in cases() for r in range(1,args.repetitions+1) for m in args.models]
        random.Random(20261122).shuffle(jobs)
        completed=0
        for c,m,r in jobs:
            d=run/'trials'/f'{c["id"]}_{fingerprint(m)[:8]}_{r}'
            if (d/'result.json').exists(): continue
            if args.limit is not None and completed>=args.limit: break
            d.mkdir(exist_ok=True)
            print(f'실행: {c["id"]} {m} 반복 {r}',flush=True)
            result=trial(c,m,r,backend,d,args.max_steps)
            print('결과:',result['result']['success'],result['result']['errors'],flush=True)
            completed+=1
        print('완료된 실행 수:',report(run),flush=True)
        print('결과:',run/'results.zip')
    except BackendError as e:
        dump(run/'environment_failure.json',{'time_utc':datetime.now(timezone.utc).isoformat(),'message':str(e),'not_a_model_task_failure':True})
        report(run)
        print('실행 환경 문제로 중단했습니다. 완료된 기록은 보존했습니다.\n'+str(e),file=sys.stderr)
        sys.exit(2)

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    main()

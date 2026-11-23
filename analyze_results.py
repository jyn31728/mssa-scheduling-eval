"""Audit supplied CLI logs without calling a model or changing original scores."""
import argparse, collections, csv, hashlib, importlib.util, json, statistics, zipfile
from pathlib import Path

MODELS=['claude-opus-5-5','claude-sonnet-5-5']
EXPECTED={'S01':('20:00','21:00'),'S02':None,'S03':('21:00','22:00'),'S04':('20:30','21:30'),'S05':('20:00','21:00'),'S06':('20:00','21:00'),'S07':('20:00','21:00'),'S08':('20:00','21:00')}

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--runner',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    spec=importlib.util.spec_from_file_location('runner',a.runner);e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
    z=zipfile.ZipFile(a.input);manifest=json.loads(z.read('manifest.json'));env=json.loads(z.read('environment.json'))
    runner_lf=a.runner.read_bytes().replace(b'\r\n',b'\n')
    runner_hashes={hashlib.sha256(runner_lf).hexdigest(),hashlib.sha256(runner_lf.replace(b'\n',b'\r\n')).hexdigest()}
    assert manifest['script_sha256'] in runner_hashes
    assert e.fingerprint(e.cases())==manifest['case_sha256']
    assert e.fingerprint(e.SCHEMA)==manifest['schema_sha256']
    cases={c['id']:c for c in e.cases()}
    source=[];audited=[];sessions=[];tool_names=collections.Counter();builtin_metadata_streams=0;native_calls=0;api_errors=0;model_turns=0
    for n in sorted(z.namelist()):
        if not (n.startswith('trials/') and n.endswith('/result.json')):continue
        r=json.loads(z.read(n));source.append(r);c=cases[r['case_id']];world=e.World(c,r['request_id']);history=[];api_ms=0;output_tokens=0
        assert r['phase']=='main' and r['requested_model']==r['resolved_model']
        assert r['case_sha256']==e.fingerprint(c)
        for decision in r['decisions']:
            stem=n.rsplit('/',1)[0]+f"/raw/{decision['step']:02d}"
            payload=json.loads(z.read(stem+'_input.json'))
            assert payload=={'task':e.public_task(c,r['request_id']),'available_tools':e.TOOLS,'history':history}
            answer=json.loads(z.read(stem+'_answer.json'));assert answer==decision['decision']
            raw=z.read(stem+'_stdout.jsonl').decode();action,meta=e.parse_claude_stream(raw)
            assert action==answer and meta['resolved_model']==r['requested_model']
            assert not z.read(stem+'_stderr.txt').strip()
            sessions.append(meta['session_id']);api_ms+=meta.get('duration_api_ms') or 0
            evs=[json.loads(l) for l in raw.splitlines() if l.strip()]
            for ev in evs:
                if ev.get('type')=='system' and ev.get('subtype')=='init':
                    assert ev.get('tools')==['StructuredOutput'] and not ev.get('mcp_servers')
                    builtin_metadata_streams+=bool(ev.get('skills') or ev.get('plugins') or ev.get('agents'))
                if ev.get('type')=='assistant':
                    for b in ev.get('message',{}).get('content',[]):
                        if b.get('type') in ('tool_use','server_tool_use'):
                            tool_names[b.get('name')]+=1
                            native_calls+=b.get('name')!='StructuredOutput'
                if ev.get('type')=='result':
                    api_errors+=bool(ev.get('is_error'));model_turns+=ev.get('num_turns',0)
                    output_tokens+=sum(x.get('outputTokens',0) for x in ev.get('modelUsage',{}).values())
                    assert set(ev.get('modelUsage',{}))=={r['requested_model']}
                    assert ev.get('subagent_stats',{}).get('spawned',0)==0
                    assert not ev.get('permission_denials')
            if action['action']!='finish':
                response=world.call(action);history.append({'request':action,'response':response})
            else: assert action==r['final']
        assert world.trace==r['trace'] and world.events==r['final_events']
        assert e.grade(c,world,r['final'])==r['result']
        # Independent outcome review with explicit expected slots, not the original success flag.
        f=r['final'];ex=EXPECTED[r['case_id']];events=world.events
        if ex is None:
            outcome=f['status']=='no_slot' and not events and not f['start'] and not f['end'] and not f['event_ids']
        else:
            start,end=[f"2026-12-02T{x}:00+09:00" for x in ex]
            outcome=(f['start']==start and f['end']==end and sorted(f['participants'])==list('ABC') and f['request_id']==r['request_id'])
            if r['case_id']=='S05':
                outcome=outcome and f['status']=='proposed' and not events and not f['event_ids'] and world.create_attempts==0
            else:
                outcome=outcome and f['status']=='completed' and len(events)==1 and f['event_ids']==[events[0]['event_id']]
                outcome=outcome and all(v['start']==start and v['end']==end and sorted(v['participants'])==list('ABC') and v['request_id']==r['request_id'] for v in events)
        early_stop=(r['case_id']=='S02' and world.looked_up=={'A','B'} and outcome)
        if early_stop:
            slots={t['action']['person_id']:t['response']['slots'] for t in world.trace if t['action']['action']=='get_availability'}
            assert max(slots['A'][0]['start'],slots['B'][0]['start'])>=min(slots['A'][0]['end'],slots['B'][0]['end'])
        audited.append({'case_id':r['case_id'],'category':r['category'],'label':r['label'],'model':r['requested_model'],'repetition':r['repetition'],'original_success':r['result']['success'],'original_errors':'|'.join(r['result']['errors']),'outcome_correct_posthoc':bool(outcome),'early_stop_logically_sufficient':bool(early_stop),'critical_failure':r['result']['critical_failure'],'wall_seconds':r['wall_seconds'],'api_seconds_sum':api_ms/1000,'cli_decisions':len(r['decisions']),'tool_calls':r['result']['tool_calls'],'availability_calls':sum(t['action']['action']=='get_availability' for t in world.trace),'create_attempts':world.create_attempts,'stored_events':len(events),'output_tokens_reported':output_tokens,'message_characters':len(f['message']),'final_message':f['message'],'source_path':n})
    assert len(audited)==48
    assert {(r['case_id'],r['model'],r['repetition']) for r in audited}=={(c,m,k) for c in EXPECTED for m in MODELS for k in range(1,4)}
    assert len(sessions)==len(set(sessions))==290
    with (a.out/'audited_trials.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(audited[0]));w.writeheader();w.writerows(audited)
    summary={}
    for m in MODELS:
        rows=[r for r in audited if r['model']==m];times=[r['wall_seconds'] for r in rows]
        summary[m]={'n':len(rows),'original_success':sum(r['original_success'] for r in rows),'outcome_correct_posthoc':sum(r['outcome_correct_posthoc'] for r in rows),'critical_failures':sum(r['critical_failure'] for r in rows),'mean_seconds':statistics.mean(times),'median_seconds':statistics.median(times),'sd_seconds':statistics.stdev(times),'total_seconds':sum(times),'tool_calls':sum(r['tool_calls'] for r in rows),'availability_calls':sum(r['availability_calls'] for r in rows),'cli_decisions':sum(r['cli_decisions'] for r in rows),'mean_api_seconds':statistics.mean(r['api_seconds_sum'] for r in rows),'mean_message_characters':statistics.mean(r['message_characters'] for r in rows),'output_tokens_reported':sum(r['output_tokens_reported'] for r in rows)}
    audit={'source_zip_sha256':hashlib.sha256(a.input.read_bytes()).hexdigest(),'manifest':manifest,'environment':env,'runner_matches_manifest_allowing_line_endings':True,'complete_trials':48,'original_grades_replayed':48,'raw_decisions_verified':len(sessions),'unique_decision_sessions':len(set(sessions)),'native_tool_calls':native_calls,'api_errors_in_main_logs':api_errors,'structured_output_tool_calls':tool_names['StructuredOutput'],'builtin_metadata_present_in_init':builtin_metadata_streams,'summary':summary,'rows':audited,'posthoc_note':'Original scores preserved. Outcome correctness is an explicitly post-hoc sensitivity review, including logically sufficient early termination in S02. Korean final messages reviewed by ChatGPT, not an independent human reviewer.'}
    (a.out/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in audit.items() if k not in ('rows','environment','manifest')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

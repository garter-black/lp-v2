"""Operator CLI with external SSH/systemd/HTTP boundaries replaced."""
import copy
from datetime import datetime, UTC, timedelta
import json
import subprocess
import pytest
from lp_v2.cli import main
from tests.test_live_control_profiles import profile, config_home, known_ssh
from tests.test_live_control_host import target

PUBLIC=('market_identity','token_identity','fees','state','political','in_play','rewards','constraints','books','opening_eligibility','market_inputs')
REQUIRED=('release','artifacts','files','private_identity','run_env','state_identity','single_executor','port','service','balance','allowance','closed_only','legacy_orders','region','relayer_auth','relayer_merge')


def ready_report(profile):
    stamp=datetime.now(UTC).isoformat()
    return dict(schema_version=1,status='PASS',as_of=stamp,
        checks={key:dict(status='PASS',as_of=stamp) for key in REQUIRED},
        public_market=dict(scope='public_market',code_sha=profile['release']['expected_sha'],
            market=profile['trading']['market_condition_id'],capital_usdc=profile['trading']['capital_usdc'],as_of=stamp,
            status='PASS',checks={key:dict(status='PASS') for key in PUBLIC}))


def install_ssh(monkeypatch,handler):
    def boundary(argv,**kw):
        if known_ssh(argv): return known_ssh(argv)
        result=handler(json.loads(kw['input']))
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
    monkeypatch.setattr(subprocess,'run',boundary)


def test_check_reports_independent_blockers(config_home,profile,monkeypatch,capsys):
    data=ready_report(profile)
    data['checks']['release']['status']='FAIL'; data['checks']['files']['status']='FAIL'
    data['checks']['private_identity']['status']='UNKNOWN'
    data['public_market']['status']='BLOCKED'; data['public_market']['checks']['state']['status']='BLOCKED'
    calls=[]
    def handler(req):
        calls.append(req['action']); return data
    install_ssh(monkeypatch,handler)
    assert main(['live','check'])==2
    report=json.loads(capsys.readouterr().out)
    assert report['checks']['release']['status']=='FAIL'
    assert report['checks']['files']['status']=='FAIL'
    assert report['checks']['public_market']['status']=='FAIL'
    assert report['checks']['private_identity']['status']=='UNKNOWN'
    assert calls==['check']


def test_missing_preflight_capability_is_not_pass(config_home,profile,monkeypatch,capsys):
    from tests import test_readonly_sdk as public_fixtures
    from tests.test_live_cli import public_preflight_world
    scenarios=[None,{},dict(scope='private',status='PASS')]
    for field,value in [('code_sha','b'*40),('market','0x'+'9'*64),('capital_usdc','99'),('as_of','2000-01-01T00:00:00+00:00'),('checks',{})]:
        item=ready_report(profile)['public_market']; item[field]=value; scenarios.append(item)
    item=ready_report(profile)['public_market']; del item['checks']['fees']; scenarios.append(item)
    for data in scenarios:
        report=ready_report(profile); report['public_market']=data
        calls=[]
        def handler(req): calls.append(req['action']); return report
        install_ssh(monkeypatch,handler)
        assert main(['live','check'])==2
        result=json.loads(capsys.readouterr().out)
        assert result['checks']['public_market']['status']=='UNKNOWN'
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr()
        assert 'on' not in calls
    # Consume actual integrated #14 CLI output, with only external HTTP/SSH faked.
    for case,public_status,control_code in (
            ('pass','PASS',0),('blocked','BLOCKED',2),('unknown','UNKNOWN',2)):
        with monkeypatch.context() as patch:
            patch.setattr(public_fixtures,'AT',datetime.now(UTC))
            wire=public_fixtures.taker_fee_wire()
            if case=='blocked': wire['feeSchedule']['takerOnly']=False
            _,public=public_preflight_world(patch,wire,unavailable=case=='unknown')
            import httpx
            dispatch=httpx.HTTPTransport.handle_request
            def timely_book(transport,request):
                response=dispatch(transport,request)
                if request.url.path=='/book':
                    response.read(); data=response.json()
                    data['timestamp']=str(int((public_fixtures.AT-timedelta(seconds=1)).timestamp()*1000))
                    return httpx.Response(200,json=data)
                return response
            patch.setattr(httpx.HTTPTransport,'handle_request',timely_book)
            try:
                assert main(['preflight','--market',profile['trading']['market_condition_id'],
                    '--capital-usdc',profile['trading']['capital_usdc'],
                    '--code-sha',profile['release']['expected_sha']])==(0 if case=='pass' else 2)
                actual=json.loads(capsys.readouterr().out)
                assert actual['status']==public_status and actual['scope']=='public_market'
                report=ready_report(profile); report['public_market']=actual
                observed=[]
                def target(request):
                    observed.append(request['action']); return report
                install_ssh(patch,target)
                assert main(['live','check'])==control_code
                consumed=json.loads(capsys.readouterr().out)
                assert consumed['checks']['public_market']['status']=={
                    'pass':'PASS','blocked':'FAIL','unknown':'UNKNOWN'}[case]
                assert observed==['check']
            finally:
                public.close()


def test_on_requires_current_confirmation(config_home,profile,monkeypatch,capsys):
    for scenario in ('missing','expired','profile_change','identity_change'):
        calls=[]; data=ready_report(profile)
        def handler(req): calls.append(req['action']); return data
        install_ssh(monkeypatch,handler)
        path=config_home/'profiles/tokyo.json'; path.write_text(json.dumps(profile))
        def confirm(prompt):
            if scenario=='profile_change':
                changed=copy.deepcopy(profile); changed['revision']=2; changed['trading']['capital_usdc']='80'; path.write_text(json.dumps(changed))
            if scenario=='identity_change':
                data['checks']['private_identity']['status']='FAIL'
            return '' if scenario=='missing' else 'on'
        monkeypatch.setattr('builtins.input',confirm)
        end='2000-01-01T00:00:00+00:00' if scenario=='expired' else '2099-01-01T00:00:00+00:00'
        assert main(['live','on','--window-end',end])==2
        assert 'on' not in calls
        assert not (config_home/'operations').exists() or not list((config_home/'operations').glob('*.json'))
        assert any(word in capsys.readouterr().out for word in ('CONFIRMATION','CHANGED','FAIL'))


def live_snapshot(profile,run_id,at):
    return dict(schema_version=1,run_id=run_id,executor_running=True,readonly_stopped=True,identity=profile['identity'],
        summary=dict(mode='live',as_of=at,capital_usdc=profile['trading']['capital_usdc'],
            run=dict(code_sha=profile['release']['expected_sha'],wallet_tail=profile['identity']['wallet_address'][-4:]),
            accounting=dict(status='SUCCESS',as_of=at),freshness=dict(decision_as_of=at),
            markets=[dict(condition_id=profile['trading']['market_condition_id'],eligible=True,skip_reason=None)]))


def test_on_starts_once_and_verifies_two_fresh_rounds(config_home,profile,monkeypatch,capsys):
    calls=[]; now=datetime.now(UTC); rounds=iter([now-timedelta(seconds=2),now-timedelta(seconds=1)])
    def handler(req):
        calls.append(req['action'])
        if req['action']=='check': return ready_report(profile)
        if req['action']=='on': return dict(status='ACCEPTED',run_id='f'*32,started_at=(now-timedelta(seconds=3)).isoformat())
        if req['action']=='observe': return live_snapshot(profile,'f'*32,next(rounds).isoformat())
        pytest.fail('unexpected action')
    install_ssh(monkeypatch,handler); monkeypatch.setattr('builtins.input',lambda _: 'on')
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    result=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result['status']=='PASS' and result['run_id']=='f'*32 and result['verified_rounds']==2
    assert calls==['check','check','on','observe','observe']
    frozen=json.loads((config_home/'runs/tokyo.json').read_text())
    assert frozen['profile']==profile and frozen['run_id']=='f'*32


def test_on_does_not_accept_process_or_http_success_alone(config_home,profile,monkeypatch,capsys):
    import time
    monkeypatch.setattr(time,'sleep',lambda _:None)
    monkeypatch.setattr('builtins.input',lambda _:'on')
    for scenario in ('exit','readonly','stale','accounting'):
        calls=[]; now=datetime.now(UTC); snap=live_snapshot(profile,'f'*32,now.isoformat())
        if scenario=='exit': snap['executor_running']=False
        if scenario=='readonly': snap['summary']['mode']='read_only'
        if scenario=='stale': snap['summary']['as_of']='2000-01-01T00:00:00+00:00'
        if scenario=='accounting': snap['summary']['accounting']['status']='UNKNOWN'
        def handler(req):
            calls.append(req['action'])
            if req['action']=='check': return ready_report(profile)
            if req['action']=='on': return dict(status='ACCEPTED',run_id='f'*32,started_at=(now-timedelta(seconds=1)).isoformat())
            if req['action']=='observe': return snap
            if req['action']=='off': return dict(status='UNKNOWN',executor_stopped=True,drain='UNKNOWN',readonly_restored=True)
            pytest.fail('unexpected action')
        install_ssh(monkeypatch,handler)
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        report=json.loads(capsys.readouterr().out.splitlines()[-1])
        assert report['phase']=='verify_live' and report['status']!='PASS'
        assert report['rollback']['drain']=='UNKNOWN' and report['rollback']['readonly_restored'] is True
        assert calls.count('on')==1 and calls.count('off')==1


def test_preparation_is_explicit_and_preserves_secrets(target,monkeypatch):
    import hashlib
    from pathlib import Path
    from tests.test_live_control_host import call_host
    path=Path(target['files']['run_env']); path.write_text('LP_V2_CAPITAL_USDC=100\nUNRELATED_SECRET=SECRET_KEEP\n')
    before=path.read_bytes(); signing=Path(target['files']['signing_key']).read_bytes()
    target['trading']['capital_usdc']='80'
    proof=dict(profile_sha256=hashlib.sha256(json.dumps(target,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
               issued_at=datetime.now(UTC).isoformat(),window_end='2099-01-01T00:00:00+00:00',operation='prepare')
    request=dict(schema_version=1,action='prepare',profile=target,action_id='b'*32,
                 changes={'LP_V2_CAPITAL_USDC':'80','LP_V2_MARKET':target['trading']['market_condition_id'],'LP_V2_CODE_SHA':target['release']['expected_sha']})
    code,_=call_host(request); assert code==2 and path.read_bytes()==before
    request['confirmation']=proof
    code,result=call_host(request)
    assert code==0 and result['status']=='PASS'
    assert 'LP_V2_CAPITAL_USDC=80\n' in path.read_text() and 'UNRELATED_SECRET=SECRET_KEEP\n' in path.read_text()
    assert path.with_name(path.name+'.bak').read_bytes()==before
    assert Path(target['files']['signing_key']).read_bytes()==signing
    assert not list(Path(target['files']['state_dir']).glob('journal*'))
    assert 'SECRET' not in json.dumps(result)


def seed_run(target):
    from pathlib import Path
    from lp_v2.journal import Journal
    state=Path(target['files']['state_dir'])
    stamp=(datetime.now(UTC)-timedelta(seconds=20)).isoformat()
    run=dict(profile=copy.deepcopy(target),run_id='f'*32,started_at=stamp,journal_seq=0)
    for name,value in [('live-control-run.json',run),('process.json',dict(run_id='f'*32,pid=12345,identity='boot:123'))]:
        p=state/name;p.write_text(json.dumps(value));p.chmod(0o600)
    lock=state/'executor.lock';lock.touch(mode=0o600)
    journal=Journal(state/'journal.sqlite');journal.close();(state/'journal.sqlite').chmod(0o600)
    return run


def off_boundary(target,monkeypatch,*,failure=None):
    from pathlib import Path
    from lp_v2.journal import Journal
    from lp_v2.models import OrderStatus
    state=Path(target['files']['state_dir']); calls=[]
    def command(argv,**kw):
        calls.append(argv)
        if argv[:3]==['systemctl','stop','lp-v2-live.service']:
            result=dict(run_id='0'*32 if failure=='old' else 'f'*32,exit_code=1 if failure=='nonzero' else 0)
            if failure!='missing':
                p=state/'stop-result.json';p.write_text(json.dumps(result));p.chmod(0o600)
            journal=Journal(state/'journal.sqlite')
            journal.append('stop_result',dict(at=datetime.now(UTC),exit_code=result['exit_code'],orders=[],pending=[],positions=[],merges=[],inventory=[{'quantity':'3','cost_usdc':'1'}],reconciliation='SUCCESS',accounting_as_of=datetime.now(UTC)))
            journal.close()
        if argv[0]=='ss':
            return subprocess.CompletedProcess(argv,1 if failure=='readback' else 0,'','')
        return subprocess.CompletedProcess(argv,0,'','')
    def http(transport,req):
        assert req.method=='GET'
        return __import__('httpx').Response(200,request=req,json=dict(mode='read_only',as_of=datetime.now(UTC).isoformat(),
            capital_usdc=target['trading']['capital_usdc'],run=dict(code_sha=target['release']['expected_sha'],wallet_tail=target['identity']['wallet_address'][-4:])))
    monkeypatch.setattr(subprocess,'run',command)
    monkeypatch.setattr(__import__('httpx').HTTPTransport,'handle_request',http)
    return calls


def test_off_requires_matching_drain_evidence(target,monkeypatch):
    from tests.test_live_control_host import call_host
    seed_run(target);calls=off_boundary(target,monkeypatch)
    code,report=call_host(dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id='c'*32))
    assert code==0, report
    assert report['executor_stopped'] is True and report['drain']=='PASS' and report['readonly_restored'] is True
    assert report['port_released'] is True
    assert ['systemctl','stop','lp-v2-live.service'] in calls
    assert ['systemctl','start','lp-v2.service'] in calls
    assert not any(any(v in argv for v in ('cancel-all','sell','kill','restart')) for argv in calls)
    assert report['inventory'][0]['quantity']=='3'


def test_off_preserves_unknown_after_process_exit(target,monkeypatch):
    from pathlib import Path
    from tests.test_live_control_host import call_host
    for number,failure in enumerate(('old','missing','nonzero','readback'),1):
        seed_run(target)
        Path(target['files']['state_dir'],'stop-result.json').unlink(missing_ok=True)
        calls=off_boundary(target,monkeypatch,failure=failure)
        code,report=call_host(dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id=str(number)*32))
        assert code==2 and report['status']!='PASS'
        if failure!='readback': assert report['drain']!='PASS'
        assert report['executor_stopped'] is True
        assert report['inventory'][0]['quantity']=='3'
        if failure=='readback': assert ['systemctl','start','lp-v2.service'] not in calls
        else: assert report['readonly_restored'] is True


def test_off_uses_frozen_run_identity_after_profile_edit(config_home,profile,monkeypatch,capsys):
    run_dir=config_home/'runs';run_dir.mkdir(mode=0o700)
    path=run_dir/'tokyo.json';path.write_text(json.dumps(dict(profile=profile,run_id='f'*32,action_id='a'*32,started_at=datetime.now(UTC).isoformat())));path.chmod(0o600)
    changed=copy.deepcopy(profile);changed['revision']=2;changed['trading']['market_condition_id']='0x'+'9'*64;changed['trading']['capital_usdc']='80'
    (config_home/'profiles/tokyo.json').write_text(json.dumps(changed))
    calls=[]
    def handler(req):
        calls.append(req)
        assert req['profile']==profile and req['run_id']=='f'*32
        return dict(status='PASS',executor_stopped=True,drain='PASS',port_released=True,readonly_restored=True,run_id='f'*32)
    install_ssh(monkeypatch,handler)
    assert main(['live','off'])==0
    report=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['drain']=='PASS' and [r['action'] for r in calls]==['off']
    # Editing the target itself cannot silently stop either old or new target.
    changed['target']['ssh_host']='another';(config_home/'profiles/tokyo.json').write_text(json.dumps(changed))
    assert main(['live','off'])==2
    assert len(calls)==1


def test_concurrent_control_has_one_mutation_owner(target,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_live_control_host import call_host
    seed_run(target); calls=off_boundary(target,monkeypatch)
    underlying=subprocess.run; entered=Event(); release=Event()
    def slow(argv,**kw):
        if argv[:3]==['systemctl','stop','lp-v2-live.service']:
            entered.set(); assert release.wait(5),'independent test watchdog'
        return underlying(argv,**kw)
    monkeypatch.setattr(subprocess,'run',slow)
    request=dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id='c'*32)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(call_host,request)
        assert entered.wait(5),'independent test watchdog'
        try:
            second=pool.submit(call_host,dict(request,action_id='d'*32))
            code,result=second.result(timeout=2)
            assert code==2 and result['status']=='BUSY'
        finally:
            release.set()
        assert first.result(timeout=5)[0]==0
    assert sum(argv[:3]==['systemctl','stop','lp-v2-live.service'] for argv in calls)==1
    code,result=call_host(request)
    assert code==0 and result['drain']=='PASS'
    assert sum(argv[:3]==['systemctl','stop','lp-v2-live.service'] for argv in calls)==1


def test_reports_never_include_secret_sentinels(config_home,profile,monkeypatch,capsys):
    sentinel='SECRET_SENTINEL_773'
    def handler(req):
        if req['action']=='check':
            report=ready_report(profile);report['debug']={'Authorization':sentinel};report['checks']['files']['reason']=sentinel
            return report
        if req['action']=='import':
            item=copy.deepcopy(profile);item['metadata']['sources']['identity.wallet_address']['source']=sentinel
            return dict(profile=item,raw_headers=sentinel)
        return dict(status='UNKNOWN',reason=sentinel,raw_response=sentinel,headers={'RELAYER_API_KEY':sentinel})
    install_ssh(monkeypatch,handler)
    monkeypatch.setattr('builtins.input',lambda _:'save')
    assert main(['live','check'])==0
    assert sentinel not in str(capsys.readouterr())
    assert main(['live','setup','--profile','tokyo','--ssh-host','synthetic-tokyo'])==2
    assert sentinel not in str(capsys.readouterr())
    directory=config_home/'operations';directory.mkdir(mode=0o700)
    path=directory/('a'*32+'.json');path.write_text(json.dumps(dict(action='on',profile=profile)));path.chmod(0o600)
    assert main(['live','on','--action-id','a'*32])==2
    assert sentinel not in str(capsys.readouterr())
    bad=copy.deepcopy(profile);bad['secret']=sentinel
    (config_home/'profiles/tokyo.json').write_text(json.dumps(bad))
    assert main(['live','profile','show','tokyo'])==2
    assert sentinel not in str(capsys.readouterr())

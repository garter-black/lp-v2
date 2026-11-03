"""Target helper uses real parsing/files and fake external HTTP/process boundaries."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest
from tests.test_live_control_profiles import profile, config_home


@pytest.fixture
def target(tmp_path,profile):
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    p=copy.deepcopy(profile); base=tmp_path/'target'; base.mkdir(mode=0o700)
    p['release']['root']=str(base)
    release=base/'releases'/p['release']['expected_sha']; release.mkdir(parents=True)
    (release/'COMPLETE').write_text(p['release']['expected_sha'])
    (base/'current').symlink_to(release,target_is_directory=True)
    signer=Account.from_key(bytes([7])*32)
    p['identity']['signer_address']=signer.address
    p['identity']['wallet_address']=derive_proxy_wallet_address(signer.address,PRODUCTION.wallet_derivation)
    for name in p['files']:
        p['files'][name]=str(base/name)
    Path(p['files']['state_dir']).mkdir(mode=0o700)
    payloads={'signing_key':'0x'+signer.key.hex(),
        'l2_credentials':json.dumps(dict(key='SECRET_KEY',secret='c2VjcmV0',passphrase='SECRET_PHRASE',**p['identity'])),
        'relayer_credentials':json.dumps(dict(key='SECRET_RELAYER',address=signer.address)),
        'run_env':'LP_V2_MARKET='+p['trading']['market_condition_id']+'\nLP_V2_CAPITAL_USDC=100\nLP_V2_CODE_SHA='+p['release']['expected_sha']+'\n'}
    for name,value in payloads.items():
        path=Path(p['files'][name]); path.write_text(value); path.chmod(0o600)
    return p


@pytest.fixture(autouse=True)
def operator_os(monkeypatch):
    from types import SimpleNamespace
    from threading import local
    from lp_v2.live_control import privileges
    uid,gid=os.getuid(),os.getgid();state=local()
    monkeypatch.setattr(privileges,'geteuid',lambda:getattr(state,'ids',(0,0,0))[1])
    monkeypatch.setattr(privileges,'getresuid',lambda:getattr(state,'ids',(0,0,0)))
    monkeypatch.setattr(privileges,'getresgid',lambda:(0,0,0))
    monkeypatch.setattr(privileges,'getgroups',lambda:[])
    monkeypatch.setattr(privileges,'setgroups',lambda groups:None)
    monkeypatch.setattr(privileges,'setresgid',lambda *ids:None)
    monkeypatch.setattr(privileges,'setresuid',lambda *ids:setattr(state,'ids',ids))
    monkeypatch.setattr(privileges,'seteuid',lambda value:setattr(state,'ids',(getattr(state,'ids',(0,0,0))[0],value,0)))
    monkeypatch.setattr(privileges,'service_account',lambda:SimpleNamespace(pw_uid=uid,pw_gid=gid))


def call_host(payload):
    from lp_v2.live_control.host import main
    output=io.StringIO()
    code=main(stdin=io.StringIO(json.dumps(payload)),stdout=output)
    return code,json.loads(output.getvalue())


def test_check_has_no_remote_mutation_or_signing(target,monkeypatch):
    calls=[]; gets=[]
    def boundary(argv,**kw):
        calls.append(argv)
        assert argv[0] in ('systemctl','git') or 'preflight' in argv
        assert not any(v in argv for v in ('start','stop','restart','enable','daemon-reload'))
        return subprocess.CompletedProcess(argv,2,'{}','')
    def http(transport,request):
        gets.append(request.method)
        assert request.method=='GET'
        return httpx.Response(503,request=request,json={'error':'SECRET_ERROR'})
    monkeypatch.setattr(subprocess,'run',boundary)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    from polymarket import SecureClient
    monkeypatch.setattr(SecureClient,'create',lambda **kw: pytest.fail('no signing client'))
    base=Path(target['release']['root'])
    before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()}
    for damaged in (False,True):
        if damaged:
            Path(target['files']['l2_credentials']).chmod(0o644)
        code,result=call_host(dict(schema_version=1,action='check',profile=target))
        assert code==2 and result['status']!='PASS'
        assert 'files' in result['checks'] and 'public_market' in result['checks']
        assert 'SECRET' not in json.dumps(result)
    after={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before}
    assert after==before
    assert all(method=='GET' for method in gets)


def test_account_readiness_requires_authoritative_evidence(target,monkeypatch):
    signer=target['identity']['signer_address']; wallet=target['identity']['wallet_address']
    scenario={}; requests=[]
    def http(transport,request):
        requests.append(request)
        path=request.url.path
        assert request.method=='GET'
        data={'/balance-allowance':{'balance':'200000000','allowances':{
            '0xE111180000d2663C0091e4f400237545B87B996B':'200000000',
            '0xe2222d279d744050d28e00520010520000310F59':'200000000'}},
            '/auth/ban-status/closed-only':{'closed_only':False},
            '/data/orders':{'data':[],'next_cursor':'LTE='},
            '/api/geoblock':{'blocked':False,'country':'US'},'/transactions':[]}
        payload=scenario.get(path,data.get(path,{}))
        return httpx.Response(scenario.get('status:'+path,200),request=request,json=payload)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    monkeypatch.setattr(subprocess,'run',lambda argv,**kw: subprocess.CompletedProcess(argv,2,'{}',''))
    def checks():
        return call_host(dict(schema_version=1,action='check',profile=target))[1]['checks']
    good=checks()
    for key in ('balance','allowance','closed_only','relayer_auth','private_identity'):
        assert good[key]['status']=='PASS'
    assert good['relayer_merge']['status']=='UNKNOWN'
    relayer=[r for r in requests if r.url.path=='/transactions']
    assert relayer and relayer[0].headers['RELAYER_API_KEY']=='SECRET_RELAYER'
    assert relayer[0].headers['RELAYER_API_KEY_ADDRESS']==signer
    for path,data,key in [('/balance-allowance',{},'balance'),('/balance-allowance',{'balance':True,'allowances':{}},'balance'),
        ('/balance-allowance',{'balance':'200000000','allowances':{}},'allowance'),
        ('/auth/ban-status/closed-only',{},'closed_only'),('/auth/ban-status/closed-only',{'closed_only':'false'},'closed_only'),
        ('/transactions',{'deployed':True},'relayer_auth'),('/transactions',[{'owner':'0x'+'9'*40}],'relayer_auth')]:
        scenario.clear(); scenario[path]=data
        assert checks()[key]['status'] in ('UNKNOWN','FAIL')
    for status in (401,403,500):
        scenario.clear(); scenario['status:/transactions']=status
        assert checks()['relayer_auth']['status']==('FAIL' if status in (401,403) else 'UNKNOWN')
    scenario.clear()
    credentials=Path(target['files']['l2_credentials']); content=json.loads(credentials.read_text()); content['wallet_address']='0x'+'9'*40
    credentials.write_text(json.dumps(content))
    assert checks()['private_identity']['status']=='FAIL'
    assert 'SECRET' not in json.dumps(checks())
    assert not any(r.url.path=='/deployed' for r in requests)


@pytest.fixture
def combined_world(target, config_home, monkeypatch):
    """Real dispatcher/SDK/locks, replacing only host OS/SSH/systemd/HTTP facts."""
    from datetime import datetime, UTC, timedelta
    import contextlib
    import importlib.util
    import stat
    import threading
    from types import SimpleNamespace
    from tests.test_live_control_profiles import known_ssh
    from tests.test_readonly_sdk import taker_fee_wire
    from lp_v2.cli import main as cli_main
    from lp_v2.live_process import ProcessLock
    from lp_v2.journal import Journal
    from lp_v2 import cli
    import datetime as datetime_module
    root=Path(target['release']['root']);release=(root/'current').resolve()
    source_path='src/lp_v2/cli.py'
    source=subprocess.check_output(['git','show','HEAD:'+source_path],text=True)
    (release/'src/lp_v2').mkdir(parents=True)
    (release/source_path).write_text(source)
    (config_home/'profiles/tokyo.json').write_text(json.dumps(target))
    world=SimpleNamespace(profile=target,clock=datetime.now(UTC),readonly=True,lease=None,
        mutations=[],requests=[],http_requests=[],authority=True,uid=os.getuid(),gid=os.getgid(),
        process_identity_valid=True,http_overrides={},stale_public=False,context=threading.local())
    world.context.ids=(0,0,0)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return world.clock.astimezone(tz) if tz else world.clock.replace(tzinfo=None)
    monkeypatch.setattr(cli,'datetime',Clock)
    monkeypatch.setattr(datetime_module,'datetime',Clock)
    import time
    monkeypatch.setattr(time,'sleep',lambda seconds:setattr(world,'clock',world.clock+timedelta(seconds=seconds)))
    if importlib.util.find_spec('lp_v2.live_control.privileges'):
        from lp_v2.live_control import privileges
        monkeypatch.setattr(privileges,'geteuid',lambda:world.context.ids[1] if world.authority else world.uid)
        monkeypatch.setattr(privileges,'getresuid',lambda:world.context.ids)
        monkeypatch.setattr(privileges,'getresgid',lambda:(0,0,0))
        monkeypatch.setattr(privileges,'getgroups',lambda:[])
        monkeypatch.setattr(privileges,'setgroups',lambda groups:None)
        monkeypatch.setattr(privileges,'setresgid',lambda *ids:None)
        monkeypatch.setattr(privileges,'setresuid',lambda *ids:setattr(world.context,'ids',ids))
        monkeypatch.setattr(privileges,'seteuid',lambda uid:setattr(world.context,'ids',(world.context.ids[0],uid,world.context.ids[2])))
        monkeypatch.setattr(privileges,'service_account',lambda:SimpleNamespace(pw_uid=world.uid,pw_gid=world.gid))
    original_stat=Path.stat;original_read_bytes=Path.read_bytes;original_read_text=Path.read_text
    def proc_stat(path,*a,**kw):
        if str(path).startswith('/proc/'):
            return SimpleNamespace(st_uid=world.uid if world.process_identity_valid else world.uid+1)
        return original_stat(path,*a,**kw)
    def proc_bytes(path,*a,**kw):
        if str(path)==f'/proc/{os.getpid()}/cmdline':return b'lp_v2\0'
        return original_read_bytes(path,*a,**kw)
    def proc_text(path,*a,**kw):
        if str(path)=='/proc/sys/kernel/random/boot_id':return 'synthetic-boot'
        if str(path)==f'/proc/{os.getpid()}/stat':return '1 (lp-v2) '+' '.join(['S']+['0']*18+['123'])
        return original_read_text(path,*a,**kw)
    monkeypatch.setattr(Path,'stat',proc_stat)
    monkeypatch.setattr(Path,'read_bytes',proc_bytes)
    monkeypatch.setattr(Path,'read_text',proc_text)
    def properties(unit):
        live=unit=='lp-v2-live.service';pid=os.getpid() if world.lease else 0
        return dict(MainPID=str(pid if live else 11223 if world.readonly else 0),
            ActiveState='active' if (world.lease if live else world.readonly) else 'inactive',User='lp-v2',
            ExecStart=str(root/'current/deploy/run-live'),ExecStop=str(root/'current/.venv/bin/lp-v2')+' stop --state-dir '+target['files']['state_dir'],
            EnvironmentFiles=target['files']['run_env'],Conflicts='lp-v2.service',TimeoutStopUSec='2min 30s',Restart='no',UnitFileState='disabled')
    def child(request):
        # OS identity facade applies only inside the real short-lived target invocation.
        world.context.ids=(0,0,0)
        output=io.StringIO()
        from lp_v2.live_control.host import main
        code=main(stdin=io.StringIO(json.dumps(request)),stdout=output)
        return code,output.getvalue()
    def command(argv,**kw):
        if known_ssh(argv):return known_ssh(argv)
        if argv[0]=='ssh':
            request=json.loads(kw['input']);world.requests.append(request)
            if not world.authority:
                return subprocess.CompletedProcess(argv,1,'','sudo: denied')
            code,output=child(request)
            return subprocess.CompletedProcess(argv,code,output,'')
        if argv[0]=='git':
            text=source_path+'\n' if 'ls-tree' in argv else source
            return subprocess.CompletedProcess(argv,0,text,'')
        if 'preflight' in argv:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):code=cli_main(argv[1:])
            return subprocess.CompletedProcess(argv,code,output.getvalue(),'')
        if argv[0]=='systemctl':
            action,unit=argv[1:3]
            assert unit in ('lp-v2.service','lp-v2-live.service')
            if action=='show':return subprocess.CompletedProcess(argv,0,'\n'.join(k+'='+v for k,v in properties(unit).items()),'')
            assert action in ('start','stop')
            if importlib.util.find_spec('lp_v2.live_control.privileges'):
                from lp_v2.live_control import privileges
                assert privileges.geteuid()==0,'systemd must use operator privilege'
            world.mutations.append((action,unit))
            if unit=='lp-v2.service':world.readonly=action=='start'
            elif action=='start':
                assert world.lease is None,'one executor only'
                previous_umask=os.umask(0o077)
                try:
                    world.lease=ProcessLock(target['files']['state_dir']).__enter__()
                    journal=Journal(Path(target['files']['state_dir'])/'journal.sqlite')
                    if not journal.events('identity'):
                        journal.append('identity',dict(owner='lp-v2:'+target['identity']['wallet_address'].lower(),condition_id=target['trading']['market_condition_id'],token_ids=('101','102')))
                    journal.append('occupation',dict(at=world.clock));journal.close()
                finally:os.umask(previous_umask)
            elif world.lease is not None:
                journal=Journal(Path(target['files']['state_dir'])/'journal.sqlite')
                journal.append('stop_result',dict(at=world.clock,exit_code=0,orders=[],pending=[],positions=[],merges=[],inventory=[],reconciliation='SUCCESS',accounting_as_of=world.clock));journal.close()
                world.lease.finish(0);world.lease.__exit__(None,None,None);world.lease=None
            return subprocess.CompletedProcess(argv,0,'','')
        if argv[0]=='ss':
            pid=os.getpid() if world.lease else 11223 if world.readonly else None
            return subprocess.CompletedProcess(argv,0,'' if pid is None else f'LISTEN 127.0.0.1:8765 users:(("lp-v2",pid={pid},fd=3))','')
        pytest.fail('unexpected external command: '+repr(argv))
    def http(transport,request):
        world.http_requests.append(request)
        assert request.method=='GET'
        path=request.url.path
        if path in world.http_overrides:
            status,data=world.http_overrides[path];return httpx.Response(status,json=data,request=request)
        if path=='/markets/keyset':data={'markets':[taker_fee_wire()],'next_cursor':None}
        elif path=='/book':
            token=request.url.params['token_id'];bid,ask=('0.46','0.48') if token=='101' else ('0.51','0.53')
            data=dict(market=target['trading']['market_condition_id'],asset_id=token,
                timestamp=str(int((world.clock-timedelta(seconds=300 if world.stale_public else 1)).timestamp()*1000)),
                bids=[dict(price=bid,size='100')],asks=[dict(price=ask,size='100')],min_order_size='1',tick_size='0.01',neg_risk=False,hash='fixture')
        elif path.startswith('/markets/'):data={'in_play':False}
        elif path=='/balance-allowance':data={'balance':'200000000','allowances':{
            '0xE111180000d2663C0091e4f400237545B87B996B':'200000000','0xe2222d279d744050d28e00520010520000310F59':'200000000'}}
        elif path=='/auth/ban-status/closed-only':data={'closed_only':False}
        elif path=='/data/orders':data={'data':[],'next_cursor':'LTE='}
        elif path=='/api/geoblock':data={'blocked':False,'country':'US'}
        elif path=='/transactions':
            if getattr(world,'advance_on_relayer',False):world.clock+=timedelta(seconds=120)
            data=[]
        elif path=='/summary':
            world.clock+=timedelta(seconds=10)
            data=dict(mode='live' if world.lease else 'read_only',as_of=world.clock.isoformat(),capital_usdc=target['trading']['capital_usdc'],
                run=dict(code_sha=target['release']['expected_sha'],wallet_tail=target['identity']['wallet_address'][-4:]),
                accounting=dict(status='SUCCESS',as_of=world.clock.isoformat()),freshness=dict(decision_as_of=world.clock.isoformat()),
                markets=[dict(condition_id=target['trading']['market_condition_id'],eligible=True,skip_reason=None)])
        else:pytest.fail('unexpected HTTP path '+path)
        return httpx.Response(200,json=data,request=request)
    monkeypatch.setattr(subprocess,'run',command)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    world.invoke=child
    yield world
    if world.lease is not None:world.lease.__exit__(None,None,None)


def test_missing_operator_authority_preserves_readonly(combined_world,monkeypatch,capsys):
    import hashlib
    from datetime import datetime,UTC
    from lp_v2.cli import main
    world=combined_world;world.authority=False
    path=Path(world.profile['files']['run_env']);before=path.read_bytes()
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
    capsys.readouterr()
    payload=dict(schema_version=1,action='prepare',profile=world.profile,action_id='b'*32,
        changes=dict(LP_V2_MARKET=world.profile['trading']['market_condition_id'],LP_V2_CAPITAL_USDC='100',LP_V2_CODE_SHA=world.profile['release']['expected_sha']),
        confirmation=dict(operation='prepare',profile_sha256=hashlib.sha256(json.dumps(world.profile,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            issued_at=world.clock.isoformat(),window_end='2099-01-01T00:00:00+00:00'))
    code,output=world.invoke(payload)
    assert code==2 and json.loads(output)['status']!='PASS'
    assert world.readonly is True and world.mutations==[] and path.read_bytes()==before
    assert not (Path(world.profile['files']['state_dir'])/'control-actions').exists()


def test_operator_route_preserves_service_owned_state(combined_world,monkeypatch,capsys):
    from lp_v2.cli import main
    world=combined_world;profile=world.profile
    monkeypatch.setattr('builtins.input',lambda _: 'on')
    credentials={Path(profile['files'][key]):Path(profile['files'][key]).read_bytes() for key in ('signing_key','l2_credentials','relayer_credentials')}
    # A release owned by the application user is never a root execution target.
    from lp_v2.live_control.privileges import BOOTSTRAP
    import sys
    executions=[]
    with monkeypatch.context() as boundary:
        boundary.setattr(os,'geteuid',lambda:0)
        boundary.setattr(os,'execv',lambda *args:executions.append(args))
        boundary.setattr(sys,'argv',['bootstrap',profile['release']['root'],profile['release']['expected_sha']])
        with pytest.raises(PermissionError):exec(BOOTSTRAP,{})
    assert executions==[]
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    capsys.readouterr()
    state=Path(profile['files']['state_dir']);journal=(state/'journal.sqlite').read_bytes()
    for path in state.rglob('*'):
        info=path.stat()
        assert info.st_uid==world.uid
        assert info.st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    count=len(world.mutations)
    for change in ('action','unit','path'):
        request=dict(schema_version=1,action='off',profile=copy.deepcopy(profile),run_id=world.lease.record['run_id'],action_id='d'*32)
        if change=='action':request['action']='restart'
        if change=='unit':request['profile']['services']['live_unit']='other.service'
        if change=='path':request['profile']['files']['state_dir']=str(state.parent/'foreign-state')
        code,_=world.invoke(request)
        assert code==2 and len(world.mutations)==count
    world.process_identity_valid=False
    code,output=world.invoke(dict(schema_version=1,action='off',profile=profile,run_id=world.lease.record['run_id'],action_id='e'*32))
    assert code==2 and len(world.mutations)==count,'foreign process must not be stopped'
    assert (state/'journal.sqlite').read_bytes()==journal
    assert all(path.read_bytes()==content for path,content in credentials.items())
    world.process_identity_valid=True
    assert main(['live','off'])==0
    capsys.readouterr()

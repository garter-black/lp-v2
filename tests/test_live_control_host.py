"""Target helper uses real parsing/files and fake external HTTP/process boundaries."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest
from tests.test_live_control_profiles import profile


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

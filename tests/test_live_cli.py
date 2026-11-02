from dataclasses import replace
from decimal import Decimal as D
import json
import os
import pytest
from tests.test_live_sdk import CID,AT,WALLET,SIGNER
from lp_v2.cli import parse_run


def live_args(tmp_path):
    signing=tmp_path/'signing'
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    synthetic=Account.from_key(bytes([7])*32)
    wallet=derive_proxy_wallet_address(synthetic.address,PRODUCTION.wallet_derivation)
    signing.write_text('0x'+synthetic.key.hex())
    signing.chmod(0o600)
    credentials=tmp_path/'l2.json'
    credentials.write_text(json.dumps(dict(key='synthetic',secret='c3ludGhldGlj',passphrase='synthetic',
        signer_address=synthetic.address,wallet_address=wallet,signature_type=1)))
    credentials.chmod(0o600)
    args=['run','--live','--capital-usdc','100','--market',CID,'--private-key-file',str(signing),
          '--state-dir',str(tmp_path/'state'),'--credentials-file',str(credentials)]
    return args,signing


def test_live_validation_and_secret_boundary(tmp_path,capsys):
    args,signing=live_args(tmp_path)
    config=parse_run(args)
    assert config.live is True and config.market==CID
    assert config.capital_usdc==D('100')
    assert parse_run(['run','--capital-usdc','100']).live is False
    for flag in ('--market','--private-key-file','--state-dir','--credentials-file'):
        index=args.index(flag)
        with pytest.raises(SystemExit):
            parse_run(args[:index]+args[index+2:])
    invalid=args.copy();invalid[invalid.index('--market')+1]='alien'
    with pytest.raises(SystemExit):
        parse_run(invalid)
    signing.chmod(0o644)
    with pytest.raises(SystemExit):
        parse_run(args)
    signing.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(signing)
    invalid=args.copy();invalid[invalid.index('--private-key-file')+1]=str(link)
    with pytest.raises(SystemExit):
        parse_run(invalid)
    assert 'synthetic' not in repr(config)
    assert 'synthetic' not in str(capsys.readouterr())


def test_stop_signals_owned_process(tmp_path,monkeypatch):
    from lp_v2.live_process import ProcessLock,stop_process
    import lp_v2.live_process as process
    signals=[]
    monkeypatch.setattr(process,'process_identity',lambda pid:'boot:123' if pid==os.getpid() else None)
    monkeypatch.setattr(os,'pidfd_open',lambda pid,flags=0:99,raising=False)
    monkeypatch.setattr(process.signal,'pidfd_send_signal',lambda fd,sig,*args:signals.append((fd,sig)),raising=False)
    original_close=os.close
    monkeypatch.setattr(os,'close',lambda fd:None if fd==99 else original_close(fd))
    state=tmp_path/'state'
    with ProcessLock(state) as lease:
        with pytest.raises(BlockingIOError):
            with ProcessLock(state):
                pass
        tick=[0]
        def wait(seconds):
            tick[0]+=seconds
            lease.finish(0)
        assert stop_process(state,monotonic=lambda:tick[0],wait=wait)==0
        assert len(signals)==1
        metadata=json.loads((state/'process.json').read_text())
        metadata['identity']='stale'
        (state/'process.json').write_text(json.dumps(metadata))
        assert stop_process(state)==1
        assert len(signals)==1
        metadata['identity']='boot:123';metadata['pid']=9999
        (state/'process.json').write_text(json.dumps(metadata))
        assert stop_process(state)==1
        assert len(signals)==1
    assert stop_process(state)==0
    assert stop_process(tmp_path/'missing')==1
    from lp_v2.cli import main
    assert main(['stop','--state-dir',str(tmp_path/'missing')])==1


@pytest.mark.parametrize('unresolved',[False,True])
def test_live_server_lifecycle(tmp_path,monkeypatch,unresolved):
    from threading import Event,enumerate as threads
    from time import monotonic,sleep
    import signal
    from lp_v2.status_server import serve
    from lp_v2 import status_server
    from tests.test_live_runtime import world
    from tests.test_status_server import request
    runner,ex,live,journal,obs=world(tmp_path)
    runner.config=replace(runner.config,live=True)
    runner.stop_timeout_s=.05
    ready=Event()
    original=ex.sdk.post_order.side_effect
    def post(signed):
        if unresolved:
            ready.set()
            raise TimeoutError('unknown')
        result=original(signed)
        if len(ex.posts)==2:
            ready.set()
        return result
    ex.sdk.post_order.side_effect=post
    events=[]
    class Server:
        def __init__(self,address,handler):
            assert address==('127.0.0.1',8765)
            self.handler=handler
        def __enter__(self):
            return self
        def handle_request(self):
            assert ready.wait(2)
            deadline=monotonic()+2
            while runner.snapshot()['as_of']=='UNKNOWN' and monotonic()<deadline:
                sleep(.001)
            body=request(self.handler)[2]
            assert body['mode']=='live' and body['as_of']!='UNKNOWN'
            assert request(self.handler,path='/healthz')[2]['capital_usdc']=='100'
            assert request(self.handler,method='POST')[0]==405
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
            events.append('signal')
        def __exit__(self,*args):
            events.append('closed')
    monkeypatch.setattr(status_server,'HTTPServer',Server)
    assert serve(runner.config,runner=runner)==(1 if unresolved else 0)
    assert events==['signal','closed']
    assert not any(t.name=='lp-v2-live' for t in threads())
    assert ex.sdk.close.call_count==1
    # Journal created on caller thread was actually used by the production thread.
    import sqlite3
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='stop_result'").fetchone()[0]==1


def test_live_factory_uses_pinned_sdk_and_frozen_identity(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import httpx
    from lp_v2.live_runtime import create_live_runner
    from polymarket import SecureClient
    from tests.test_readonly_sdk import external_sdk,market_wire,CID as market_id
    from tests.fakes.live_exchange import Exchange
    args,_=live_args(tmp_path)
    args[args.index('--market')+1]=market_id
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=SIGNER)))
    relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay)]
    config=replace(parse_run(args),started_at=AT)
    (tmp_path/'state').mkdir(mode=0o700)
    ex=Exchange()
    ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=config.credentials.wallet_address;ex.sdk.signer=config.credentials.signer_address
    object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None))
    seen=[]
    def create(**kwargs):
        seen.append(kwargs)
        assert kwargs['credentials'].key=='synthetic'
        assert kwargs['api_key'].key=='synthetic-relayer'
        assert kwargs['wallet']==config.credentials.wallet_address
        return ex.sdk
    monkeypatch.setattr(SecureClient,'create',create)
    public,_=external_sdk(rows=[market_wire()])
    http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False,'blocked':False,'country':'US'})))
    runner=create_live_runner(config,public=public,http=http)
    assert runner.condition_id==market_id and runner.sdk.token_ids==('101','102')
    assert len(seen)==1
    runner.sdk.close();runner.observations.close()


@pytest.mark.parametrize('invalid',['key','signer','wallet','deposit','type'])
def test_factory_rejects_identity_before_sdk_wallet_setup(tmp_path,monkeypatch,invalid):
    from lp_v2.live_runtime import create_live_runner
    from polymarket import SecureClient
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_beacon_deposit_wallet_address
    args,signing=live_args(tmp_path)
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=SIGNER)));relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay)]
    config=replace(parse_run(args),started_at=AT)
    c=config.credentials
    if invalid=='key':
        signing.write_text('invalid signing material')
    elif invalid=='signer':
        config=replace(config,credentials=replace(c,signer_address=SIGNER))
    elif invalid=='wallet':
        config=replace(config,credentials=replace(c,wallet_address=WALLET))
    elif invalid=='deposit':
        config=replace(config,credentials=replace(c,wallet_address=derive_beacon_deposit_wallet_address(c.signer_address,PRODUCTION.wallet_derivation)))
    else:
        config=replace(config,credentials=replace(c,signature_type=0))
    constructed=[]
    def create(**kw):
        constructed.append(kw)
        raise AssertionError('wallet constructor must not be reached')
    monkeypatch.setattr(SecureClient,'create',create)
    import httpx
    from tests.test_readonly_sdk import external_sdk,market_wire,CID as market_id
    public,_=external_sdk(rows=[market_wire()])
    http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False})))
    with pytest.raises(ValueError,match='identity|signing'):
        create_live_runner(replace(config,market=market_id),public=public,http=http)
    assert constructed==[]


def test_stop_after_executor_exit_is_idempotent(tmp_path,monkeypatch):
    from lp_v2.live_process import ProcessLock,stop_process
    import lp_v2.live_process as process
    monkeypatch.setattr(process,'process_identity',lambda pid:'boot:123' if pid==os.getpid() else None)
    signals=[]
    monkeypatch.setattr(os,'pidfd_open',lambda *args:signals.append('opened'),raising=False)
    state=tmp_path/'state'
    with ProcessLock(state) as lease:
        lease.finish(1)
        before=(state/'stop-result.json').read_bytes()
        metadata=json.loads((state/'process.json').read_text())
        for invalid in (dict(metadata,identity='stale'),dict(metadata,pid=9999)):
            (state/'process.json').write_text(json.dumps(invalid))
            assert stop_process(state)==1
        (state/'process.json').write_text('{')
        assert stop_process(state)==1
        (state/'process.json').write_text(json.dumps(metadata))
    assert stop_process(state)==0
    assert stop_process(state)==0
    assert (state/'stop-result.json').read_bytes()==before
    assert signals==[]
    assert stop_process(tmp_path/'missing')==1
    (state/'executor.lock').chmod(0o644)
    assert stop_process(state)==1
    assert signals==[]

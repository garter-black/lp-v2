"""Short-lived target helper. JSON stdin only; no HTTP mutation surface."""
import json
from pathlib import Path
import subprocess
import sys
from lp_v2.live_control.profiles import validate, read_json, secure_directory, now
from lp_v2.cli import owner_file


def fact(status, reason):
    return dict(status=status,reason=reason,as_of=now())


def run(argv, timeout=15):
    return subprocess.run(argv,text=True,capture_output=True,timeout=timeout,check=False)


def check(profile):
    checks={}
    root=Path(profile['release']['root']); expected=profile['release']['expected_sha']
    try:
        current=(root/'current').resolve(strict=True)
        good=current==root/'releases'/expected and (current/'COMPLETE').read_text().strip()==expected
        checks['release']=fact('PASS' if good else 'FAIL','RELEASE_IDENTITY')
    except Exception:
        checks['release']=fact('UNKNOWN','RELEASE_UNAVAILABLE')
    try:
        for name,path in profile['files'].items():
            if name=='state_dir': secure_directory(Path(path))
            else:
                with owner_file(path): pass
        checks['files']=fact('PASS','OWNED_PRIVATE_FILES')
    except Exception:
        checks['files']=fact('FAIL','FILE_PERMISSIONS')
    try:
        result=run([str(root/'current/.venv/bin/lp-v2'),'preflight','--market',profile['trading']['market_condition_id'],
                    '--capital-usdc',profile['trading']['capital_usdc'],'--code-sha',expected])
        public=json.loads(result.stdout)
    except Exception:
        public={}
    checks.update(filesystem_checks(profile))
    checks.update(account_checks(profile))
    checks['public_market']=fact('UNKNOWN','PREFLIGHT_CAPABILITY_UNVERIFIED')
    return dict(schema_version=1,status='UNKNOWN',as_of=now(),checks=checks,public_market=safe_public(profile,public))



def account_checks(profile):
    from lp_v2.readonly_sdk import L2Credentials, ReadOnlySDK
    from datetime import datetime, UTC
    names=('private_identity','balance','allowance','closed_only','legacy_orders','region','relayer_auth','relayer_merge')
    result={name:fact('UNKNOWN','ACCOUNT_EVIDENCE_MISSING') for name in names}
    try:
        credentials=L2Credentials(**read_json(profile['files']['l2_credentials']))
        from eth_account import Account
        from polymarket._internal.wallet import classify_wallet_type
        from polymarket.environments import PRODUCTION
        with owner_file(profile['files']['signing_key']) as source:
            signer=Account.from_key(source.read().strip()).address
        identity=profile['identity']
        valid=(type(credentials.signature_type) is int and credentials.signature_type==1
               and credentials.signer_address.lower()==signer.lower()==identity['signer_address'].lower()
               and credentials.wallet_address.lower()==identity['wallet_address'].lower()
               and classify_wallet_type(signer=signer,wallet=credentials.wallet_address,config=PRODUCTION.wallet_derivation)=='POLY_PROXY')
        result['private_identity']=fact('PASS' if valid else 'FAIL','FULL_SIGNER_PROXY_IDENTITY')
        if not valid:
            return result
        relayer=read_json(profile['files']['relayer_credentials'])
        sdk=ReadOnlySDK(credentials=credentials)
        try:
            result.update(sdk.read_account_readiness(datetime.now(UTC),capital_usdc=profile['trading']['capital_usdc'],identity=identity,relayer=relayer))
        finally:
            sdk.close()
    except Exception:
        pass
    return result


def confirmed(request,operation):
    from lp_v2.live_control.profiles import fingerprint,timestamp
    from lp_v2.live_control.workflow import fresh
    from datetime import datetime,UTC
    proof=request.get('confirmation',{})
    return (proof.get('operation')==operation and proof.get('profile_sha256')==fingerprint(request['profile'])
            and fresh(proof.get('issued_at')) and timestamp(proof.get('window_end'))>datetime.now(UTC))


def prepare(request):
    from lp_v2.live_control.profiles import atomic_bytes
    if not confirmed(request,'prepare'):
        return dict(status='UNKNOWN',reason='CONFIRMATION_REQUIRED')
    profile=request['profile']; changes=request.get('changes')
    expected=dict(LP_V2_CAPITAL_USDC=profile['trading']['capital_usdc'],
                  LP_V2_MARKET=profile['trading']['market_condition_id'],LP_V2_CODE_SHA=profile['release']['expected_sha'])
    if changes!=expected:
        return dict(status='UNKNOWN',reason='PREPARATION_INVALID')
    path=Path(profile['files']['run_env'])
    with owner_file(path) as source:
        old=source.read()
    lines=[line for line in old.splitlines() if line.split('=',1)[0] not in expected]
    value=('\n'.join(lines+[key+'='+value for key,value in expected.items()])+'\n').encode()
    atomic_bytes(path.with_name(path.name+'.bak'),old.encode())
    atomic_bytes(path,value)
    with owner_file(path) as source:
        if source.read().encode()!=value:
            return dict(status='UNKNOWN',reason='PREPARATION_READBACK_FAILED')
    return dict(status='PASS',reason='ENV_PREPARED')



def process_stopped(profile):
    import os,fcntl
    try:
        with owner_file(Path(profile['files']['state_dir'])/'executor.lock') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            return True
    except Exception:
        return False


def journal_rows(profile,kind):
    import sqlite3
    from lp_v2.journal import decode
    path=Path(profile['files']['state_dir'])/'journal.sqlite'
    # No Journal constructor, pragmas or database creation on observational reads.
    with owner_file(path): pass
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        return [(seq,decode(payload)) for seq,payload in db.execute('SELECT seq,payload FROM events WHERE kind=? ORDER BY seq',(kind,))]
    finally:
        db.close()


def inventory_report(rows):
    from decimal import Decimal
    result=[]
    for row in rows:
        values={}
        for key in ('quantity','cost_usdc','valuation_usdc'):
            value=row.get(key)
            if value is None: continue
            number=Decimal(str(value))
            if not number.is_finite() or number<0: raise ValueError()
            values[key]=str(number)
        result.append(values)
    return result


def off(request):
    from lp_v2.live_control.profiles import timestamp
    profile=request['profile']; state=Path(profile['files']['state_dir'])
    record=read_json(state/'live-control-run.json')
    if record['profile']!=profile or record['run_id']!=request.get('run_id'):
        return dict(status='UNKNOWN',reason='FROZEN_RUN_MISMATCH')
    process=read_json(state/'process.json')
    if process['run_id']!=record['run_id']:
        return dict(status='UNKNOWN',reason='PROCESS_RUN_MISMATCH')
    result=run(['systemctl','stop',profile['services']['live_unit']],timeout=160)
    stopped=process_stopped(profile)
    port=run(['ss','-H','-ltn','sport = :8765'])
    released=port.returncode==0 and port.stdout.strip()==''
    drained='UNKNOWN'; inventory=[]
    try:
        seq,drain=journal_rows(profile,'stop_result')[-1]
        inventory=inventory_report(drain['inventory'])
        final=read_json(state/'stop-result.json')
        if (final['run_id']==record['run_id'] and type(final['exit_code']) is int and final['exit_code']==0
                and type(drain['exit_code']) is int and drain['exit_code']==0 and drain['reconciliation']=='SUCCESS'
                and seq>record['journal_seq'] and timestamp(drain['at'].isoformat())>timestamp(record['started_at'])
                and not drain['pending'] and all(order.authoritative and order.status.value in ('filled','cancelled') for order in drain['orders'])):
            drained='PASS'
        elif final['run_id']==record['run_id'] and final['exit_code']!=0:
            drained='FAIL'
    except Exception:
        pass
    restored=False
    if stopped and released:
        restore=run(['systemctl','start',profile['services']['readonly_unit']])
        if restore.returncode==0:
            try:
                import httpx
                from lp_v2.live_control.workflow import fresh
                with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
                    response=client.get('http://127.0.0.1:8765/summary'); response.raise_for_status(); row=response.json()
                restored=(row['mode']=='read_only' and fresh(row['as_of']) and row['run']['code_sha']==profile['release']['expected_sha']
                          and row['capital_usdc']==profile['trading']['capital_usdc']
                          and row['run']['wallet_tail']==profile['identity']['wallet_address'][-4:])
            except Exception:
                pass
    return dict(status='PASS' if result.returncode==0 and stopped and released and drained=='PASS' and restored else 'UNKNOWN',
        executor_stopped=stopped,port_released=released,drain=drained,readonly_restored=restored,inventory=inventory,run_id=record['run_id'])



def mutate(request):
    import os,fcntl,re,stat
    from lp_v2.live_control.profiles import atomic_json,fingerprint
    profile=request['profile']; action=request['action']; action_id=request.get('action_id')
    if not isinstance(action_id,str) or not re.fullmatch('[0-9a-f]{32}',action_id):
        return dict(status='UNKNOWN',reason='ACTION_INVALID')
    if action in ('on','prepare') and not confirmed(request,action):
        return dict(status='UNKNOWN',reason='CONFIRMATION_REQUIRED')
    state=Path(profile['files']['state_dir']);secure_directory(state)
    fd=os.open(state/'control.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError()
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='BUSY',reason='CONTROL_BUSY')
        directory=state/'control-actions';directory.mkdir(mode=0o700,exist_ok=True);secure_directory(directory)
        path=directory/(action_id+'.json')
        if path.exists() or path.is_symlink():
            prior=read_json(path)
            if prior['profile_sha256']!=fingerprint(profile) or prior['action']!=action:
                return dict(status='UNKNOWN',reason='ACTION_IDENTITY_MISMATCH')
            return prior['result']
        record=dict(action=action,profile_sha256=fingerprint(profile),result=dict(status='UNKNOWN',reason='ACTION_PENDING'),as_of=now())
        atomic_json(path,record)
        try:
            result=off(request) if action=='off' else start(request) if action=='on' else prepare(request)
        except Exception:
            result=dict(status='UNKNOWN',reason='ACTION_OUTCOME_UNKNOWN')
        record['result']=result;atomic_json(path,record)
        return result
    finally:
        os.close(fd)


def action_result(request):
    import re
    from lp_v2.live_control.profiles import fingerprint
    action_id=request.get('action_id')
    if not isinstance(action_id,str) or not re.fullmatch('[0-9a-f]{32}',action_id):
        raise ValueError()
    profile=request['profile']
    state=Path(profile['files']['state_dir']);secure_directory(state);secure_directory(state/'control-actions')
    record=read_json(state/'control-actions'/(action_id+'.json'))
    if record['profile_sha256']!=fingerprint(profile):
        raise ValueError()
    return record['result']



def env_values(profile):
    values={}
    with owner_file(profile['files']['run_env']) as source:
        for line in source:
            line=line.strip()
            if not line or line.startswith('#'): continue
            key,separator,value=line.partition('=')
            if not separator or key in values: raise ValueError()
            values[key]=value
    return values


def expected_env(profile):
    return dict(LP_V2_CAPITAL_USDC=profile['trading']['capital_usdc'],LP_V2_MARKET=profile['trading']['market_condition_id'],LP_V2_CODE_SHA=profile['release']['expected_sha'])


def system_properties(unit):
    response=run(['systemctl','show',unit,'--property=MainPID,ActiveState,User,ExecStart,ExecStop,EnvironmentFiles,Conflicts,TimeoutStopUSec,Restart,UnitFileState'])
    if response.returncode!=0: raise ValueError()
    return dict(line.split('=',1) for line in response.stdout.splitlines() if '=' in line)


def filesystem_checks(profile):
    checks={}
    root=Path(profile['release']['root']); current=root/'current'
    try:
        listing=run(['git','-C',str(root/'repo'),'ls-tree','-r','--name-only',profile['release']['expected_sha'],'src/lp_v2'])
        paths=listing.stdout.splitlines()
        if listing.returncode or not paths: raise ValueError()
        import importlib.util
        good=True
        for name in paths:
            if not name.endswith('.py'): continue
            reference=run(['git','-C',str(root/'repo'),'show',profile['release']['expected_sha']+':'+name])
            if reference.returncode: raise ValueError()
            module=name[4:-3].replace('/','.')
            if module.endswith('.__init__'): module=module[:-9]
            spec=importlib.util.find_spec(module)
            good=good and (current/name).read_text()==reference.stdout and spec is not None and Path(spec.origin).read_text()==reference.stdout
        checks['artifacts']=fact('PASS' if good else 'FAIL','SOURCE_RUNTIME_IDENTITY')
    except Exception:
        checks['artifacts']=fact('UNKNOWN','ARTIFACT_PROOF_MISSING')
    try:
        actual=env_values(profile); expected=expected_env(profile)
        good=all(actual.get(key)==value for key,value in expected.items())
        checks['run_env']=fact('PASS' if good else 'FAIL','ENV_EXPECTATION_DIFFERS')
    except Exception:
        checks['run_env']=fact('UNKNOWN','ENV_UNAVAILABLE')
    state=Path(profile['files']['state_dir'])
    try:
        secure_directory(state)
        if (state/'journal.sqlite').exists():
            identities=journal_rows(profile,'identity')
            good=bool(identities) and all(row['owner']=='lp-v2:'+profile['identity']['wallet_address'].lower()
                and row['condition_id']==profile['trading']['market_condition_id'] for _,row in identities)
            checks['state_identity']=fact('PASS' if good else 'FAIL','JOURNAL_FROZEN_IDENTITY')
        else:
            checks['state_identity']=fact('PASS','EXPLICIT_NEW_STATE')
    except Exception:
        checks['state_identity']=fact('UNKNOWN','STATE_IDENTITY_UNAVAILABLE')
    try:
        live=system_properties(profile['services']['live_unit'])
        readonly=system_properties(profile['services']['readonly_unit'])
        valid=(live.get('User')=='lp-v2' and str(current/'deploy/run-live') in live.get('ExecStart','')
            and str(current/'.venv/bin/lp-v2') in live.get('ExecStop','') and ' stop ' in live.get('ExecStop','')
            and profile['files']['state_dir'] in live.get('ExecStop','')
            and profile['files']['run_env'] in live.get('EnvironmentFiles','')
            and 'lp-v2.service' in live.get('Conflicts','')
            and live.get('Restart')=='no' and live.get('UnitFileState') in ('disabled','static')
            and readonly.get('User')=='lp-v2')
        checks['service']=fact('PASS' if valid else 'FAIL','EFFECTIVE_SERVICE_CONTRACT')
        stopped=live.get('MainPID')=='0' and live.get('ActiveState') in ('inactive','failed')
        lock_free=process_stopped(profile) if (state/'executor.lock').exists() else True
        checks['single_executor']=fact('PASS' if stopped and lock_free else 'FAIL','EXECUTOR_AND_LOCK')
        port=run(['ss','-H','-ltn','sport = :8765'])
        if port.returncode: raise ValueError()
        free=not port.stdout.strip()
        owned_readonly=False
        if not free and readonly.get('MainPID') not in ('0',None) and readonly.get('ActiveState')=='active':
            # A listening port must be attributable to the exact readonly process.
            listeners=run(['ss','-H','-ltnp','sport = :8765'])
            owned_readonly=listeners.returncode==0 and ('pid='+readonly['MainPID']+',') in listeners.stdout
        checks['port']=fact('PASS' if free or owned_readonly else 'FAIL','PORT_OWNER')
    except Exception:
        for name in ('service','single_executor','port'):
            checks.setdefault(name,fact('UNKNOWN','SERVICE_EVIDENCE_MISSING'))
    return checks


def observe(request):
    profile=request['profile'];state=Path(profile['files']['state_dir'])
    process=read_json(state/'process.json')
    record=read_json(state/'live-control-run.json')
    if record['profile']!=profile or record['run_id']!=request.get('run_id') or process['run_id']!=record['run_id']:
        raise ValueError()
    from lp_v2.live_process import process_identity
    identity=process_identity(process['pid'])
    live=system_properties(profile['services']['live_unit']);readonly=system_properties(profile['services']['readonly_unit'])
    import httpx
    with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
        response=client.get('http://127.0.0.1:8765/summary');response.raise_for_status();row=response.json()
    return dict(schema_version=1,run_id=record['run_id'],executor_running=identity is not None and identity==process['identity']
                and str(process['pid'])==live.get('MainPID') and live.get('ActiveState')=='active',
                readonly_stopped=readonly.get('MainPID')=='0' and readonly.get('ActiveState')=='inactive',
                identity=profile['identity'],summary=safe_summary(row))


def start(request):
    from lp_v2.live_control.workflow import normalize_report
    from lp_v2.live_control.profiles import atomic_json,timestamp
    import time
    profile=request['profile'];state=Path(profile['files']['state_dir'])
    report=normalize_report(profile,check(profile))
    if report['status']!='PASS': return report
    if not confirmed(request,'on'): return dict(status='UNKNOWN',reason='CONFIRMATION_EXPIRED')
    existing=read_json(state/'process.json') if (state/'process.json').exists() else {}
    previous=existing.get('run_id')
    seq=max((seq for seq,_ in journal_rows(profile,'occupation')),default=0) if (state/'journal.sqlite').exists() else 0
    record=dict(profile=profile,run_id=None,started_at=now(),journal_seq=seq,action_id=request['action_id'])
    atomic_json(state/'live-control-run.json',record)
    if run(['systemctl','stop',profile['services']['readonly_unit']]).returncode:
        return dict(status='UNKNOWN',reason='READONLY_STOP_FAILED')
    # Accepted start may run even if its response is lost: the action ledger was persisted first.
    response=run(['systemctl','start',profile['services']['live_unit']],timeout=30)
    for _ in range(10):
        try:
            process=read_json(state/'process.json')
            from lp_v2.live_process import process_identity
            if process['run_id']!=previous and process_identity(process['pid']) is not None and process_identity(process['pid'])==process['identity']:
                record['run_id']=process['run_id'];atomic_json(state/'live-control-run.json',record)
                return dict(status='ACCEPTED',run_id=record['run_id'],started_at=record['started_at'])
        except Exception:
            pass
        time.sleep(1)
    return dict(status='UNKNOWN',reason='START_OUTCOME_UNKNOWN')



def import_profile(partial):
    from lp_v2.live_control.profiles import safe_name
    import re
    name=safe_name(partial['name']);root=partial['release']['root'];target=partial['target']
    if (not re.fullmatch(r'/[A-Za-z0-9_/-]+',root) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',target['ssh_host'])
            or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}',target['host_key_sha256'])):
        raise ValueError()
    profile=dict(schema_version=1,name=name,revision=1,target=target,release=dict(root=root),trading={},identity={},
        files=dict(signing_key='/etc/lp-v2/signing-key',l2_credentials='/etc/lp-v2/credentials.json',
                   relayer_credentials='/etc/lp-v2/relayer.json',run_env='/etc/lp-v2/run.env',state_dir=root+'/state'),
        services=dict(user='lp-v2',readonly_unit='lp-v2.service',live_unit='lp-v2-live.service',status_port=8765),
        metadata=dict(updated_at=now(),sources={}))
    live=system_properties('lp-v2-live.service')
    if (live.get('User')!='lp-v2' or profile['files']['run_env'] not in live.get('EnvironmentFiles','')
            or root+'/current/deploy/run-live' not in live.get('ExecStart','')
            or profile['files']['state_dir'] not in live.get('ExecStop','')):
        raise ValueError('explicit import locations do not match effective service')
    sha=(Path(root)/'current/COMPLETE').read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}',sha): raise ValueError()
    profile['release']['expected_sha']=sha
    values=env_values(profile)
    if values.get('LP_V2_CODE_SHA') not in (None,sha):
        raise ValueError('conflicting version sources')
    for key,field in [('LP_V2_MARKET','market_condition_id'),('LP_V2_CAPITAL_USDC','capital_usdc')]:
        if key in values: profile['trading'][field]=values[key]
    credentials=read_json(profile['files']['l2_credentials'])
    for field in ('signer_address','wallet_address','signature_type'):
        if field in credentials: profile['identity'][field]=credentials[field]
    from lp_v2.live_control.profiles import FIELDS
    for group,fields in FIELDS.items():
        for field in fields:
            if field in profile[group]:
                source={'target':'target.ssh','release':'target.release','trading':'target.run_env','identity':'target.identity','files':'target.files','services':'target.unit'}[group]
                profile['metadata']['sources'][group+'.'+field]=dict(source=source,observed_at=now())
    validate(profile)
    return dict(profile=profile,status='IMPORTED',scope='configuration_only')


def safe_public(profile,raw):
    from lp_v2.live_control.workflow import public_check,PUBLIC
    validated=public_check(profile,raw)
    if validated['status']=='UNKNOWN':
        return dict(scope='public_market',status='UNKNOWN',checks={key:dict(status='UNKNOWN') for key in PUBLIC})
    return dict(scope='public_market',status=raw['status'],code_sha=raw['code_sha'],market=raw['market'],
        capital_usdc=raw['capital_usdc'],as_of=raw['as_of'],checks={key:dict(status=raw['checks'][key]['status']) for key in PUBLIC})



def safe_summary(raw):
    import re
    from decimal import Decimal
    from lp_v2.live_control.profiles import timestamp
    def stamp(value):
        try: return timestamp(value).isoformat()
        except Exception: return 'UNKNOWN'
    def matching(value,pattern):
        return value if isinstance(value,str) and re.fullmatch(pattern,value) else 'UNKNOWN'
    try:
        capital=Decimal(raw['capital_usdc'])
        capital=str(capital) if capital.is_finite() and capital>0 else 'UNKNOWN'
        return dict(mode=raw['mode'] if raw['mode'] in ('live','read_only') else 'UNKNOWN',as_of=stamp(raw.get('as_of')),
            capital_usdc=capital,run=dict(code_sha=matching(raw['run'].get('code_sha'),r'[0-9a-f]{40}'),
                wallet_tail=matching(raw['run'].get('wallet_tail'),r'[0-9a-fA-F]{4}')),
            accounting=dict(status='SUCCESS' if raw.get('accounting',{}).get('status')=='SUCCESS' else 'UNKNOWN',
                as_of=stamp(raw.get('accounting',{}).get('as_of'))),freshness=dict(decision_as_of=stamp(raw.get('freshness',{}).get('decision_as_of'))),
            markets=[dict(condition_id=matching(row.get('condition_id'),r'0x[0-9a-fA-F]{64}'),
                          eligible=row.get('eligible') is True,skip_reason=None if row.get('skip_reason') is None else 'INELIGIBLE') for row in raw['markets']])
    except Exception:
        return dict(mode='UNKNOWN')


def main(*,stdin=None,stdout=None):
    stdin=stdin or sys.stdin; stdout=stdout or sys.stdout
    try:
        request=json.loads(stdin.read(131073))
        if type(request.get('schema_version')) is not int or request['schema_version']!=1 or request.get('action') not in ('check','prepare','off','result','on','observe','import'):
            raise ValueError()
        profile=request['profile']
        if request['action']!='import': validate(profile,complete=True)
        if request['action']=='import': result=import_profile(profile)
        elif request['action']=='check': result=check(profile)
        elif request['action']=='result': result=action_result(request)
        elif request['action']=='observe': result=observe(request)
        else: result=mutate(request)
    except Exception:
        result=dict(schema_version=1,status='UNKNOWN',reason='HELPER_REQUEST_INVALID')
    print(json.dumps(result),file=stdout)
    return 0 if result.get('status')=='PASS' else 2


if __name__=='__main__':
    raise SystemExit(main())

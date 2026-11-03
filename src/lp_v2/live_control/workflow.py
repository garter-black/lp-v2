"""Operator orchestration; check is observational."""
from lp_v2.live_control.remote import request


REQUIRED=('release','artifacts','files','private_identity','run_env','state_identity','single_executor','port','service','balance','allowance','closed_only','legacy_orders','region','relayer_auth','relayer_merge')
PUBLIC=('market_identity','token_identity','fees','state','political','in_play','rewards','constraints','books','opening_eligibility','market_inputs')


def fresh(value, *, seconds=60):
    from lp_v2.live_control.profiles import timestamp
    from datetime import datetime, UTC
    try:
        return 0 <= (datetime.now(UTC)-timestamp(value)).total_seconds() <= seconds
    except Exception:
        return False


def normalize_report(profile,report):
    checks={}
    report=report if isinstance(report,dict) else {}
    raw=report.get('checks',{})
    raw=raw if isinstance(raw,dict) else {}
    compatible=type(report.get('schema_version')) is int and report['schema_version']==1 and fresh(report.get('as_of'))
    for key in REQUIRED:
        row=raw.get(key,{})
        row=row if isinstance(row,dict) else {}
        status=row.get('status','UNKNOWN')
        if not compatible or not fresh(row.get('as_of')):
            status='UNKNOWN'
        checks[key]=dict(status=status if status in ('PASS','FAIL') else 'UNKNOWN',reason='REMOTE_CHECK_'+key.upper())
    checks['public_market']=public_check(profile,report.get('public_market'))
    statuses=[check['status'] for check in checks.values()]
    return dict(schema_version=1,status='PASS' if all(s=='PASS' for s in statuses) else 'FAIL' if 'FAIL' in statuses else 'UNKNOWN',checks=checks)


def public_check(profile,public):
    status='UNKNOWN'
    try:
        if (not isinstance(public,dict) or public.get('scope')!='public_market'
                or public.get('code_sha')!=profile['release']['expected_sha']
                or public.get('market')!=profile['trading']['market_condition_id']
                or public.get('capital_usdc')!=profile['trading']['capital_usdc']
                or not fresh(public.get('as_of'))):
            raise ValueError()
        rows=public['checks']
        values=[rows[key]['status'] for key in PUBLIC]
        if not all(v in ('PASS','BLOCKED','UNKNOWN') for v in values):
            raise ValueError()
        if public['status']=='BLOCKED' and 'BLOCKED' in values:
            status='FAIL'
        elif public['status']=='PASS' and all(v=='PASS' for v in values):
            status='PASS'
    except Exception:
        pass
    return dict(status=status,reason='PUBLIC_MARKET' if status!='UNKNOWN' else 'PREFLIGHT_CAPABILITY_MISSING_OR_INVALID')


def check(profile):
    return normalize_report(profile,request(profile,'check'))


def on(profile, *, action_id=None, window_end=None, prepare_env=False):
    from lp_v2.live_control.profiles import config_root, secure_directory, atomic_json, read_json, timestamp, ProfileError
    from datetime import datetime, UTC
    import json, os, re
    from uuid import uuid4
    action_id=action_id or uuid4().hex
    if not re.fullmatch('[0-9a-f]{32}',action_id):
        raise ProfileError('ACTION_INVALID')
    directory=config_root()/'operations'; directory.mkdir(mode=0o700,exist_ok=True); secure_directory(directory)
    path=directory/(action_id+'.json')
    if path.exists() or path.is_symlink():
        prior=read_json(path)
        if prior.get('action')!='on': return dict(status='UNKNOWN',reason='ACTION_IDENTITY_MISMATCH')
        started=request(prior['profile'],'result',action_id=action_id)
        return verify_start(prior['profile'],started,action_id=action_id) if started.get('status')=='ACCEPTED' else dict(status='UNKNOWN',reason='REMOTE_OUTCOME_UNKNOWN')
    if not window_end or timestamp(window_end)<=datetime.now(UTC):
        return dict(status='UNKNOWN',reason='CONFIRMATION_REQUIRED')
    report=check(profile)
    preparable=prepare_env and all(row['status']=='PASS' for key,row in report['checks'].items() if key!='run_env')
    if report.get('status')!='PASS' and not preparable:
        return report
    print(json.dumps(dict(action='on',action_id=action_id,profile=profile,window_end=window_end,prepare_run_env=prepare_env,rollback='Owned stop/drain; restore readonly only after executor/port release.')))
    if input('Type on to confirm this target and window: ')!='on':
        return dict(status='UNKNOWN',reason='CONFIRMATION_REQUIRED')
    from lp_v2.live_control.profiles import load
    if load(profile['name'])!=profile or timestamp(window_end)<=datetime.now(UTC):
        return dict(status='UNKNOWN',reason='CONFIRMATION_TARGET_CHANGED')
    current=check(profile)
    if current!=report or (current['status']!='PASS' and not preparable):
        return dict(status='UNKNOWN',reason='CONFIRMATION_FACTS_CHANGED',checks=current['checks'])
    record=dict(action='on',profile=profile,action_id=action_id,window_end=window_end,status='UNKNOWN')
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        return action_report(request(profile,'result',action_id=action_id))
    with os.fdopen(fd,'w') as output:
        json.dump(record,output); output.flush(); os.fsync(output.fileno())
    try:
        from lp_v2.live_control.profiles import fingerprint,now
        if prepare_env:
            import hashlib
            changes=dict(LP_V2_CAPITAL_USDC=profile['trading']['capital_usdc'],LP_V2_MARKET=profile['trading']['market_condition_id'],LP_V2_CODE_SHA=profile['release']['expected_sha'])
            preparation=request(profile,'prepare',action_id=hashlib.sha256((action_id+'prepare').encode()).hexdigest()[:32],changes=changes,
                confirmation=dict(operation='prepare',profile_sha256=fingerprint(profile),issued_at=now(),window_end=window_end))
            if preparation.get('status')!='PASS' or check(profile)['status']!='PASS':
                return dict(status='UNKNOWN',reason='PREPARATION_NOT_READY')
        proof=dict(operation='on',profile_sha256=fingerprint(profile),issued_at=now(),window_end=window_end)
        started=request(profile,'on',action_id=action_id,window_end=window_end,confirmation=proof)
        return verify_start(profile,started,action_id=action_id)
    except Exception:
        return dict(status='UNKNOWN',reason='REMOTE_OUTCOME_UNKNOWN',action_id=action_id)


def verify_start(profile,started,*,action_id):
    from lp_v2.live_control.profiles import config_root, atomic_json, secure_directory, timestamp
    import re, time
    run_id=started.get('run_id')
    if started.get('status')!='ACCEPTED' or not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id) or not fresh(started.get('started_at')):
        return dict(status='UNKNOWN',reason='START_NOT_VERIFIED',action_id=action_id)
    directory=config_root()/'runs'; directory.mkdir(mode=0o700,exist_ok=True); secure_directory(directory)
    atomic_json(directory/(profile['name']+'.json'),dict(profile=profile,run_id=run_id,action_id=action_id,started_at=started['started_at']))
    previous=None; rounds=0
    for attempt in range(12):
        try:
            snapshot=request(profile,'observe',run_id=run_id)
        except Exception:
            break
        moment=valid_round(profile,run_id,snapshot,started['started_at'])
        if moment is not None and (previous is None or all(value>old for value,old in zip(moment,previous))):
            rounds+=1; previous=moment
            if rounds==2:
                return dict(status='PASS',run_id=run_id,action_id=action_id,verified_rounds=2)
        if moment is None:
            rounds=0;previous=None
        if attempt<11:
            time.sleep(5)
    import hashlib
    rollback_id=hashlib.sha256((action_id+'rollback').encode()).hexdigest()[:32]
    try:
        rollback=action_report(request(profile,'off',run_id=run_id,action_id=rollback_id))
    except Exception:
        rollback=dict(status='UNKNOWN',drain='UNKNOWN')
    return dict(status='UNKNOWN',reason='START_NOT_VERIFIED',phase='verify_live',run_id=run_id,action_id=action_id,rollback=rollback)


def valid_round(profile,run_id,snapshot,started_at):
    from lp_v2.live_control.profiles import timestamp
    try:
        row=snapshot['summary']; markets=row['markets']; accounting=row['accounting']
        if (snapshot['run_id']!=run_id or snapshot['executor_running'] is not True or snapshot['readonly_stopped'] is not True
                or snapshot['identity']!=profile['identity'] or row['mode']!='live'
                or row['capital_usdc']!=profile['trading']['capital_usdc']
                or row['run']['code_sha']!=profile['release']['expected_sha']
                or row['run']['wallet_tail']!=profile['identity']['wallet_address'][-4:]
                or len(markets)!=1 or markets[0]['condition_id']!=profile['trading']['market_condition_id']
                or accounting['status']!='SUCCESS'
                or not all(fresh(value) and timestamp(value)>timestamp(started_at) for value in
                           (row['as_of'],accounting['as_of'],row['freshness']['decision_as_of']))):
            return None
        return tuple(timestamp(value) for value in (accounting['as_of'],row['as_of'],row['freshness']['decision_as_of']))
    except Exception:
        return None


def action_report(raw):
    result=dict(status=raw.get('status') if raw.get('status') in ('PASS','FAIL','UNKNOWN','BUSY') else 'UNKNOWN')
    for key in ('executor_stopped','readonly_restored','port_released'):
        if type(raw.get(key)) is bool:
            result[key]=raw[key]
    result['drain']=raw.get('drain') if raw.get('drain') in ('PASS','FAIL','UNKNOWN') else 'UNKNOWN'
    if isinstance(raw.get('inventory'),list):
        try:
            from lp_v2.live_control.host import inventory_report
            result['inventory']=inventory_report(raw['inventory'])
        except Exception:
            result['inventory_status']='UNKNOWN'
    return result


def off(profile,*,action_id=None):
    from lp_v2.live_control.profiles import config_root,read_json,validate,secure_directory
    import hashlib,json,os,re
    record=read_json(config_root()/'runs'/(profile['name']+'.json'))
    frozen=record['profile']; validate(frozen,complete=True)
    if (frozen['name']!=profile['name'] or frozen['target']!=profile['target']
            or frozen['release']['root']!=profile['release']['root'] or frozen['files']['state_dir']!=profile['files']['state_dir']):
        return dict(status='UNKNOWN',reason='FROZEN_TARGET_SELECTION_REQUIRED')
    action_id=action_id or hashlib.sha256((record['run_id']+'off').encode()).hexdigest()[:32]
    if not re.fullmatch('[0-9a-f]{32}',action_id):
        return dict(status='UNKNOWN',reason='ACTION_INVALID')
    directory=config_root()/'operations';directory.mkdir(mode=0o700,exist_ok=True);secure_directory(directory)
    path=directory/(action_id+'.json')
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        previous=read_json(path)
        return action_report(request(previous['profile'],'result',action_id=action_id))
    with os.fdopen(fd,'w') as output:
        json.dump(dict(profile=frozen,run_id=record['run_id'],action='off'),output);output.flush();os.fsync(output.fileno())
    try:
        result=request(frozen,'off',run_id=record['run_id'],action_id=action_id)
        safe=action_report(result)
        if result.get('run_id')!=record['run_id'] or not (safe.get('executor_stopped') is True and safe.get('port_released') is True
                and safe.get('readonly_restored') is True and safe['drain']=='PASS'):
            safe['status']='UNKNOWN'
        return safe
    except Exception:
        return dict(status='UNKNOWN',reason='REMOTE_OUTCOME_UNKNOWN')

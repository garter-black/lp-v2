from datetime import timedelta
from dataclasses import replace
from decimal import Decimal as D
from lp_v2.cli import RunConfig
from tests.test_readonly_sdk import AT
from tests.test_scenarios import plan


class ScriptedReads:
    def __init__(self):
        self.fail=False; self.reward=100.; self.calls=[]
    def read_markets(self, now):
        self.calls.append(('markets',now))
        if self.fail: raise RuntimeError('unavailable')
        p=plan(yes='0.46',no='0.51')
        p=replace(p,market=replace(p.market,fees_enabled=False,rewards_daily_rate=self.reward),fee_per_share=(D('0'),D('0')))
        return [dict(title='央行',condition_id='m',plan=p,eligible=True,skip_reason=None),
                dict(title='缺失',condition_id='missing',plan=None,eligible=False,score='UNKNOWN',skip_reason='市场安全数据缺失或无效')]
    def read_wallet(self,now):
        self.calls.append(('wallet',now))
        return dict(available_usdc='UNKNOWN',as_of=None) if self.fail else dict(available_usdc='182.4',as_of=now)
    def read_region(self): return 'ok'
    def wallet_tail(self): return 'a3F9'
    def safety(self,since): return dict(orders_sent=0,cancels_sent=0,since=since)
    def __getattr__(self,name):
        if name in ('place_order','cancel_order','merge'): raise AssertionError('mutation surface accessed')
        raise AttributeError(name)
    def close(self): pass


def test_day_of_proposals_never_sends_orders():
    from lp_v2.readonly_runtime import ReadOnlyRunner
    sdk=ScriptedReads(); runner=ReadOnlyRunner(RunConfig(D('100'),AT),sdk,code_sha='abc1234')
    for n in range(8640):
        d=runner.step(AT+timedelta(seconds=n*10))
        assert d['safety']==dict(orders_sent=0,cancels_sent=0,since=AT.isoformat())
        assert all(d['capital'][k]=='0' for k in ('inventory_cost','open_buys','pending_requests','hedge_reserve','fee_reserve','total'))
        assert D(d['capital']['proposed_reserve'])<=100
        p=d['markets'][0]['proposed']
        assert p['yes']==dict(price='0.46',quantity='10')
        assert p['no']==dict(price='0.51',quantity='10')
        assert p['pair_cost']=='0.97' and p['safety_margin']=='0.03'
        assert p['reserved_usdc']=='10'
        assert D(p['pair_cost'])<=1-D(p['safety_margin'])
        assert d['markets'][1]['proposed'] is None


def test_refresh_cadence_and_failures():
    from lp_v2.readonly_runtime import ReadOnlyRunner
    sdk=ScriptedReads(); runner=ReadOnlyRunner(RunConfig(D('100'),AT),sdk)
    at=lambda n: AT+timedelta(seconds=n)
    first=runner.step(at(0)); score=first['markets'][0]['score']; sdk.reward=200.
    for n in (9,10,29,30,599):
        d=runner.step(at(n))
        assert d['markets'][0]['score']==score
        if n==9:
            assert d['freshness']['decision_as_of']==at(0).isoformat()
        if n==29:
            assert d['freshness']['wallet_as_of']==at(0).isoformat()
        if n==30:
            assert d['freshness']['wallet_as_of']==at(30).isoformat()
    d=runner.step(at(600))
    assert d['markets'][0]['score']!=score
    assert d['freshness']['scores_as_of']==at(600).isoformat()
    assert [t for k,t in sdk.calls if k=='markets']==[at(n) for n in (0,10,29,30,599,600)]
    assert [t for k,t in sdk.calls if k=='wallet']==[at(n) for n in (0,30,599,600)]
    before=d['freshness'].copy(); sdk.fail=True
    d=runner.step(at(1200))
    assert d['freshness']==before
    assert d['wallet']['available_usdc']=='UNKNOWN'
    assert all(m['proposed'] is None for m in d['markets'])
    sdk.fail=False
    d=runner.step(at(1800))
    assert d['freshness']==dict(decision_as_of=at(1800).isoformat(),scores_as_of=at(1800).isoformat(),wallet_as_of=at(1800).isoformat())
    assert d['markets'][0]['proposed'] and d['markets'][1]['proposed'] is None
    sdk.read_markets=lambda now: []
    d=runner.step(at(2400))
    assert d['markets']==[] and d['capital']['proposed_reserve']=='0'
    # Snapshot consumers cannot mutate the published object.
    d['capital']['total']='999'
    assert runner.snapshot()['capital']['total']=='0'


def test_proposal_reprice_waits_sixty_seconds():
    from lp_v2.readonly_runtime import ReadOnlyRunner
    class ChangingReads(ScriptedReads):
        change='safe'
        def read_markets(self,now):
            rows=super().read_markets(now); p=rows[0]['plan']
            if now>AT:
                p=replace(p,books=(replace(p.books[0],best_bid=D('0.45'),best_ask=D('0.47')),p.books[1]))
                if self.change=='cross':
                    p=replace(p,books=(replace(p.books[0],best_bid=D('0.44'),best_ask=D('0.46')),p.books[1]))
                elif self.change=='band':
                    p=replace(p,market=replace(p.market,rewards_max_spread=0.1))
                elif self.change=='fee':
                    p=replace(p,fee_per_share=(D('0.02'),D('0.02')))
                elif self.change=='capital':
                    p=replace(p,quantity=D('110'))
                elif self.change=='identity':
                    p=replace(p,market=replace(p.market,tokens=tuple(replace(t,token_id=t.token_id+'new') for t in p.market.tokens)),books=tuple(replace(b,token_id=b.token_id+'new') for b in p.books))
                elif self.change=='eligibility':
                    p=replace(p,market=replace(p.market,accepting_orders=False))
                rows[0]['plan']=p
            return rows
    sdk=ChangingReads();runner=ReadOnlyRunner(RunConfig(D('100'),AT),sdk)
    first=runner.step(AT)['markets'][0]['proposed']
    assert first['yes']==dict(price='0.46',quantity='10')
    for seconds in (10,20,30,40,50,59):
        d=runner.step(AT+timedelta(seconds=seconds))
        assert d['markets'][0]['proposed']==first
        if seconds!=59:
            assert d['freshness']['decision_as_of']==(AT+timedelta(seconds=seconds)).isoformat()
        assert d['capital']['total']=='0' and d['safety']['orders_sent']==0
    d=runner.step(AT+timedelta(seconds=60))
    assert d['markets'][0]['proposed']['yes']==dict(price='0.45',quantity='10')
    assert d['markets'][0]['proposed']['no']==dict(price='0.51',quantity='10')
    for change in ('cross','band','fee','capital','identity','eligibility'):
        sdk=ChangingReads();sdk.change=change;runner=ReadOnlyRunner(RunConfig(D('100'),AT),sdk)
        assert runner.step(AT)['markets'][0]['proposed']
        d=runner.step(AT+timedelta(seconds=10))
        assert d['markets'][0]['proposed'] is None,change
        assert d['capital']['proposed_reserve']==d['capital']['total']=='0'
        assert d['safety']['orders_sent']==d['safety']['cancels_sent']==0


def test_excluded_market_keeps_known_score_and_reason():
    from lp_v2.readonly_runtime import ReadOnlyRunner
    from lp_v2.readonly_sdk import ReadOnlySDK
    from tests.test_readonly_sdk import market_wire,external_sdk
    import httpx
    class ExcludedReads(ScriptedReads):
        score='38.2'
        def read_markets(self,now):
            return [dict(title='中间价过高',condition_id='excluded',score=self.score,
                         eligible=False,skip_reason='中间价 0.93 超出 0.10–0.90',plan=None),
                    dict(title='无估分',condition_id='missing',eligible=False,
                         skip_reason='缺少奖励输入',plan=None)]
    sdk=ExcludedReads();runner=ReadOnlyRunner(RunConfig(D('100'),AT),sdk)
    first=runner.step(AT)
    row=first['markets'][0]
    assert row['score']=='38.2' and row['skip_reason']=='中间价 0.93 超出 0.10–0.90'
    assert row['eligible'] is False and row['proposed'] is None
    assert first['markets'][1]['score']=='UNKNOWN'
    assert first['markets'][1]['skip_reason']=='缺少奖励输入'
    sdk.score='39.9'
    assert runner.step(AT+timedelta(seconds=590))['markets'][0]['score']=='38.2'
    d=runner.step(AT+timedelta(seconds=600))
    assert d['markets'][0]['score']=='39.9'
    assert d['markets'][0]['skip_reason']=='中间价 0.93 超出 0.10–0.90'
    assert all(m['proposed'] is None for m in d['markets'])
    p,_=external_sdk([market_wire(bestBid='0.92',bestAsk='0.94')])
    sdk=ReadOnlySDK(public=p,http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False}))))
    row=sdk.read_markets(AT)[0]
    assert row['score']!='UNKNOWN'
    assert row['skip_reason']=='中间价 0.93 超出 0.10–0.90'
    assert row['plan'] is None and row['eligible'] is False
    sdk.close()

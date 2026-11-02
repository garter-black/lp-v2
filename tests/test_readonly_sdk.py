from datetime import UTC, datetime
from decimal import Decimal as D
import httpx

AT = datetime(2026, 11, 2, tzinfo=UTC)


def test_readonly_adapter_has_no_mutation_surface():
    from lp_v2.readonly_sdk import ReadOnlySDK
    from polymarket import PublicClient
    public = PublicClient()
    sdk = ReadOnlySDK(public=public, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    for name in ('place_order', 'cancel_order', 'cancel_all', 'merge', 'create_limit_order', 'post_order', 'create_api_key', 'request', 'get', 'public', 'transport'):
        assert not hasattr(sdk, name)
    assert sdk.read_wallet(AT)['available_usdc'] == 'UNKNOWN'
    assert sdk.safety(AT) == dict(orders_sent=0, cancels_sent=0, since=AT)
    sdk.close()


CID = '0x' + '1' * 64
BAD = '0x' + '2' * 64


def market_wire(cid=CID, **overrides):
    return dict(dict(id='1', conditionId=cid, question='央行?', outcomes=['Yes','No'], clobTokenIds=['101','102'],
                active=True, closed=False, acceptingOrders=True, enableOrderBook=True,
                orderMinSize='1', orderPriceMinTickSize='0.01', feesEnabled=False,
                rewardsMinSize='10', rewardsMaxSpread=5,
                clobRewards=[dict(id='1', conditionId=cid, assetAddress='0x2791bca1f2de4661ed88a30c99a7a9449aa84174', rewardsAmount='100', rewardsDailyRate='100', startDate='2026-01-01')],
                bestBid='0.46', bestAsk='0.48', liquidityNum='2000', volume24hr='10000',
                tags=[dict(id='2', label='Politics', slug='politics')]), **overrides)


def external_sdk(rows=None, book_mismatch=False):
    from polymarket import PublicClient
    from polymarket.clients._transport import SyncTransport
    seen=[]
    def handle(req):
        seen.append((req.method, str(req.url)))
        assert req.method == 'GET'
        if req.url.path == '/markets/keyset':
            # SDK keyset paginator: second page empty when cursor is returned.
            if req.url.params.get('after_cursor'):
                return httpx.Response(200, json={'markets': [], 'next_cursor': None})
            return httpx.Response(200, json={'markets': rows if rows is not None else [market_wire(), market_wire(BAD, bestBid='0.92', bestAsk='0.94')], 'next_cursor':'page2'})
        if req.url.path == '/book':
            token=req.url.params['token_id']; bid,ask=('0.46','0.48') if token=='101' else ('0.51','0.53')
            return httpx.Response(200,json=dict(market=BAD if book_mismatch else CID,asset_id=token,timestamp='1793577600000',
                bids=[dict(price=bid,size='100')],asks=[dict(price=ask,size='100')],min_order_size='1',tick_size='0.01',neg_risk=False,hash='fixture'))
        if req.url.path.startswith('/markets/'):
            return httpx.Response(200,json={'in_play':False})
        raise AssertionError(str(req.url))
    p=PublicClient()
    for name in ('gamma','clob'):
        getattr(p._ctx,name).close()
        from dataclasses import replace
        p._ctx=replace(p._ctx, **{name: SyncTransport(base_url='https://'+name+'.polymarket.com', client=httpx.Client(base_url='https://'+name+'.polymarket.com', transport=httpx.MockTransport(handle)))})
    return p,seen


def test_public_market_and_book_reads():
    from lp_v2.readonly_sdk import ReadOnlySDK
    from lp_v2.quoting import quote_pair
    p,seen=external_sdk()
    sdk=ReadOnlySDK(public=p, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'in_play':False}))))
    rows=sdk.read_markets(AT)
    assert len(rows)==2
    good,bad=rows
    assert good['title']=='央行?' and good['skip_reason'] is None
    plan=good['plan']; m=plan.market
    assert tuple(t.token_id for t in m.tokens)==('101','102')
    assert (m.tick_size,m.min_order_size,m.rewards_min_size)==(D('0.01'),D('1'),D('10'))
    assert m.rewards_daily_rate==100 and m.rewards_max_spread==5
    assert plan.fee_per_share==(D('0'),D('0')) and not m.fees_enabled
    pair=quote_pair(m,*plan.books,planned_quantity=D('10'),fee_per_pair=D('0'),safety_cushion=D('0.02'))
    assert [q.price for q in pair]==[D('0.46'),D('0.51')]
    assert sum(q.price for q in pair)<=D('0.98')
    assert bad['plan'] is None and bad['skip_reason']
    assert all(method=='GET' for method,url in seen)
    sdk.close()
    for wire,mismatch,score in [(market_wire(feesEnabled=True),False,'UNKNOWN'),(market_wire(rewardsMaxSpread=None),False,'UNKNOWN'),(market_wire(),True,'3.4643')]:
        p,_=external_sdk([wire],mismatch); sdk=ReadOnlySDK(public=p, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'in_play':False}))))
        row=sdk.read_markets(AT)[0]
        assert row['plan'] is None and row['eligible'] is False and row['score']==score and row['skip_reason']
        sdk.close()


def test_wallet_read_uses_existing_l2_credentials_only(caplog):
    import base64, hashlib, hmac
    from lp_v2.readonly_sdk import ReadOnlySDK, L2Credentials
    creds=L2Credentials('synthetic-key',base64.urlsafe_b64encode(b'synthetic-secret').decode(),'synthetic-pass', '0x'+'a'*40,'0x'+'b'*40,2)
    seen=[]
    def handle(r):
        seen.append(r)
        assert r.method=='GET' and r.url.host=='clob.polymarket.com'
        assert r.url.path in ('/balance-allowance','/data/orders')
        message=f'{int(AT.timestamp())}GET{r.url.path}'.encode()
        assert r.headers['POLY_SIGNATURE']==base64.urlsafe_b64encode(hmac.new(b'synthetic-secret',message,hashlib.sha256).digest()).decode()
        assert r.headers['POLY_ADDRESS']=='0x'+'a'*40
        if r.url.path=='/balance-allowance':
            assert dict(r.url.params)==dict(asset_type='COLLATERAL',signature_type='2')
            return httpx.Response(200,json=dict(balance='182400000',allowances={}))
        return httpx.Response(200,json=dict(data=[dict(id='legacy')],next_cursor='LTE='))
    sdk=ReadOnlySDK(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=creds)
    assert sdk.read_wallet(AT)==dict(available_usdc='182.4',as_of=AT)
    assert sdk.read_orders(AT)==dict(legacy_order_count=1,as_of=AT)
    assert len(seen)==2 and sdk.safety(AT)['orders_sent']==0
    assert 'synthetic' not in repr(creds)+repr(sdk)+caplog.text
    sdk.close()
    for payload in ({},dict(balance='NaN',allowances={}),dict(balance='-1',allowances={}),dict(balance=True,allowances={})):
        sdk=ReadOnlySDK(http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))),credentials=creds)
        assert sdk.read_wallet(AT)==dict(available_usdc='UNKNOWN',as_of=None)
        sdk.close()
    def fail(r): raise RuntimeError('synthetic-secret '+creds.signer_address)
    sdk=ReadOnlySDK(http=httpx.Client(transport=httpx.MockTransport(fail)),credentials=creds)
    assert sdk.read_wallet(AT)==dict(available_usdc='UNKNOWN',as_of=None)
    assert 'synthetic' not in caplog.text
    sdk.close()

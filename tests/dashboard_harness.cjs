const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const base=JSON.parse(input.html.match(/id="summary-data">([\s\S]*?)<\/script>/)[1]);
const columns=['标的','估分','状态（拟挂 / 不挂 + 原因）','拟挂 Yes（价 × 量）','拟挂 No（价 × 量）','两边合计成本 / 安全垫','拟占用资金','LP 收益率','今日奖励','今日返佣','份额占比','已配对利润','未配对库存','净收益率 %/天'];
function setup(demo=false){
 const target={innerHTML:''},buttons=['normal','wallet','unreachable','stale'].map(state=>({dataset:{state},setAttribute(){},addEventListener(){}}));
 const document={getElementById:id=>id==='summary-data'?{textContent:JSON.stringify(base)}:target,querySelectorAll:()=>buttons,documentElement:{dataset:{}}};
 const timers=[],requests=[];let payload=structuredClone(base),fail=false;
 payload.run.code_sha='live123';payload.markets[0].title='<img src=x onerror=alert(1)>';payload.wallet={};payload.schema_version=2;
 const context=vm.createContext({document,structuredClone,Intl,Date,Number,URLSearchParams,AbortController,setTimeout,clearTimeout,location:{search:demo?'?demo=1':'',hash:''},history:{replaceState(){}},
  setInterval:(fn,ms)=>timers.push({fn,ms}),fetch:async(...args)=>{requests.push(args);if(fail)throw Error('offline');return {ok:true,json:async()=>payload};}});
 vm.runInContext(input.js,context);
 return {context,target,timers,requests,setPayload:p=>payload=p,setFail:()=>fail=true};
}
(async()=>{
 const live=setup();await new Promise(setImmediate);
 assert.deepEqual(Array.from(vm.runInContext('cols',live.context)),columns);
 assert.equal(live.timers[0].ms,5000);assert.equal(live.requests[0][0],'/summary');
 assert.match(live.target.innerHTML,/live123/);assert.doesNotMatch(live.target.innerHTML,/abc1234/);
 assert.match(live.target.innerHTML,/接口版本较新/);assert.match(live.target.innerHTML,/&lt;img/);assert.doesNotMatch(live.target.innerHTML,/<img/);
 assert.match(live.target.innerHTML,/钱包可用[\s\S]*?UNKNOWN/);
 for (const age of [15,16,60,61,1500,1501]) {
   const d=structuredClone(base),now=Date.parse(base.as_of);d.as_of=new Date(now-age*1000).toISOString();d.freshness.decision_as_of=d.as_of;d.freshness.scores_as_of=d.as_of;
   live.context.payload=d;live.context.now=now;vm.runInContext("render('live',payload,now)",live.context);
   assert.equal(live.target.innerHTML.includes('数据过期</span>'),age>15);
   assert.equal(live.target.innerHTML.includes('历史拟挂 · 决策停滞'),age>60);
   assert.equal(live.target.innerHTML.includes('估分过期</span>'),age>1500);
 }
 const excluded=structuredClone(base);excluded.markets[0].score='UNKNOWN';excluded.markets[0].skip_reason='缺少奖励输入';excluded.markets[0].eligible=false;excluded.markets[0].proposed=null;
 live.setPayload(excluded);await live.timers[0].fn();assert.match(live.target.innerHTML,/缺少奖励输入/);
 live.setPayload({});await live.timers[0].fn();assert.match(live.target.innerHTML,/UNKNOWN/);
 live.setFail();await live.timers[0].fn();assert.match(live.target.innerHTML,/lp_v2 不可达/);assert.doesNotMatch(live.target.innerHTML,/live123/);
 const demo=setup(true);assert.equal(demo.requests.length,0);assert.equal(demo.timers.length,0);
 vm.runInContext("render('wallet')",demo.context);assert.match(demo.target.innerHTML,/钱包余额读不到/);
 vm.runInContext("render('unreachable')",demo.context);assert.match(demo.target.innerHTML,/lp_v2 不可达/);
 vm.runInContext("render('stale')",demo.context);assert.match(demo.target.innerHTML,/决策停滞/);assert.match(demo.target.innerHTML,/估分过期/);
 vm.runInContext("render('normal')",demo.context);assert.match(demo.target.innerHTML,/182.40/);
 console.log('offline dashboard behaviors PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});

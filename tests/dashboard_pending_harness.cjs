const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {html,js}=JSON.parse(fs.readFileSync(0,'utf8'));
const base=JSON.parse(html.match(/id="summary-data">([\s\S]*?)<\/script>/)[1]);
let now=Date.parse(base.as_of),counter=0,calls=0,resolveOld;
const timers=new Map(),target={innerHTML:''};
const document={getElementById:id=>id==='summary-data'?{textContent:JSON.stringify(base)}:target,querySelectorAll:()=>[],documentElement:{dataset:{}}};
function addTimer(fn,ms,interval){const id=++counter;timers.set(id,{fn,ms,interval,at:now+ms});return id;}
const initial=structuredClone(base);initial.run.code_sha='first-live';
const recovery=structuredClone(base);recovery.run.code_sha='newest-live';
const context=vm.createContext({document,structuredClone,Intl,Number,URLSearchParams,AbortController,
 Date:class extends Date {static now(){return now;}},location:{search:'',hash:''},
 setInterval:(fn,ms)=>addTimer(fn,ms,true),setTimeout:(fn,ms)=>addTimer(fn,ms,false),
 clearTimeout:id=>timers.delete(id),
 fetch:()=>{calls++;if(calls===1)return Promise.resolve({ok:true,json:async()=>initial});
   if(calls===2)return new Promise(resolve=>{resolveOld=resolve;});
   return Promise.resolve({ok:true,json:async()=>recovery});}});
async function flush(){for(let i=0;i<8;i++)await Promise.resolve();}
async function advance(ms){const end=now+ms;while(true){const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];if(!due)break;
 const [id,t]=due;now=t.at;if(t.interval)t.at+=t.ms;else timers.delete(id);t.fn();await flush();}now=end;await flush();}
(async()=>{
 vm.runInContext(js,context);await flush();assert.match(target.innerHTML,/first-live/);
 await advance(5000);assert.equal(calls,2);
 await advance(5000);assert.equal(calls,2,'pending poll must not overlap');
 await advance(6000);
 assert(target.innerHTML.includes('lp_v2 不可达')||target.innerHTML.includes('数据过期</span>'),'hung request must not look fresh at sixteen seconds');
 await advance(5000);assert.match(target.innerHTML,/newest-live/);
 resolveOld({ok:true,json:async()=>({...initial,run:{...initial.run,code_sha:'late-obsolete'}})});await flush();
 assert.match(target.innerHTML,/newest-live/);assert.doesNotMatch(target.innerHTML,/late-obsolete/);
 console.log('pending timeout, single-flight, late result and recovery PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});

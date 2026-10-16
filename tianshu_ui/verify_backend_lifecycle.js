// 独立 Node 生命周期回归：事件模型明确标注；真实进程/TCP仅使用临时随机端口，不连接业务后端或COM。
const assert = require('assert/strict');
const { EventEmitter, once } = require('events');
const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { BackendLifecycle, isManagedBase, isEndpointListening, terminateProcessTree } = require('./backend_lifecycle');
const tests=[];
const test=(name,run)=>tests.push({name,run});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
async function flush(){for(let i=0;i<15;i++)await Promise.resolve();}
function model(options={}){
  const children=[],logs=[],unexpected=[];
  let lifecycle;
  const state={settings:{apiBase:'http://127.0.0.1:8000',model:'first'},external:false,killCount:0};
  lifecycle=new BackendLifecycle({
    getSettings:()=>({...state.settings}),
    start:settings=>{const child=new EventEmitter();child.pid=1000+children.length;child.ready=true;child.configuration={...settings};children.push(child);return {child,logPath:'test.log'};},
    probeReady:async()=>state.external||Boolean(lifecycle.current?.child.ready),
    listening:async()=>Boolean(lifecycle.current&&!lifecycle.current.exited),
    terminate:async child=>{state.killCount++;child.emit('exit',0,null);},
    log:message=>logs.push(message),onUnexpectedExit:(record,exit)=>unexpected.push({record,exit}),
    stopTimeoutMs:100,startTimeoutMs:100,pollMs:2,...options,
  });
  return {lifecycle,state,children,logs,unexpected};
}
async function healthy(base){
  return new Promise(resolve=>{const req=http.get(`${base}/api/health`,{timeout:100},res=>{res.resume();res.on('end',()=>resolve(res.statusCode===200));});req.on('error',()=>resolve(false));req.on('timeout',()=>req.destroy(new Error('test probe timeout')));});
}
async function listen(server,port=0){await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(port,'127.0.0.1',resolve);});return server.address().port;}
async function close(server){if(server.listening)await new Promise(resolve=>server.close(resolve));}

function mainAdapter(options={}){
  const children=[],notifications=[],logs=[],handlers=new Map();let manager,quitCalls=0;
  class TestLifecycle extends BackendLifecycle { constructor(deps){super({...deps,stopTimeoutMs:100,startTimeoutMs:100,pollMs:2,listening:async()=>false,terminate:async child=>child.emit('exit',0,null),...options});manager=this;} }
  const app=new EventEmitter();Object.assign(app,{isPackaged:false,getPath:()=>'/test-user-data',whenReady:()=>Promise.resolve(),quit:()=>{quitCalls++;}});
  const windows=[];
  class Window extends EventEmitter {constructor(){super();this.webContents=new EventEmitter();this.webContents.send=(channel,payload)=>notifications.push({channel,payload});this.webContents.setWindowOpenHandler=()=>{};windows.push(this);}loadFile(){}show(){}isDestroyed(){return false;}static getAllWindows(){return windows;}}
  const fileSystem={mkdirSync(){},appendFileSync(_file,text){logs.push(text);},readFileSync(){return '{}';},existsSync(){return true;},createWriteStream(){return new EventEmitter();}};
  const transport={request(_args,callback){const request=new EventEmitter();request.end=()=>queueMicrotask(()=>{if(!manager?.current?.child.ready){request.emit('error',new Error('test unavailable'));return;}const response=new EventEmitter();response.statusCode=200;response.setEncoding=()=>{};callback(response);response.emit('data','{}');response.emit('end');});return request;}};
  const context={require(name){if(name==='electron')return {app,BrowserWindow:Window,ipcMain:{handle:(name,fn)=>handlers.set(name,fn),on(){}},shell:{},dialog:{showErrorBox:()=>{}}};if(name==='./backend_lifecycle')return {BackendLifecycle:TestLifecycle};if(name==='fs')return fileSystem;if(name==='path')return path;if(name==='http'||name==='https')return transport;if(name==='child_process')return {spawn(){const child=new EventEmitter();child.pid=4000+children.length;child.ready=true;child.stdout={pipe(){}};child.stderr={pipe(){}};children.push(child);return child;}};throw new Error(`Unexpected module ${name}`);},__dirname:__dirname,process:{platform:'win32',pid:99,env:{APPDATA:'/test'},versions:{electron:'test',node:process.versions.node}},setTimeout,clearTimeout,Buffer,URL,console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'main.js'),'utf8'),context,{filename:'main.js'});
  return {app,children,notifications,logs,handlers,windows,get manager(){return manager;},get quitCalls(){return quitCalls;}};
}
async function mainSettled(h){await flush();await h.manager.queue;await flush();}

test('[真实独立Node进程] 旧服务1.8秒后才退出，restart等待实际exit，不能1.5秒复用',async()=>{
  const reserve=http.createServer();const port=await listen(reserve);await close(reserve);
  const base=`http://127.0.0.1:${port}`;const children=[],exits=[];
  const code="const h=require('http');const s=h.createServer((q,r)=>r.end('{}'));s.listen(Number(process.argv[1]),'127.0.0.1');process.on('message',m=>{if(m==='stop')setTimeout(()=>s.close(()=>process.exit(0)),Number(process.argv[2]));});";
  const lifecycle=new BackendLifecycle({getSettings:()=>({apiBase:base}),ownedBase:base,managed:()=>true,
    probeReady:healthy,start:()=>{const child=spawn(process.execPath,['-e',code,String(port),String(children.length?0:1800)],{windowsHide:true,stdio:['ignore','ignore','ignore','ipc']});children.push(child);child.once('exit',()=>exits.push({pid:child.pid,at:Date.now()}));return {child};},
    terminate:child=>new Promise((resolve,reject)=>child.send('stop',error=>error?reject(error):resolve())),stopTimeoutMs:5000,startTimeoutMs:5000,pollMs:10,
  });
  try{
    await lifecycle.ensure();const started=Date.now();const restart=lifecycle.restart();let settled=false;restart.finally(()=>{settled=true;});
    await delay(1550);assert.equal(settled,false);assert.equal(children.length,1);assert(await healthy(base));
    const result=await restart;assert(Date.now()-started>=1800);assert.equal(result.reused,false);assert.equal(children.length,2);assert.equal(exits[0].pid,children[0].pid);assert(await healthy(base));
  }finally{await lifecycle.shutdown();for(const child of children){if(child.exitCode===null&&child.signalCode===null){const exited=once(child,'exit');child.kill();await exited;}}}
});

test('[真实TCP+事件模型] child已exit但HTTP500仍监听，必须等TCP端口释放',async()=>{
  let server=http.createServer((_req,res)=>{res.statusCode=500;res.end('{}');});const port=await listen(server),base=`http://127.0.0.1:${port}`;
  let secondServer;const first=new EventEmitter();first.pid=2001;const spawned=[];
  const lifecycle=new BackendLifecycle({getSettings:()=>({apiBase:base}),ownedBase:base,managed:()=>true,probeReady:healthy,
    start:()=>{const child=new EventEmitter();child.pid=2002;spawned.push(child);secondServer=http.createServer((_q,r)=>r.end('{}'));secondServer.listen(port,'127.0.0.1');return {child};},
    terminate:async child=>{child.emit('exit',0,null);if(child!==first)await close(secondServer);},stopTimeoutMs:1000,startTimeoutMs:1000,pollMs:10,
  });
  lifecycle.track(first);
  try{const restart=lifecycle.restart();await delay(100);assert.equal(first.listenerCount('exit'),0);assert.equal(spawned.length,0);assert.equal(await healthy(base),false);assert.equal(await isEndpointListening(base),true);await close(server);const result=await restart;assert.equal(result.reused,false);assert.equal(spawned.length,1);assert(await healthy(base));}
  finally{await close(server);await close(secondServer||server);await lifecycle.shutdown();}
});

test('[事件模型] restart杀进程命令未完成时不得复用健康旧服务',async()=>{
  const command=deferred();const h=model({terminate:()=>command.promise});await h.lifecycle.ensure();const child=h.children[0];const restart=h.lifecycle.restart();await flush();assert.equal(h.children.length,1);assert(h.lifecycle.current.stopRequested);child.emit('exit',0,null);await flush();assert.equal(h.children.length,1);command.resolve();await restart;assert.equal(h.children.length,2);h.lifecycle.terminate=async child=>child.emit('exit',0,null);await h.lifecycle.shutdown();
});

test('[事件模型] 退出延迟和监听释放分别等待，只有两者完成才启动',async()=>{
  let listening=true;const h=model({terminate:async()=>{},listening:async()=>listening});await h.lifecycle.ensure();const restart=h.lifecycle.restart();await flush();assert.equal(h.children.length,1);h.children[0].emit('exit',0,null);await delay(10);assert.equal(h.children.length,1);listening=false;await restart;assert.equal(h.children.length,2);h.children[1].emit('exit',0,null);await h.lifecycle.shutdown();
});

test('[事件模型] 旧child迟到exit/error不清新child或安排恢复',async()=>{
  const h=model();await h.lifecycle.ensure();const old=h.children[0];await h.lifecycle.restart();const current=h.lifecycle.current;old.emit('exit',1,null);old.emit('error',new Error('late old failure'));assert.equal(h.lifecycle.current,current);assert.equal(h.unexpected.length,0);await h.lifecycle.shutdown();
});

test('[事件模型] 杀进程失败明确拒绝，保留own记录且无伪就绪',async()=>{
  const h=model({terminate:async()=>{throw new Error('kill denied');}});await h.lifecycle.ensure();const current=h.lifecycle.current;await assert.rejects(h.lifecycle.restart(),/kill denied/);assert.equal(h.children.length,1);assert.equal(h.lifecycle.current,current);assert(h.lifecycle.records.has(current));assert(h.logs.some(e=>e.includes('stop failed')));h.lifecycle.terminate=async child=>child.emit('exit',0,null);await h.lifecycle.shutdown();
});

test('[事件模型] 停止命令超时明确失败，禁止启动替代进程',async()=>{
  const h=model({terminate:()=>new Promise(()=>{})});await h.lifecycle.ensure();await assert.rejects(h.lifecycle.restart(),/停止后端命令超时/);assert.equal(h.children.length,1);h.lifecycle.terminate=async child=>child.emit('exit',0,null);await h.lifecycle.shutdown();
});

test('[事件模型] actual exit超时明确失败，禁止启动替代进程',async()=>{
  const h=model({terminate:async()=>{}});await h.lifecycle.ensure();await assert.rejects(h.lifecycle.restart(),/实际退出超时/);assert.equal(h.children.length,1);h.lifecycle.terminate=async child=>child.emit('exit',0,null);await h.lifecycle.shutdown();
});

test('[事件模型] TCP端口释放超时不把仍监听服务当作external复用',async()=>{
  const h=model({listening:async()=>true});await h.lifecycle.ensure();await assert.rejects(h.lifecycle.restart(),/端口释放超时/);assert.equal(h.children.length,1);assert.equal(h.lifecycle.records.size,1);await assert.rejects(h.lifecycle.ensure(),/端口释放超时/);assert.equal(h.children.length,1);h.lifecycle.listening=async()=>false;await h.lifecycle.shutdown();
});

test('[事件模型] 两个并发restart按次序读取新配置，无死锁、无漏重启',async()=>{
  const firstStop=deferred();let calls=0;const h=model({terminate:async child=>{if(!calls++)await firstStop.promise;child.emit('exit',0,null);}});await h.lifecycle.ensure();h.state.settings.model='second';const first=h.lifecycle.restart();await flush();h.state.settings.model='third';const second=h.lifecycle.restart();firstStop.resolve();const result=await Promise.all([first,second]);assert.equal(result.length,2);assert.equal(h.children.length,3);assert.equal(h.children.at(-1).configuration.model,'third');assert.equal(h.lifecycle.records.size,1);await h.lifecycle.shutdown();
});

test('[事件模型] 新配置在第一轮spawn之后保存，第二次restart不被吞并',async()=>{
  const h=model();await h.lifecycle.ensure();const firstReady=deferred();h.lifecycle.probeReady=async()=>{if(h.lifecycle.current&&h.children.length===2)return firstReady.promise;return Boolean(h.lifecycle.current?.child.ready);};
  h.state.settings.model='second';const first=h.lifecycle.restart();await delay(5);assert.equal(h.children.length,2);assert.equal(h.children[1].configuration.model,'second');h.state.settings.model='third';const second=h.lifecycle.restart();firstReady.resolve(true);await assert.rejects(first,/停止请求|正在停止/);await second;assert.equal(h.children.length,3);assert.equal(h.children[2].configuration.model,'third');await h.lifecycle.shutdown();
});

test('[事件模型] startup期间退出立即reject，不等60秒健康超时',async()=>{
  const h=model({startTimeoutMs:60000});h.lifecycle.probeReady=async()=>false;const startup=h.lifecycle.ensure();await delay(5);const at=Date.now();h.children[0].emit('exit',1,null);await assert.rejects(startup,/退出/);assert(Date.now()-at<100);assert.equal(h.unexpected.length,1);h.lifecycle.listening=async()=>false;await h.lifecycle.shutdown();
});

test('[事件模型] spawn error没有exit也结束startup并保留可排查错误',async()=>{
  const h=model();h.lifecycle.probeReady=async()=>false;const startup=h.lifecycle.ensure();await delay(5);h.children[0].pid=undefined;h.children[0].emit('error',new Error('ENOENT test'));await assert.rejects(startup,/ENOENT/);assert.equal(h.lifecycle.current,null);assert.equal(h.unexpected.length,1);await h.lifecycle.shutdown();
});

test('[事件模型] old record晚退出不能触发当前child的自动恢复',async()=>{
  const h=model();const old=new EventEmitter();old.pid=501;h.lifecycle.track(old);const next=new EventEmitter();next.pid=502;const current=h.lifecycle.track(next);old.emit('exit',70,null);assert.equal(h.lifecycle.current,current);assert.equal(h.unexpected.length,0);next.emit('exit',0,null);assert.equal(h.unexpected.length,1);await h.lifecycle.shutdown();
});

test('[事件模型] 已生成pid的kill/send error不是actual exit，不丢ownership或伪ready',async()=>{
  const h=model({terminate:async child=>child.emit('error',new Error('EPERM still alive'))});await h.lifecycle.ensure();const current=h.lifecycle.current;await assert.rejects(h.lifecycle.restart(),/实际退出超时/);assert.equal(h.lifecycle.current,current);assert.equal(current.exited,false);assert.equal(h.children.length,1);assert.equal(h.unexpected.length,0);h.lifecycle.terminate=async child=>child.emit('exit',0,null);await h.lifecycle.shutdown();
});

test('[事件模型] 主动stop造成code1不恢复；非主动code0丢失服务允许恢复',async()=>{
  const h=model({terminate:async child=>child.emit('exit',1,null)});await h.lifecycle.ensure();await h.lifecycle.restart();assert.equal(h.unexpected.length,0);h.children[1].emit('exit',0,null);assert.equal(h.unexpected.length,1);await h.lifecycle.recover(h.unexpected[0].record);assert.equal(h.children.length,3);await h.lifecycle.shutdown();
});

test('[事件模型] 迟到auto recovery跨过手动restart代次时跳过',async()=>{
  const h=model();await h.lifecycle.ensure();h.children[0].emit('exit',1,null);const old=h.unexpected[0].record;await h.lifecycle.restart();const current=h.lifecycle.current;assert.equal(await h.lifecycle.recover(old),null);assert.equal(h.lifecycle.current,current);assert.equal(h.children.length,2);await h.lifecycle.shutdown();
});

test('[事件模型] shutdown同步阻止排队ensure/restart和auto recovery复活',async()=>{
  const h=model();await h.lifecycle.ensure();const queuedEnsure=h.lifecycle.ensure();const queuedRestart=h.lifecycle.restart();const rejects=Promise.all([assert.rejects(queuedEnsure,/关闭/),assert.rejects(queuedRestart,/关闭/)]);await h.lifecycle.shutdown();await rejects;assert.equal(h.children.length,1);assert.equal(h.lifecycle.current,null);await assert.rejects(h.lifecycle.ensure(),/关闭/);
});

test('[事件模型] shutdown在startup健康等待期间到达，不返回ready也不恢复',async()=>{
  const health=deferred();const h=model();let probes=0;h.lifecycle.probeReady=async()=>++probes===1?false:health.promise;const startup=h.lifecycle.ensure();const rejection=assert.rejects(startup,/退出|关闭/);await delay(5);await h.lifecycle.shutdown();health.resolve(true);await rejection;assert.equal(h.children.length,1);assert.equal(h.unexpected.length,0);
});

test('[事件模型] 外部健康服务ensure/restart/shutdown均不启动也不强停',async()=>{
  const h=model();h.state.external=true;h.state.settings.apiBase='http://localhost:18002';assert.equal((await h.lifecycle.ensure()).reused,true);assert.equal((await h.lifecycle.restart()).reused,true);await h.lifecycle.shutdown();assert.equal(h.children.length,0);assert.equal(h.state.killCount,0);assert(h.logs.some(e=>e.includes('ownership=external')));
});

test('[事件模型] 不可达自定义本地/远端服务不误拉8000或杀external',async()=>{
  for(const apiBase of ['http://localhost:18002','https://example.test','http://127.0.0.1:8000/other']){const h=model();h.state.settings.apiBase=apiBase;await assert.rejects(h.lifecycle.ensure(),/外部后端不可达/);await assert.rejects(h.lifecycle.restart(),/外部后端不可达/);await h.lifecycle.shutdown();assert.equal(h.children.length,0);assert.equal(h.state.killCount,0);}
});

test('[事件模型] 修改apiBase后仍只等待owned原监听目标，不停止新外部服务',async()=>{
  const endpoints=[];const h=model({listening:async base=>{endpoints.push(base);return false;}});await h.lifecycle.ensure();h.state.settings.apiBase='http://localhost:18002';h.state.external=true;await h.lifecycle.restart();assert.deepEqual(endpoints,['http://127.0.0.1:8000']);assert.equal(h.state.killCount,1);assert.equal(h.children.length,1);await h.lifecycle.shutdown();
});

test('[真实独立Node进程] 平台停止命令成功后确有actual exit',async()=>{
  const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{windowsHide:true,stdio:'ignore'});const exited=once(child,'exit');try{await terminateProcessTree(child);const [code,signal]=await exited;assert(code!==null||signal!==null);}finally{if(child.exitCode===null&&child.signalCode===null){child.kill();await exited;}}
});

test('[纯函数] 仅本地默认http8000作为managed，其他端口/协议/路径均external',async()=>{
  assert(isManagedBase('http://127.0.0.1:8000'));assert(isManagedBase('http://localhost:8000/'));for(const base of ['http://localhost:18002','https://127.0.0.1:8000','http://example.test:8000','http://127.0.0.1:8000/sub'])assert.equal(isManagedBase(base),false);
});

test('[main真实源码VM适配] restart IPC等待child退出才通知ready，没有固定1.5s复用',async()=>{
  const command=deferred();const h=mainAdapter({terminate:async child=>{await command.promise;child.emit('exit',0,null);}});await mainSettled(h);assert.equal(h.children.length,1);const readyBefore=h.notifications.filter(e=>e.channel==='backend:ready').length;
  const restart=h.handlers.get('backend:restart')();await flush();assert.equal(h.children.length,1);assert.equal(h.notifications.filter(e=>e.channel==='backend:ready').length,readyBefore);command.resolve();const result=await restart;assert.equal(result.reused,false);assert.equal(h.children.length,2);assert.equal(h.notifications.filter(e=>e.channel==='backend:ready').length,readyBefore+1);await h.manager.shutdown();
});

test('[main真实源码VM适配] restart停止失败向IPC报错，不发送伪ready',async()=>{
  const h=mainAdapter({terminate:async()=>{throw new Error('adapter kill failure');}});await mainSettled(h);const count=h.notifications.filter(e=>e.channel==='backend:ready').length;await assert.rejects(h.handlers.get('backend:restart')(),/adapter kill failure/);assert.equal(h.notifications.filter(e=>e.channel==='backend:ready').length,count);assert(h.logs.some(e=>e.includes('stop failed')));h.manager.terminate=async child=>child.emit('exit',0,null);await h.manager.shutdown();
});

test('[main真实源码VM适配] before-quit阻止过早退出，等待清理且重复quit不重复任务',async()=>{
  const command=deferred();const h=mainAdapter({terminate:async child=>{await command.promise;child.emit('exit',0,null);}});await mainSettled(h);let prevented=0;h.app.emit('before-quit',{preventDefault(){prevented++;}});h.app.emit('before-quit',{preventDefault(){prevented++;}});await flush();assert.equal(h.quitCalls,0);assert.equal(prevented,2);command.resolve();await h.manager.shutdown();await flush();assert.equal(h.quitCalls,1);h.app.emit('before-quit',{preventDefault(){throw new Error('already cleaned');}});
});

test('[main真实源码VM适配] 窗口关闭fire-and-forget的失败有catch日志，不复活后端',async()=>{
  const h=mainAdapter({terminate:async()=>{throw new Error('close kill failure');}});await mainSettled(h);h.windows[0].emit('closed');await flush();assert(h.logs.some(e=>e.includes('shutdown failed reason=window-closed')&&e.includes('close kill failure')));assert(h.manager.closing);assert.equal(h.children.length,1);h.children[0].emit('exit',1,null);await flush();assert.equal(h.children.length,1);
});

test('[main真实源码VM适配] 异常exit可恢复，但迟到旧error不能通知或破坏新child',async()=>{
  const h=mainAdapter();await mainSettled(h);const old=h.children[0];old.emit('exit',1,null);await mainSettled(h);assert.equal(h.children.length,2);const current=h.manager.current,notifications=h.notifications.length;old.emit('error',new Error('late old error'));old.emit('exit',70,null);await flush();assert.equal(h.manager.current,current);assert.equal(h.notifications.length,notifications);assert(h.logs.some(e=>e.includes('reason=unexpected-exit')));await h.manager.shutdown();
});

(async()=>{let failed=0;for(const {name,run}of tests){try{await run();console.log(`[OK] ${name}`);}catch(error){failed++;console.error(`[FAIL] ${name}\n${error.stack}`);}}console.log(`结果: ${tests.length-failed}/${tests.length} 通过；事件模型与独立Node/TCP，不是真COM测试`);process.exitCode=failed?1:0;})().catch(error=>{console.error(error);process.exitCode=1;});

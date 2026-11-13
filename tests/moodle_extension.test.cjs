const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('windows/wb-extension/moodle.js', 'utf8');
function fixture() {
  const local = {enabled: true, token: 'test', server: 'https://jarvis.test'}, session = {}, commands = [];
  const tab = {id: 7, url: 'https://moodle.test/course/view.php?id=42', status:'complete'};
  const page = {course:'42', width:800, height:600, links:['https://moodle.test/mod/quiz/view.php?id=1','https://moodle.test/mod/quiz/view.php?id=2']};
  let focus = true, href = '';
  const storage = values => ({get: async keys => Object.fromEntries((Array.isArray(keys)?keys:[keys]).filter(k=>k in values).map(k=>[k, structuredClone(values[k])])),
    set: async value => Object.assign(values, structuredClone(value)), remove: async key => {delete values[key];}});
  let onMessage;
  const listener = {addListener() {}};
  const context = vm.createContext({URL, Date, Number, Uint8Array, String, crypto:require('node:crypto').webcrypto, btoa, fetch: async()=>({blob: async()=>({})}),
    setTimeout: f => queueMicrotask(f), createImageBitmap: async()=>({close(){}}),
    OffscreenCanvas: class {getContext(){return {drawImage(){}}} async convertToBlob(){return {arrayBuffer: async()=>new Uint8Array([255,216,1]).buffer}}},
    chrome:{storage:{local:storage(local),session:storage(session)},
      tabs:{query:async()=>[tab],get:async()=>tab, update:async(id, options)=>{assert.equal(id,7);Object.assign(tab,options);}},
      scripting:{executeScript:async({func})=>[{result:func.toString().includes('activeElement') ? focus : func.toString().includes('elementFromPoint') ? {href,password:false} : page}]},
      debugger:{attach:async()=>{},detach:async()=>{},onDetach:listener,sendCommand:async(target,name,args)=>{commands.push({name,args});return {data:'/9gB'}}},
      alarms:{onAlarm:listener},runtime:{id:'ext',onMessage:{addListener(fn){onMessage=fn;}}}}});
  vm.runInContext(code, context);
  vm.runInContext('moodleTick = async () => {}; moodlePost = async () => ({ok:true})', context);
  let seq=0;
  return {local,session,tab,page,commands, context,
    message:type=>new Promise(resolve=>onMessage({type},{id:'ext'},resolve)),
    bind:()=>vm.runInContext(`moodleBind({id:7,url:'${tab.url}'})`,context),
    act: async(action,id=String(++seq))=> {
      context.job={id,run:'run',expires:Date.now()/1000+20,action};
      return vm.runInContext("chrome.storage.session.get('moodle').then(({moodle:s})=>moodleAction(s,job))",context);
    }, focus:value=>{focus=value;},href:value=>{href=value;}};
}
test('pin course, capture CSS screenshot, open first quiz, fill and advance locally',async()=>{
  const f=fixture();await f.bind();
  let shot=await f.act({action:'snapshot'});
  assert.equal(shot.width,800);assert.equal(shot.quiz,null);
  assert.ok(!JSON.stringify(shot).includes('moodle.test'));
  shot=await f.act({action:'open'});assert.equal(shot.quiz,'test-1');
  await f.act({action:'click',x:10,y:20});
  await f.act({action:'type',text:'answer'});
  assert.equal(f.commands.find(c=>c.name==='Input.insertText').args.text,'answer');
  assert.equal((await f.act({action:'next'})).quiz,'test-2');
  assert.equal((await f.act({action:'next'})).done,true);
});
test('stopped connection, wrong course and wrong quiz never click',async()=>{
  const f=fixture();await f.bind();await f.act({action:'open'});
  f.page.course='99';await assert.rejects(f.act({action:'click',x:10,y:10}),/другой курс/);
  f.page.course='42';f.tab.url='https://moodle.test/mod/quiz/view.php?id=2';
  await assert.rejects(f.act({action:'click',x:10,y:10}),/другому тесту/);
  f.local.enabled=false;await assert.rejects(f.act({action:'click',x:10,y:10}),/остановлено/);
  assert.ok(!f.commands.some(c=>c.name==='Input.dispatchMouseEvent'));
});
test('duplicate clicks and arbitrary navigation are rejected',async()=>{
  const f=fixture();await f.bind();await f.act({action:'open'});
  await f.act({action:'click',x:10,y:10},'same');
  await assert.rejects(f.act({action:'click',x:10,y:10},'same'),/Повтор/);
  const before=f.commands.length;f.href('https://other.test/');
  await assert.rejects(f.act({action:'click',x:10,y:10}),/Переход вне/);
  assert.equal(f.commands.length,before);
});
test('password/non-answer focus, out-of-bounds coordinates and executable actions fail',async()=>{
  const f=fixture();await f.bind();await f.act({action:'open'});
  f.focus(false);await assert.rejects(f.act({action:'type',text:'secret'}),/текстовое поле/);
  await assert.rejects(f.act({action:'click',x:1000,y:10}),/координаты/);
  await assert.rejects(f.act({action:'eval',text:'evil()'}),/Неизвестная/);
});

test('bind errors survive popup closure and explain wrong page or missing PC connection',async()=>{
  const f=fixture();f.tab.url='https://moodle.test/mod/quiz/view.php?id=1';
  let reply=await f.message('moodleBind');
  assert.equal(reply.ok,false);assert.match(f.local.moodleStatus,/страницу курса/);
  assert.equal(f.session.moodle,undefined);
  await new Promise(resolve=>setImmediate(resolve));
  f.local.enabled=false;
  reply=await f.message('moodleBind');
  assert.equal(reply.ok,false);assert.match(f.local.moodleStatus,/Сначала подключите/);
});
test('server failure is visible and rolls back the debugger session',async()=>{
  const f=fixture();vm.runInContext("moodlePost = async () => {throw new Error('HTTP 404: сервер не обновлён');}",f.context);
  const reply=await f.message('moodleBind');
  assert.equal(reply.ok,false);assert.match(f.local.moodleStatus,/404/);
  assert.equal(f.session.moodle,undefined);
});
test('successful bind reports test count only after server confirmation',async()=>{
  const f=fixture();const reply=await f.message('moodleBind');
  assert.equal(reply.ok,true);assert.match(f.local.moodleStatus,/Найдено тестов: 2/);
});

test('stop while debugger is attaching cancels bind and releases the debugger',async()=>{
  const f=fixture();let release, attached=false, detached=false;
  f.context.chrome.debugger.attach=()=>new Promise(resolve=>{release=()=>{attached=true;resolve();};});
  f.context.chrome.debugger.detach=async()=>{detached=true;};
  const pending=f.message('moodleBind');
  await new Promise(resolve=>setImmediate(resolve));
  await f.message('moodleStop');release();
  assert.equal((await pending).ok,false);
  assert.ok(attached && detached);assert.equal(f.session.moodle,undefined);
});
test('inspect reads the page without assigning a run, navigating, typing or clicking',async()=>{
  const f=fixture();await f.bind();
  f.page.courseTitle='Облака';f.page.tests=[{title:'Тест 1'}];
  const before=structuredClone(f.session.moodle), url=f.tab.url;
  const result=await f.act({action:'inspect'});
  assert.equal(result.course,'Облака');assert.equal(result.tests[0].title,'Тест 1');
  assert.deepEqual(f.session.moodle,before);assert.equal(f.tab.url,url);
  assert.ok(f.commands.every(c=>c.name==='Page.captureScreenshot'));
});
test('scroll chooses the central scrollable pane and falls back to the document',()=>{
  const f=fixture();const calls=[];
  const root={scrollBy:options=>calls.push(['root',options.top])};
  const pane={scrollHeight:1800,clientHeight:600,parentElement:root,scrollBy:options=>calls.push(['pane',options.top])};
  const child={parentElement:pane};
  f.context.document={body:{},documentElement:root,scrollingElement:root,elementFromPoint:()=>child};
  f.context.innerWidth=800;f.context.innerHeight=600;
  f.context.getComputedStyle=e=>({overflowY:e===pane?'auto':'visible'});
  vm.runInContext('moodleScroll(480)',f.context);
  assert.deepEqual(calls,[['pane',480]]);
  pane.scrollHeight=600;
  vm.runInContext('moodleScroll(-480)',f.context);
  assert.deepEqual(calls,[['pane',480],['root',-480]]);
});

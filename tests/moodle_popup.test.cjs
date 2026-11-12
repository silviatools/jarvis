const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('windows/wb-extension/popup.js','utf8');
function popup(store={}, send=async()=>({ok:false,error:'Откройте страницу курса Moodle.'})) {
  const elements={}, intervals=[], listeners=[];
  const getElement=id=>elements[id] ||= {value:'',textContent:'',disabled:false,handlers:{},addEventListener(type,fn){this.handlers[type]=fn;}};
  const local={get:async keys=>Object.fromEntries((Array.isArray(keys)?keys:[keys]).filter(k=>k in store).map(k=>[k,store[k]])),
    set:async values=>{Object.assign(store,values);for(const listener of listeners)listener(Object.fromEntries(Object.entries(values).map(([k,v])=>[k,{newValue:v}])),'local');}};
  const ctx=vm.createContext({document:{getElementById:getElement}, URL, Date, Promise, Error,
    setInterval:fn=>intervals.push(fn), setTimeout:()=>1,clearTimeout:()=>{},
    chrome:{storage:{local,onChanged:{addListener:fn=>listeners.push(fn)}},runtime:{getManifest:()=>({version:'0.4.1'}),sendMessage:send}}});
  vm.runInContext(code,ctx);
  return {elements,intervals,local,store};
}
test('bind shows progress immediately and preserves failure through refresh and reopen',async()=>{
  let resolve;
  const p=popup({},()=>new Promise(r=>{resolve=r;}));
  await new Promise(r=>setImmediate(r));
  const click=p.elements.moodleBind.handlers.click();
  assert.match(p.elements.moodleStatus.textContent,/Подключаю/);
  assert.equal(p.elements.moodleBind.disabled,true);
  resolve({ok:false,error:'Откройте страницу курса Moodle.'});await click;
  assert.equal(p.elements.moodleBind.disabled,false);
  for(const refresh of p.intervals)await refresh();
  assert.match(p.elements.moodleStatus.textContent,/страницу курса/);
  const reopened=popup(p.store);await new Promise(r=>setImmediate(r));
  assert.match(reopened.elements.moodleStatus.textContent,/страницу курса/);
});
test('unavailable worker gives persistent reload instructions',async()=>{
  const p=popup({},async()=>{throw new Error('Could not establish connection. Receiving end does not exist.');});
  await p.elements.moodleBind.handlers.click();
  assert.match(p.store.moodleStatus,/chrome:\/\/extensions/);
});
test('successful response does not replace detailed worker status with generic Done',async()=>{
  const p=popup({},async()=>{await p.local.set({moodleStatus:'Курс подключён. Найдено тестов: 2.'});return {ok:true};});
  await p.elements.moodleBind.handlers.click();
  assert.match(p.elements.moodleStatus.textContent,/Найдено тестов: 2/);
});

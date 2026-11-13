const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('index (9).html','utf8');
function setup(){
 const context={fetch:null, JSON, console, reportAiUsage(){},lastVoiceUserText:()=>'',buildVoiceAgentSystemPrompt:()=>'',
 executeVoiceTool:async()=>({data:{},output:{ok:true,tests:[{title:'Тест 1'}],_image:{media_type:'image/jpeg',b64:'IMAGE_FIXTURE'}}})};
 for(const name of ['Tg','Max','Market','Watch','Mail','Plan','Moodle','Route'])context['load'+name+'Tools']=async()=>{};
 for(const name of ['TG','MAX','MARKET','WATCH','MAIL','PLAN','MOODLE','ROUTE','VOICE'])context[name+'_AGENT_TOOLS']=[];
 return vm.createContext(context);
}
test('Claude voice inspection sends image blocks instead of base64 in tool JSON',async()=>{
 const c=setup(),requests=[];
 c.fetch=async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>requests.length===1?
 {content:[{type:'tool_use',id:'1',name:'moodle_inspect',input:{}}]}:{content:[{type:'text',text:'Виден тест 1'}]}}};
 const start=source.indexOf('async function runVoiceAgentClaude('),end=source.indexOf('// Те же инструменты',start);
 vm.runInContext(source.slice(start,end),c);
 await vm.runInContext("runVoiceAgentClaude([], 'key', 'model', {}, null, 3)",c);
 const blocks=requests[1].messages.at(-1).content[0].content;
 assert.equal(blocks[1].type,'image');assert.equal(blocks[1].source.data,'IMAGE_FIXTURE');
 assert.ok(!blocks[0].text.includes('IMAGE_FIXTURE'));
});
test('OpenAI voice inspection appends image after all tool responses',async()=>{
 const c=setup(),requests=[];
 c.openaiVoiceStep=async(key,model,effort,messages)=>{requests.push(JSON.parse(JSON.stringify(messages)));return requests.length===1?
 {content:'',toolCalls:[{id:'1',function:{name:'moodle_inspect',arguments:'{}'}}]}:{content:'Виден тест 1',toolCalls:[]}};
 const start=source.indexOf('async function runVoiceAgentOpenAI('),end=source.indexOf('async function callAI(',start);
 assert.ok(start>=0);
 vm.runInContext(source.slice(start,end),c);
 await vm.runInContext("runVoiceAgentOpenAI([], 'key', 'model', {}, null, 3)",c);
 const messages=requests[1];
 assert.equal(messages.at(-1).content[1].image_url.url,'data:image/jpeg;base64,IMAGE_FIXTURE');
 assert.ok(!messages.find(m=>m.role==='tool').content.includes('IMAGE_FIXTURE'));
});

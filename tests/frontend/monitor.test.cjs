const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

class Element {
  constructor() { this.textContent = ''; this.value = ''; this.children = []; }
  replaceChildren() { this.children = []; }
  append(child) { this.children.push(child); }
}
class Shadow {
  constructor() { this.nodes = Object.fromEntries(['select','#clear','#pause','#status','#error','pre'].map(id => [id, new Element()])); }
  querySelector(id) { return this.nodes[id]; }
}
function createHarness(gateways = [{entry_id:'one',title:'Gateway <script>'},{entry_id:'two',title:'Second'}]) {
  let Constructor;
  const context = vm.createContext({
    HTMLElement: class { attachShadow() { this.shadowRoot = new Shadow(); } },
    customElements: { define(name, cls) { assert.equal(name,'coselig-modbus-monitor'); Constructor=cls; } },
    document: { createElement() { return new Element(); } }, Date, String,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../../custom_components/coselig_modbus_hub/monitor.js'),'utf8'),context);
  const calls=[],subscriptions=[];
  const hass={ language:'zh-TW',callWS:async (request) => { calls.push(request); return request.type.endsWith('/list') ? gateways : undefined; },
    connection:{subscribeMessage:async(callback,request) => { const sub={callback,request,closed:false}; subscriptions.push(sub); return async() => {sub.closed=true;}; }} };
  const panel=new Constructor(); panel.isConnected=true; panel.connectedCallback(); panel.hass=hass;
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  return {panel,hass,calls,subscriptions,flush};
}
const snapshot = (hex='33 06 08 2A 05 50') => ({connected:true,poll_interval:0,polling_paused:true,entries:[
  {timestamp:'2026-10-14T06:32:08.214Z',direction:'TX',raw_hex:hex,latency_ms:null},
  {timestamp:'2026-10-14T06:32:08.246Z',direction:'OK',raw_hex:'',latency_ms:32},
]});

test('raw HEX and latency render as text; pause freezes only the view',async()=>{
  const h=createHarness(); await h.flush();
  assert.equal(h.subscriptions.length,1);
  assert.equal(h.panel.shadowRoot.querySelector('select').children[0].textContent,'Gateway <script>');
  h.subscriptions[0].callback(snapshot());
  const pre=h.panel.shadowRoot.querySelector('pre');
  assert.match(pre.textContent,/33 06 08 2A 05 50/); assert.match(pre.textContent,/32.0 ms/);
  assert.match(h.panel.shadowRoot.querySelector('#status').textContent,/背景輪詢已暫停/);
  const before=pre.textContent;
  h.panel.shadowRoot.querySelector('#pause').onclick();
  h.subscriptions[0].callback(snapshot('80 80'));
  assert.equal(pre.textContent,before); assert.equal(h.subscriptions[0].closed,false);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  assert.match(pre.textContent,/80 80/);
  await h.panel.shadowRoot.querySelector('#clear').onclick();
  assert.equal(pre.textContent,'');
  assert.ok(h.calls.every(call=>['coselig_modbus_hub/monitor/list','coselig_modbus_hub/monitor/clear'].includes(call.type)));
  h.panel.disconnectedCallback(); await h.flush(); assert.equal(h.subscriptions[0].closed,true);
});

test('switch gateway ignores old events; reconnect maintains one active subscription',async()=>{
  const h=createHarness(); await h.flush();
  await h.panel._select('two');
  assert.equal(h.subscriptions[0].closed,true);
  h.subscriptions[0].callback(snapshot('STALE'));
  assert.doesNotMatch(h.panel.shadowRoot.querySelector('pre').textContent,/STALE/);
  h.subscriptions[1].callback(snapshot('NEW'));
  assert.match(h.panel.shadowRoot.querySelector('pre').textContent,/NEW/);
  h.panel.hass=h.hass; await h.flush(); assert.equal(h.subscriptions.length,2);
  h.panel.disconnectedCallback(); h.panel.connectedCallback(); await h.flush();
  assert.equal(h.subscriptions.filter(sub=>!sub.closed).length,1);
  h.panel.disconnectedCallback();
});

test('no loaded gateway and server errors do not invent controls',async()=>{
  const h=createHarness([]); await h.flush();
  assert.equal(h.subscriptions.length,0);
  assert.match(h.panel.shadowRoot.querySelector('#status').textContent,/尚無/);
  await h.panel._clear(); assert.equal(h.calls.length,1);
  h.hass.callWS=async()=>{throw new Error('unauthorized');};
  await h.panel._connect(); assert.equal(h.panel.shadowRoot.querySelector('#error').textContent,'unauthorized');
  h.panel.disconnectedCallback();
});

test('late subscription resolution after detach is unsubscribed',async()=>{
  const h=createHarness(); await h.flush();
  let finish; let closed=false;
  h.hass.connection.subscribeMessage=()=>new Promise(resolve=>{finish=resolve;});
  const pending=h.panel._select('two');
  h.panel.disconnectedCallback();
  finish(async()=>{closed=true;}); await pending;
  assert.equal(closed,true);
});

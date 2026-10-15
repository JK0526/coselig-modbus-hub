const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const monitorSource = fs.readFileSync(path.join(__dirname, '../../custom_components/coselig_modbus_hub/monitor.js'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this._text = '';
    this.value = '';
    this.checked = tag === 'input';
    this.children = [];
    this.dataset = {};
    this.className = '';
    this.scrollTop = 0;
    this.clientHeight = 60;
    this._scrollHeight = null;
    this._rect = null;
    this.scrollIntoViewCalls = [];
    this.parentNode = null;
    this.disabled = false;
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get scrollHeight() { return this._scrollHeight ?? this.children.length * 10; }
  set scrollHeight(value) { this._scrollHeight = value; }
  replaceChildren(...children) { this._text = ''; this.children = children; for (const child of children) child.parentNode = this; }
  append(...children) { this.children.push(...children); for (const child of children) child.parentNode = this; }
  scrollIntoView(options) { this.scrollIntoViewCalls.push(options); }
  getBoundingClientRect() {
    if (this._rect) return this._rect;
    if (this.parentNode) {
      const index = this.parentNode.children.indexOf(this);
      const top = this.parentNode.getBoundingClientRect().top + index * 10 - this.parentNode.scrollTop;
      return { top, bottom: top + 10 };
    }
    return { top: 0, bottom: this.clientHeight };
  }
}

const selectors = [
  '#gateway', '#slave-filter', '#type-filter', '#clear', '#pause', '#auto-scroll',
  '#error', '#trace', '#gateway-status', '#gateway-poll', '#gateway-devices',
  '#gateway-last', '#gateway-average', '#gateway-queue', '#gateway-queue-peak',
  '#stat-success', '#stat-failed', '#stat-timeout', '#stat-crc', '#stat-reconnect',
  '#devices', '#device-summary', '#decoder', '#sidebar', '#decoder-card', '#stats-caption', '#stats-grid',
];
class Shadow {
  constructor() { this.nodes = Object.fromEntries(selectors.map((id) => [id, new Element(id === '#auto-scroll' ? 'input' : 'div')])); }
  querySelector(id) { return this.nodes[id]; }
}

function createHarness(gateways = [{ entry_id: 'one', title: 'Gateway <script>' }, { entry_id: 'two', title: 'Second' }]) {
  let Constructor;
  const context = vm.createContext({
    HTMLElement: class { attachShadow() { this.shadowRoot = new Shadow(); } },
    customElements: { define(name, cls) { assert.equal(name, 'coselig-modbus-monitor'); Constructor = cls; } },
    document: { createElement(tag) { return new Element(tag); } }, Date, String,
  });
  vm.runInContext(monitorSource, context);
  const calls = [], subscriptions = [];
  const hass = {
    language: 'zh-TW',
    callWS: async (request) => { calls.push(request); return request.type.endsWith('/list') ? gateways : undefined; },
    connection: {
      subscribeMessage: async (callback, request) => {
        const sub = { callback, request, closed: false };
        subscriptions.push(sub);
        return async () => { sub.closed = true; };
      },
    },
  };
  const panel = new Constructor();
  panel.isConnected = true;
  panel.connectedCallback();
  panel.hass = hass;
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  return { panel, hass, calls, subscriptions, flush, Constructor };
}

const entry = (direction, rawHex = '', latency = null, source = 'poll') => ({
  timestamp: '2026-10-14T06:32:08.214Z', direction, raw_hex: rawHex, latency_ms: latency, source,
});
const snapshot = (entries = [entry('TX', '33 06 08 2A 05 50'), entry('OK', '', 32)]) => ({
  connected: true, poll_interval: 2, polling_paused: false,
  coordinator: {
    configured_slaves: 2, available_slaves: 1, last_poll_round_ms: 218.4,
    average_poll_round_ms: 230.9, queue_depth: 0, queue_peak: 24,
  },
  statistics: { successful_requests: 1584, failed_requests: 4, timeout_count: 3, crc_error_count: 1, reconnect_count: 2 },
  devices: [
    { slave: 63, model: 'r410', group_a: null, group_b: null, available: true, consecutive_failures: 0 },
    { slave: 70, model: 'p404', group_a: 'single', group_b: 'single', available: false, consecutive_failures: 2 },
  ],
  entries,
});

test('raw trace renders safely; pause freezes presentation but subscription continues', async () => {
  const h = createHarness(); await h.flush();
  assert.equal(h.subscriptions.length, 1);
  assert.equal(h.panel.shadowRoot.querySelector('#gateway').children[0].textContent, 'Gateway <script>');
  h.subscriptions[0].callback(snapshot());
  const trace = h.panel.shadowRoot.querySelector('#trace');
  assert.match(trace.textContent, /33 06 08 2A 05 50/);
  assert.match(trace.textContent, /32\.0 ms/);
  const previous = trace.textContent;
  h.panel.shadowRoot.querySelector('#pause').onclick();
  h.subscriptions[0].callback(snapshot([entry('TX', '80 80')]));
  assert.equal(trace.textContent, previous);
  assert.equal(h.subscriptions[0].closed, false);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  assert.match(trace.textContent, /80 80/);
  await h.panel.shadowRoot.querySelector('#clear').onclick();
  assert.equal(trace.children.some((row) => row.className.startsWith('trace-entry')), false);
  assert.equal(h.panel._history.length, 0);
  assert.equal(h.panel._transactions.length, 0);
  assert.ok(h.calls.every((call) => ['coselig_modbus_hub/monitor/list', 'coselig_modbus_hub/monitor/clear'].includes(call.type)));
  h.panel.disconnectedCallback(); await h.flush(); assert.equal(h.subscriptions[0].closed, true);
});

test('fixed trace colors and theme-aware card styles remain present', () => {
  assert.match(monitorSource, /\.trace-tx,\.trace-raw-tx\{color:#42a5f5\}/);
  assert.match(monitorSource, /\.trace-rx,\.trace-ok,\.trace-raw-rx\{color:#66bb6a\}/);
  assert.match(monitorSource, /\.trace-timeout\{color:#ffb74d\}/);
  assert.match(monitorSource, /\.trace-crc-error,\.trace-error\{color:#ef5350\}/);
  assert.match(monitorSource, /\.trace-time,\.trace-latency\{color:var\(--secondary-text-color\);/);
  assert.match(monitorSource, /background:var\(--card-background-color\)/);
});

test('TX, RX, OK and every error status receive presentation classes', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot([
    entry('TX', 'AA BB'), entry('RX', 'CC DD'), entry('OK'), entry('TIMEOUT'), entry('CRC ERROR'), entry('ERROR'),
  ]));
  const rows = h.panel.shadowRoot.querySelector('#trace').children;
  assert.deepEqual([...rows].map((row) => row.className), [
    'trace-entry trace-tx', 'trace-entry trace-rx', 'trace-entry trace-ok',
    'trace-entry trace-timeout', 'trace-entry trace-crc-error', 'trace-entry trace-error',
  ]);
  assert.equal(rows[0].children[2].className, 'trace-raw trace-raw-tx');
  assert.equal(rows[1].children[2].className, 'trace-raw trace-raw-rx');
  assert.equal(rows[0].children[0].className, 'trace-time');
  h.panel.disconnectedCallback();
});

test('diagnostic cards render gateway runtime metrics, counters and slave health', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  const node = (id) => h.panel.shadowRoot.querySelector(id).textContent;
  assert.equal(node('#gateway-status'), '● 已連線');
  assert.equal(node('#gateway-poll'), '2.0 秒');
  assert.equal(node('#gateway-devices'), '1 / 2 在線');
  assert.equal(node('#gateway-last'), '218.4 ms');
  assert.equal(node('#gateway-average'), '230.9 ms');
  assert.equal(node('#gateway-queue'), '0');
  assert.equal(node('#gateway-queue-peak'), '24');
  assert.equal(node('#stat-success'), '1584'); assert.equal(node('#stat-failed'), '4');
  assert.equal(node('#stat-timeout'), '3'); assert.equal(node('#stat-crc'), '1'); assert.equal(node('#stat-reconnect'), '2');
  assert.match(monitorSource, /<div id="stats-caption" class="section-caption">自本次啟動起累計<\/div>/);
  assert.doesNotMatch(monitorSource, /本次執行期間/);
  const rows = h.panel.shadowRoot.querySelector('#devices').children;
  assert.match(rows[0].textContent, /#63 R410.*在線/);
  assert.match(rows[1].textContent, /#70 P404.*離線.*失敗 2 次/);
  h.panel.disconnectedCallback();
});

test('Gateway card independently shows connected polling state, health and queue values', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-status').textContent, '● 已連線');
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-poll').textContent, '2.0 秒');
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-devices').textContent, '1 / 2 在線');
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-queue-peak').textContent, '24');
  h.panel.disconnectedCallback();
});

test('communication counters are separate runtime values', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  assert.deepEqual([
    h.panel.shadowRoot.querySelector('#stat-success').textContent,
    h.panel.shadowRoot.querySelector('#stat-failed').textContent,
    h.panel.shadowRoot.querySelector('#stat-timeout').textContent,
    h.panel.shadowRoot.querySelector('#stat-crc').textContent,
    h.panel.shadowRoot.querySelector('#stat-reconnect').textContent,
  ], ['1584', '4', '3', '1', '2']);
  h.panel.disconnectedCallback();
});

test('Device Health lists each configured Slave once with failure state', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  const text = h.panel.shadowRoot.querySelector('#devices').textContent;
  assert.match(text, /#63 R410.*在線/);
  assert.match(text, /#70 P404.*離線.*失敗 2 次/);
  assert.equal(h.panel.shadowRoot.querySelector('#devices').children.length, 2);
  h.panel.disconnectedCallback();
});

test('Slave filter retains its associated RX and status transaction rows', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 01 00 00 00 04'), entry('RX', '3F 01 01 02'), entry('OK', '', 12), entry('TX', '46 03 00 00 00 01'), entry('TIMEOUT')]));
  h.panel.shadowRoot.querySelector('#slave-filter').value = '63';
  h.panel.shadowRoot.querySelector('#slave-filter').onchange();
  assert.equal(h.panel.shadowRoot.querySelector('#trace').children.length, 3);
  assert.match(h.panel.shadowRoot.querySelector('#trace').textContent, /OK/);
  h.panel.shadowRoot.querySelector('#type-filter').value = 'Error';
  h.panel.shadowRoot.querySelector('#slave-filter').value = 'all';
  h.panel.shadowRoot.querySelector('#type-filter').onchange();
  assert.equal(h.panel.shadowRoot.querySelector('#trace').children.length, 1);
  assert.match(h.panel.shadowRoot.querySelector('#trace').textContent, /TIMEOUT/);
  h.panel.disconnectedCallback();
});

test('Error type filter includes timeout and CRC error status rows', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01'), entry('RX', '3F 83 03'), entry('CRC ERROR'), entry('TX', '46 03 00 00 00 01'), entry('TIMEOUT'), entry('TX', '01 03 00 00 00 01'), entry('ERROR')]));
  h.panel.shadowRoot.querySelector('#type-filter').value = 'Error';
  h.panel.shadowRoot.querySelector('#type-filter').onchange();
  assert.deepEqual([...h.panel.shadowRoot.querySelector('#trace').children].map((row) => row.className), [
    'trace-entry trace-crc-error', 'trace-entry trace-timeout', 'trace-entry trace-error',
  ]);
  h.panel.disconnectedCallback();
});

test('direction and status type filters isolate TX, RX and OK rows', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01'), entry('RX', '3F 03 02 00 64'), entry('OK', '', 5)]));
  const filter = h.panel.shadowRoot.querySelector('#type-filter');
  for (const [type, expected] of [['TX', 'TX'], ['RX', 'RX'], ['OK', 'OK']]) {
    filter.value = type; filter.onchange();
    assert.equal(h.panel.shadowRoot.querySelector('#trace').children.length, 1);
    assert.match(h.panel.shadowRoot.querySelector('#trace').textContent, new RegExp(expected));
  }
  h.panel.disconnectedCallback();
});

test('clicking a Device Health row applies only a frontend Slave filter', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  const row = h.panel.shadowRoot.querySelector('#devices').children[0]; row.onclick();
  assert.equal(h.panel.shadowRoot.querySelector('#slave-filter').value, '63');
  row.onclick();
  assert.equal(h.panel.shadowRoot.querySelector('#slave-filter').value, 'all');
  assert.equal(h.calls.filter((call) => call.type.endsWith('/clear')).length, 0);
  h.panel.disconnectedCallback();
});

test('auto-scroll can turn off without pausing new trace rendering', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01')]));
  assert.equal(trace.scrollTop, trace.scrollHeight);
  const auto = h.panel.shadowRoot.querySelector('#auto-scroll');
  trace.scrollTop = 0;
  auto.checked = false; auto.onchange({ target: auto });
  assert.equal(trace.scrollTop, 0);
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01'), entry('RX', '3F 03 02 00 64')]));
  assert.equal(trace.children.length, 2);
  assert.equal(trace.scrollTop, 0);
  assert.equal(h.panel._paused, false);
  h.panel.disconnectedCallback();
});

test('pause and auto-scroll operate independently; resume renders latest snapshot', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01')]));
  const auto = h.panel.shadowRoot.querySelector('#auto-scroll'); auto.checked = false; auto.onchange({ target: auto });
  trace.scrollTop = 0;
  h.panel.shadowRoot.querySelector('#pause').onclick();
  h.subscriptions[0].callback(snapshot([entry('TX', 'NEW')]));
  assert.doesNotMatch(trace.textContent, /NEW/);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  assert.match(trace.textContent, /NEW/);
  assert.equal(trace.scrollTop, 0);
  h.panel.disconnectedCallback();
});

test('pure Modbus packet decoder parses hex, FC01 request and relay bit order', () => {
  const C = createHarness().Constructor;
  const tx = C.parseHex('3F 01 00 00 00 04 00 00');
  assert.equal(tx.length, 8);
  assert.equal(JSON.stringify(C.decodeRequest(tx, [{ slave: 63, model: 'r410' }]).slice(-2)), JSON.stringify([
    ['Count', '4'], ['Purpose', 'R410 Poll · Relay 1–4'],
  ]));
  const rx = C.parseHex('3F 01 01 02 00 00');
  assert.equal(JSON.stringify(C.decodeResponse(rx, tx, [{ slave: 63, model: 'r410' }]).slice(-4)), JSON.stringify([
    ['Relay 1', 'OFF'], ['Relay 2', 'ON'], ['Relay 3', 'OFF'], ['Relay 4', 'OFF'],
  ]));
  assert.equal(C.parseHex('AA XX').length, 0);
});

test('FC02 scanner probe and FC03 common, P404 and P210 decoders label correct purposes', () => {
  const C = createHarness().Constructor;
  const request = (hex, device, source = 'poll') => C.decodeRequest(C.parseHex(hex), device ? [device] : [], source);
  assert.ok(request('3F 02 00 00 00 04 00 00', null, 'scan').some(([key, value]) => key === 'Purpose' && value === 'R410 family identification probe'));
  const probeRequest = C.parseHex('3F 02 00 00 00 04 00 00');
  const probeResponse = C.decodeResponse(C.parseHex('3F 02 01 02 00 00'), probeRequest);
  assert.ok(probeResponse.some(([key, value]) => key === 'Purpose' && value === 'R410 family identification probe'));
  assert.ok(probeResponse.some(([key, value]) => key === 'Probe bit 2' && value === 'ON'));
  assert.ok(probeResponse.every(([key]) => !/^Input \d/.test(key)));
  assert.ok(request('3F 03 00 00 00 01 00 00').some(([key, value]) => key === 'Purpose' && value.includes('Common discovery')));
  assert.ok(request('46 03 08 2A 00 04 00 00', { slave: 70, model: 'p404' }).some(([key, value]) => key === 'Purpose' && value === 'P404 Poll'));
  assert.ok(request('3E 03 08 2A 00 02 00 00', { slave: 62, model: 'p210' }).some(([key, value]) => key === 'Purpose' && value === 'P210 Poll'));
  assert.equal(request('3E 04 00 10 00 02 00 00').slice(-2).map((field) => field[0]).join(','), 'Start,Count');
});

test('FC03 response maps configured dimmer outputs and displays raw plus low byte', () => {
  const C = createHarness().Constructor;
  const req404 = C.parseHex('33 03 08 2A 00 04 00 00');
  const rx404 = C.decodeResponse(C.parseHex('33 03 08 00 00 00 00 00 64 00 32 00 00'), req404, [{ slave: 51, model: 'p404', group_a: 'single', group_b: 'cct' }]);
  assert.equal(rx404.slice(-4).map(([label]) => label).join(','), '0x082A CH1,0x082B CH2,0x082C Group B Brightness,0x082D Group B CCT');
  assert.equal(rx404[rx404.length - 2][1], '0x0064 · 100');
  const req210 = C.parseHex('3E 03 08 2A 00 02 00 00');
  const rx210 = C.decodeResponse(C.parseHex('3E 03 04 00 64 00 32 00 00'), req210, [{ slave: 62, model: 'p210', group_a: 'cct' }]);
  assert.equal(rx210.at(-2)[0], '0x082A Group A Brightness');
  assert.equal(rx210.at(-1)[0], '0x082B Group A CCT');
  assert.ok(C.decodeRequest(req210, [], 'scan').some(([key, value]) => key === 'Purpose' && value === 'Dimmer family probe'));
});

test('FC05 coil ON/OFF and FC06 dimmer transition/target decode without writes', () => {
  const C = createHarness().Constructor;
  const on = C.decodeRequest(C.parseHex('3F 05 00 01 FF 00 00 00'), [{ slave: 63, model: 'r410' }]);
  const off = C.decodeRequest(C.parseHex('3F 05 00 01 00 00 00 00'), [{ slave: 63, model: 'r410' }]);
  assert.ok(on.some(([key, value]) => key === 'Coil' && value.includes('Relay 2')));
  assert.ok(on.some(([key, value]) => key === 'Value' && value.includes('ON')));
  assert.ok(off.some(([key, value]) => key === 'Value' && value.includes('OFF')));
  const fc06 = C.decodeRequest(C.parseHex('33 06 08 2A 05 31 00 00'), [{ slave: 51, model: 'p404', group_a: 'single' }]);
  assert.ok(fc06.some(([key, value]) => key === 'Register' && value.includes('CH1')));
  assert.ok(fc06.some(([key, value]) => key === 'Transition byte' && value === '0x05'));
  assert.ok(fc06.some(([key, value]) => key === 'Target' && value === '49'));
});

test('exception and CRC-error transaction show authoritative failure result', () => {
  const C = createHarness().Constructor;
  const exception = C.decodeResponse(C.parseHex('3E 83 03 00 00'), C.parseHex('3E 03 00 00 00 01 00 00'));
  assert.ok(exception.some(([key, value]) => key === 'Meaning' && value === 'Illegal Data Value'));
  const tx = entry('TX', '3E 03 00 00 00 01 00 00');
  const rx = entry('RX', '3E 03 02 00 64 00 00');
  const crc = entry('CRC ERROR');
  const transaction = C.buildTransactions([tx, rx, crc])[0];
  assert.ok(C.decodeTransaction(transaction).some(([key, value]) => key === 'Result' && value === 'CRC ERROR'));
});

test('packet row selection decodes transaction and Clear clears selection only', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 05 00 01 FF 00 00 00'), entry('RX', '3F 05 00 01 FF 00 00 00'), entry('OK', '', 5)]));
  h.panel.shadowRoot.querySelector('#trace').children[1].onclick();
  assert.match(h.panel.shadowRoot.querySelector('#decoder').textContent, /Relay 2/);
  assert.match(h.panel.shadowRoot.querySelector('#decoder').textContent, /OK/);
  const counters = h.panel.shadowRoot.querySelector('#stat-success').textContent;
  await h.panel._clear();
  assert.equal(h.panel.shadowRoot.querySelector('#decoder').textContent, '選取一筆 Trace 以檢視封包。');
  assert.equal(h.panel.shadowRoot.querySelector('#stat-success').textContent, counters);
  h.panel.disconnectedCallback();
});

test('responsive viewport workspace gives the trace and sidebar independent scrolling', () => {
  assert.match(monitorSource, /:host\{display:flex;flex-direction:column;height:100vh;height:100dvh;overflow:hidden/);
  assert.match(monitorSource, /\.layout\{display:grid;grid-template-columns:minmax\(0,1fr\) minmax\(320px,380px\);grid-template-areas:"trace sidebar";gap:14px;flex:1;min-height:0;overflow:hidden\}/);
  assert.match(monitorSource, /\.trace-panel\{grid-area:trace;display:flex;flex-direction:column;min-height:0\}/);
  assert.match(monitorSource, /\.trace-list\{flex:1;min-height:0;overflow:auto;min-width:0\}/);
  assert.match(monitorSource, /\.sidebar\{[^}]*height:100%;min-height:0;overflow-y:auto/);
  assert.match(monitorSource, /\.device-list\{display:grid;gap:4px;max-height:240px;overflow-y:auto\}/);
  assert.match(monitorSource, /\.decoder-card\{position:static\}/);
  assert.doesNotMatch(monitorSource, /position:\s*sticky/);
  assert.match(monitorSource, /\.decoder-grid\{[^}]*max-height:min\(38vh,360px\);overflow-y:auto/);
  assert.match(monitorSource, /@media\(max-width:900px\)\{\:host\{height:auto;min-height:100vh;min-height:100dvh;overflow:visible\}/);
  assert.match(monitorSource, /@media\(max-width:900px\)[\s\S]*?\.sidebar\{height:auto;overflow:visible\}/);
  assert.match(monitorSource, /@media\(max-width:900px\)[^\n]*\.decoder-grid\{max-height:40vh\}/);
  assert.match(monitorSource, /grid-template-areas:"sidebar" "trace"/);
  assert.match(monitorSource, /\.trace-raw\{display:block;min-width:0;overflow-x:auto;white-space:pre\}/);
});

test('Decoder card uses normal flow, bounded inner scrolling, and stats caption stays outside metrics', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  const statsCard = monitorSource.match(/<section class="card"><h2>通訊統計<\/h2>([\s\S]*?)<\/section>/)?.[1] || '';
  assert.match(statsCard, /<div id="stats-caption" class="section-caption">自本次啟動起累計<\/div><div id="stats-grid" class="stats-grid">/);
  assert.doesNotMatch(statsCard, /本次執行期間/);
  const metrics = statsCard.split('<div id="stats-grid" class="stats-grid">')[1];
  assert.ok(metrics);
  assert.doesNotMatch(metrics, /section-caption|stats-caption/);
  assert.match(monitorSource, /\.section-caption\{margin:-6px 0 8px;color:var\(--secondary-text-color\);font-size:11px\}/);
  assert.match(h.panel.shadowRoot.querySelector('#devices').textContent, /#63 R410/);
  assert.equal(h.panel.shadowRoot.querySelector('#stat-success').textContent, '1584');
  assert.doesNotMatch(monitorSource, /started_at|uptime_seconds|elapsed_seconds/);
  assert.equal(Object.hasOwn(snapshot(), 'started_at'), false);
  assert.equal(Object.hasOwn(snapshot(), 'uptime_seconds'), false);
  h.panel.disconnectedCallback();
});

test('Decoder scrollIntoView runs only on a user click when the Decoder is outside the desktop Sidebar viewport', async () => {
  const h = createHarness(); await h.flush();
  const sidebar = h.panel.shadowRoot.querySelector('#sidebar');
  const decoderCard = h.panel.shadowRoot.querySelector('#decoder-card');
  sidebar.clientHeight = 200;
  sidebar.scrollHeight = 600;
  sidebar._rect = { top: 0, bottom: 200 };
  decoderCard._rect = { top: 240, bottom: 360 };
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 05 00 01 FF 00 00 00'), entry('RX', '3F 05 00 01 FF 00 00 00'), entry('OK', '', 5)]));
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);

  sidebar.scrollTop = 45;
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 05 00 01 FF 00 00 00'), entry('RX', '3F 05 00 01 FF 00 00 00'), entry('OK', '', 5)]));
  assert.equal(sidebar.scrollTop, 45);
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);

  h.panel.shadowRoot.querySelector('#pause').onclick();
  h.subscriptions[0].callback(snapshot([entry('TX', 'NEW'), entry('RX', 'NEW RESPONSE')]));
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);

  h.panel.shadowRoot.querySelector('#type-filter').value = 'TX';
  h.panel.shadowRoot.querySelector('#type-filter').onchange();
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);
  h.panel.shadowRoot.querySelector('#type-filter').value = 'all';
  h.panel.shadowRoot.querySelector('#type-filter').onchange();
  h.panel.shadowRoot.querySelector('#trace').children[1].onclick();
  assert.match(h.panel.shadowRoot.querySelector('#decoder').textContent, /Result/);
  assert.equal(decoderCard.scrollIntoViewCalls.length, 1);
  assert.equal(decoderCard.scrollIntoViewCalls[0].block, 'nearest');
  assert.equal(decoderCard.scrollIntoViewCalls[0].behavior, 'smooth');
  h.panel.disconnectedCallback();
});

test('Selecting a transaction does not scroll Decoder when it is already visible', async () => {
  const h = createHarness(); await h.flush();
  const sidebar = h.panel.shadowRoot.querySelector('#sidebar');
  const decoderCard = h.panel.shadowRoot.querySelector('#decoder-card');
  sidebar.clientHeight = 200;
  sidebar.scrollHeight = 600;
  sidebar._rect = { top: 0, bottom: 200 };
  decoderCard._rect = { top: 40, bottom: 160 };
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01')]));
  h.panel.shadowRoot.querySelector('#trace').children[0].onclick();
  assert.match(h.panel.shadowRoot.querySelector('#decoder').textContent, /Function/);
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);
  h.panel.disconnectedCallback();
});

test('Mobile single-column layout keeps Sidebar in normal flow and does not scroll the page on trace click', async () => {
  const h = createHarness(); await h.flush();
  const sidebar = h.panel.shadowRoot.querySelector('#sidebar');
  const decoderCard = h.panel.shadowRoot.querySelector('#decoder-card');
  sidebar.clientHeight = 360;
  sidebar.scrollHeight = 360;
  sidebar._rect = { top: 0, bottom: 360 };
  decoderCard._rect = { top: 520, bottom: 640 };
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01')]));
  h.panel.shadowRoot.querySelector('#trace').children[0].onclick();
  assert.equal(decoderCard.scrollIntoViewCalls.length, 0);
  h.panel.disconnectedCallback();
});

test('Snapshot and resumed renders preserve Sidebar position without backend uptime fields', async () => {
  const h = createHarness(); await h.flush();
  const sidebar = h.panel.shadowRoot.querySelector('#sidebar');
  sidebar.scrollTop = 72;
  const renderDecoder = h.panel._renderDecoder.bind(h.panel);
  h.panel._renderDecoder = (...args) => { sidebar.scrollTop = 0; renderDecoder(...args); };
  h.subscriptions[0].callback(snapshot());
  assert.equal(sidebar.scrollTop, 72);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  h.subscriptions[0].callback(snapshot([entry('TX', 'NEW')]));
  assert.equal(sidebar.scrollTop, 72);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  assert.equal(sidebar.scrollTop, 72);
  const snapshotCall = h.calls.find((call) => call.type.endsWith('/list'));
  assert.deepEqual(Object.keys(snapshot()).sort(), [
    'connected', 'coordinator', 'devices', 'entries', 'poll_interval', 'polling_paused', 'statistics',
  ]);
  assert.equal(snapshotCall.type, 'coselig_modbus_hub/monitor/list');
  h.panel.disconnectedCallback();
});

test('diagnostic cards and Monitor title use Traditional Chinese', () => {
  assert.match(monitorSource, /<h1>Coselig Modbus 診斷監控<\/h1>/);
  for (const title of ['原始封包', '閘道器', '封包解析', '設備狀態', '通訊統計']) {
    assert.ok(monitorSource.includes(`<h2>${title}</h2>`) || monitorSource.includes(`<h2>${title} <small`));
  }
  assert.doesNotMatch(monitorSource, /<h2>(Gateway|Packet Decoder|Device Health|Communication Statistics)<\/h2>/);
  assert.doesNotMatch(monitorSource, /<span id="status"/);
  const cardOrder = ['閘道器', '封包解析', '設備狀態', '通訊統計'].map((title) => monitorSource.indexOf(`<h2>${title}`));
  assert.deepEqual(cardOrder, [...cardOrder].sort((left, right) => left - right));
});

test('entry keys dedupe snapshots and bounded local history keeps timestamp order', () => {
  const C = createHarness().Constructor;
  const a = { ...entry('TX', '3F 03', 2), timestamp: '2026-10-14T00:00:01.000Z' };
  const b = { ...entry('RX', '3F 03', 3), timestamp: '2026-10-14T00:00:02.000Z' };
  const c = { ...entry('OK', '', 4), timestamp: '2026-10-14T00:00:03.000Z' };
  assert.equal(C.entryKey(a), C.entryKey({ ...a }));
  assert.notEqual(C.entryKey(a), C.entryKey(b));
  assert.equal(JSON.stringify(C.mergeTraceHistory([a, b], [a, b, c], 10).map((item) => item.timestamp)), JSON.stringify([a.timestamp, b.timestamp, c.timestamp]));
  assert.equal(C.mergeTraceHistory([a, b], [a, b, c], 10).length, 3);
  const many = Array.from({ length: 1005 }, (_, index) => ({
    ...entry('TX', '3F 03'),
    timestamp: new Date(Date.UTC(2026, 0, 1) + index * 1000).toISOString(),
  }));
  const bounded = C.mergeTraceHistory([], many, 1000);
  assert.equal(bounded.length, 1000);
  assert.equal(bounded[0].timestamp, many[5].timestamp);
  assert.equal(bounded.at(-1).timestamp, many.at(-1).timestamp);
});

function numberedEntries(first, last) {
  return Array.from({ length: last - first + 1 }, (_, offset) => ({
    ...entry('TX', '3F 03 00 00 00 01'),
    timestamp: new Date(Date.UTC(2026, 0, 1) + (first + offset) * 1000).toISOString(),
  }));
}

test('snapshot rollover keeps frontend local history after backend entries disappear', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot(numberedEntries(0, 199)));
  const oldKey = h.panel._history[0] && h.Constructor.entryKey(h.panel._history[0]);
  assert.equal(h.panel._history.length, 200);
  h.subscriptions[0].callback(snapshot(numberedEntries(1, 200)));
  assert.equal(h.panel._history.length, 201);
  assert.equal(h.Constructor.entryKey(h.panel._history[0]), oldKey);
  assert.equal(h.panel._history.at(-1).timestamp, numberedEntries(200, 200)[0].timestamp);
  h.panel.disconnectedCallback();
});

test('Auto-scroll OFF preserves the same entry anchor and offset across a backend ring rollover', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  h.subscriptions[0].callback(snapshot(numberedEntries(0, 199)));
  const auto = h.panel.shadowRoot.querySelector('#auto-scroll');
  auto.checked = false; auto.onchange({ target: auto });
  trace.scrollTop = 500;
  const anchorRow = trace.children[50];
  const anchorKey = anchorRow.dataset.entryKey;
  const anchorOffset = anchorRow.getBoundingClientRect().top - trace.getBoundingClientRect().top;
  h.subscriptions[0].callback(snapshot(numberedEntries(1, 200)));
  const anchored = trace.children.find((row) => row.dataset.entryKey === anchorKey);
  assert.ok(anchored);
  assert.equal(anchored.getBoundingClientRect().top - trace.getBoundingClientRect().top, anchorOffset);
  assert.equal(h.panel._history.length, 201);
  h.panel.disconnectedCallback();
});

test('missing anchor falls back to oldest surviving row instead of jumping to bottom', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  h.subscriptions[0].callback(snapshot(numberedEntries(0, 999)));
  const auto = h.panel.shadowRoot.querySelector('#auto-scroll');
  auto.checked = false; auto.onchange({ target: auto });
  trace.scrollTop = 5000;
  const staleAnchor = trace.children[500].dataset.entryKey;
  for (const [first, last] of [[1000, 1199], [1200, 1399], [1400, 1599]]) {
    h.subscriptions[0].callback(snapshot(numberedEntries(first, last)));
  }
  assert.equal(h.panel._history.length, 1000);
  assert.ok(!trace.children.some((row) => row.dataset.entryKey === staleAnchor));
  assert.equal(trace.scrollTop, 0);
  assert.ok(trace.scrollTop < trace.scrollHeight - trace.clientHeight);
  h.panel.disconnectedCallback();
});

test('re-enabling Auto-scroll immediately moves to the latest row', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  h.subscriptions[0].callback(snapshot(numberedEntries(0, 19)));
  const auto = h.panel.shadowRoot.querySelector('#auto-scroll');
  auto.checked = false; auto.onchange({ target: auto });
  trace.scrollTop = 10;
  auto.checked = true; auto.onchange({ target: auto });
  assert.equal(trace.scrollTop, trace.scrollHeight);
  h.panel.disconnectedCallback();
});

test('Pause continues ingesting history without rendering until resumed', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  const first = numberedEntries(0, 1);
  h.subscriptions[0].callback(snapshot(first));
  const oldDom = trace.textContent;
  h.panel.shadowRoot.querySelector('#pause').onclick();
  h.subscriptions[0].callback(snapshot(numberedEntries(0, 3)));
  assert.equal(h.panel._history.length, 4);
  assert.equal(trace.textContent, oldDom);
  h.panel.shadowRoot.querySelector('#pause').onclick();
  assert.equal(trace.children.filter((row) => row.className.includes('trace-entry')).length, 4);
  h.panel.disconnectedCallback();
});

test('stable packet selection survives history insertion before the selected transaction', async () => {
  const h = createHarness(); await h.flush();
  const old = { ...entry('TX', '3F 03 00 00 00 01'), timestamp: '2026-01-01T00:00:01.000Z' };
  const selected = { ...entry('TX', '3F 05 00 02 FF 00'), timestamp: '2026-01-01T00:00:02.000Z' };
  h.subscriptions[0].callback(snapshot([old, selected]));
  const trace = h.panel.shadowRoot.querySelector('#trace');
  trace.children[1].onclick();
  const selectedKey = h.Constructor.entryKey(selected);
  assert.equal(h.panel._selectedTransactionKey, selectedKey);
  const earlier = { ...entry('TX', '01 03 00 00 00 01'), timestamp: '2026-01-01T00:00:00.000Z' };
  h.subscriptions[0].callback(snapshot([earlier, old, selected]));
  const row = trace.children.find((item) => item.dataset.entryKey === selectedKey);
  assert.ok(row.className.includes('selected'));
  assert.equal(h.panel._selectedTransactionKey, selectedKey);
  assert.match(h.panel.shadowRoot.querySelector('#decoder').textContent, /Relay 3/);
  h.panel.disconnectedCallback();
});

test('gateway switching ignores stale events and keeps a single live subscription', async () => {
  const h = createHarness(); await h.flush();
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03')]));
  assert.equal(h.panel._history.length, 1);
  await h.panel._select('two');
  assert.equal(h.panel._history.length, 0);
  assert.equal(h.subscriptions[0].closed, true);
  h.subscriptions[0].callback(snapshot([entry('TX', 'STALE')]));
  assert.doesNotMatch(h.panel.shadowRoot.querySelector('#trace').textContent, /STALE/);
  h.subscriptions[1].callback(snapshot([entry('TX', 'NEW')]));
  assert.match(h.panel.shadowRoot.querySelector('#trace').textContent, /NEW/);
  assert.equal(h.panel._history.length, 1);
  h.panel.hass = h.hass; await h.flush(); assert.equal(h.subscriptions.length, 2);
  h.panel.disconnectedCallback(); h.panel.connectedCallback(); await h.flush();
  assert.equal(h.subscriptions.filter((item) => !item.closed).length, 1);
  h.panel.disconnectedCallback();
});

test('no loaded gateway and server errors do not invent controls', async () => {
  const h = createHarness([]); await h.flush();
  assert.equal(h.subscriptions.length, 0);
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-status').textContent, '尚未載入');
  await h.panel._clear(); assert.equal(h.calls.length, 1);
  h.hass.callWS = async () => { throw new Error('unauthorized'); };
  await h.panel._connect(); assert.equal(h.panel.shadowRoot.querySelector('#error').textContent, 'unauthorized');
  h.panel.disconnectedCallback();
});

test('late subscription resolution after detach is unsubscribed', async () => {
  const h = createHarness(); await h.flush();
  let finish; let closed = false;
  h.panel._connection.subscribeMessage = () => new Promise((resolve) => { finish = resolve; });
  const pending = h.panel._select('two'); h.panel.disconnectedCallback();
  finish(async () => { closed = true; }); await pending; assert.equal(closed, true);
});

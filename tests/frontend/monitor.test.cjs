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
    this.className = '';
    this.scrollTop = 0;
    this.disabled = false;
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get scrollHeight() { return this.children.length * 10; }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  append(...children) { this.children.push(...children); }
}

const selectors = [
  '#gateway', '#slave-filter', '#type-filter', '#clear', '#pause', '#auto-scroll',
  '#status', '#error', '#trace', '#gateway-status', '#gateway-poll', '#gateway-devices',
  '#gateway-last', '#gateway-average', '#gateway-queue', '#gateway-queue-peak',
  '#stat-success', '#stat-failed', '#stat-timeout', '#stat-crc', '#stat-reconnect',
  '#devices', '#decoder',
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
  assert.equal(node('#gateway-status'), '● Connected');
  assert.equal(node('#gateway-poll'), '2.0 s');
  assert.equal(node('#gateway-devices'), '1 / 2 available');
  assert.equal(node('#gateway-last'), '218.4 ms');
  assert.equal(node('#gateway-average'), '230.9 ms');
  assert.equal(node('#gateway-queue'), '0');
  assert.equal(node('#gateway-queue-peak'), '24');
  assert.equal(node('#stat-success'), '1584'); assert.equal(node('#stat-failed'), '4');
  assert.equal(node('#stat-timeout'), '3'); assert.equal(node('#stat-crc'), '1'); assert.equal(node('#stat-reconnect'), '2');
  const rows = h.panel.shadowRoot.querySelector('#devices').children;
  assert.match(rows[0].textContent, /#63 R410.*Online/);
  assert.match(rows[1].textContent, /#70 P404.*Offline.*Failures: 2/);
  assert.match(monitorSource, /本次執行期間/);
  h.panel.disconnectedCallback();
});

test('Gateway card independently shows connected polling state, health and queue values', async () => {
  const h = createHarness(); await h.flush(); h.subscriptions[0].callback(snapshot());
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-status').textContent, '● Connected');
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-poll').textContent, '2.0 s');
  assert.equal(h.panel.shadowRoot.querySelector('#gateway-devices').textContent, '1 / 2 available');
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
  assert.match(text, /#63 R410.*Online/);
  assert.match(text, /#70 P404.*Offline.*Failures: 2/);
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
  assert.equal(h.calls.filter((call) => call.type.endsWith('/clear')).length, 0);
  h.panel.disconnectedCallback();
});

test('auto-scroll can turn off without pausing new trace rendering', async () => {
  const h = createHarness(); await h.flush();
  const trace = h.panel.shadowRoot.querySelector('#trace');
  h.subscriptions[0].callback(snapshot([entry('TX', '3F 03 00 00 00 01')]));
  assert.equal(trace.scrollTop, trace.scrollHeight);
  const auto = h.panel.shadowRoot.querySelector('#auto-scroll'); auto.checked = false; auto.onchange({ target: auto });
  trace.scrollTop = 0;
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

test('responsive layout uses a single-column breakpoint below desktop width', () => {
  assert.match(monitorSource, /grid-template-columns:minmax\(0,1fr\) minmax\(300px,360px\)/);
  assert.match(monitorSource, /@media\(max-width:900px\)\{\.layout\{grid-template-columns:minmax\(0,1fr\);grid-template-areas:"sidebar" "trace"\}/);
  assert.match(monitorSource, /\.trace-raw\{display:block;min-width:0;overflow-x:auto;white-space:pre\}/);
});

test('gateway switching ignores stale events and keeps a single live subscription', async () => {
  const h = createHarness(); await h.flush(); await h.panel._select('two');
  assert.equal(h.subscriptions[0].closed, true);
  h.subscriptions[0].callback(snapshot([entry('TX', 'STALE')]));
  assert.doesNotMatch(h.panel.shadowRoot.querySelector('#trace').textContent, /STALE/);
  h.subscriptions[1].callback(snapshot([entry('TX', 'NEW')]));
  assert.match(h.panel.shadowRoot.querySelector('#trace').textContent, /NEW/);
  h.panel.hass = h.hass; await h.flush(); assert.equal(h.subscriptions.length, 2);
  h.panel.disconnectedCallback(); h.panel.connectedCallback(); await h.flush();
  assert.equal(h.subscriptions.filter((item) => !item.closed).length, 1);
  h.panel.disconnectedCallback();
});

test('no loaded gateway and server errors do not invent controls', async () => {
  const h = createHarness([]); await h.flush();
  assert.equal(h.subscriptions.length, 0);
  assert.match(h.panel.shadowRoot.querySelector('#status').textContent, /尚無/);
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

// Read-only diagnostic view. Every detail comes from the existing runtime snapshot.
const FUNCTION_NAMES = {
  1: "Read Coils",
  2: "Read Discrete Inputs",
  3: "Read Holding Registers",
  4: "Read Input Registers",
  5: "Write Single Coil",
  6: "Write Single Register",
};
const EXCEPTION_MEANINGS = {
  1: "Illegal Function",
  2: "Illegal Data Address",
  3: "Illegal Data Value",
  4: "Slave Device Failure",
};

class CoseligModbusMonitor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._generation = 0;
    this._paused = false;
    this._autoScroll = true;
    this._entry = null;
    this._latest = null;
    this._transactions = [];
    this._selectedIndex = null;
    this._unsubscribe = null;
    this._ready = false;
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected && this._connection !== value.connection) this._connect();
  }

  connectedCallback() {
    if (!this._ready) {
      this.shadowRoot.innerHTML = `<style>
        :host{display:block;padding:20px;color:var(--primary-text-color);background:var(--primary-background-color);min-height:100vh;box-sizing:border-box}
        *{box-sizing:border-box} h1{font-size:22px;margin:0} h2{font-size:16px;margin:0 0 12px} h3{font-size:14px;margin:0 0 8px}
        header{display:flex;align-items:center;gap:12px;margin-bottom:14px}.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
        button,select{font:inherit;min-height:36px;padding:7px 9px;border:1px solid var(--divider-color);border-radius:6px;color:var(--primary-text-color);background:var(--card-background-color)}
        button{cursor:pointer}.toolbar label{display:flex;align-items:center;gap:5px}.toolbar input{accent-color:var(--primary-color)}
        #status{margin:0 0 12px;color:var(--secondary-text-color)} #error{color:var(--error-color);margin:8px 0}
        .layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,360px);grid-template-areas:"trace sidebar";gap:14px;align-items:start}
        .card{min-width:0;padding:14px;border:1px solid var(--divider-color);border-radius:9px;background:var(--card-background-color)}
        .trace-panel{grid-area:trace}.sidebar{grid-area:sidebar;display:grid;gap:12px;min-width:0}
        .trace-list{max-height:72vh;overflow:auto;min-width:0}.trace-entry{display:grid;grid-template-columns:88px 78px minmax(0,1fr) 64px;gap:8px;align-items:start;padding:4px 6px;border:1px solid transparent;border-radius:5px;line-height:1.45;font:12px/1.45 var(--code-font-family,monospace);cursor:pointer}
        .trace-entry:hover{background:var(--secondary-background-color)}.trace-entry.selected{border-color:var(--primary-color);background:var(--secondary-background-color)}
        .trace-time,.trace-latency{color:var(--secondary-text-color);white-space:nowrap}.trace-tx,.trace-raw-tx{color:#42a5f5}.trace-rx,.trace-ok,.trace-raw-rx{color:#66bb6a}.trace-timeout{color:#ffb74d}.trace-crc-error,.trace-error{color:#ef5350}
        .trace-raw{display:block;min-width:0;overflow-x:auto;white-space:pre}.trace-status{white-space:pre}.trace-latency{text-align:right}
        .metric-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;margin:0}.metric-grid dt{color:var(--secondary-text-color)}.metric-grid dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
        .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.stat{padding:8px;border-radius:6px;background:var(--secondary-background-color)}.stat span{display:block;color:var(--secondary-text-color);font-size:12px}.stat strong{font-size:18px;font-variant-numeric:tabular-nums}
        .device-list{display:grid;gap:5px}.device-row{width:100%;display:flex;align-items:center;justify-content:space-between;gap:8px;text-align:left;border:0;padding:7px 8px}.device-row.active{outline:1px solid var(--primary-color)}.device-main{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.device-health{white-space:nowrap;color:var(--secondary-text-color);font-size:12px}.online{color:var(--success-color,var(--primary-text-color))}.offline{color:var(--error-color)}
        .decoder-grid{display:grid;grid-template-columns:minmax(100px,auto) minmax(0,1fr);gap:6px 10px;margin:0}.decoder-grid dt{color:var(--secondary-text-color)}.decoder-grid dd{margin:0;overflow-wrap:anywhere}.empty{color:var(--secondary-text-color);padding:10px 4px}
        @media(max-width:900px){.layout{grid-template-columns:minmax(0,1fr);grid-template-areas:"sidebar" "trace"}.trace-list{max-height:55vh}}
        @media(max-width:560px){:host{padding:12px}.trace-entry{grid-template-columns:70px 65px minmax(0,1fr);gap:5px;font-size:11px}.trace-latency{grid-column:3;text-align:left}.stats-grid{grid-template-columns:1fr 1fr}}
      </style>
      <header><h1>Coselig Modbus Diagnostic Monitor</h1></header>
      <div class="toolbar">
        <select id="gateway" aria-label="Gateway"></select>
        <select id="slave-filter" aria-label="Slave filter"><option value="all">Slave：全部</option></select>
        <select id="type-filter" aria-label="Direction and type filter">
          <option value="all">類型：全部</option><option value="TX">TX</option><option value="RX">RX</option><option value="OK">OK</option><option value="Error">Error</option>
        </select>
        <button id="clear">清除</button><button id="pause">暫停畫面</button>
        <label><input id="auto-scroll" type="checkbox" checked>自動捲動</label>
      </div>
      <div id="status"></div><div id="error" role="alert"></div>
      <div class="layout">
        <section class="card trace-panel"><h2>Raw Trace</h2><div id="trace" class="trace-list" aria-label="Modbus Raw HEX"></div></section>
        <aside class="sidebar">
          <section class="card"><h2>Gateway</h2><dl class="metric-grid">
            <dt>Status</dt><dd id="gateway-status">—</dd><dt>Polling</dt><dd id="gateway-poll">—</dd>
            <dt>Devices</dt><dd id="gateway-devices">—</dd><dt>Last Poll</dt><dd id="gateway-last">—</dd>
            <dt>Average Poll</dt><dd id="gateway-average">—</dd><dt>Queue</dt><dd id="gateway-queue">—</dd>
            <dt>Queue Peak</dt><dd id="gateway-queue-peak">—</dd>
          </dl></section>
          <section class="card"><h2>Communication Statistics</h2><div class="stats-grid">
            <div class="stat"><span>Successful</span><strong id="stat-success">0</strong></div><div class="stat"><span>Failed</span><strong id="stat-failed">0</strong></div>
            <div class="stat"><span>Timeout</span><strong id="stat-timeout">0</strong></div><div class="stat"><span>CRC Error</span><strong id="stat-crc">0</strong></div>
            <div class="stat"><span>Reconnect</span><strong id="stat-reconnect">0</strong></div><small>本次執行期間</small>
          </div></section>
          <section class="card"><h2>Device Health</h2><div id="devices" class="device-list"></div></section>
          <section class="card"><h2>Packet Decoder</h2><dl id="decoder" class="decoder-grid"><dd class="empty">選取一筆 Trace 以檢視封包。</dd></dl></section>
        </aside>
      </div>`;
      this.shadowRoot.querySelector("#gateway").onchange = (event) => this._select(event.target.value);
      this.shadowRoot.querySelector("#slave-filter").onchange = () => this._renderTrace();
      this.shadowRoot.querySelector("#type-filter").onchange = () => this._renderTrace();
      this.shadowRoot.querySelector("#clear").onclick = () => this._clear();
      this.shadowRoot.querySelector("#pause").onclick = () => {
        this._paused = !this._paused;
        this.shadowRoot.querySelector("#pause").textContent = this._paused ? "繼續畫面" : "暫停畫面";
        if (!this._paused) this._render();
      };
      this.shadowRoot.querySelector("#auto-scroll").onchange = (event) => {
        this._autoScroll = Boolean(event.target.checked);
      };
      this._ready = true;
    }
    if (this._hass) this._connect();
  }

  disconnectedCallback() {
    this._generation++;
    this._stopSubscription();
    this._connection = null;
  }

  _stopSubscription() {
    if (this._unsubscribe) {
      this._unsubscribe().catch(() => {});
      this._unsubscribe = null;
    }
  }

  _error(error) {
    this.shadowRoot.querySelector("#error").textContent = String(error.message || error);
  }

  async _connect() {
    this._connection = this._hass.connection;
    this._stopSubscription();
    const generation = ++this._generation;
    try {
      const gateways = await this._hass.callWS({ type: "coselig_modbus_hub/monitor/list" });
      if (generation !== this._generation) return;
      const select = this.shadowRoot.querySelector("#gateway");
      select.replaceChildren();
      for (const gateway of gateways) {
        const option = document.createElement("option");
        option.value = gateway.entry_id;
        option.textContent = gateway.title;
        select.append(option);
      }
      select.disabled = gateways.length === 0;
      if (gateways.length) {
        select.value = gateways.some((item) => item.entry_id === this._entry) ? this._entry : gateways[0].entry_id;
        await this._select(select.value);
      } else {
        this._entry = null;
        this._latest = null;
        this._transactions = [];
        this.shadowRoot.querySelector("#status").textContent = "尚無已載入的 Coselig Gateway";
        this.shadowRoot.querySelector("#trace").replaceChildren();
        this._renderDecoder(null);
      }
    } catch (error) { if (generation === this._generation) this._error(error); }
  }

  async _select(entry) {
    this._stopSubscription();
    this._entry = entry;
    this._latest = null;
    this._selectedIndex = null;
    this.shadowRoot.querySelector("#trace").replaceChildren();
    this._renderDecoder(null);
    const generation = ++this._generation;
    try {
      const unsubscribe = await this._connection.subscribeMessage((snapshot) => {
        if (generation !== this._generation) return;
        this._latest = snapshot;
        if (!this._paused) this._render();
      }, { type: "coselig_modbus_hub/monitor/subscribe", entry_id: entry });
      if (generation !== this._generation) { await unsubscribe(); return; }
      this._unsubscribe = unsubscribe;
      this.shadowRoot.querySelector("#error").textContent = "";
    } catch (error) { if (generation === this._generation) this._error(error); }
  }

  async _clear() {
    if (!this._entry) return;
    const generation = this._generation;
    try {
      await this._hass.callWS({ type: "coselig_modbus_hub/monitor/clear", entry_id: this._entry });
      if (generation !== this._generation) return;
      this._latest = this._latest ? { ...this._latest, entries: [] } : null;
      this._selectedIndex = null;
      this._render();
      this.shadowRoot.querySelector("#error").textContent = "";
    } catch (error) { if (generation === this._generation) this._error(error); }
  }

  _render() {
    if (!this._latest) return;
    const snapshot = this._latest;
    const coordinator = snapshot.coordinator || {};
    const stats = snapshot.statistics || {};
    this.shadowRoot.querySelector("#status").textContent = `Gateway ${snapshot.connected ? "● Connected" : "○ Disconnected"} · ${snapshot.polling_paused ? "Polling Paused" : `Polling ${snapshot.poll_interval ?? "—"} s`}`;
    this.shadowRoot.querySelector("#gateway-status").textContent = snapshot.connected ? "● Connected" : "○ Disconnected";
    this.shadowRoot.querySelector("#gateway-poll").textContent = snapshot.polling_paused ? "Paused" : `${Number(snapshot.poll_interval ?? 0).toFixed(1)} s`;
    this.shadowRoot.querySelector("#gateway-devices").textContent = `${coordinator.available_slaves ?? 0} / ${coordinator.configured_slaves ?? 0} available`;
    this.shadowRoot.querySelector("#gateway-last").textContent = `${Number(coordinator.last_poll_round_ms || 0).toFixed(1)} ms`;
    this.shadowRoot.querySelector("#gateway-average").textContent = `${Number(coordinator.average_poll_round_ms || 0).toFixed(1)} ms`;
    this.shadowRoot.querySelector("#gateway-queue").textContent = String(coordinator.queue_depth ?? 0);
    this.shadowRoot.querySelector("#gateway-queue-peak").textContent = String(coordinator.queue_peak ?? 0);
    this.shadowRoot.querySelector("#stat-success").textContent = String(stats.successful_requests ?? 0);
    this.shadowRoot.querySelector("#stat-failed").textContent = String(stats.failed_requests ?? 0);
    this.shadowRoot.querySelector("#stat-timeout").textContent = String(stats.timeout_count ?? 0);
    this.shadowRoot.querySelector("#stat-crc").textContent = String(stats.crc_error_count ?? 0);
    this.shadowRoot.querySelector("#stat-reconnect").textContent = String(stats.reconnect_count ?? 0);
    this._transactions = CoseligModbusMonitor.buildTransactions(snapshot.entries || []);
    this._renderDevices(snapshot.devices || []);
    this._renderFilters(snapshot.devices || []);
    this._renderTrace();
    this._renderDecoder(this._selectedIndex == null ? null : this._transactions[this._selectedIndex]);
  }

  _renderDevices(devices) {
    const list = this.shadowRoot.querySelector("#devices");
    list.replaceChildren();
    if (!devices.length) {
      const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "尚未設定 Slave"; list.append(empty); return;
    }
    for (const device of devices) {
      const button = document.createElement("button");
      button.className = `device-row${this.shadowRoot.querySelector("#slave-filter").value === String(device.slave) ? " active" : ""}`;
      button.onclick = () => {
        const filter = this.shadowRoot.querySelector("#slave-filter");
        filter.value = String(device.slave);
        this._renderTrace();
      };
      const name = document.createElement("span"); name.className = "device-main";
      name.textContent = `${device.available ? "●" : "○"} #${device.slave} ${String(device.model || "").toUpperCase()}`;
      const health = document.createElement("span"); health.className = `device-health ${device.available ? "online" : "offline"}`;
      health.textContent = device.available ? "Online" : "Offline";
      if (device.consecutive_failures > 0) health.textContent += ` · Failures: ${device.consecutive_failures}`;
      button.append(name, health); list.append(button);
    }
  }

  _renderFilters(devices) {
    const filter = this.shadowRoot.querySelector("#slave-filter");
    const selected = filter.value || "all";
    const ids = new Set(devices.map((item) => Number(item.slave)));
    for (const transaction of this._transactions) if (transaction.slave != null) ids.add(transaction.slave);
    filter.replaceChildren();
    const all = document.createElement("option"); all.value = "all"; all.textContent = "Slave：全部"; filter.append(all);
    for (const slave of [...ids].sort((a, b) => a - b)) {
      const option = document.createElement("option"); option.value = String(slave); option.textContent = `Slave ${slave}`; filter.append(option);
    }
    filter.value = ids.has(Number(selected)) ? selected : "all";
  }

  _renderTrace() {
    const list = this.shadowRoot.querySelector("#trace");
    const oldScrollTop = list.scrollTop || 0;
    list.replaceChildren();
    const slaveFilter = this.shadowRoot.querySelector("#slave-filter").value;
    const typeFilter = this.shadowRoot.querySelector("#type-filter").value;
    const devices = this._latest?.devices || [];
    this._transactions.forEach((transaction, transactionIndex) => {
      if (slaveFilter !== "all" && transaction.slave !== Number(slaveFilter)) return;
      for (const entry of transaction.entries) {
        const status = String(entry.direction).toUpperCase();
        if (typeFilter === "TX" && status !== "TX") continue;
        if (typeFilter === "RX" && status !== "RX") continue;
        if (typeFilter === "OK" && status !== "OK") continue;
        if (typeFilter === "Error" && !["TIMEOUT", "CRC ERROR", "ERROR"].includes(status)) continue;
        const date = new Date(entry.timestamp);
        const time = `${date.toLocaleTimeString(this._hass?.language || "zh-TW", { hour12: false })}.${String(date.getMilliseconds()).padStart(3, "0")}`;
        const classKey = status.toLowerCase().replaceAll(" ", "-");
        const row = document.createElement("div");
        row.className = `trace-entry trace-${classKey}${this._selectedIndex === transactionIndex ? " selected" : ""}`;
        row.onclick = () => { this._selectedIndex = transactionIndex; this._renderTrace(); this._renderDecoder(transaction, devices); };
        const timestamp = document.createElement("span"); timestamp.className = "trace-time"; timestamp.textContent = time;
        const direction = document.createElement("span"); direction.className = `trace-status trace-${classKey}`; direction.textContent = status.padEnd(9);
        const raw = document.createElement("span"); raw.className = `trace-raw${status === "TX" || status === "RX" ? ` trace-raw-${status.toLowerCase()}` : ""}`; raw.textContent = entry.raw_hex || "";
        row.append(timestamp, direction, raw);
        if (entry.latency_ms != null) { const latency = document.createElement("span"); latency.className = "trace-latency"; latency.textContent = `${Number(entry.latency_ms).toFixed(1)} ms`; row.append(latency); }
        list.append(row);
      }
    });
    if (!list.children.length) { const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "沒有符合篩選條件的 Trace"; list.append(empty); }
    if (this._autoScroll) list.scrollTop = list.scrollHeight;
    else list.scrollTop = oldScrollTop;
  }

  _renderDecoder(transaction, devices = this._latest?.devices || []) {
    const decoder = this.shadowRoot.querySelector("#decoder");
    decoder.replaceChildren();
    if (!transaction) { const empty = document.createElement("dd"); empty.className = "empty"; empty.textContent = "選取一筆 Trace 以檢視封包。"; decoder.append(empty); return; }
    const device = devices.find((item) => item.slave === transaction.slave) || null;
    for (const [label, value] of CoseligModbusMonitor.decodeTransaction(transaction, device)) {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      decoder.append(dt, dd);
    }
  }

  static parseHex(value) {
    const text = String(value || "").trim();
    if (!text) return [];
    const parts = text.split(/\s+/);
    return parts.every((part) => /^[0-9a-f]{2}$/i.test(part)) ? parts.map((part) => Number.parseInt(part, 16)) : [];
  }

  static buildTransactions(entries) {
    const transactions = [];
    let current = null;
    for (const entry of entries) {
      if (String(entry.direction).toUpperCase() === "TX") {
        current = { tx: entry, rx: null, statuses: [], entries: [entry], slave: this.parseHex(entry.raw_hex)[0] ?? null };
        transactions.push(current);
      } else {
        if (!current) { current = { tx: null, rx: null, statuses: [], entries: [], slave: null }; transactions.push(current); }
        current.entries.push(entry);
        if (String(entry.direction).toUpperCase() === "RX") current.rx = entry;
        else current.statuses.push(entry);
      }
    }
    return transactions;
  }

  static _deviceName(slave, devices) {
    const device = devices?.find((item) => item.slave === slave);
    return device ? String(device.model).toUpperCase() : "Unknown";
  }

  static _purpose(fn, start, count, device, source) {
    const model = device?.model;
    if (fn === 1 && model === "r410" && start === 0 && count === 4) return "R410 Poll · Relay 1–4";
    if (fn === 2 && start === 0 && count === 4) return "R410 family identification probe";
    if (fn === 3 && start === 0 && count === 1) return "Common discovery probe · Modbus ID register";
    if (fn === 3 && start === 0x082a && count === 2) return model === "p210" ? "P210 Poll" : "Dimmer family probe";
    if (fn === 3 && start === 0x082a && count === 4 && model === "p404") return "P404 Poll";
    if (source === "scan" && fn === 3 && start === 0x082a) return "Dimmer family probe";
    if (fn === 5 && model === "r410" && start <= 3) return `Relay ${start + 1}`;
    return "—";
  }

  static decodeRequest(bytes, devices = [], source = "") {
    if (!Array.isArray(bytes) || bytes.length < 6) return [];
    const slave = bytes[0], fn = bytes[1], deviceName = this._deviceName(slave, devices);
    const result = [["Direction", "Request"], ["Slave", `${slave} (0x${slave.toString(16).padStart(2, "0").toUpperCase()})`], ["Device", deviceName], ["Function", `FC${String(fn).padStart(2, "0")} · ${FUNCTION_NAMES[fn] || "Unknown"}`]];
    if (fn >= 1 && fn <= 4) {
      const start = bytes[2] * 256 + bytes[3], count = bytes[4] * 256 + bytes[5];
      result.push(["Start", `0x${start.toString(16).padStart(4, "0").toUpperCase()} (${start})`], ["Count", String(count)]);
      const purpose = this._purpose(fn, start, count, devices.find((item) => item.slave === slave), source);
      if (purpose !== "—") result.push(["Purpose", purpose]);
    } else if (fn === 5 || fn === 6) {
      const address = bytes[2] * 256 + bytes[3], value = bytes[4] * 256 + bytes[5];
      if (fn === 5) {
        result.push(["Coil", `${address}${devices.find((item) => item.slave === slave)?.model === "r410" && address < 4 ? ` · Relay ${address + 1}` : ""}`], ["Value", value === 0xff00 ? "ON (0xFF00)" : value === 0 ? "OFF (0x0000)" : `0x${value.toString(16).padStart(4, "0").toUpperCase()}`]);
      } else {
        const semantic = this._registerName(address, devices.find((item) => item.slave === slave));
        result.push(["Register", semantic ? `${semantic} (0x${address.toString(16).padStart(4, "0").toUpperCase()})` : `0x${address.toString(16).padStart(4, "0").toUpperCase()}`], ["Raw", `0x${value.toString(16).padStart(4, "0").toUpperCase()}`]);
        if (address >= 0x082a && address <= 0x082d) result.push(["Transition byte", `0x${(value >> 8).toString(16).padStart(2, "0").toUpperCase()}`], ["Target", String(value & 0xff)]);
      }
    }
    return result;
  }

  static _registerName(address, device) {
    if (!device || !["p404", "p210"].includes(device.model) || address < 0x082a || address > 0x082d) return "";
    const channel = address - 0x082a;
    const groupField = channel < 2 ? "group_a" : "group_b";
    const mode = device[groupField] || "single";
    if (mode === "cct") return `Group ${channel < 2 ? "A" : "B"} ${channel % 2 === 0 ? "Brightness" : "CCT"}`;
    return `CH${channel + 1}`;
  }

  static decodeResponse(bytes, requestBytes, devices = []) {
    if (!Array.isArray(bytes) || bytes.length < 5) return [];
    const slave = bytes[0], fn = bytes[1];
    const device = devices.find((item) => item.slave === slave) || null;
    const result = [["Direction", "Response"], ["Slave", `${slave} (0x${slave.toString(16).padStart(2, "0").toUpperCase()})`], ["Device", this._deviceName(slave, devices)]];
    if (fn & 0x80) {
      const exception = bytes[2];
      result.push(["Function", `Exception Response · FC${String(fn & 0x7f).padStart(2, "0")}`], ["Exception Code", `0x${exception.toString(16).padStart(2, "0").toUpperCase()}`], ["Meaning", EXCEPTION_MEANINGS[exception] || "Unknown exception"]);
      return result;
    }
    result.push(["Function", `FC${String(fn).padStart(2, "0")} · ${FUNCTION_NAMES[fn] || "Unknown"}`]);
    if ([1, 2].includes(fn) && requestBytes?.length >= 6) {
      const count = requestBytes[4] * 256 + requestBytes[5];
      const start = requestBytes[2] * 256 + requestBytes[3];
      if (fn === 2 && start === 0 && count === 4) {
        result.push(["Purpose", "R410 family identification probe"]);
      }
      for (let index = 0; index < count; index++) {
        const value = Boolean(bytes[3 + Math.floor(index / 8)] & (1 << (index % 8)));
        const name = fn === 1 ? `Relay ${start + index + 1}` : `Probe bit ${start + index + 1}`;
        result.push([name, value ? "ON" : "OFF"]);
      }
    } else if ([3, 4].includes(fn)) {
      const count = requestBytes?.length >= 6 ? requestBytes[4] * 256 + requestBytes[5] : Math.floor((bytes[2] || 0) / 2);
      const start = requestBytes?.length >= 6 ? requestBytes[2] * 256 + requestBytes[3] : 0;
      for (let index = 0; index < count && 3 + index * 2 + 1 < bytes.length - 2; index++) {
        const raw = bytes[3 + index * 2] * 256 + bytes[4 + index * 2];
        const address = start + index;
        result.push([`0x${address.toString(16).padStart(4, "0").toUpperCase()} ${this._registerName(address, device)}`.trim(), `0x${raw.toString(16).padStart(4, "0").toUpperCase()} · ${raw & 0xff}`]);
      }
    } else if ((fn === 5 || fn === 6) && bytes.length >= 8) {
      const address = bytes[2] * 256 + bytes[3], value = bytes[4] * 256 + bytes[5];
      if (fn === 5) result.push([device?.model === "r410" && address < 4 ? `Relay ${address + 1}` : `Coil ${address}`, value === 0xff00 ? "ON" : value === 0 ? "OFF" : `0x${value.toString(16).toUpperCase()}`]);
      else result.push([this._registerName(address, device) || `Register ${address}`, `0x${value.toString(16).padStart(4, "0").toUpperCase()}`]);
    }
    return result;
  }

  static decodeTransaction(transaction, device = null) {
    const devices = device ? [device] : [];
    const fields = [];
    const request = this.parseHex(transaction.tx?.raw_hex);
    const response = this.parseHex(transaction.rx?.raw_hex);
    if (transaction.tx) fields.push(...this.decodeRequest(request, devices, transaction.tx.source));
    if (transaction.rx) fields.push(...this.decodeResponse(response, request, devices));
    const status = transaction.statuses.at(-1);
    if (status) fields.push(["Result", String(status.direction).toUpperCase()]);
    else if (transaction.tx) fields.push(["Result", "Pending"]);
    if (status?.latency_ms != null) fields.push(["Latency", `${Number(status.latency_ms).toFixed(1)} ms`]);
    const event = transaction.tx || transaction.rx || transaction.entries[0];
    if (event?.timestamp) fields.splice(1, 0, ["Timestamp", new Date(event.timestamp).toLocaleString()]);
    return fields;
  }
}

customElements.define("coselig-modbus-monitor", CoseligModbusMonitor);

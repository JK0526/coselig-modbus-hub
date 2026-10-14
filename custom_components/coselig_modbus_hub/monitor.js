// Read-only console. All device and connection settings stay in HA's native flows.
class CoseligModbusMonitor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._generation = 0;
    this._paused = false;
    this._entry = null;
    this._latest = null;
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
        :host{display:block;padding:24px;color:var(--primary-text-color);background:var(--primary-background-color);min-height:100vh;box-sizing:border-box}
        header{display:flex;gap:12px;flex-wrap:wrap;align-items:center} h1{font-size:22px;margin:0 0 16px}
        button,select{font:inherit;padding:8px;border:1px solid var(--divider-color);border-radius:6px;color:inherit;background:var(--card-background-color)}
        #status{margin:16px 0} #error{color:var(--error-color)}
        pre{background:var(--card-background-color);padding:16px;border-radius:8px;max-height:70vh;overflow:auto;font-size:13px;white-space:pre}
      </style><h1>Coselig Modbus Monitor</h1><header><select aria-label="Gateway"></select>
        <button id="clear">清除</button><button id="pause">暫停畫面</button></header>
        <div id="status"></div><div id="error" role="alert"></div><pre aria-label="Modbus Raw HEX"></pre>`;
      this.shadowRoot.querySelector("select").onchange = (event) => this._select(event.target.value);
      this.shadowRoot.querySelector("#clear").onclick = () => this._clear();
      this.shadowRoot.querySelector("#pause").onclick = () => {
        this._paused = !this._paused;
        this.shadowRoot.querySelector("#pause").textContent = this._paused ? "繼續畫面" : "暫停畫面";
        if (!this._paused) this._render();
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
      const select = this.shadowRoot.querySelector("select");
      select.replaceChildren();
      for (const gateway of gateways) {
        const option = document.createElement("option");
        option.value = gateway.entry_id;
        option.textContent = gateway.title;
        select.append(option);
      }
      select.disabled = gateways.length === 0;
      if (gateways.length) {
        select.value = gateways.some((g) => g.entry_id === this._entry) ? this._entry : gateways[0].entry_id;
        await this._select(select.value);
      } else {
        this._entry = null;
        this._latest = null;
        this.shadowRoot.querySelector("#status").textContent = "尚無已載入的 Coselig Gateway";
        this.shadowRoot.querySelector("pre").textContent = "";
      }
    } catch (error) { if (generation === this._generation) this._error(error); }
  }
  async _select(entry) {
    this._stopSubscription();
    this._entry = entry;
    this._latest = null;
    this.shadowRoot.querySelector("pre").textContent = "";
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
      this.shadowRoot.querySelector("pre").textContent = "";
      this.shadowRoot.querySelector("#error").textContent = "";
    } catch (error) { if (generation === this._generation) this._error(error); }
  }
  _render() {
    if (!this._latest) return;
    const snapshot = this._latest;
    this.shadowRoot.querySelector("#status").textContent = `Gateway ${snapshot.connected ? "● Connected" : "○ Disconnected"} · Polling ${snapshot.poll_interval ?? "—"} s${snapshot.polling_paused ? "（背景輪詢已暫停）" : ""}`;
    const lines = snapshot.entries.map((entry) => {
      const date = new Date(entry.timestamp);
      const time = `${date.toLocaleTimeString(this._hass.language || "zh-TW", { hour12: false })}.${String(date.getMilliseconds()).padStart(3, "0")}`;
      return `${time} ${entry.direction.padEnd(9)} ${entry.raw_hex}${entry.latency_ms == null ? "" : `  ${entry.latency_ms.toFixed(1)} ms`}`;
    });
    const console = this.shadowRoot.querySelector("pre");
    console.textContent = lines.join("\n");
    console.scrollTop = console.scrollHeight;
  }
}
customElements.define("coselig-modbus-monitor", CoseligModbusMonitor);

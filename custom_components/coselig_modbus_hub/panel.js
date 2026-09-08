
/* Coselig Hub management panel. It uses only Home Assistant's public panel API. */
(function () {
  "use strict";

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");

  class CoseligModbusHubPanel extends HTMLElement {
    set hass(value) {
      this._hass = value;
      if (!this._ready) this._render();
    }

    connectedCallback() {
      this._ready = true;
      this._render();
      this._load();
      this._timer = window.setInterval(() => this._load(), 3000);
    }

    disconnectedCallback() {
      if (this._timer) window.clearInterval(this._timer);
    }

    async _load(force) {
      if (!this._hass) return;
      try {
        this._data = await this._hass.callWS({ type: "coselig_modbus_hub/list" });
        this._error = null;
        const formFocused = this.querySelector("#channel-form") && this.querySelector("#channel-form").contains(document.activeElement);
        if (force || !formFocused) this._render();
      } catch (error) {
        this._error = error.message || String(error);
        this._render();
      }
    }

    _selected() {
      const entries = (this._data && this._data.entries) || [];
      return entries.find((entry) => entry.entry_id === this._selectedId) || entries[0];
    }

    async _call(message) {
      try {
        await this._hass.callWS(message);
        await this._load(true);
        return true;
      } catch (error) {
        this._error = error && error.message ? error.message : String(error);
        // Keep the current form in place so a failed save does not discard
        // the values the user just entered.  The next periodic load will
        // still refresh the panel when the form is not focused.
        const main = this.querySelector("main");
        if (main) {
          let notice = this.querySelector("#coselig-error");
          if (!notice) {
            notice = document.createElement("p");
            notice.id = "coselig-error";
            notice.className = "offline";
            main.appendChild(notice);
          }
          notice.textContent = this._error;
        } else {
          this._render();
        }
        return false;
      }
    }

    _render() {
      const entry = this._selected();
      const entries = (this._data && this._data.entries) || [];
      const state = entry && entry.state ? entry.state : {};
      const channels = entry ? entry.channels || [] : [];
      const selectedId = entry ? entry.entry_id : "";
      this.innerHTML = `
        <style>
          :host { display:block; min-height:100%; background:var(--primary-background-color); color:var(--primary-text-color); }
          main { max-width:1100px; margin:0 auto; padding:28px; }
          h1 { font-size:28px; margin:0 0 6px; } p { color:var(--secondary-text-color); }
          .toolbar, .cards, .form, .table { background:var(--card-background-color); border-radius:16px; padding:18px; margin:16px 0; box-shadow:var(--ha-card-box-shadow, 0 1px 4px #0002); }
          .toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
          select, input { color:inherit; background:var(--input-fill-color, var(--card-background-color)); border:1px solid var(--divider-color); border-radius:8px; padding:10px; }
          button { color:var(--text-primary-color); background:var(--primary-color); border:0; border-radius:8px; padding:10px 14px; cursor:pointer; }
          button.delete { background:var(--error-color, #b3261e); } button.secondary { background:var(--secondary-background-color); }
          .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
          .metric { padding:12px; border-radius:12px; background:var(--secondary-background-color); } .metric b { display:block; font-size:22px; margin-top:4px; }
          .online { color:var(--success-color, #16803c); } .offline { color:var(--error-color, #b3261e); }
          form { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; align-items:end; } label { display:grid; gap:6px; color:var(--secondary-text-color); font-size:13px; }
          table { width:100%; border-collapse:collapse; } th,td { text-align:left; padding:10px 6px; border-bottom:1px solid var(--divider-color); } .empty { padding:24px; text-align:center; color:var(--secondary-text-color); }
          @media(max-width:600px) { main { padding:16px; } }
        </style>
        <main>
          <h1>Coselig Modbus Hub</h1>
          <p>管理 TCP 閘道器、輪詢與燈具通道</p>
          ${entries.length ? `<div class="toolbar"><label>Hub<select id="hub">${entries.map((item) => `<option value="${esc(item.entry_id)}" ${item.entry_id === selectedId ? "selected" : ""}>${esc(item.title)} · ${esc(item.host)}:${esc(item.port)}</option>`).join("")}</select></label><label><input id="polling" type="checkbox" ${entry.options.polling_enabled ? "checked" : ""}> 啟用輪詢</label><label>間隔（秒）<input id="interval" type="number" min="0.5" step="0.5" value="${esc(entry.options.poll_interval)}"></label><button id="save-poll">套用輪詢設定</button></div>
          <div class="cards"><div class="metric">TCP 狀態<b class="${state.connected ? "online" : "offline"}">${state.connected ? "已連線" : "未連線"}</b></div><div class="metric">設備端點<b>${channels.length}</b></div><div class="metric">佇列<b>${esc(state.queue_depth || 0)}</b></div><div class="metric">逾時次數<b>${esc(state.timeout_count || 0)}</b></div></div>
          <section class="form"><h2>新增或修改通道</h2><form id="channel-form"><label>型號<select id="model"><option value="p404">P404</option><option value="p210">P210</option><option value="U4">U4</option></select></label><label>Slave ID<input id="slave" type="number" min="1" max="247" required></label><label>通道<select id="channel"><option>1</option><option>2</option><option>3</option><option>4</option><option>a</option><option>b</option></select></label><label>名稱<input id="name" required placeholder="例如：客廳主燈"></label><label>最低亮度<input id="minimum" type="number" min="1" max="100" value="2"></label><label>最暖 mired<input id="mired_min" type="number" value="175"></label><label>最冷 mired<input id="mired_max" type="number" value="455"></label><button type="submit">儲存通道</button></form></section>
          <section class="table"><h2>已註冊通道</h2>${channels.length ? `<table><thead><tr><th>名稱</th><th>型號</th><th>Slave</th><th>通道</th><th>類型</th><th></th></tr></thead><tbody>${channels.map((channel) => `<tr><td>${esc(channel.name)}</td><td>${esc(channel.model)}</td><td>${esc(channel.slave)}</td><td>${esc(channel.channel)}</td><td>${esc(channel.kind)}</td><td><button class="secondary edit" data-slave="${esc(channel.slave)}" data-channel="${esc(channel.channel)}">編輯</button> <button class="delete remove" data-slave="${esc(channel.slave)}" data-channel="${esc(channel.channel)}">刪除</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">尚未註冊通道。從上方表單新增第一盞燈。</div>`}</section>
          ${state.last_error ? `<p class="offline">最近錯誤：${esc(state.last_error)}</p>` : ""}
          ${this._error ? `<p class="offline">${esc(this._error)}</p>` : ""}
          </main>` : `<section class="form"><h2>尚未設定 Hub</h2><p>請先在 Home Assistant 的「設定 → 裝置與服務」加入 Coselig Modbus Hub。</p></section></main>`}
      `;
      this._bind(entry);
    }

    _bind(entry) {
      if (!entry) return;
      this.querySelector("#hub").addEventListener("change", (event) => { this._selectedId = event.target.value; this._render(); });
      const channelSelect = this.querySelector("#channel");
      const modelSelect = this.querySelector("#model");
      const updateChannelChoices = () => {
        const choices = modelSelect.value === "p404" ? ["1", "2", "3", "4", "a", "b"] : modelSelect.value === "p210" ? ["1", "2"] : ["1"];
        const current = channelSelect.value;
        channelSelect.innerHTML = choices.map((item) => `<option>${item}</option>`).join("");
        if (choices.includes(current)) channelSelect.value = current;
      };
      modelSelect.addEventListener("change", updateChannelChoices);
      updateChannelChoices();
      this.querySelector("#save-poll").addEventListener("click", () => this._call({ type:"coselig_modbus_hub/set_polling", entry_id:entry.entry_id, enabled:this.querySelector("#polling").checked, interval:Number(this.querySelector("#interval").value) }));
      this.querySelector("#channel-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.target;
        const submit = form.querySelector("button[type=submit]");
        if (submit) submit.disabled = true;
        const model = this.querySelector("#model").value;
        const channel = this.querySelector("#channel").value;
        const saved = await this._call({ type:"coselig_modbus_hub/save_channel", entry_id:entry.entry_id, channel:{ model, slave:Number(this.querySelector("#slave").value), channel, kind:(channel === "a" || channel === "b") ? "dual" : "single", name:this.querySelector("#name").value.trim(), minimum:Number(this.querySelector("#minimum").value), mired_min:Number(this.querySelector("#mired_min").value), mired_max:Number(this.querySelector("#mired_max").value) } });
        if (saved) {
          const refreshedForm = this.querySelector("#channel-form");
          if (refreshedForm) refreshedForm.reset();
        } else if (submit) {
          submit.disabled = false;
        }
      });
      this.querySelectorAll(".remove").forEach((button) => button.addEventListener("click", () => { if (window.confirm("刪除這個通道並清除其 Discovery 設定？")) this._call({ type:"coselig_modbus_hub/remove_channel", entry_id:entry.entry_id, slave:Number(button.dataset.slave), channel:button.dataset.channel }); }));
      this.querySelectorAll(".edit").forEach((button) => button.addEventListener("click", () => { const channel = entry.channels.find((item) => String(item.slave) === button.dataset.slave && item.channel === button.dataset.channel); if (!channel) return; ["model","slave","channel","name","minimum","mired_min","mired_max"].forEach((key) => { const field = this.querySelector(`#${key}`); if (field) field.value = channel[key]; }); window.scrollTo({ top: 0, behavior: "smooth" }); }));
    }
  }
  customElements.define("coselig-modbus-hub-panel", CoseligModbusHubPanel);
})();

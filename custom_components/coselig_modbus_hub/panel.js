/* Coselig Hub management panel. It uses only Home Assistant's public panel API. */
(function () {
  "use strict";

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");

  const EMPTY_DRAFT = Object.freeze({
    model: "p404",
    slave: "",
    channel: "1",
    name: "",
    minimum: "2",
    mired_min: "175",
    mired_max: "455",
  });

  const CHANNEL_FIELDS = [
    "model", "slave", "channel", "name", "minimum", "mired_min", "mired_max",
  ];

  class CoseligModbusHubPanel extends HTMLElement {
    constructor() {
      super();
      this._data = null;
      this._selectedId = "";
      this._channelDraft = { ...EMPTY_DRAFT };
      this._pollDraft = null;
      this._formDirty = false;
      this._pollDirty = false;
      this._savingChannel = false;
      this._savingPoll = false;
      this._loadSequence = 0;
      this._error = "";
    }

    set hass(value) {
      this._hass = value;
      if (this._ready) this._updateView();
    }

    connectedCallback() {
      this._ready = true;
      this._renderShell();
      this._load();
      this._timer = window.setInterval(() => this._load(), 3000);
    }

    disconnectedCallback() {
      if (this._timer) window.clearInterval(this._timer);
      this._timer = null;
    }

    _renderShell() {
      if (this.querySelector("#coselig-shell")) return;
      this.innerHTML =
        "<style>" +
        ":host { display:block; min-height:100%; background:var(--primary-background-color); color:var(--primary-text-color); }" +
        "main { max-width:1100px; margin:0 auto; padding:28px; } h1 { font-size:28px; margin:0 0 6px; } p { color:var(--secondary-text-color); }" +
        ".toolbar, .cards, .form, .table { background:var(--card-background-color); border-radius:16px; padding:18px; margin:16px 0; box-shadow:var(--ha-card-box-shadow, 0 1px 4px #0002); }" +
        ".toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; } select, input { color:inherit; background:var(--input-fill-color, var(--card-background-color)); border:1px solid var(--divider-color); border-radius:8px; padding:10px; }" +
        "button { color:var(--text-primary-color); background:var(--primary-color); border:0; border-radius:8px; padding:10px 14px; cursor:pointer; } button:disabled { opacity:.6; cursor:wait; }" +
        "button.delete { background:var(--error-color, #b3261e); } button.secondary { background:var(--secondary-background-color); }" +
        ".cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; } .metric { padding:12px; border-radius:12px; background:var(--secondary-background-color); } .metric b { display:block; font-size:22px; margin-top:4px; }" +
        ".online { color:var(--success-color, #16803c); } .offline { color:var(--error-color, #b3261e); } form { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; align-items:end; } label { display:grid; gap:6px; color:var(--secondary-text-color); font-size:13px; }" +
        "table { width:100%; border-collapse:collapse; } th,td { text-align:left; padding:10px 6px; border-bottom:1px solid var(--divider-color); } .empty { padding:24px; text-align:center; color:var(--secondary-text-color); } .hidden { display:none !important; }" +
        "@media(max-width:600px) { main { padding:16px; } }" +
        "</style>" +
        "<main id='coselig-shell'>" +
          "<h1 id='panel-title'>Coselig Modbus Hub</h1>" +
          "<p id='panel-subtitle'>管理 TCP 閘道器、輪詢與燈具通道</p>" +
          "<section id='empty-state' class='form hidden'><h2>尚未設定 Hub</h2><p>請先在 Home Assistant 的「設定 → 裝置與服務」加入 Coselig Modbus Hub。</p></section>" +
          "<div id='hub-content' class='hidden'>" +
            "<div class='toolbar'>" +
              "<label>Hub<select id='hub'></select></label>" +
              "<label><input id='polling' type='checkbox'> 啟用輪詢</label>" +
              "<label>間隔（秒）<input id='interval' type='number' min='0.5' step='0.5'></label>" +
              "<button id='save-poll' type='button'>套用輪詢設定</button>" +
            "</div>" +
            "<div class='cards'>" +
              "<div class='metric'>TCP 狀態<b id='metric-connected'></b></div>" +
              "<div class='metric'>設備端點<b id='metric-channels'>0</b></div>" +
              "<div class='metric'>佇列<b id='metric-queue'>0</b></div>" +
              "<div class='metric'>逾時次數<b id='metric-timeouts'>0</b></div>" +
            "</div>" +
            "<section class='form'><h2>新增或修改通道</h2>" +
              "<form id='channel-form'>" +
                "<label>型號<select id='model'><option value='p404'>P404</option><option value='p210'>P210</option><option value='U4'>U4</option></select></label>" +
                "<label>Slave ID<input id='slave' type='number' min='1' max='247' required></label>" +
                "<label>通道<select id='channel'></select></label>" +
                "<label>名稱<input id='name' required placeholder='例如：客廳主燈'></label>" +
                "<label>最低亮度<input id='minimum' type='number' min='1' max='100'></label>" +
                "<label>最暖 mired<input id='mired_min' type='number'></label>" +
                "<label>最冷 mired<input id='mired_max' type='number'></label>" +
                "<button id='save-channel' type='submit'>儲存通道</button>" +
              "</form>" +
            "</section>" +
            "<section class='table'><h2>已註冊通道</h2><div id='channels-table'></div></section>" +
          "</div>" +
          "<p id='last-error' class='offline hidden'></p>" +
          "<p id='coselig-error' class='offline hidden'></p>" +
        "</main>";
      this._bindShell();
      this._syncFormFromDraft();
    }

    _bindShell() {
      const form = this.querySelector("#channel-form");
      const hub = this.querySelector("#hub");
      const model = this.querySelector("#model");
      const polling = this.querySelector("#polling");
      const interval = this.querySelector("#interval");

      hub.addEventListener("change", (event) => {
        this._selectedId = event.target.value;
        this._formDirty = false;
        this._channelDraft = { ...EMPTY_DRAFT };
        this._pollDirty = false;
        this._pollDraft = null;
        this._clearError();
        this._updateView();
      });

      form.addEventListener("input", (event) => {
        const field = event.target;
        if (!field.id || !CHANNEL_FIELDS.includes(field.id)) return;
        this._channelDraft[field.id] = field.value;
        this._formDirty = true;
      });
      form.addEventListener("change", (event) => {
        const field = event.target;
        if (!field.id || !CHANNEL_FIELDS.includes(field.id)) return;
        this._channelDraft[field.id] = field.value;
        this._formDirty = true;
        if (field.id === "model") this._updateChannelChoices();
      });
      model.addEventListener("change", () => this._updateChannelChoices());

      const markPollDirty = () => {
        this._pollDirty = true;
        this._pollDraft = { enabled: polling.checked, interval: interval.value };
      };
      polling.addEventListener("change", markPollDirty);
      interval.addEventListener("input", markPollDirty);
      interval.addEventListener("change", markPollDirty);

      this.querySelector("#save-poll").addEventListener("click", () => this._savePolling());
      form.addEventListener("submit", (event) => this._saveChannel(event));
      this.querySelector("#channels-table").addEventListener("click", (event) => this._handleChannelTableClick(event));
    }

    async _load() {
      if (!this._hass) return;
      const sequence = ++this._loadSequence;
      try {
        const data = await this._hass.callWS({ type: "coselig_modbus_hub/list" });
        if (sequence !== this._loadSequence) return;
        this._data = data;
        this._error = "";
        this._updateView();
      } catch (error) {
        if (sequence !== this._loadSequence) return;
        this._showError(error && error.message ? error.message : String(error));
      }
    }

    _entries() {
      return (this._data && this._data.entries) || [];
    }

    _selected() {
      const entries = this._entries();
      if (!entries.length) return null;
      const selected = entries.find((entry) => entry.entry_id === this._selectedId);
      return selected || entries[0];
    }

    _updateView() {
      if (!this._ready || !this.querySelector("#coselig-shell")) return;
      const entries = this._entries();
      const empty = this.querySelector("#empty-state");
      const content = this.querySelector("#hub-content");
      if (!entries.length) {
        empty.classList.remove("hidden");
        content.classList.add("hidden");
        this._setHidden("#last-error", true);
        return;
      }

      const entry = this._selected();
      this._selectedId = entry.entry_id;
      empty.classList.add("hidden");
      content.classList.remove("hidden");
      this._renderHubOptions(entries, entry.entry_id);
      this._updatePolling(entry);
      this._updateMetrics(entry);
      this._updateChannels(entry);
      this._setHidden("#last-error", !entry.state || !entry.state.last_error);
      if (entry.state && entry.state.last_error) {
        this.querySelector("#last-error").textContent = "最近錯誤：" + entry.state.last_error;
      }
      this._setHidden("#coselig-error", !this._error);
      if (this._error) this.querySelector("#coselig-error").textContent = this._error;
    }

    _renderHubOptions(entries, selectedId) {
      const select = this.querySelector("#hub");
      if (this._containsFocus(select)) return;
      const signature = entries.map((item) => item.entry_id + "|" + item.title + "|" + item.host + "|" + item.port).join("\n");
      if (select.dataset.signature !== signature) {
        select.innerHTML = entries.map((item) =>
          "<option value='" + esc(item.entry_id) + "'>" + esc(item.title) + " · " + esc(item.host) + ":" + esc(item.port) + "</option>"
        ).join("");
        select.dataset.signature = signature;
      }
      select.value = selectedId;
    }

    _updatePolling(entry) {
      const polling = this.querySelector("#polling");
      const interval = this.querySelector("#interval");
      if (this._pollDirty || this._containsFocus(polling) || this._containsFocus(interval)) return;
      polling.checked = Boolean(entry.options && entry.options.polling_enabled);
      interval.value = entry.options && entry.options.poll_interval != null ? entry.options.poll_interval : 3;
      this._pollDraft = { enabled: polling.checked, interval: interval.value };
    }

    _updateMetrics(entry) {
      const state = entry.state || {};
      const connected = this.querySelector("#metric-connected");
      connected.textContent = state.connected ? "已連線" : "未連線";
      connected.className = state.connected ? "online" : "offline";
      this.querySelector("#metric-channels").textContent = String((entry.channels || []).length);
      this.querySelector("#metric-queue").textContent = String(state.queue_depth || 0);
      this.querySelector("#metric-timeouts").textContent = String(state.timeout_count || 0);
    }

    _updateChannels(entry) {
      const channels = entry.channels || [];
      const container = this.querySelector("#channels-table");
      if (!channels.length) {
        container.innerHTML = "<div class='empty'>尚未註冊通道。從上方表單新增第一盞燈。</div>";
        return;
      }
      container.innerHTML =
        "<table><thead><tr><th>名稱</th><th>型號</th><th>Slave</th><th>通道</th><th>類型</th><th></th></tr></thead><tbody>" +
        channels.map((channel) =>
          "<tr><td>" + esc(channel.name) + "</td><td>" + esc(channel.model) + "</td><td>" +
          esc(channel.slave) + "</td><td>" + esc(channel.channel) + "</td><td>" + esc(channel.kind) +
          "</td><td><button class='secondary edit' data-slave='" + esc(channel.slave) +
          "' data-channel='" + esc(channel.channel) + "'>編輯</button> <button class='delete remove' data-slave='" +
          esc(channel.slave) + "' data-channel='" + esc(channel.channel) + "'>刪除</button></td></tr>"
        ).join("") +
        "</tbody></table>";
    }

    _updateChannelChoices() {
      const model = this.querySelector("#model");
      const select = this.querySelector("#channel");
      if (!model || !select) return;
      const choices = model.value === "p404" ? ["1", "2", "3", "4", "a", "b"] : model.value === "p210" ? ["1", "2"] : ["1"];
      const current = this._channelDraft.channel || select.value;
      select.innerHTML = choices.map((item) => "<option value='" + item + "'>" + item + "</option>").join("");
      select.value = choices.includes(current) ? current : choices[0];
      this._channelDraft.channel = select.value;
    }

    _syncFormFromDraft() {
      CHANNEL_FIELDS.forEach((id) => {
        const field = this.querySelector("#" + id);
        if (field && id !== "channel") field.value = this._channelDraft[id] || "";
      });
      this._updateChannelChoices();
      const channel = this.querySelector("#channel");
      if (channel) channel.value = this._channelDraft.channel || channel.value;
    }

    _saveChannel(event) {
      event.preventDefault();
      if (this._savingChannel) return;
      const entry = this._selected();
      if (!entry) return;
      this._channelDraft = this._readFormDraft();
      const form = event.target;
      if (!form.reportValidity()) return;
      this._savingChannel = true;
      const submit = this.querySelector("#save-channel");
      submit.disabled = true;
      const channel = this._channelDraft;
      this._call({
        type: "coselig_modbus_hub/save_channel",
        entry_id: entry.entry_id,
        channel: {
          model: channel.model,
          slave: Number(channel.slave),
          channel: channel.channel,
          kind: channel.channel === "a" || channel.channel === "b" ? "dual" : "single",
          name: channel.name.trim(),
          minimum: Number(channel.minimum),
          mired_min: Number(channel.mired_min),
          mired_max: Number(channel.mired_max),
        },
      }).then((saved) => {
        if (saved) {
          this._formDirty = false;
          this._channelDraft = { ...EMPTY_DRAFT };
          this._syncFormFromDraft();
        }
      }).finally(() => {
        this._savingChannel = false;
        submit.disabled = false;
      });
    }

    async _savePolling() {
      if (this._savingPoll) return;
      const entry = this._selected();
      if (!entry) return;
      const polling = this.querySelector("#polling");
      const interval = this.querySelector("#interval");
      const value = Number(interval.value);
      if (!Number.isFinite(value) || value < 0.5 || value > 3600) {
        this._showError("輪詢間隔必須介於 0.5 到 3600 秒。");
        return;
      }
      this._savingPoll = true;
      const button = this.querySelector("#save-poll");
      button.disabled = true;
      const saved = await this._call({
        type: "coselig_modbus_hub/set_polling",
        entry_id: entry.entry_id,
        enabled: polling.checked,
        interval: value,
      });
      if (saved) {
        this._pollDirty = false;
        this._pollDraft = { enabled: polling.checked, interval: String(value) };
      }
      this._savingPoll = false;
      button.disabled = false;
    }

    _readFormDraft() {
      const draft = {};
      CHANNEL_FIELDS.forEach((id) => {
        const field = this.querySelector("#" + id);
        draft[id] = field ? field.value : "";
      });
      return draft;
    }

    async _call(message) {
      try {
        const result = await this._hass.callWS(message);
        this._clearError();
        await this._load();
        return result;
      } catch (error) {
        this._showError(error && error.message ? error.message : String(error));
        return false;
      }
    }

    _handleChannelTableClick(event) {
      const button = event.target.closest("button");
      if (!button) return;
      const entry = this._selected();
      if (!entry) return;
      if (button.classList.contains("remove")) {
        if (!window.confirm("刪除這個通道並清除其 Discovery 設定？")) return;
        this._call({
          type: "coselig_modbus_hub/remove_channel",
          entry_id: entry.entry_id,
          slave: Number(button.dataset.slave),
          channel: button.dataset.channel,
        });
        return;
      }
      if (button.classList.contains("edit")) {
        const channel = (entry.channels || []).find((item) =>
          String(item.slave) === button.dataset.slave && item.channel === button.dataset.channel
        );
        if (!channel) return;
        this._channelDraft = {};
        CHANNEL_FIELDS.forEach((id) => {
          this._channelDraft[id] = String(channel[id] == null ? "" : channel[id]);
        });
        this._formDirty = true;
        this._syncFormFromDraft();
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    }

    _containsFocus(element) {
      let active = document.activeElement;
      while (active && active.shadowRoot && active.shadowRoot.activeElement) {
        active = active.shadowRoot.activeElement;
      }
      return Boolean(element && (active === element || element.contains(active)));
    }

    _setHidden(selector, hidden) {
      const element = this.querySelector(selector);
      if (element) element.classList.toggle("hidden", hidden);
    }

    _showError(message) {
      this._error = message;
      const notice = this.querySelector("#coselig-error");
      if (notice) {
        notice.textContent = message;
        notice.classList.remove("hidden");
      }
    }

    _clearError() {
      this._error = "";
      this._setHidden("#coselig-error", true);
    }
  }

  customElements.define("coselig-modbus-hub-panel", CoseligModbusHubPanel);
})();

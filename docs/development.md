# v1.0 開發架構與驗證依據

更新：2026-10-14。v1.0 取代 v0.2.x 的 MQTT／Panel／Channel 架構；歷史問題盤點保留於 `docs/specs/`，不作新版開發規範。

## 模組與責任

| 模組 | 責任 |
| --- | --- |
| `models.py` | 嚴格 Device／Model／GroupMode、輸出占用、身份、亮度與色溫轉換 |
| `protocol.py` | CRC16、0x03／0x06、例外回覆、完整 echo、拆包與單一 ADU 讀取 |
| `transport.py` | 一個 socket／worker、單筆在途、控制優先、bounded latest-wins、交易統計 |
| `scanner.py` | 獨佔 bus、最小共同 0x03 probe、合法回覆才列 Slave；無型號判斷 |
| `coordinator.py` | 單循環目標週期、逐 Slave 輪詢、真實 cache、可用性與重連退避、HA listener |
| `hub.py` | 組合 Transport／Coordinator，管理啟停與暫停輪詢的掃描 |
| `light.py` | 原生燈光、立即 optimistic 更新、正常 poll 校正、Kelvin color mode |
| `config_flow.py` | Gateway／進度掃描／型號／Group、Options、Reconfigure |
| `__init__.py` | typed `ConfigEntry[Hub].runtime_data`、啟動 retry、平台／關機清理、registry 差異移除 |
| `diagnostics.py` | 小型 runtime 快照、固定 Host 遮罩、不持久化 |

唯一正式核心位於 `custom_components/coselig_modbus_hub/`。沒有額外抽象基底、外部 packet library、第二套 runtime、自訂 registry 或 MQTT dependency。

## 資料與交易

永久 data 僅 `host`／`port`；options 僅 `poll_interval`、`scan_start`、`scan_end`、`devices[]`。P210 不含 group 欄位。Entry schema 2.1；舊 schema 明確拒絕自動遷移。

Entity 操作先寫 HA optimistic 狀態，再依同一操作順序入列。CCT 的亮度與色溫為不同合併 key，各自在 bus 上驗證 0x06 echo。Python 3.14 eager task 用於避免同一 loop tick 的 ON／OFF 或 CCT 子指令順序反轉。

worker 先取控制，再取 poll。每個控制 key 由 Slave、邏輯輸出、命令類別組成；替換只限 pending，舊等待者得到明確的 superseded 結果（`None`）。正在執行的封包不撤回。故障後清除 pending 控制；沒有 retry／read-back／跨重連 replay。

Coordinator 每個 Slave 只送一筆讀取並等待，cache 低 byte 值只接受 0～100。輪詢結果附 request 開始的 monotonic 時間，Entity 忽略在最近一次操作前已開始的舊輪詢；後續 poll 才校正 optimistic 值。write echo 不改真實 cache。

目標週期以整輪開始時間計算；過慢只 sleep(0)，沒有排程下一輪的 timer／task 或 backlog。掃描先取得整輪 lock，再取得 Transport submission gate，等待舊交易完成，保證 scan 與 poll 不穿插；取消也釋放 lock。掃描期間 UI 控制明確拒絕。

TCP 連線錯誤與 Slave transaction timeout 分開分類。`TimeoutError` 繼承 `OSError`，必須先處理 timeout，不能落入 TCP disconnect 分支。CRC 另有專屬 error，避免把 wrong slave／function／device exception 算成 CRC。

## Registry lifecycle

每 Slave 一個 HA Device，identifier 包含 Entry ID／Slave；不建立 Gateway Device。所有 native light 設定 `has_entity_name=True`。

setup 時依最新 profiles 列出有效 unique_id，只刪除同 Entry／本 integration 不再使用的輸出。存續 entity registry 項目完全不更新 user name。移除 Device 僅使用 HA 2026.10 的 `async_remove_device` API。

Options 使用 `OptionsFlowWithReload`；Reconfigure 使用 `async_update_reload_and_abort`。無手動 options update listener／第二次 reload。新 Host 先通過 TCP，才修改 Entry，Entry ID 與 registry 身份保持不變。

HA unload 先確認平台卸載成功，再停止 Hub；失敗保留 runtime。關機事件同樣停止 Coordinator／worker 與 socket。掃描流由 native progress task 取消生命週期與 `async_remove()` 釋放資源。

## 測試與可重現環境

`pyproject.toml`／`uv.lock` 固定測試依賴。HA **2026.10.1**、Python **3.14.7**、`pytest-homeassistant-custom-component` **0.13.372**。

涵蓋：protocol、transport／priority／latest-wins／capacity／取消／重連、scanner／合法回覆／自訂範圍、profiles／register 不重疊、Coordinator／backoff／no backlog／failure isolation、Native Light／optimistic／CCT、完整 native Config／Options／Reconfigure manager、實際 HA platform service、Device／Entity registry／Group 差異、Diagnostics／redaction、setup／unload／關機清理。

CI 使用 `pytest --cov-branch`，整體最低 90%；`config_flow.py` 行與分支要求 100%。涵蓋率報告由 `coverage.json` 產生，不將環境、fixture 或歷史文件計入產品 coverage。

## 官方 API 依據

以官方文件及已安裝的 HA 2026.10.1 原始碼核對；官方頁面會持續更新，實際相容性依固定版本測試。

- [Config Flow／Reconfigure](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Data entry flow／progress task](https://developers.home-assistant.io/docs/data_entry_flow_index/)
- [OptionsFlowWithReload](https://developers.home-assistant.io/docs/core/integration/options_flow/)
- [Config Entry runtime_data](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/)
- [Native Light／Kelvin API](https://developers.home-assistant.io/docs/core/entity/light/)
- [Entity Registry](https://developers.home-assistant.io/docs/entity_registry_index/)
- [Diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/)
- [HACS integration repository](https://www.hacs.xyz/docs/publish/integration/)

這些來源確認 HA／HACS API，**不證明 Coselig 特有 register、高 byte、CCT 接線或 Gateway timeout 特性**。實機仍依 [驗收 Checklist](v1.0-acceptance.md) 留存結果。

# v1.3.1 開發架構與驗證依據

更新：2026-10-15。v1.0 取代 v0.2.x 的 MQTT／Panel／Channel 架構；歷史問題盤點保留於 `docs/specs/`，不作新版開發規範。

## 模組與責任

| 模組 | 責任 |
| --- | --- |
| `models.py` | 嚴格 Device／Model／GroupMode、輸出占用、身份、亮度與色溫轉換 |
| `protocol.py` | CRC16、通用 FC01～FC06、例外回覆、完整 echo、拆包與單一 ADU 讀取 |
| `transport.py` | 一個 socket／worker、單筆在途、控制優先、bounded latest-wins、交易統計 |
| `scanner.py` | 獨佔 bus、共同 discovery、依 Profile family probes 辨識、型號由使用者確認 |
| `coordinator.py` | 單循環目標週期、逐 Slave 輪詢、真實 cache、可用性與重連退避、HA listener |
| `hub.py` | 組合 Transport／Coordinator，管理啟停與暫停輪詢的掃描 |
| `profiles.py` | 集中模型 register map、PollSpec、OutputSpec、dimmer codec、family probes |
| `switch.py` | R410 原生 Relay Switch、optimistic 狀態、FC05 與 poll 校正 |
| `light.py` | 原生燈光、立即 optimistic 更新、正常 poll 校正、Kelvin color mode |
| `config_flow.py` | Gateway／搜尋／新增確認、單設備修改、獨立移除確認、通訊設定、Reconfigure |
| `__init__.py` | typed `ConfigEntry[Hub].runtime_data`、啟動 retry、平台／關機清理、registry 差異移除 |
| `diagnostics.py` | 小型 runtime 快照、固定 Host 遮罩、不持久化 |

新增 `trace.py` 為200筆記憶體旁路，`monitor.py／monitor.js` 提供管理員唯讀診斷快照、HEX Trace、篩選與 Packet Decoder。正式部署新增 http／websocket_api dependencies，沒有 MQTT。

唯一正式核心位於 `custom_components/coselig_modbus_hub/`。沒有額外抽象基底、外部 packet library、第二套 runtime、自訂 registry 或 MQTT dependency。

## 資料與交易

永久 data 僅 `host`／`port`；options 僅 `poll_interval`、`scan_start`、`scan_end`、`devices[]`。P404 保存 `group_a`／`group_b`，P210 只保存 `group_a`，R410 不保存 Group；v1.1.0 缺少 P210 `group_a` 時由 `Device.from_dict()` 預設為 `single`。Entry schema 2.1；舊 schema 明確拒絕自動遷移。

Entity 操作先寫 HA optimistic 狀態，再依同一操作順序入列。CCT 的亮度與色溫為不同合併 key，各自在 bus 上驗證 0x06 echo。Python 3.14 eager task 用於避免同一 loop tick 的 ON／OFF 或 CCT 子指令順序反轉。

worker 先取控制，再取 poll。每個控制 key 由 Slave、邏輯輸出、命令類別組成；替換只限 pending，舊等待者得到明確的 superseded 結果（`None`）。正在執行的封包不撤回。故障後清除 pending 控制；沒有 retry／read-back／跨重連 replay。

Coordinator 依 Profile 的 PollSpec 逐 Slave 送一筆讀取並等待，狀態使用 address-keyed holding/input registers 與 coils/discrete inputs。低 byte 與 0～100 驗證只存在 Dimmer Profile。輪詢結果附 request 開始的 monotonic 時間，Entity 忽略在最近一次操作前已開始的舊輪詢；後續 poll 才校正 optimistic 值。write echo 不改真實 cache。

目標週期以整輪開始時間計算；過慢只 sleep(0)，沒有排程下一輪的 timer／task 或 backlog。掃描先取得整輪 lock，再取得 Transport submission gate，等待舊交易完成，保證 scan 與 poll 不穿插；取消也釋放 lock。掃描期間 UI 控制明確拒絕。

TCP 連線錯誤與 Slave transaction timeout 分開分類。`TimeoutError` 繼承 `OSError`，必須先處理 timeout，不能落入 TCP disconnect 分支。CRC 另有專屬 error，避免把 wrong slave／function／device exception 算成 CRC。

## Registry lifecycle

每 Slave 一個 Parent HA Device，identifier 包含 Entry ID／Slave；每個有效輸出使用原生 Child Device，identifier 維持 entity unique_id，並以 Parent 的真實 `DeviceEntry.id` 建立 `parent_device_id`。不建立 Gateway Device，也不使用 `via_device_id` 表示邏輯輸出。所有 native light／switch 設定 `has_entity_name=True`。

setup 時依最新 profiles 列出有效 unique_id，只刪除同 Entry／本 integration 不再使用的輸出。存續 entity registry 項目完全不更新 user name。移除 Device 僅使用 HA 2026.10 的 `async_remove_device` API。

Options Flow 首頁只分「設備管理／通訊設定」。新增 selector 僅包含本次新找到的 Slave；Existing／Missing 透過文字區塊說明，不進入 Model／Group，也不能在新增流程移除。Edit 一次處理一個 Slave，Remove 獨立多選並另有確認頁。新增、修改、移除的 pending state 留在 Flow instance；只有確認 step 會以原 options 深複製後更新 `devices`，保留 poll interval、搜尋範圍與未知欄位。P404 預設、R410 唯一候選快進及人類可讀輸出選項都由 Flow 提供，不更動 schema 2.1。

Options profiles 使用 `OptionsFlowWithReload`；polling step 設 `automatic_reload=False` 並直接更新 runtime／喚醒 scheduler，無 options update listener。Reconfigure 使用 `async_update_reload_and_abort`。無手動 options update listener／第二次 reload。新 Host 先通過 TCP，才修改 Entry，Entry ID 與 registry 身份保持不變。

HA unload 先確認平台卸載成功，再停止 Hub；失敗保留 runtime。關機事件同樣停止 Coordinator／worker 與 socket。掃描流由 native progress task 取消生命週期與 `async_remove()` 釋放資源。v1.1.2 的 Parent 與 entity unique_id 原地保留；v1.2.0 由 LightEntity forwarding 建立 Child，HA entity registry 會保留 entity_id、名稱與 Area。

## 測試與可重現環境

`pyproject.toml`／`uv.lock` 固定測試依賴。HA **2026.10.1**、Python **3.14.7**、`pytest-homeassistant-custom-component` **0.13.372**。

涵蓋：protocol、transport／priority／latest-wins／capacity／取消／重連、scanner／合法回覆／自訂範圍、profiles／register 不重疊、Coordinator／backoff／no backlog／failure isolation、Native Light／optimistic／CCT、完整 native Config／Options／Reconfigure manager、實際 HA platform service、Device／Entity registry／Group 差異、Diagnostics／redaction、setup／unload／關機清理。

CI 使用 `pytest --cov-branch`，整體最低 99%；`config_flow.py` 行與分支要求 100%。涵蓋率報告由 `coverage.json` 產生，不將環境、fixture 或歷史文件計入產品 coverage。

## 官方 API 依據

以官方文件及已安裝的 HA 2026.10.1 原始碼核對；官方頁面會持續更新，實際相容性依固定版本測試。

- [Config Flow／Reconfigure](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Data entry flow／progress task](https://developers.home-assistant.io/docs/data_entry_flow_index/)
- [OptionsFlowWithReload](https://developers.home-assistant.io/docs/core/integration/options_flow/)
- [Config Entry runtime_data](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/)
- [Native Light／Kelvin API](https://developers.home-assistant.io/docs/core/entity/light/)
- [Entity Registry](https://developers.home-assistant.io/docs/entity_registry_index/)
- [Device registry child devices](https://developers.home-assistant.io/docs/device_registry_index/)
- [Diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/)
- [HACS integration repository](https://www.hacs.xyz/docs/publish/integration/)

這些來源確認 HA／HACS API，**不證明 Coselig 特有 register、高 byte、CCT 接線或 Gateway timeout 特性**。實機仍依 [驗收 Checklist](v1.0-acceptance.md) 留存結果。

## v1.1 行為

TCP 成功後先顯示範圍，首次1～247；按開始搜尋才建立progress task。初次搜尋結果必須明確勾選Slave，再指定型號／Group。既有保存範圍不改寫。取消不保存未完成的範圍，也不關閉已載入Hub。

Polling=0 只讓背景scheduler等待Event，既有狀態／failure保留，寫入與scan不受停輪詢影響。0→非零立即喚醒、讀取真實狀態。起動時仍有單次bootstrap read；paused reconnect不發FC03，只恢復已確認且failure<3的可用性。

20輪時間deque只量測bus round，不含lock等待／下一輪sleep；不自動調interval或降頻Slave。原生 Entity／Child Device Registry 的 area_id 完全由 HA 與使用者管理，不加入 area 欄位或 mapping table。

Monitor訂閱每0.5秒讀取ring、Coordinator health／round time、Transport counters／queue 與Configured Device metadata，只在整體快照變更時推送；helper只取runtime記憶體，沒有任何Transport.submit／Modbus I/O。Statistics與Clear Trace分離。Sidebar有Gateway、Communication Statistics、Device Health、Packet Decoder；Slave／Type filter、裝置點擊與Auto-scroll都只有前端效果。transaction grouping由Raw entries推導，Decoder涵蓋FC01～FC06與FC81～FC86 exception；Trace status保留CRC權威判定。Pause只凍結render，Auto-scroll獨立。所有WS命令要求admin，只提供list／subscribe／clear，沒有register或設定寫入。Trace例外隔離；完整RX記錄在原CRC驗證前，結果之後另記OK／ERROR及latency。狀態色維持固定TX／RX／OK／TIMEOUT／CRC ERROR／ERROR，背景與卡片使用HA theme variables。

## v1.1.1 行為

P210 的 Group A 可選 `single` 或 `cct`；single 建立 CH1／CH2，cct 只建立 `group_a` 雙色溫 entity，兩者均固定 FC03 起點 `0x082A`、count 2。P404 的 Group A／B 行為不變。切換輸出模式沿用既有 registry 差異清理與原生 Area／名稱保存策略。

## v1.3.0 baseline

詳見 [v1.3.0 architecture](v1.3.0-architecture.md)。R410 只建立四個 Switch；FC02 僅供 Scanner identification，無 DI runtime poll 或 entity。

## v1.3.1 UI 行為

Config Flow options 以 HA Native menu/form 實作，不新增 frontend；Flow 管理器在確認摘要前不呼叫 `async_create_entry`。既有 Device 只在獨立 Edit／Remove intents 被選取，Add 的 selector 僅接受 New Slave，Missing 保留。對未修改 Slave 重新序列化的是原 `Device` profile 的值，entity registry identities、Parent／Child identifiers、Area 與自訂名稱策略維持不變。Entry version/minor version仍是2.1。

Diagnostic Monitor 擴充原有 subscribe snapshot，保留list／subscribe／clear三種WebSocket commands與約500ms changed-only publish。前端只衍生 transaction、filter及decoder view，不建立Graph／歷史資料，也不增加Modbus request；Clear只清Trace ring，不清Transport counters。

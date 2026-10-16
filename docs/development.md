# 開發與維護指南

更新：2026-10-16。本文描述目前架構、Runtime 行為、維護界線與可重現驗證方式；排程容量與語意按 v1.6.0 更新。S402 實機驗收已依已回報項目通過並核准 v1.4.0 發布；未回報的硬體情境列於 [S402 驗收紀錄](v1.4.0-s402-acceptance.md)。使用者操作請看 [README](../README.md)；版本差異以 [GitHub Releases](https://github.com/JK0526/coselig-modbus-hub/releases) 與各版 Release Notes 為準。歷史問題盤點保留在 `docs/specs/`，不作新版規範。

## 架構與模組責任

每個 Config Entry 對應一個 Gateway connection、Hub、Transport、Coordinator 與設備集合；多個 Config Entry 彼此隔離。主要程式位於 `custom_components/coselig_modbus_hub/`。

| 模組 | 責任 |
| --- | --- |
| `models.py` | Device／Model／GroupMode、序列化與亮度／色溫轉換 |
| `profiles.py` | DeviceProfile、register map、PollSpec、OutputSpec、dimmer codec 與 family probe |
| `protocol.py` | CRC16、通用 FC01～FC06、例外回覆、echo 驗證、ADU 解析 |
| `transport.py` | 一條 TCP socket、序列化 TX worker、唯一 Continuous RX reader、pending response Future、控制優先、bounded queue 與交易統計 |
| `scanner.py` | 獨佔 bus 搜尋、Slave discovery 與 family probe |
| `coordinator.py` | 輪詢週期、per-Hub runtime Poll pause、設備狀態 cache、availability、failure threshold 與 reconnect |
| `hub.py` | 組合 Transport／Coordinator，管理啟停與掃描 |
| `light.py`／`switch.py` | 原生 HA entity、optimistic state、FC06／FC05 控制 |
| `event.py` | 每顆 S402 的四個原生 EventEntity 與 Gateway availability |
| `s402.py` | S402 unsolicited frame signature、CRC／DI parser 與 options ID validation |
| `config_flow.py` | 初始設定、搜尋範圍、Options intents、Reconfigure |
| `__init__.py` | Config Entry runtime_data、平台生命週期與 registry 差異清理 |
| `diagnostics.py` | 遮罩 Host 的 runtime diagnostics |
| `trace.py`／`monitor.py`／`monitor.js` | 記憶體 trace、管理員 WebSocket API、EVENT routing 與 Poll runtime 控制前端 |

正式整合只有一套 runtime；不使用 MQTT、自訂 Entity Registry、資料庫、外部 Modbus packet library 或第二套 Gateway worker。

## Device Profiles 與 Modbus Protocol

`DeviceProfile` 集中定義 pollable／controllable 設備能力，Coordinator 依 `PollSpec` 讀取，Entity 平台依 `OutputSpec` 建立 Light／Switch。S402 不屬於 `Model` 或 `DeviceProfile`：它不 poll、不 control、不 read-back，只由 Transport 收取非請求事件。設備能力基線：

| Model | Runtime Poll | Outputs |
| --- | --- | --- |
| P404 | FC03，start `0x082A`，count 4 | Group A、Group B；single 或 CCT |
| P210 | FC03，start `0x082A`，count 2 | Group A；single 或 CCT |
| R410 | FC01，start 0，count 4 | Relay 1–4，FC05 |

P404 的 Group A 使用 CH1／CH2，Group B 使用 CH3／CH4；P210 只有 Group A。Single mode 建立兩個 channel outputs，CCT mode 建立一個 group output。P210 不因 CCT 改變 poll count。R410 DI probe 只供 Scanner family identification，不建立 DI entity，也不作 runtime DI poll。

Dimmer control 使用 FC06 與既有 `0x05xx` codec；profile 共用亮度及色溫 conversion。CCT 的 brightness／color temperature 由同組連續 registers 表達。零亮度為 off，非零最低亮度為 2%；HA 色溫使用 Kelvin API，轉換範圍為 175–455 mired。Conversion 保持集中於 models／profiles，不在各 Entity 複製。

Dimmer response 的 register low byte 必須在 0–100；不合法值不更新輸出 cache。Profile 同時負責 Single／CCT 輸出的 register 占用與 entity key，避免兩種輸出模式同時建立重疊 Entity。P404 預設、P404／P210 人工確認，以及無法辨認 family 時保留 Unknown，皆由 Config Flow 負責。

Transport 收送 RTU frame over TCP，不使用 MBAP。通用 protocol layer 支援 FC01～FC06：bit response 依 Modbus packed-bit order 解讀，register response 解成 UINT16。驗證包括 CRC16、Slave／Function 對應、byte count／frame length 與 write echo；Modbus exception、wrong slave／function、timeout 與 CRC error 分開分類。FC05／FC06 write 必須回覆完整相符 echo 才算成功；不自動 retry、read-back 或跨 reconnect replay。

`TimeoutError` 同時屬於 `OSError`，Transport 必須先分類 timeout，再分類 TCP socket error，避免把 Slave transaction timeout 誤判為 Gateway disconnect。CRC 使用獨立 error；錯 Slave、錯 Function 與 device exception 不計入 CRC failure。

Scanner 不替 P404／P210 猜測型號，型號由使用者確認。若 Entry 已設定 S402，Slave #1 仍可被 scanner probe，但 Config Flow 將其標為保留衝突並拒絕加入。普通 Slave #1 已設定時，新增 S402 會被拒絕。S402 ID 與 Modbus Slave 位址分屬不同 namespace，因此 S402 #51 可與 P404 Slave #51 共存。探測流程依序為：

1. Common discovery：FC03，start 0，count 1。
2. Dimmer family probe：FC03，start `0x082A`，count 2。
3. Relay family probe：FC02，start 0，count 4；只辨認 R410 family，不暴露 DI。

搜尋獨佔 bus：Coordinator 暫停開始新一輪，等待既有交易，取得整輪與 Transport submission lock 後才送 probe；取消或完成都釋放 lock 並恢復原 Polling 狀態。控制在掃描期間明確拒絕，不排入 queue。Family probes 均不符合時保留 Unknown 供使用者判斷，不將未辨識設備自動當成 Dimmer 或 Relay。

## Runtime、Polling 與控制順序

每個 Gateway 的 Transport 使用一條 TCP socket、一個 worker 與一份共享的 `OrderedDict` 控制排程，同時間最多一筆 Modbus request 在途。Stateful controls 以 `CommandKey` 合併，最多保留 256 個不同 pending keys；同 key 最新值取代尚未送出的舊值，並保留原排程位置。此容量計的是命令 key，不等於實體設備或迴路數；例如一個 CCT output 可使用亮度與色溫兩個 key。已送出的 ADU 不會撤回；同 key 新命令會排在進行中的請求之後。

R410 Cover actions 以獨立容量最多保留 32 個 pending `ActionSlot`，不合併、嚴格 FIFO，每個已接受 action 只送出一次。Stateful 與 Action 配額互不占用，因此最多可有 288 個 pending controls；兩類仍按同一份全域順序交錯送出，並非各自建立 worker 或 bus。Control 優先於 Poll，Poll queue 不累積逾期輪次；Coordinator 逐 Slave 發送並等待單筆 poll。Gateway／bus failure 時丟棄所有尚未送出的 stateful controls 與 actions，不在 reconnect 後重播。

Transport 啟動後另建立一個長期 RX task，這是該 socket 唯一呼叫 `protocol.receive()` 的地方；TX worker 不讀 StreamReader。每個 `_exchange()` 在寫 request 前建立唯一 pending response Future，再寫入並等待原始 timeout。RX task 讀完整 RTU frame 後先檢查保留 signature `01 06 01 A0`：signature candidate 永遠消耗並交給 pure S402 parser，絕不完成普通 pending Future；有效且已設定 ID 的事件交給 Hub listener，unknown ID 只保留 EVENT trace。其他 frame 才交給 pending Future，原 `_exchange()` 接著呼叫既有 `validate(request, response)`。插入任意數量 S402 event 都不 await automation、不重置 request timeout，也不增加 Modbus request counters。

S402 frame 的 DI byte 只接受 bit `01`／`02`／`04`／`08`，映射 DI1–DI4；CRC 與 frame length 必須完整有效。signature candidate 中 CRC／DI invalid 的封包被消耗並寫 debug log，不會觸發 HA event，也不會污染 CRC request counter。S402 ID 使用 byte 4 的完整 uint8 範圍 0–255；本 repo 現有 source docs、`docs/specs/` 與 tests 沒有更窄 ID 範圍，故沒有套用一般 Modbus Slave 1–247 限制。

RX EOF／socket error 只把 Transport 狀態標為 disconnected 並呼叫已註冊的 loss callback；Hub 將通知交給 Coordinator。Reconnect/backoff 仍只有 Coordinator 負責。Intentional `disconnect()`／`close()` 會先標記 teardown，再停止 RX task 並清理 pending Future，不觸發 loss callback。Gateway 重連後 Coordinator 一定發布一次更新，即使 devices 為空或 Polling=0，讓 S402 EventEntity availability 即時恢復。

Scheduler 以 bus round 的開始時間計算目標週期；當一輪超過 interval 時立即開始後續工作，不補排積欠的 timer/task。poll request 記錄 monotonic start time。Entity 收到較新的使用者操作後，會忽略操作前已啟動的 poll response，避免舊資料覆寫 optimistic state；後續正常 poll 再校正真實狀態。

Entity control 立即更新 HA optimistic state，再依操作順序提交 Transport。寫入 echo 不直接寫入設備真實 cache；控制失敗保留 optimistic 顯示，交由後續 poll 校正。Gateway failure 會清除 pending control；離線時拒絕新控制，重連後不播放舊指令。

Home Assistant 2026.10／Python 3.14 eager task 行為用於維持同一 loop tick 內 ON／OFF 與 CCT 子指令的提交順序。P404、P210 dimmer 的 brightness／CCT 使用各自的 command key，pending command 合併不會跨不同 register 或 Relay 輸出。

Poll interval 的合法值為 0 或至少 0.5 秒。Polling=0 停止背景 scheduler，但初次 setup 仍做一次 bootstrap read。實體控制、Scanner 與 Monitor 不因背景 Polling=0 停用；HA Update Entity service 仍可要求明確讀取。恢復非零 interval 時喚醒 Coordinator 並重新讀取真實狀態。Paused 狀態重連時不加發 FC03；只恢復先前已確認且連續失敗少於 3 次設備的可用性，狀態仍是最後已知值。這是目前 Runtime 行為，不另存 scheduler backlog 或 uptime。

連續 Slave transaction 失敗達 3 次才標記該 Slave unavailable；成功交易清除失敗計數並恢復。TCP refused／reset／EOF 代表 Gateway disconnect，所有設備 unavailable，reconnect backoff 為 1／2／4／8／15／30 秒，之後上限維持 30 秒。啟動離線由 ConfigEntryNotReady 交給 Home Assistant retry。一般 Slave timeout／CRC 不直接等同整個 Gateway offline；整輪全部失敗時重建 TCP。

## Config Entry 與 HA Registry

Config Entry data 僅保存 `host`／`port`；options 保存 `poll_interval`、`scan_start`、`scan_end`、`devices[]` 與相容選填的 `s402_devices: [{"id": 7}]`。缺少該欄位視為空清單；schema 維持 version 2、minor version 1，沒有 migration。S402 ID 必須在 0–255 且同一 Entry 不重複，與 `devices[].slave` 不共用 namespace。P404 保存兩個 group mode，P210 只保存 `group_a`，R410 不保存 Group。舊 P210 設定缺少 `group_a` 時由 `Device.from_dict()` 預設為 `single`。

Config／Options Flow 使用 Home Assistant native menu／form；新增、修改、移除 pending state 留在 Flow instance，只有明確確認頁才呼叫 `async_create_entry`。新增 selector 只接受本次找到的 New Slave；Existing／Missing 不會被新增流程改寫。Edit 一次修改一台；Remove 為獨立多選流程。確認後以既有 options 為底，只更新 `devices`，保留輪詢、搜尋範圍與未知欄位。

Options 使用 `OptionsFlowWithReload`；輪詢週期 step 設 `automatic_reload=False`，直接更新 runtime 並喚醒 scheduler，不使用 options update listener 或第二次 reload。Reconfigure 使用 `async_update_reload_and_abort`；先測試新 Gateway，成功才替換 Host／Port，保留 Entry ID 與 registry identity。

每個 Slave 有一個 Parent Device；有效輸出使用 Child Device。每顆 S402 是一個 Parent Device `Coselig S402 #<id>`，含 4 個 `has_entity_name=True` EventEntity，unique_id 為 `{entry_id}_s402_<id>_di1` 至 `_di4`；四個 Entity 共用同一 Parent，不建立 Child。Native Entity 設 `has_entity_name=True`。Modbus Parent identifier 由 Entry ID 與 Slave 組成，Entity unique_id 使用 `{entry_id}_slave_{slave}_chN`、`_group_a`、`_group_b` 或 relay key；不包含 Gateway IP、display name 或 entity_id。Child 的 identifier 沿用 entity unique_id；parent_device_id 指向 HA 真正的 Parent Device Registry ID，不用 `via_device_id` 表示邏輯輸出。

S402 `EventEntity` 使用 `EventDeviceClass.BUTTON` 與 `ButtonEventType.PRESS_END`。它沒有 per-device poll health；availability 僅依 Coordinator Gateway connection。`async_added_to_hass()` 註冊該 ID／DI listener，`async_on_remove()` 解除；事件呼叫 `_trigger_event()` 後寫 HA state。每次 reload 會建立新的 Hub／Entity listener set，舊 Entity unload 時移除 subscription。

Setup 依 profiles 計算目前有效 unique_id，只移除同 Entry／本整合不再提供的舊輸出。存續 Entity Registry entry 不重建、不改 user name、entity_id、Area 或 automation reference。Group mode 切換只移除受影響 outputs；另一 Group 的 entity 與 metadata 保留。使用者可在 HA 原生 Entity Registry 為各輸出分別指定 Area。整合不設定 suggested_area、不建立 Area 欄位或 mapping database，也不覆寫 Registry Area。已移除 output 若日後重建，HA 不保證還原被刪除 registry item 的 metadata。

HA unload 先確認 platform unload 成功，再停止 Hub；若 platform unload 失敗則保留 runtime。HA shutdown 會停止 Coordinator、Transport worker 與 socket。Scanner progress task 在取消或移除時釋放整輪／submission lock。

Device Registry 移除使用 Home Assistant 2026.10 原生 `async_remove_device` API。只有被移除或不再有效的 Device／Entity 才進入 registry removal；存續項目不以 reload 重建。

## Diagnostics 與多 Gateway 隔離

Diagnostics 只輸出 runtime snapshot；Host／IP 遮罩，可列出 configured S402 IDs，但不包含 packet history、last press history、密碼或其他 HA Device。統計為記憶體計數，restart／reload 歸零。Coordinator round time 從取得輪詢 lock、第一個 configured Slave 開始至本輪最後一個完成；不含輪間等待與掃描 lock 等待。Average 僅取最近 20 輪。不自動調整 polling interval，也不依失聯 Slave 降頻。

Diagnostics 不建立 sensors，不提供可持久化 Trace 或其他裝置資料。Log 只在狀態改變或 queue full 時輸出 warning；逐次通訊細節留在 DEBUG。不因暫時網路異常建立 Repair。

每個 Config Entry 的 socket、queue、polling、devices、cache、statistics 與 monitor trace 都獨立。Integration 不提供跨 Gateway 共用 bus arbitration；部署建議一條 RS485 Bus 由一個 Gateway／Master 管理。不得假設同 Gateway 多個 TCP clients 一定安全。

## Monitor 前後端資料流

Monitor 僅供管理員使用，WebSocket command 僅有 list／subscribe／clear。Backend 每約 500 ms 讀既有 ring、Coordinator health、Transport counters／queue 與 device metadata；只在 snapshot 改變時發布。Monitor helper 不呼叫 Transport submission，不產生 Modbus read/write。後端 TraceBuffer 固定 200 entries，記錄完整 RX frame 與後續交易結果；RX frame 在原 CRC 驗證前記錄，CRC／交易結果再以 OK／ERROR status 追加。trace logger 例外會被隔離，不影響正常通訊。TX／RX／OK／TIMEOUT／CRC ERROR／ERROR 採固定前景狀態色；背景與卡片仍使用 Home Assistant theme variables。

Browser page 的 in-memory history 上限為 1000 entries。Entry stable key 由 timestamp、direction、raw hex、source 與 latency 組成；snapshot merge 先去重，再按 timestamp 排序並保留上限內最新資料。Gateway 切換、Clear 或 page reload 會清空 local history；Clear 同時清 backend trace，保留 Transport statistics。Pause 凍結 render 但仍 ingest snapshot。

Transaction 由 raw entries 分組，stable key 使用交易 TX entry key；孤立狀態則使用該組第一筆 entry key。EVENT 每筆永遠是 standalone trace row，且不清除前一個 TX transaction 的 grouping pointer，因此 `TX → EVENT → RX → OK` 仍是 Poll transaction 加一個獨立 EVENT。EVENT decoder 依 source semantics 解讀 S402 ID／DI／single click／PRESS_END／configured 狀態，不套普通 Slave #1 FC06 decoder。Trace timestamp 在同一毫秒內單調遞增，避免相同 DI 的快速重複事件被前端 stable-key merge 去重。Auto-scroll OFF 會以目前可見 row key 與 viewport offset 保留閱讀位置；原 row 被裁切時用後續 timestamp 定位，沒有後續資料則回到最舊可見 row。重新開啟 Auto-scroll 才回到最新 row。

Sidebar 使用單一主要垂直 scroll context；Decoder 卡片在 normal flow，desktop 內容高度上限為 `min(38vh, 360px)` 並可內部捲動。只有使用者 click Trace row 且 Decoder 超出 Sidebar viewport 時，才呼叫 smooth `scrollIntoView`。Snapshot refresh 會保留 Sidebar scrollTop；filter、pause/resume 不會主動捲動。窄螢幕改為 Sidebar 在 Trace 上方的自然單欄，不對整頁強制 scrollIntoView。Device Health 清單固定最高 240px；通訊統計值直接呈現既有 Transport counters。Caption「自本次啟動起累計」只是文案，沒有 runtime duration／uptime 欄位。

Monitor 的「暫停輪詢」只改 Coordinator 的執行期狀態，不寫入 Config Entry Options；每個 Config Entry 各自保存該狀態，reload 後清除。設定週期為 0 時，Monitor 顯示「設定停用」且不能 runtime-resume。此控制只停背景輪詢，不取消 in-flight request，不攔截明確控制、Scanner、Continuous RX、S402 EVENT 或 reconnect；恢復時喚醒原 scheduler，不建立 poll backlog。

所有 filter、transaction decoder、Device Health 與 counters presentation 都是 frontend view。Monitor 不讀 Recorder、不寫資料庫、不提供任意 Modbus command／register write。

## 測試與可重現環境

開發依賴由 `pyproject.toml`／`uv.lock` 固定。基準環境：Home Assistant **2026.10.1**、Python **3.14.7**、pytest-homeassistant-custom-component **0.13.372**。v1.3.3 基線：**440 Python tests、36 frontend tests、99.57% branch coverage**；Config Flow line／branch coverage 要求 100%。

v1.4.0 自動驗證結果：**520 Python tests、39 frontend tests、99.75% branch coverage、Config Flow line／branch coverage 100%**。S402 實機驗收結果與未確認情境分列於 [驗收紀錄](v1.4.0-s402-acceptance.md)；自動測試不作為硬體驗收證據。

v1.6.0 scheduler 軟體／自動化驗收：**PASS**，包含 **603 Python tests、39 frontend tests、99.96% branch coverage、Config Flow line／branch coverage 100%**。大型 100+ circuits 實體部署壓力測試未直接進行；此限制與自動化軟體驗收狀態分開記錄於[驗收清單](v1.6.0-scheduler-acceptance.md)。

本機檢查命令：

```sh
uv sync --locked --python 3.14
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
uv lock --check
uv run pytest --timeout=30 --cov-fail-under=99.5
node --test tests/frontend/monitor.test.cjs
```

Node 使用內建 test runner，無額外 frontend dependency。Python 測試使用正式 custom component、Home Assistant APIs 與 loopback Gateway。CI 執行 Ruff、format、frontend tests、完整 pytest、coverage gate 與 translation JSON consistency。Coverage report 輸出為 `coverage.json`；環境程式、fixture 與歷史文件不計入產品 coverage。

## 官方 API 與架構文件

以官方文件及已安裝的 Home Assistant 2026.10.1 原始碼核對；官方文件會更新，實際相容性以固定版本測試為準。

- [Config Flow／Reconfigure](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Data entry flow／progress task](https://developers.home-assistant.io/docs/data_entry_flow_index/)
- [OptionsFlowWithReload](https://developers.home-assistant.io/docs/core/integration/options_flow/)
- [Config Entry runtime_data](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/)
- [Native Light／Kelvin API](https://developers.home-assistant.io/docs/core/entity/light/)
- [Entity Registry](https://developers.home-assistant.io/docs/entity_registry_index/)
- [Device Registry child devices](https://developers.home-assistant.io/docs/device_registry_index/)
- [Diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/)
- [HACS integration repository](https://www.hacs.xyz/docs/publish/integration/)
- [v1.3.0 DeviceProfile／R410 architecture](v1.3.0-architecture.md)
- [實機驗收 checklist](v1.0-acceptance.md)

官方來源確認 Home Assistant／HACS API，不證明 Coselig 特有 register map、dimmer byte 語意、CCT wiring 或 Gateway timeout 特性；這些仍依設備規格及實機驗收。

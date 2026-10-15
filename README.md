# Coselig Modbus Hub v1.2.1

將 Coselig **P404／P210** 透過 TCP／RS485 Gateway 加入 Home Assistant，直接建立 Native LightEntity。設備與連線設定全走 HA 原生介面；不需要 MQTT、Node-RED 或 YAML。新增的唯讀 Modbus Monitor 只顯示通訊封包，不提供設備管理或寫入工具。

```text
P404 / P210 → RS485 → TCP Gateway → RTU Transport → Coordinator → Native LightEntity
                                    └→ Memory Trace → Read-only Monitor
```

套件 **1.2.1**，domain `coselig_modbus_hub`，Config Entry schema `2.1`。最低 HA Core **2026.10.0**；本機測試使用 **2026.10.1／Python 3.14.7**。

## 安裝與第一次設定

1. HACS Custom repositories 加入 `https://github.com/JK0526/coselig-modbus-hub`，類型 **Integration**，下載並重啟 HA。本專案未宣稱已收錄 HACS 預設清單。
2. 設定 → 裝置與服務 → 新增整合 → **Coselig Modbus Hub**。
3. 輸入 Host／Port（Port 預設 502）。先測試真正 TCP；失敗留在原頁，可修改重試。
4. TCP 成功後先顯示 **搜尋 Modbus 設備**。第一次預設 `1～247`，可改成現場範圍，例如 `51～80` 或 `1～10`。**進入頁面不會掃描；按「開始搜尋」後才送 request。**
5. 原生 Progress 顯示搜尋進度與找到設備數。若找不到，可回到範圍頁重試、改範圍或取消，不建立半完成 Entry。
6. 勾選要加入的 Slave，再逐台指定 **P404／P210**（預設 P404）；Scanner 不猜型號、不自動加入設備。
7. P404 設定 Group A／B；P210 只設定 Group A，預設 **2 × 單路調光**，也可選 **1 × 雙色溫 CCT**。完成後建立每 Slave 一個 Parent Device，並為每個有效輸出建立原生 Child Device 與 LightEntity。

搜尋範圍限整數 `1～247` 且 start≤end；縮小範圍可節省無回應位址的等待時間。共同 probe 為 `0x03／0x082A／count 2`，只接受 slave／function／length／CRC 全部合法的回應。

## 設備輸出

| 設備／模式 | 原生燈光 |
| --- | --- |
| P404 A single／B single（預設） | CH1、CH2、CH3、CH4 |
| P404 A CCT／B single | 雙色溫 A、CH3、CH4 |
| P404 A single／B CCT | CH1、CH2、雙色溫 B |
| P404 A CCT／B CCT | 雙色溫 A、雙色溫 B |
| P210 Group A single（預設） | CH1、CH2 |
| P210 Group A CCT | 雙色溫 A |

Group A 使用 CH1＋CH2，Group B 使用 CH3＋CH4。CCT 沿用第一個 register 控制亮度、第二個控制色溫的協議；請確認接線與模式。HA 色溫使用 Kelvin API，沿用 175～455 mired 映射，約 2198～5714 K。非零最低亮度 2%；零為關閉。U4 不支援。

型號由使用者指定。P210 不論 single 或 CCT 都固定使用 2-register poll；P210 誤選為 P404 時，4-register poll 不相容。本版不新增自動型號偵測、猜測或提醒流程。

## Modbus Monitor

管理員登入後，左側 **Coselig Modbus Monitor** 可選擇已載入 Gateway，查看：

- 瀏覽器本地時間（含毫秒）、TX／RX 完整 Raw HEX。
- OK／CRC ERROR／TIMEOUT／ERROR 與 transaction latency（毫秒）。
- Gateway TCP connected 與目前 polling interval。

TX、RX、OK、TIMEOUT、CRC ERROR、ERROR 會依 HA Light／Dark theme 的 CSS variables 顯示不同色彩，timestamp、latency 使用次要文字色，Raw HEX 沿用 TX／RX 色系。

畫面最多保留 **200 筆記憶體 trace**；每約 0.5 秒送出有變更的快照，不增加 Modbus request。HA 重啟／integration reload 後清空；不寫 Recorder、資料庫或檔案，也不把 packet history 放進 Diagnostics。

只提供 **清除、暫停畫面、繼續畫面**。暫停畫面不停止 TraceBuffer、Modbus、輪詢或控制。清除只清記憶體 trace。離開頁面取消訂閱。

此介面 **唯讀**：沒有任意 command、register 寫入、設備／通道 CRUD、Scanner 管理或 Gateway 設定。所有設定仍走 HA 原生流程。初次安裝尚未建立 Entry 時的搜尋使用獨立暫時連線，因此不顯示在已載入 Gateway 的 Monitor；Entry 建立後的重新搜尋會標記為 scan。

## 輪詢與 Polling=0

選項 → **輪詢週期**：

- `2.0` 秒：預設正常背景輪詢。
- `0` 秒：停止背景 FC03；不累加因「沒有輪詢」而產生的失敗計數，保留最後已知狀態與可用性。
- 非零必須至少 `0.5` 秒；`0.1`／`0.2` 等會拒絕。

變更週期直接套用到目前 runtime，**不需 reload／restart**。`0 → 2` 會立即喚醒 scheduler，完整輪詢校正真實狀態。`2 → 0` 不取消已在 bus 上的 request，但不再提交後續背景讀取。

**Polling=0 不停用控制、Scanner、Monitor 或 Gateway 連線。** ON／OFF、brightness、CCT 仍送 FC06 並立即 optimistic 更新 UI。暫停期間沒有背景讀取，所以外部操作不會自動同步；可恢復 polling，或明確使用 HA 的更新實體服務讀取。

HA 啟動／Entry reload 沒有持久化 runtime cache，因此即使設定 0，仍先做一次初始化讀取以確認設備與取得初始狀態；之後不啟動背景讀取。TCP 明確失聯仍會讓實體 unavailable 並重連。Paused 模式重連時不增加 FC03，僅恢復先前已確認且 failure<3 的設備可用性；狀態仍是最後已知值。

非零模式維持：每 Slave 每輪一筆讀取（P404 4、P210 2 registers）、單筆 in-flight、Control>Poll、32 筆 bounded control queue、latest-wins、無 poll backlog。整輪超過週期時立即下一輪，不積累排程。寫入只驗完整 echo，不額外 read-back、不自動 retry／replay。操作前已開始的舊 poll 不會覆蓋新 optimistic 值。

## 管理與重新搜尋

選項 → **管理設備／輸出模式** 可保留、移除、修改指定型號與 Group 模式。

選項 → **重新掃描設備** 先顯示上次完成管理時保存的 `scan_start／scan_end`。例如上次 `51～80`，本次先帶入 `51～80`；仍需按「開始搜尋」，不自動掃任何範圍。既有 v1.0 儲存的範圍會保留，不強行改成新預設。

搜尋先暫停 Coordinator、等舊交易完成，再獨佔 bus。結束／取消後恢復原有狀態，包括 Polling=0。掃描期間控制明確拒絕，不累積操作。Existing／Missing 預設保留；New 由使用者勾選加入，取消勾選才刪除。全部 Missing 也不會自動刪。

Group 模式切換只替換指定 Group 的 Entity Registry 項目；另一 Group 的 unique_id、entity_id、自訂名稱與 Area 保留。被移除輸出的自動化參照需人工調整；切回不保證還原已刪實體的 metadata。

## Parent／Child Device 與個別 Area

每個 Slave 是一個 Parent Device，CH1～CH4 或 Group A／B 是其原生 Child Device。Parent 可代表弱電箱等實體位置；Child 沿用 HA 原生 parent-child registry 關係。

在 HA 的個別實體設定頁，將 **CH1→客廳、CH2→餐廳、CH3→玄關、CH4→廚房**，或將 CCT A／B 分別指定不同 Area。使用 HA 原生 Entity Registry，不新增 area mapping、room database 或 integration 欄位。

整合不設定 suggested_area、不儲存 area mapping，也不改 entity_registry.area_id；reload、重新搜尋及 options change 都不覆寫使用者 Area 或名稱。每個 Child 可獨立指定 Area，未指定時由 HA 處理 Parent 的繼承顯示。刪除／替換的輸出則屬新的 registry lifecycle，請按需求重新設定。

## Reconfigure、身份與故障恢復

重新設定 Host／Port 時，先驗證新 TCP，成功才更新與 reload，失敗保留原值。Device identifier 使用 Entry ID＋Slave，unique_id 為 `{entry_id}_slave_{slave}_chN`／`_group_a`／`_group_b`，不含 IP、name 或 entity_id。

已成功讀取的 Slave 前兩次交易失敗保持 available，第三次 unavailable；成功清計數與恢復。TCP refused／reset／EOF 為 Gateway 離線，全部 unavailable，依 `1／2／4／8／15／30／30…` 秒退避重連。啟動離線由 ConfigEntryNotReady 交給 HA 自動 retry。

正常 polling 下，單 Slave timeout／CRC 不立即讓整個 Hub offline；整輪全部失敗重建 TCP。Write 失敗不立刻閃回舊 optimistic 值，交由正常 poll 校正；Polling=0 時需恢復 polling 才能自動校正。Bus 失敗會清除 pending 控制，離線時拒絕控制，重連不播放舊指令。

warning 只記狀態變化與 queue full；逐次通訊細節只記 DEBUG。不為暫時網路錯誤建立 Repair。

## Diagnostics

整合選單 → **下載診斷資料**。Host／IP 遮罩，統計只存在 runtime、restart／reload 歸零。包含：

```yaml
coordinator:
  poll_interval: 2.0
  polling_paused: false
  configured_slaves: 12
  available_slaves: 12
  last_poll_round_ms: 680
  average_poll_round_ms: 710
  queue_depth: 0
  queue_peak: 2
```

Round time 從取得輪詢 lock、開始第一個 configured Slave 到本輪最後一個完成，不含輪間等待或掃描 lock 等待。Average 只保存最近 **20 輪**。沒有自動調整 interval，也不新增 unavailable Slave 降頻。

原有成功／失敗交易、timeout／CRC／reconnect、per-device availability／failure count／groups 保留。不建立診斷 sensors，不包含密碼、其他 HA Device 或 trace history。

## 升級與已知限制

**v1.0.x → v1.1.1**：HACS → Coselig Modbus Hub → Update → 重啟 HA；保留 Entry、devices、名稱與 Area，不需刪除重建。載入新版後改 polling interval 不需再 reload。v1.1.0 的 P210 舊設定缺少 `group_a` 時會自動視為 `single`。

**v1.1.2 → v1.2.0**：保留既有 Parent Device、entity_id、unique_id、名稱與 Area。重新載入後 HA 會為每個有效輸出建立原生 Child Device，不需刪除整合或重新加入設備。

**v1.2.0 → v1.2.1**：更新 Child Device 預設名稱為 `P404 #51 · CH1`、`P210 #62 · 雙色溫 A` 等完整名稱；既有 entity_id、unique_id、名稱與 Area 保留，不需刪除或重新加入設備。

**v0.2.x → v1.x** 仍是 Clean Break：先備份 HA，移除舊 Entry，更新、重啟並重新新增。舊 MQTT retained Discovery／實體／自動化需自行檢查清理；新版不連 broker。正式切換前停用寫入相同設備的舊流程。

- 使用帶 CRC 的 RTU over TCP，不是 MBAP Modbus TCP。
- 無硬體序號與可靠自動辨識型號；不同 DNS 別名指向同一 Gateway 無法辨認。
- RTU 無 transaction ID；關閉 TCP 可隔離該連線，仍不能保證透明 Gateway 不把舊 RS485 回覆送至新連線。
- 0x082A、0x05 high byte、最低亮度與 CCT 映射沿用舊程式；不宣稱全部協議細節已實機確認。
- 長時間失聯 Slave 仍逐輪探測，增加 bus latency；0.5 秒 transaction timeout 保持不變。
- Monitor 僅可看到記錄到 ring buffer 的完整封包；因非法 header 中止 receive 的損毀封包可能只有 TX／ERROR，沒有完整 RX。

## 開發與驗收

```sh
uv sync --locked --python 3.14
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
uv run pytest --timeout=30 --cov-fail-under=99
node --test tests/frontend/monitor.test.cjs
```

Python 測試直接使用正式 custom component、HA APIs 與 loopback Gateway；前端以 Node 內建 runner 獨立測試，不計入 Python coverage。Config Flow 行／分支要求100%，整體 coverage≥99%。

詳見 [v1.2.1 發布說明](docs/v1.2.1-release-notes.md)、[v1.2.0 發布說明](docs/v1.2.0-release-notes.md)、[v1.1.2 發布說明](docs/v1.1.2-release-notes.md)、[v1.1.1 發布說明](docs/v1.1.1-release-notes.md)、[v1.1 實作與發布報告](docs/v1.1-implementation-report.md)、[開發架構](docs/development.md)、[實機驗收清單](docs/v1.0-acceptance.md)。**尚未完成 24～48 小時實機 soak test，測試通過不代表現場全部功能已驗收。**

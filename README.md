# Coselig Modbus Hub v1.0

將 Coselig **P404／P210** 透過 TCP／RS485 閘道器加入 Home Assistant，直接建立原生 LightEntity。設定與設備管理全部使用 Home Assistant 原生介面；不需要 MQTT、Node-RED、YAML 或自製面板。

```text
P404 / P210 → RS485 → TCP Gateway → RTU Transport → Coordinator → Native LightEntity
```

最低 Home Assistant Core：**2026.10.0**。目前自動測試基線：**2026.10.1／Python 3.14.7**。套件版本 `1.0.0`，Config Flow schema `2.1`，domain `coselig_modbus_hub`。

## 安裝與首次設定

1. 在 HACS 的 Custom repositories 加入 `https://github.com/JK0526/coselig-modbus-hub`，類型選 **Integration**，下載此整合並重新啟動 HA。此說明不代表已收錄於 HACS 預設清單。
2. 設定 → 裝置與服務 → 新增整合 → **Coselig Modbus Hub**。
3. 輸入 Gateway IP／主機名稱與連接埠（預設 `502`）。先測試 TCP，失敗時留在原頁，可修改後重試。
4. 自動掃描 Slave `1～32`；原生進度頁顯示進度與已找到設備數。掃不到時可重試、改範圍或取消，不建立半完成 Entry。
5. 逐一指定 **P404／P210**（預設 P404）。掃描只驗證合法 Modbus 回應，**不猜型號**。
6. 為每顆 P404 選擇兩組輸出模式；預設都是 **2 × 單路調光**。P210 跳過此步。
7. 完成後自動建立設備與燈光實體，可使用 HA 的名稱、區域、儀表板與自動化功能。

## 支援的設備與輸出

| 型號／模式 | HA 燈光實體 |
| --- | --- |
| P404 A single／B single（預設） | CH1、CH2、CH3、CH4 |
| P404 A CCT／B single | 雙色溫 A、CH3、CH4 |
| P404 A single／B CCT | CH1、CH2、雙色溫 B |
| P404 A CCT／B CCT | 雙色溫 A、雙色溫 B |
| P210（固定） | CH1、CH2 |

Group A 使用 CH1＋CH2，Group B 使用 CH3＋CH4。CCT 沿用舊協議：第一個 register 為亮度，第二個為色溫；請確認設備接線與模式符合實機。**U4 不在 v1.0 支援範圍內。**

HA 色溫使用 Kelvin API，沿用舊協議的 175～455 mired 映射，約 **2198～5714 K**。非零亮度最低 **2%**，零表示關閉。

## 輪詢與控制

- 每個 Gateway 一個 Config Entry、每個 Slave 一個 HA Device；不建立額外 Gateway Device。
- 操作後立即更新 HA optimistic 狀態，再送 Modbus write；UI 不等待 RS485 回覆。寫入仍完整驗證 `0x06` echo，不額外 read-back。
- 下一次正常輪詢校正為真實狀態；在操作前已開始的舊輪詢不會把新 optimistic 值改回去。
- 永遠只有一筆 request 在途。控制優先於待執行的輪詢；已送出的 request 不取消。
- 待執行同輸出的同類控制採 latest-wins。控制佇列上限 **32**；可合併時更新 pending，其他新控制明確拒絕並記錄 warning。
- 預設輪詢 **2 秒**，最低 **0.5 秒**。每輪按 Slave 逐筆 await；整輪超過目標週期時立即開始下一輪，不累積 backlog。

## 管理設備、重新掃描與模式切換

設定 → 裝置與服務 → Coselig Modbus Hub → 選項：

- **輪詢週期**：只調整目標秒數。
- **管理設備／輸出模式**：保留或移除設備、修正指定型號及 P404 Group 模式。
- **重新掃描設備**：可設定範圍 `1～247`，成功完成管理流程後保存最後使用範圍。

重新掃描會先暫停 Coordinator，Scanner 獨佔 bus，結束或取消後恢復輪詢。掃描期間控制會明確拒絕，不累積操作。

結果標示 Existing／Missing／New；**既有設備，包括 Missing，預設全部保留**。新設備必須勾選才加入，取消勾選才移除。全部既有設備失聯時也不會自動刪除。

Group single ↔ CCT 切換會移除該 Group 已不適用的 Entity Registry 項目並建立新實體；另一 Group 的 unique_id、entity_id 與使用者名稱保留。被移除輸出的自動化參照需由使用者調整；切回時不保證還原曾刪除實體的自訂資訊。

## 更換 Gateway 位址（Reconfigure）

在整合選單選擇 **重新設定**，修改 Host／Port。新 TCP 連線成功後才更新與 reload，失敗保留原設定。

設備 identifier 使用 `{entry_id}_slave_{slave}`，燈光 unique_id 使用 `{entry_id}_slave_{slave}_chN` 或 `_group_a`／`_group_b`，不含 IP。改 IP 或正常 reload 不會覆寫使用者在 HA 修改的實體名稱，也不會重建永久身份。

## 故障恢復

- 已成功讀取的 Slave 前兩次交易失敗保持 available，連續第三次失敗才 unavailable；成功後清除計數並恢復。尚未成功讀取的設備保持 unavailable。
- TCP refused／reset／EOF 等明確錯誤視為 Gateway 離線，所有實體 unavailable；自動重連退避為 `1、2、4、8、15、30、30…` 秒。重連後立即完整輪詢，再逐台恢復實際狀態。
- 單一 Slave timeout／CRC／非法回應只計該 Slave 失敗，不立即判定 Gateway 離線。整輪全部失敗時重建 TCP。
- 首次載入 Gateway 離線使用 `ConfigEntryNotReady`，由 HA 自動 retry。
- 寫入失敗不立即回退 optimistic 值，交由正常輪詢校正。**不自動重試寫入；bus 失敗後清除尚未執行的控制，離線時拒絕控制，重連不播放舊指令。**
- warning 記錄 Gateway lost／Slave unavailable 等狀態變化與 queue full；逐筆輪詢與失敗細節只記 DEBUG。不為暫時性故障建立 Repair。

## Diagnostics

整合選單 → **下載診斷資料**。包含套件版本、TCP socket 是否連線、輪詢週期、即時佇列深度／峰值、成功與失敗交易、逾時／CRC／重連次數，以及各設備模式、可用性與連續失敗計數。

Host／IP 一律遮罩；不含 packet history、其他 HA 設備或密碼。統計只保存在 runtime，HA 重啟或整合 reload 後歸零。不額外建立診斷 sensors。

## v0.2.x 切換與已知限制

v1.0 是 **Clean Break**，不轉換舊 channel／MQTT 設定。先備份 HA 及舊整合設定，移除舊整合 Entry，安裝新版後重新設定。舊 MQTT 實體、retained Discovery 與自動化需自行檢查及清理；新版不連 broker，也不自動操作這些資料。正式切換前停用控制相同設備的舊輪詢／寫入流程。

- 使用 **RTU over TCP**（帶 CRC），不是標準 Modbus TCP／MBAP。
- 無法自動辨識 P404／P210，也沒有設備序號可作硬體身份。相同正規化 Host＋Port 可避免重複設定；不同 DNS 別名是否指向同一 Gateway 無法自動辨認。
- RTU 沒有 transaction ID。逾時／非法回覆後關閉 TCP，可隔離該連線資料，仍無法保證透明閘道器不把舊 RS485 回覆送至新 TCP。
- `0x082A`、寫入高 byte `0x05`、最低亮度與 CCT 映射沿用舊程式與測試，**尚未以實機確認**。不宣稱 `0x05` 代表調光速度。
- 長時間失聯 Slave 仍逐輪嘗試讀取，會增加 bus round latency；不宣稱多設備時固定 2 秒內更新。
- TCP 固定單筆 timeout 0.5 秒，實機閘道器延遲特性仍需驗收。

## 開發驗證與實機驗收狀態

使用 Python 3.14.2 以上，安裝 uv 後：

```sh
uv sync --locked --python 3.14
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
uv run pytest --timeout=30
```

測試直接載入正式 custom component 與真正 HA 2026.10.1 API，使用合成設備與 loopback TCP Gateway，不接現場設備。CI 要求整體分支覆蓋率 ≥90%，Config Flow 行與分支 100%。最終本機結果：**240 項測試通過**；整體 coverage（行＋分支）**99.32%**，純分支 **97.60%**，Config Flow 行與分支 **100%**。詳見 [完整實作報告](docs/v1.0-implementation-report.md)、[開發架構與驗證依據](docs/development.md) 及 [v1.0 實機驗收 Checklist](docs/v1.0-acceptance.md)。

**目前實機驗收與 24～48 小時 soak test 尚未執行；自動測試通過不等於現場已驗證。**

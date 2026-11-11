# Coselig Modbus Hub

將 Coselig P404、P210、R410 與 S402，透過 RS485 → TCP Gateway 原生整合進 Home Assistant。整合建立原生 Light／Switch／Event 與 Device，使用 Home Assistant Config Flow 設定，無需 MQTT、Node-RED 或 YAML，並內建管理員限定的 Modbus Monitor。

目前穩定版本：**v1.6.4**。本版修復 Gateway 自動重連與 P404／P210 輪詢中斷後的恢復。R410 Cover 硬體驗收與 device-oriented Options Flow 實機驗證均已通過。Scheduler 容量、排序、latest-wins、Action FIFO、失敗清理、Poll、Scanner 與 S402 routing 已通過軟體／自動化驗證；100+ circuits 的大型實體部署壓力測試未直接進行。Config Entry schema 維持 **2.1**。

## 功能特色

- 支援 P404／P210 PWM Dimmer 與 R410 Relay。
- 支援 S402 四路 DI 單擊場景事件，使用 Home Assistant EventEntity。
- 支援 R410 瞬時 Relay 控制 Cover，提供兩路與三路模式、Relay 自由映射及每組 Cover 獨立脈衝時間。
- 依使用者設定的 Slave ID 範圍搜尋 Modbus 設備。
- 建立原生 Home Assistant Device、Light 與 Switch。
- 可修改既有設備型號與輸出模式，也可個別移除設備。
- 背景 Polling 週期可調整或停用。
- 每個 Gateway 最多排入 256 個不同的 pending Stateful CommandKey；同 key 的未送出命令採 latest-wins。
- Light／Switch 先確認命令已入列再更新 HA 狀態。雙色溫的亮度與色溫一起檢查佇列容量。
- R410 Cover Action 有獨立 32 筆 FIFO 容量；Stateful 與 Action 共用全域控制順序，實際 Modbus 通訊仍維持單一 request in flight。
- 管理員可在 Monitor 暫停／恢復背景 Poll，且不會修改儲存的 Poll 週期；Control 與 S402 EVENT 維持運作。
- 內建 Monitor 可查看通訊紀錄，管理員可控制執行期背景 Poll。
- 可建立多個 Gateway Config Entry，分別管理不同的 RS485 Bus。

## 支援設備

| 型號 | Home Assistant 實體 | 輸出模式 |
| --- | --- | --- |
| Coselig P404 | Light | CH1、CH2、CH3、CH4；或雙色溫 A、雙色溫 B |
| Coselig P210 | Light | CH1、CH2；或雙色溫 A |
| Coselig R410 | Switch／Cover | Relay 1–4；可將未使用 Relay 配置為瞬時 Cover 控制 |
| Coselig S402 | Event | DI1–DI4 單擊事件（`PRESS_END`） |

P404 的 Group A 對應 CH1／CH2，Group B 對應 CH3／CH4；每組可設成兩路單獨調光或一組雙色溫。P210 只有 Group A。S402 的 DI 是無狀態單擊事件，不輪詢，也不代表 S402 在線健康狀態。

## 系統需求

- Home Assistant Core **2026.10.0** 或更新版本。
- 可由 Home Assistant 主機連線的 TCP → RS485 Gateway。
- 支援 Coselig 設備的 RS485 Bus 與正確的串列設定。

本整合使用 **Modbus RTU frame over TCP**。它不是 MBAP Modbus TCP；請確認 Gateway 使用 RTU over TCP 模式。

## 安裝

1. 在 HACS 開啟 **自訂儲存庫**，加入 [JK0526/coselig-modbus-hub](https://github.com/JK0526/coselig-modbus-hub)，類型選 **Integration**。
2. 安裝 Coselig Modbus Hub，並依 Home Assistant 提示重新啟動。
3. 本專案以 GitHub Release 提供更新；目前不是 HACS 預設儲存庫中的整合。穩定版會透過 HACS 一般更新檢查提供；選擇預發布版本時才需啟用預發布版本更新。

## 第一次設定

在 **設定 → 裝置與服務 → 新增整合** 選擇 **Coselig Modbus Hub**，依序完成：

1. 輸入 Gateway Host 與 Port（預設 502），測試連線。
2. 選擇 **搜尋 Modbus 設備** 或 **新增 S402 場景輸入**。只有一般 Modbus 搜尋會進入搜尋範圍頁；進入該頁不會自動掃描。
3. 搜尋時設定 Slave ID 範圍（第一次預設 1–247，也可改為 51–80），再按 **開始搜尋**。選擇找到的 Slave、指定型號與輸出模式，檢視摘要後確認新增。
4. 新增 S402 時輸入 S402 本機 ID（0–255），檢視四個 DI 單擊事件摘要後確認。S402-only Gateway 不需要先掃描 Modbus Slave。

Scanner 先讀取 FC03 `0x082A` 起始的 4 個暫存器，成功時推薦 P404。失敗時改讀 2 個，若成功則依前一次的回應推薦 P210，並在型號頁顯示依據與可信度。只有高可信度推薦會預選，低可信度需要手動選擇。請以設備標示為準。R410 可由搜尋結果直接進入確認。新增流程只處理本次找到的新設備；Existing 與 Missing 設備會保留，不會在此流程被刪除。

S402 ID 與 Modbus Slave 位址屬於不同位址空間。啟用 S402 4x 模式後，同一條 Bus 的普通 Modbus Slave #1 為保留地址；S402 事件不需要 ACK，也不會新增 Modbus request。

## 設備管理

前往 **設定 → 裝置與服務 → Coselig Modbus Hub → 選項 → 設備管理**：

- **新增裝置**可搜尋 Modbus 設備或新增 S402 場景輸入。
- **管理已安裝裝置**使用同一個 P404／P210／R410／S402 選擇器；輸出模式與移除操作位於所選設備下。
- P404／P210 可在設備頁修正型號或調整輸出模式。型號修正會重新載入整合、清除執行期退避並驗證新型號。A 組與父裝置保留原本識別。P404 改成 P210 會移除 B 組，請先檢查引用 B 組的自動化。
- R410 的輸出配置與 Cover 管理位於所選 R410 下；S402 管理位於所選 S402 下。
- **通訊設定**提供背景輪詢週期與搜尋範圍。Gateway Host／Port 使用 Home Assistant 原生重新設定流程。
- 返回或取消不會保存部分修改；必須在確認頁完成變更。掃描中若整合重新載入或卸載，待操作完成後可從掃描錯誤頁重試。

使用者可在 Home Assistant 的實體設定中自訂名稱與 Area；修改其他設備或重新載入整合不會覆寫這些設定。

## R410 Momentary Cover

R410 可透過硬體 Momentary 功能，對外部窗簾、捲門或車庫門控制器的**乾接點瞬時輸入**送出 OPEN、STOP、CLOSE 命令。這項功能只模擬按鈕脈衝；外部控制器必須負責馬達安全、方向互鎖、障礙物偵測與行程限位。**不可將 R410 Cover 用作直接切換市電馬達正反轉的控制器。**

每個 Cover 可選兩路模式（OPEN 與 CLOSE 各使用一個 Relay，STOP 自動同時觸發兩者）或三路模式（OPEN、STOP、CLOSE 各使用一個 Relay）。Relay 可任意選擇，不必連續排列；未配置給 Cover 的 R410 Relay 仍作為一般 Switch。每個 Cover 可設定 0.1–25.0 秒脈衝時間，預設 0.5 秒，由 R410 硬體計時。

此 Cover 沒有實體位置回授。成功送出的 HA OPEN／CLOSE 命令會以 assumed state 顯示為 open／closed，代表「最後一次成功發出的 HA 命令」，不代表門體實際位置；不會推測位置百分比、移動狀態、行程時間或最終實體位置。STOP、整合重新載入，以及 Gateway 或 R410 Slave 不可用後恢復連線時，狀態會回到 unknown。R410 Cover 硬體驗收已通過；驗收範圍記錄於 [硬體驗收報告](docs/v1.5.0-r410-cover-acceptance.md)。

## 輪詢設定

預設背景輪詢週期為 **2 秒**，可在 **選項 → 通訊設定 → 輪詢週期**調整。設定為 0 時：

- 停止背景讀取；明確的實體控制仍可使用，Scanner 與 Monitor 也可使用。Continuous RX 仍接收 S402 事件。
- 背景讀取停止期間，實體不會自動反映外部操作造成的狀態變化。
- 恢復非零 Polling 後會重新讀取設備狀態。

週期變更會套用到目前整合，不需重建 Config Entry；是否需要重新啟動 Home Assistant，請依更新版本的 Release Notes 操作。

## Modbus Monitor

管理員登入後，從側邊欄開啟 **Coselig Modbus Monitor**。桌面版的 Raw Trace 與 Sidebar 可各自捲動；窄畫面使用單欄。工具列提供 Gateway selector、Slave filter、type filter、Clear、Pause 與 Auto-scroll。

Sidebar 顯示 Gateway 狀態、Packet Decoder、Device Health 與 Communication Statistics。S402 事件以獨立 EVENT Trace 顯示，不併入前後 Poll transaction；點選事件可查看 S402 ID、DI、單擊類型與是否已設定。點選封包可檢視解碼資訊；Decoder 不在 Sidebar 可視範圍時，才會在使用者點選後捲入視野。統計文案為「自本次啟動起累計」，S402 事件不增加 Modbus request 統計。

Backend TraceBuffer 保留最多 **200 筆**，目前頁面另暫存最多 **1000 筆**供瀏覽。瀏覽器 reload 或切換 Gateway 後，前端暫存歷史會消失。Clear 清除 Trace，但保留通訊統計；「暫停畫面」只凍結畫面更新，Auto-scroll 可控制是否跟隨最新封包。「暫停輪詢」只停止背景 Poll；不取消已送出的 request，也不影響 Control、Scanner、Continuous RX、S402 EVENT 或 reconnect。Monitor 不會發出任意 Modbus register 寫入。

## 多 Gateway

可建立多個 Gateway Config Entry，例如：

| Gateway | RS485 Bus |
| --- | --- |
| 192.168.1.204:502 | Bus A |
| 192.168.1.205:502 | Bus B |

每個 Entry 分別管理自己的連線、Polling、Queue、設備與 Monitor 資料。建議採用 **1 Gateway = 1 RS485 Bus = 1 Coselig Config Entry**。不要讓多個 TCP Gateway／Master 同時控制同一條 RS485 Bus；本整合不提供 multi-master arbitration，也不保證同一 Gateway 的多個 TCP client 可安全共用。

## 已知限制

- P404／P210 的推薦依據是 FC03 可讀範圍及回覆特徵。掃描先用共用識別暫存器確認 Slave 有回覆。型號推薦與故障隔離的細節請參考 [調光器辨識與通訊恢復](docs/dimmer-detection-and-recovery.md)。
- R410 Momentary Cover 沒有實體位置回授；open／closed 只表示最後一次成功發出的 HA 命令。STOP、重新載入或通訊恢復後狀態為 unknown；方向控制、安全與限位由外部乾接點控制器負責。硬體驗收範圍與安全界線請參考 [驗收報告](docs/v1.5.0-r410-cover-acceptance.md)。
- S402 只提供四路 DI 單擊事件；不提供按下／放開、雙擊或長按事件，也無法推斷單顆 S402 的在線狀態。
- S402 4x 模式會保留普通 Modbus Slave #1；同一 Gateway／Bus 不得同時設定普通 Slave #1。
- R410 DI 目前不提供實體。
- Monitor 不提供 arbitrary Modbus register write；輪詢暫停只改執行期排程狀態。
- 不提供 RS485 multi-master arbitration。

## 疑難排解

### 找不到 Gateway

確認 Gateway 已供電並連上網路、Host／Port 正確，且 Home Assistant 主機能連到該 TCP 端點。也請確認目前使用的是 Gateway 的 RTU over TCP 模式。

### 找不到 Slave

確認實際 Slave address 落在搜尋範圍內，並核對 RS485 A／B polarity、baud rate、串列設定與線路終端。可先使用較小的實際範圍，或依現場地址調整搜尋範圍。

### P210 狀態異常

在管理已安裝裝置中選取該 Slave，使用「修正型號」改為 P210。誤設為 P404 時，背景輪詢會繼續使用原先的 4 暫存器設定。連續三次失敗後，該 Slave 會進入獨立退避，其餘 Slave 仍照排程輪詢。

### Monitor 出現 timeout 或 CRC error

這些狀態表示該次通訊交易未正常完成或檢查失敗。先確認 RS485 wiring、Gateway 連線與設備狀態；Monitor 本身只呈現既有通訊結果。

## 更新

測試版 **v1.6.5-beta.5** 將被動 RX 整合到 Monitor 原始封包時間軸，可用 Slave 或「未配對 RX」篩選，並點選查看 HEX、功能碼與 CRC。請參考 [beta.5 發布說明](docs/v1.6.5-beta.5-release-notes.md)。

測試版 **v1.6.5-beta.3** 新增掃描結果的「全部新增」與待辨識設備的批次型號確認。請參考 [beta.3 發布說明](docs/v1.6.5-beta.3-release-notes.md)。

HACS 會依 GitHub Release 提供可更新版本。**v1.6.4** 是目前穩定版，可透過一般 HACS 更新檢查取得，無需啟用預發布版本更新。更新前請閱讀 [v1.6.4 Release Notes](docs/v1.6.4-release-notes.md)；Config Entry schema 維持 2.1，不需只為更新版本而刪除並重建整合。

## 開發與驗收文件

- [開發與維護指南](docs/development.md)
- [v1.6.4 Release Notes](docs/v1.6.4-release-notes.md)
- [v1.6.5-beta.2 Release Notes](docs/v1.6.5-beta.2-release-notes.md)
- [v1.6.5-beta.1 Release Notes](docs/v1.6.5-beta.1-release-notes.md)
- [v1.6.3 Release Notes](docs/v1.6.3-release-notes.md)
- [v1.6.0 Release Notes](docs/v1.6.0-release-notes.md)
- [v1.6.0 排程驗收紀錄](docs/v1.6.0-scheduler-acceptance.md)
- [v1.6.0-beta.1 Release Notes](docs/v1.6.0-beta.1-release-notes.md)
- [v1.3.3 Release Notes](docs/v1.3.3-release-notes.md)
- [v1.4.0 Release Notes](docs/v1.4.0-release-notes.md)
- [S402 實機驗收清單](docs/v1.4.0-s402-acceptance.md)
- [v1.5.0 Release Notes](docs/v1.5.0-release-notes.md)
- [R410 Cover 實機驗收報告](docs/v1.5.0-r410-cover-acceptance.md)
- [架構文件](docs/v1.3.0-architecture.md)
- [實機驗收清單](docs/v1.0-acceptance.md)
- [所有 GitHub Releases](https://github.com/JK0526/coselig-modbus-hub/releases)

### 批次新增掃描結果

在掃描結果勾選「全部新增（使用預設輸出模式）」。已安裝的設備（含本次未掃到的設備）會保留；啟用 S402 時略過保留的 Slave #1。高可信度調光器與已辨識的 R410 可直接套用預設模式。其餘設備請依實體標籤選擇同型號的設備，分批套用。確認摘要後才寫入；若需要個別設定 CCT，請使用原有的手動選擇流程。

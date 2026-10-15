# Coselig Modbus Hub

將 Coselig P404、P210、R410 與 S402，透過 RS485 → TCP Gateway 原生整合進 Home Assistant。整合建立原生 Light／Switch／Event 與 Device，使用 Home Assistant Config Flow 設定，無需 MQTT、Node-RED 或 YAML，並內建管理員限定的 Modbus Monitor。

目前測試版本：**v1.4.0-beta.2**（S402 實機共存驗收中）。Config Entry schema 維持 **2.1**。

## 功能特色

- 支援 P404／P210 PWM Dimmer 與 R410 Relay。
- 支援 S402 四路 DI 單擊場景事件，使用 Home Assistant EventEntity。
- 依使用者設定的 Slave ID 範圍搜尋 Modbus 設備。
- 建立原生 Home Assistant Device、Light 與 Switch。
- 可修改既有設備型號與輸出模式，也可個別移除設備。
- 背景 Polling 週期可調整或停用。
- 管理員可在 Monitor 暫停／恢復背景 Poll，且不會修改儲存的 Poll 週期；Control 與 S402 EVENT 維持運作。
- 內建 Monitor 可查看通訊紀錄，管理員可控制執行期背景 Poll。
- 可建立多個 Gateway Config Entry，分別管理不同的 RS485 Bus。

## 支援設備

| 型號 | Home Assistant 實體 | 輸出模式 |
| --- | --- | --- |
| Coselig P404 | Light | CH1、CH2、CH3、CH4；或雙色溫 A、雙色溫 B |
| Coselig P210 | Light | CH1、CH2；或雙色溫 A |
| Coselig R410 | Switch | Relay 1–4 |
| Coselig S402 | Event | DI1–DI4 單擊事件（`PRESS_END`） |

P404 的 Group A 對應 CH1／CH2，Group B 對應 CH3／CH4；每組可設成兩路單獨調光或一組雙色溫。P210 只有 Group A。R410 的 DI 目前不提供實體。S402 的 DI 是無狀態單擊事件，不輪詢，也不代表 S402 在線健康狀態。

## 系統需求

- Home Assistant Core **2026.10.0** 或更新版本。
- 可由 Home Assistant 主機連線的 TCP → RS485 Gateway。
- 支援 Coselig 設備的 RS485 Bus 與正確的串列設定。

本整合使用 **Modbus RTU frame over TCP**。它不是 MBAP Modbus TCP；請確認 Gateway 使用 RTU over TCP 模式。

## 安裝

1. 在 HACS 開啟 **自訂儲存庫**，加入 [JK0526/coselig-modbus-hub](https://github.com/JK0526/coselig-modbus-hub)，類型選 **Integration**。
2. 安裝 Coselig Modbus Hub，並依 Home Assistant 提示重新啟動。
3. 本專案以 GitHub Release 提供更新；目前不是 HACS 預設儲存庫中的整合。Beta 預發布版本需在 HACS 該儲存庫啟用預發布版本更新，才會納入更新檢查。

## 第一次設定

在 **設定 → 裝置與服務 → 新增整合** 選擇 **Coselig Modbus Hub**，依序完成：

1. 輸入 Gateway Host 與 Port（預設 502），測試連線。
2. 選擇 **搜尋 Modbus 設備** 或 **新增 S402 場景輸入**。只有一般 Modbus 搜尋會進入搜尋範圍頁；進入該頁不會自動掃描。
3. 搜尋時設定 Slave ID 範圍（第一次預設 1–247，也可改為 51–80），再按 **開始搜尋**。選擇找到的 Slave、指定型號與輸出模式，檢視摘要後確認新增。
4. 新增 S402 時輸入 S402 本機 ID（0–255），檢視四個 DI 單擊事件摘要後確認。S402-only Gateway 不需要先掃描 Modbus Slave。

P404 與 P210 無法可靠地由協議自動區分，型號頁預設 P404，請依實際設備選擇。R410 可由搜尋結果直接進入確認。新增流程只處理本次找到的新設備；Existing 與 Missing 設備會保留，不會在此流程被刪除。

S402 ID 與 Modbus Slave 位址屬於不同位址空間。啟用 S402 4x 模式後，同一條 Bus 的普通 Modbus Slave #1 為保留地址；S402 事件不需要 ACK，也不會新增 Modbus request。

## 設備管理

前往 **設定 → 裝置與服務 → Coselig Modbus Hub → 選項 → 設備管理**，可選擇：

- **新增／搜尋設備**：搜尋並加入新 Slave。
- **修改既有設備**：變更一台設備的型號或輸出模式，確認摘要後套用。
- **移除設備**：選擇要移除的設備並在確認頁完成操作。
- **S402 場景輸入**：新增或移除 S402。每次變更都要在摘要頁確認；此處不會掃描或輪詢 S402。

使用者可在 Home Assistant 的實體設定中自訂名稱與 Area；修改其他設備或重新載入整合不會覆寫這些設定。

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

- P404／P210 無法由協議可靠自動區分，請在設定時指定正確型號。
- R410 DI inputs currently not exposed.
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

確認 Config Entry 中選的是 P210。若把 P210 設成 P404，輪詢設定與設備不符，可能無法正常讀取。

### Monitor 出現 timeout 或 CRC error

這些狀態表示該次通訊交易未正常完成或檢查失敗。先確認 RS485 wiring、Gateway 連線與設備狀態；Monitor 本身只呈現既有通訊結果。

## 更新

HACS 會依 GitHub Release 提供可更新版本；若要安裝本次 Beta，請在 HACS 該 Repository 啟用 **預發布版本（pre-release）**更新。更新後請閱讀該版本的 Release Notes，依說明決定是否需要 reload 或重新啟動 Home Assistant。v1.4.0-beta.2 的 Config Entry schema 維持 2.1，不需只為更新版本而刪除並重建整合。此 Beta 尚待 S402 與 Poll／Control 共存的實機驗收。

## 開發與驗收文件

- [開發與維護指南](docs/development.md)
- [v1.3.3 Release Notes](docs/v1.3.3-release-notes.md)
- [v1.4.0-beta.2 Release Notes](docs/v1.4.0-beta.2-release-notes.md)
- [S402 實機驗收清單](docs/v1.4.0-s402-acceptance.md)
- [架構文件](docs/v1.3.0-architecture.md)
- [實機驗收清單](docs/v1.0-acceptance.md)
- [所有 GitHub Releases](https://github.com/JK0526/coselig-modbus-hub/releases)

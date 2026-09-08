# 開發決策與前置工作 — 2026-09-08

## 本次範圍

使用者已授權直接實作。先完成可離線驗證的核心，不以候選規格冒充實機事實。
產品目標為 HACS 自訂整合、MQTT bridge、設備管理面板；核心應可獨立測試，避免與 HA UI 耦合。

## 已完成（截至 2026-09-08）

- 可獨立部署的 `custom_components/coselig_modbus_hub` 骨架與 Config Flow／Options Flow。
- RTU over TCP 0x03／0x06、CRC、回覆配對、拆包、控制優先佇列與逾時重連。
- 可自行建立的通道資料驗證，以及 MQTT Discovery／availability topic 產生器。
- HA MQTT adapter：有通道設定時才啟動，發布 retained Discovery，並把控制 topic 路由到 runtime。
- Admin WebSocket API 與側邊欄面板：通道 CRUD、輪詢設定與狀態摘要。
- 14 項本機測試通過；未連線任何現場 gateway，也未發布現場 MQTT。

## 已採用的開發規格

- 依 flow 使用帶 CRC 的 RTU over TCP；單一在途 request。
- P404 讀 4 registers、P210 讀 2、U4 暫依 flow 讀 4 且僅支援 Single 1。
- 起點 0x082A；寫入高 byte 保留 0x05，不對外宣稱是已確認調光速度。
- 色溫依通道設定；每個 Hub 使用獨立的 topic 與 unique ID。
- 第一版預計沿用 HA 的 MQTT 整合；broker 帳密不複製到本專案。
- 3 秒為輪詢目標週期；完成不了時不能堆積同設備輪詢。

## 部署時需要的資料

1. Home Assistant 安裝方式與 Core 版本，用來決定相容基線及測試環境。
2. TCP 閘道器品牌、型號、韌體與串列設定；部署時由使用者自行提供 IP/port。
3. P404／P210 各一台可測設備，以及 U4 是否可測；指定不影響現場的通道與時段。
4. HA MQTT 是否已運作；首次測試使用獨立 namespace，正式部署時沿用整合產生的 topic。

不需要提供 MQTT 明文密碼；整合沿用 Home Assistant 已設定的 MQTT。

## 驗收與切換

- 核心：錯誤 CRC／錯誤設備／錯誤 echo 不得當成功；拆包黏包、逾時、取消、佇列滿均有測試。
- 通訊：實機讀寫與回讀；驗證 TCP 跨連線遲到回覆、U4、0x05、色溫端點。
- 整合：Discovery、斷線可用性、重啟恢復、設備刪除與 retained 清理。
- 管理：新增修改設備、重疊驗證、全域輪詢與單設備開關、繁體中文介面。
- 切換：備份 HA 與原 flow；停用原流程命令消費與輪詢，再啟用新系統；避免雙寫。
- 回復：停止新系統，再恢復原 flow，檢查 Discovery 與實體對應。
- 正式放行前需由部署者提供端點清單並完成連續運行紀錄，尚未執行。

## 官方參考

- Modbus 0x03、0x06 及例外回覆：https://www.modbus.org/file/secure/modbusprotocolspecification.pdf
- HA 整合結構：https://developers.home-assistant.io/docs/creating_integration_file_structure/
- MQTT Light：https://www.home-assistant.io/integrations/light.mqtt/

官方 Modbus 文件只支持標準功能碼行為，不證明 Coselig 特有 register、高 byte 或色溫能力。

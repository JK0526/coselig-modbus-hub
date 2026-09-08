# Coselig Modbus Hub

目前已完成可載入的 Home Assistant custom integration、MQTT bridge 與 Hub 管理面板。
目標：以 Home Assistant 整合與友善管理介面取代 Node-RED，保留 MQTT 燈控。

## HACS 安裝

1. 在 HACS → Integrations 搜尋並下載 `Coselig Modbus Hub`。若你的 HACS 尚未顯示此項目，再使用 Custom repositories，類型選 `Integration`。
2. 重新啟動 Home Assistant。
3. 到設定 → 裝置與服務 → 新增整合，搜尋 `Coselig Modbus Hub`。
4. 輸入你的 TCP gateway 位址與連接埠，建立 Hub。
5. 確認 Home Assistant 的 MQTT 整合已連線，再開啟左側 `Coselig Hub` 面板，逐一新增你的設備通道。

若儲存通道失敗，面板會保留目前輸入並顯示後端錯誤；先依錯誤內容處理 MQTT 連線或通道欄位，再重新儲存。表單只有在儲存成功後才會清空。

本專案不內建任何現場 IP、設備名稱或通道清單；每個安裝環境都必須自行輸入。若從其他系統切換，請先停用原本的輪詢與控制流程，避免同時寫入設備。

## 已實作

- RTU over TCP：0x03 讀取、0x06 寫入、CRC16、例外回覆、完整寫入 echo 驗證。
- TCP 拆包讀取、單連線序列交易、控制優先、有界佇列、未完成輪詢去重。
- 逾時／無效回覆後斷線，下筆命令重新連線；關閉時取消等待命令。
- 可自行建立的通道設定、型號與通道重疊檢查、最低亮度、單一色溫換算。
- 狀態輸出只使用 state topic，絕不發布至 set topic。
- HA Config Flow 可保存 TCP 位址、連接埠、timeout 與輪詢設定。
- HA entry 啟停會管理單一 Hub runtime 與背景輪詢生命週期。
- MQTT 適配器可發布 retained Discovery／availability、訂閱控制，並隔離 state 與 set topic。
- 管理面板可顯示 Hub 狀態、切換輪詢，並新增、編輯、刪除通道。
- 管理面板提供通道新增、修改、刪除與輪詢狀態。

## 本機驗證

目前已在 Python 3.13.3 驗證，僅使用標準函式庫。在專案根目錄執行：

```sh
python3 -m unittest discover -s tests -v
```

測試僅使用合成通道與本機 loopback TCP 模擬器，不連線任何現場 gateway 或 MQTT。

## 後續工作

- 重試／退避策略、完整診斷統計與診斷下載。
- HA 實機載入測試、Discovery 切換與正式遷移文件。

## 已知限制

RTU 沒有 transaction ID。關閉舊 TCP 可隔離該連線資料，但無法保證透明閘道器不把舊串列回覆轉發至新連線；目前固定採逾時斷線、重新連線策略。輪詢已具備去重與優先佇列，重試與退避列為後續工作。
色溫範圍、U4 Channel 1 與寫入高 byte 0x05 依目前專案決策固定採用。
目前 queue 滿會明確拋出 QueueFull，不會默默捨棄已接受的控制。取消等待中的呼叫不保證撤回已送出的硬體寫入。

前置資料及驗收規劃見 [開發決策](docs/development.md)。


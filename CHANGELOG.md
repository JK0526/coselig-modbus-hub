# CHANGELOG

## v1.6.5

- beta.1：P210／P404 辨識、型號修正、單台 Slave 退避、TCP 軟重置及設定流程競態修復。
- beta.2：修復控制被拒後的樂觀狀態、CCT 成對入列、舊命令覆蓋新狀態及掃描中斷清理。
- beta.3：掃描結果全部新增、低可信度及未知型號批次確認，保留手動新增。
- beta.4：管理員可啟用被動 RX 監聽，Poll=0 時也能觀察 Gateway 轉送的完整未配對封包。
- beta.5：統一原始封包時間軸，加入未配對 RX 篩選與解碼。
- beta.6：管理員手動 RTU HEX、可選 CRC16、FC01～FC06／FC0F／FC10 與自動維護隔離。
- beta.7：修正 MANUAL 欄位、HEX 對齊與手動表單的桌面及手機排版。

操作方式及已知風險見 [v1.6.5 發布說明](docs/v1.6.5-release-notes.md)。更早版本見 [GitHub Releases](https://github.com/JK0526/coselig-modbus-hub/releases)。

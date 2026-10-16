# 公开验证证据

- `验收报告.md`：已验证行为、范围和未验收事项。
- `acceptance.json`：后台/开发桌面/打包桌面的数量与运行时摘要。
- `public-verification.json`：逐项结果和协议哈希检查，未包含本机路径与主机名。
- `package-*.png`：实际打包 Electron 窗口截图，均为合成模拟数据。

完整 JUnit、运行报告、逐文件清单、ZIP 校验和进程清理由 `scripts/Station.ps1` 复验后写入本目录，原始运行文件不跟踪到 Git。Release 另外附带 ZIP 的 SHA-256、包内 manifest 和 ZIP 解压桌面验收 JSON。

默认回归是软件、本机网络或模拟证据。0.3.0 另有显式运行的 `esp32-dev-live.json`、`esp32-package-live.json` 和对应实拍截图，保留在本机、从 Git 排除；摘要在验收报告。单 ESP32 的 HTTP/JPEG 显示不证明 UART、光学时序、标定、语音、紧急处置或飞行性能。

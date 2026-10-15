# 公开验证证据

- `验收报告.md`：已验证行为、范围和未验收事项。
- `acceptance.json`：后台/开发桌面/打包桌面的数量与运行时摘要。
- `public-verification.json`：逐项结果和协议哈希检查，未包含本机路径与主机名。
- `package-*.png`：实际打包 Electron 窗口截图，均为合成模拟数据。

完整 JUnit、运行报告、逐文件清单、ZIP 校验和进程清理由 `scripts/Station.ps1` 复验后写入本目录，原始运行文件不跟踪到 Git。Release 另外附带 ZIP 的 SHA-256、包内 manifest 和 ZIP 解压桌面验收 JSON。

所有记录是软件、本机网络或模拟证据；不能作为真实串口、无线、相机、语音、紧急处置和飞行性能证据。

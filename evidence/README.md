# 公开验证证据

- `验收报告.md`：已验证行为、范围和未验收事项。
- `acceptance.json`：后台/开发桌面/打包桌面的数量与运行时摘要。
- `public-verification.json`：逐项结果和协议哈希检查，未包含本机路径与主机名。
- `workbench-public-summary.json`、`workbench-package-*.png`：0.8.0实际Electron工作台、合成JPEG和本机模型响应夹具；实时识别未知，无实机或飞行资格。实际公司文字结果单列在text摘要中。
- `package-*.png`：实际打包 Electron 窗口截图，均为合成模拟数据。
- `fc-usb-public-summary.json`：0.5.0 开发版/目录包及物理拔插的独立实板检查，保存计数/哈希和预期拔线错误；不含本机设备路径。
- `fc-usb-package-online.png`、`fc-usb-package-disconnected.png`：真实飞控 USB 台架窗口和明确断开后的清空状态。
- `text-public-summary.json`：当前版本实际公司无思考文本接口；`text-v0.6.0-public-summary.json`保留先前版本。
- `voice-public-summary.json`、`voice-package-review.png`：测试麦克风/实际音频图及本机CPU参考样音的独立语音审阅结果；不计作公司Linux部署或真人普通话准确率。

完整 JUnit、运行报告、逐文件清单、ZIP 校验和进程清理由 `scripts/Station.ps1` 复验后写入本目录，原始运行文件不跟踪到 Git。Release 另外附带 ZIP 的 SHA-256、包内 manifest 和 ZIP 解压桌面验收 JSON。

默认回归是软件、本机网络或模拟证据。0.3.0 另有显式运行的 `esp32-dev-live.json`、`esp32-package-live.json` 和对应实拍截图，保留在本机、从 Git 排除；摘要在验收报告。单 ESP32 的 HTTP/JPEG 显示不证明 UART、光学时序、标定、语音、紧急处置或飞行性能。

历史 ESP32 摘要保留原版本，不作为 0.5.0 新实测。0.5.0 完整 USB 原始日志与设备信息仍仅存本机；公开文件采用摘录。USB 更新不代表无线 RC 或板内采样时刻有效。

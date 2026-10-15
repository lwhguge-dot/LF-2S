# LF-2S station

- 在 Windows 上始终使用 PowerShell 7（pwsh），不使用 powershell.exe。
- 独立恢复、检查、打包入口为 `scripts/Station.ps1`；Python 使用本仓库 `.venv`。
- 先阅读 README、docs/01_需求与状态契约.md 和 docs/03_验收与后续计划.md。
- 当前 S2 版本仅本机合成任务与只读回放，没有真实设备出口；未知值使用 null，64 位协议编号用十进制字符串。
- 保留普通监控未签认配置和显式任务模拟器的隔离，模拟测试资格不能当生产签认。回放期间禁止连接、场景、任务、图像等实时入口。
- `protocol/manifest.json` 记录受控协议原始字节哈希；不要手改生成协议。
- 保留第三方许可；不要提交 node_modules、.venv、build、release、日志或凭据。
- 软件与模拟测试不能写成真实硬件、飞行、紧急处置或全部系统已验收。
- 产品、硬件、模型和动作语义未知时询问项目负责人，其余已授权开发继续推进。

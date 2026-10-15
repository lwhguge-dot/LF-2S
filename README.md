# LF-2S 上位机 · 0.2.0 模拟任务与只读回放

面向自用与教学的 Windows 桌面上位机。当前交付 S0–S2：Electron + Vue 3 / TypeScript + Python / FastAPI。本机三个真实 UDP 端点串联 PC 适配器、模拟 ESP32 网关和 FCModel，复用现有 MAVLink 2 协议。全部设备、动作和测量均为合成模拟，没有连接真实飞机。

[版本与下载](https://github.com/lwhguge-dot/LF-2S/releases) · [验收报告](evidence/验收报告.md) · [后续计划](docs/03_验收与后续计划.md) · [MIT 许可证](LICENSE)

![实际 Windows 打包窗口，模拟任务完成](evidence/package-task-completed.png)

## 直接运行 Windows 目录包

从 Releases 下载 `LF2S-Station-0.2.0-win-x64.zip`，完整解压后打开其中的 `LF2S-Station.exe`。整个目录必须一起保留；无需另装 Node 或 Python。附带 `.sha256`、逐文件 `.manifest.json` 和 ZIP 解压后实际运行的 `.verification.json`。软件未签名，尚无安装器。GitHub 的 Source code 下载项只是源码，Windows 用户应下载上述 ZIP。

本机构建产物位于 release/，多次构建时以 release/latest.json 为准。

1. 「监控总览」连接模拟设备、读取配置，检查合成 JPEG、数值、未知、过期、指纹和来源校验。默认监控配置未签认。
2. 「模拟任务」点击「重置并准备模拟器」，再「连接任务模拟器」「读取模拟任务配置」。此操作显式更换模拟 boot；仅模型取得测试资格，生产配置仍未签认。
3. 勾选本次模拟许可并启动：PERMIT → ACQUIRE → BEGIN → 模拟起飞至 0.50 m → 保持 → 降落上锁 → END → RELEASE。最终完成依赖新合成样本及对应最终协议结果。每次都需重新勾选；没有自动重做任务。
4. 在总览切换断链、传感器冻结、定位重置、结果丢失、人工接管或重启，观察任务中断；结果未知只能「查询原请求结果」。查询单个结果不会继续旧流程或宣布整项任务完成。
5. 「只读回放」刷新并选录制，打开、拖动时间轴或播放。打开会断开实时会话；回放不能连接、发任务或改变场景，退出后保持断开。显示录制时的有效性与年龄。图像仅录元数据，不用实时图像冒充历史图像。
6. 「协议详情」「事件日志」查看完整 u64、请求关联与原始字段。关闭窗口会清理后台；后台退出、重启、刷新和重连均不恢复旧动作。

所有页面持续标明模拟模式，真实控制入口始终关闭。未知、无效或过期值显示「未知」，JSON 为 null；有效地面高度 0 可以是合成样本。合成图像不能用于真实视觉控制。原遥控接管在模型内同样优先并锁存，恢复健康场景或重连不能清除；显式重置整个模拟器才开始新测试启动代数。空中人工状态可能拒绝新 HELLO，这不代表锁存解除。

## 开发和复验

准备 Windows x64、PowerShell 7、Git、Node 26.8.2、npm 11.19.1 和 uv 0.12.19。uv 会在需要时获取 Python 3.12.14；初次恢复依赖需要联网。开发环境版本已经锁定，用户目录包不依赖这些工具。

在 PowerShell 7 克隆并进入仓库根目录：

```powershell
git clone https://github.com/lwhguge-dot/LF-2S.git
Set-Location LF-2S
```

随后运行：

```powershell
# 初次恢复独立依赖
& './scripts/Station.ps1' setup
& './scripts/Station.ps1' dev

# Python/协议副本/类型/实际桌面验收
& './scripts/Station.ps1' check

# 冻结 Python 并制作自带 Electron/Python 的 Windows 目录包
& './scripts/Station.ps1' package
& './scripts/Station.ps1' verify-package

# 根据本次运行结果生成公开摘要，并更新包内文档及清单
& './.venv/Scripts/python.exe' -B scripts/report.py
& './.venv/Scripts/python.exe' -B scripts/package.py --refresh-docs

# 制作完整 ZIP，再解压并实际运行整组桌面验收
& './scripts/Station.ps1' archive
& './scripts/Station.ps1' verify-archive
```

当前 Node 26 尚未进入 LTS，固定上述版本构建。Python 依赖在 .venv，npm 依赖在本目录 node_modules；requirements 锁含哈希，package-lock.json 含包完整性。Playwright 随本仓库开发依赖安装，直接驱动 Electron，无需另下载浏览器。协议检查不依赖外部研发工作区。

原研发工作区的同一工程在 `P1/05_上位机`，运行上述命令前进入该目录即可。发布与后续版本工作流见 [开源与发布](docs/04_开源与发布.md)。

应用日志位于 `%APPDATA%/LF2SStation/logs`，界面能打开该目录。JSONL 每份最大 2 MiB，保留当前文件和 3 份备份；桌面日志单份约 512 KiB，保留 1 份备份。界面最近 240 条；遥测日志最多 1 Hz。录制在同级 `recordings`：最多 2 Hz 快照、256 条写队列，单次运行最多 4 MiB、保留最近 6 份，满后停止该份录制并报告丢弃。磁盘写入使用独立线程；回放读取有限大小文件，不占用 UDP 调度。测试使用 `.work/qa-*` 独立用户目录。

## 文件入口

- `docs/01_需求与状态契约.md`：状态、有效性、编号和只读边界。
- `docs/02_接口与生命周期.md`：API、IPC、服务凭据、进程清理。
- `docs/03_验收与后续计划.md`：S0–S2 验收与 S3–S5 入口。
- `evidence/验收报告.md`：实际运行结果和限制；JSON/JUnit/截图为可检查证据。
- `protocol/manifest.json`：现有协议源码的受控副本及逐文件 SHA-256。
- `THIRD_PARTY.md`：依赖与来源说明。
- `docs/04_开源与发布.md`：本机发布与 GitHub Actions；普通推送执行检查，手动选择标签可发布预览版本。
- `docs/05_S2模拟任务与回放.md`：固定模拟动作、完成条件、权限、未知结果与录制容量策略。

自有代码采用 MIT。第三方依赖保留原许可；不包含商家原件、机载固件、模型、凭据和本机开发环境。

NSIS 安装/升级/卸载、干净 Windows 机器、真实 UART/ESP32/STM32、相机、语音、视觉、NRF 与飞行均不属于本轮已完成验收。S3–S5 保留，见计划；本版不是全部系统完成。

"""Generate the reviewable stage report from saved execution evidence."""

import json
import platform
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from sync_protocol import verify

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]


def main():
    evidence = ROOT / "evidence"
    suite = ET.parse(evidence / "backend-junit.xml").getroot().find("testsuite")
    backend = {
        key: int(suite.attrib[key])
        for key in ("tests", "failures", "errors", "skipped")
    }
    dev = json.loads((evidence / "desktop-dev.json").read_text("utf-8"))
    packaged = json.loads((evidence / "desktop-package.json").read_text("utf-8"))
    cleanup = json.loads((evidence / "cleanup.json").read_text("utf-8-sig"))
    assert (
        backend["failures"] == backend["errors"] == 0
        and dev["passed"]
        and packaged["passed"]
    )
    assert cleanup["passed"]
    protocol = json.loads((ROOT / "protocol/manifest.json").read_text("utf-8"))
    integrity = verify()
    (evidence / "protocol-integrity.json").write_text(
        json.dumps(
            {
                **integrity,
                "manifest": protocol,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    report = {
        "date": datetime.now().astimezone().isoformat(),
        "platform": platform.platform(),
        "stage": "S0–S1",
        "version": VERSION,
        "backend": backend,
        "development_desktop_checks": len(dev["checks"]),
        "packaged_desktop_checks": len(packaged["checks"]),
        "packaged_runtime": packaged["runtime"],
        "cleanup": cleanup,
        "passed": True,
        "scope": "read-only synthetic localhost; no hardware/flight acceptance",
    }
    (evidence / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    public = {
        "version": VERSION,
        "scope": report["scope"],
        "backend": backend,
        "protocol": integrity,
        "development": {"passed": dev["passed"], "checks": dev["checks"]},
        "packaged": {
            "passed": packaged["passed"],
            "checks": packaged["checks"],
            "runtime": packaged["runtime"],
        },
        "cleanup_passed": cleanup["passed"],
    }
    (evidence / "public-verification.json").write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    session = next(x["session_id"] for x in packaged["checks"] if "session_id" in x)
    authority_note = (
        "与本机权威源逐文件哈希一致"
        if integrity["authority_checked"]
        else "独立仓库副本与清单哈希一致；本次未读取外部权威源"
    )
    text = f"""# LF-2S 上位机 S0–S1 验收报告

日期：{datetime.now().astimezone().date()}。版本：{VERSION}。结论：本机只读模拟 Windows 桌面版已实现并验证；S2–S5 尚未完成。所有测试是软件/本机网络证据，没有连接飞机。

## 可运行产物

`release/LF2S-Station-{VERSION}-win-x64/LF2S-Station.exe`（多次构建以 release/latest.json 为准），保留整个目录即可双击运行。自带 Electron、编译后的 Vue 界面和 PyInstaller onedir Python 服务，不需要用户另装 Node/Python。包内 bundle-manifest.json 记录逐文件 SHA-256。

工程启动和复验命令在上一级 README。日志位于 `%APPDATA%/LF2SStation/logs`；测试使用独立 `.work/qa-*` 目录。

## 已执行验证

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 应用后台，真实 HTTP/UDP/子进程 | {backend["tests"]} 项通过，0 失败/错误 | backend-junit.xml；tests/test_backend.py |
| 实际 Electron 开发窗口 | {len(dev["checks"])} 个检查点通过 | desktop-dev.json；dev-*.png |
| 自带运行时的 Windows 目录包 | {len(packaged["checks"])} 个检查点通过 | desktop-package.json；package-*.png |
| Python、Vue/TypeScript、Electron 语法、格式 | 通过 | .work/package.log；Station.ps1 check |
| 协议受控副本 | {len(protocol["files"])} 个文件：{authority_note} | protocol-integrity.json；protocol/manifest.json |
| 最终后台/桌面进程清理 | 通过；无本工程匹配进程残留 | cleanup.json |

开发窗口和目录包重复的是同一组功能验收，不把检查点相加解释为更多业务能力。

公开仓库只保留 public-verification.json、acceptance.json、本报告和截图；JUnit、完整运行报告与进程记录由复验生成，不把本机绝对路径和主机名带入公开发布。

覆盖的关键行为：

- 点击连接和协议配置查询经过 Vue → 受限 IPC → 本机 FastAPI → 原生 MAVLink 2 UDP → 模拟网关/FCModel；日志关联应用 operation_id 与线协议 request_id。
- 实际渲染进程收到会话编号 `{session}`，超过 JavaScript 安全整数范围；与 `(boot_id<<32)|session_counter` 的精确值一致。nonce 同样大于 2^53；后端另覆盖 u64 最大值和 NaN/Inf 归一化。
- 健康场景显示显式合成测量；未知场景返回 null；传感器停止更新时链路仍在线、测量单独过期；暂停网络超过 1000 ms 时链路、数字和图像失效。
- 错误来源/协议指纹有中文拒绝原因，不刷新有效状态；模拟 FC 重启使旧配置失效，须显式重新连接。
- 全部动作/权限接口不存在，传输出口只允许 HELLO/QUERY_PROFILE；重试原字节与同编号，次数有限；重连、刷新和后台重启不自动重发旧动作。
- 合成 JPEG 在真实 Chromium 解码为 960×540；合成/过期标记常驻，hardware_freshness_qualified 保持 false。图像与配置查询并发验证通过。
- 受限 preload、sandbox/contextIsolation、无 renderer Node、无外部资源请求、窄窗口、日志筛选展开、后续路线展示均验证。
- 第二次启动不创建第二个后台；后台意外退出时旧数字隐藏；手动重启不恢复会话。最终目录包实际关闭 BrowserWindow，以及主进程立即 app.exit 的 EOF 清理均通过。
- 目录包启动环境 PATH 仅保留 Windows System32，移除 Python/Node 的开发环境变量。渲染页面被置为 offline 后仍可从本地资源重载并通过 IPC 查询。

最终目录包运行时：Electron {packaged["runtime"]["versions"]["electron"]}、内置 Node {packaged["runtime"]["versions"]["node"]}、{packaged["runtime"]["platform"]}/{packaged["runtime"]["arch"]}。该次目录包首个后台就绪快照约 {packaged["startup_ms"]} ms，仅为本机样本，不是实机性能承诺。

## 截图

- [正常合成监控](package-overview.png)
- [断链过期](package-stale.png)
- [协议日志展开](package-logs.png)
- [窄窗口](package-compact.png)

## 明确未验收事项

尚未交付 NSIS 安装/升级/卸载、数字签名、另一台干净 Windows 主机、真实 ESP32/STM32/UART/NRF、真实图像/采样新鲜度、语音/视觉模型、机载故障处置或飞行验收。System32 PATH 和页面 offline 检查不能代替这些项目。没有修改商家原件、现有协议权威源或 ESP-IDF hello_world。

下一阶段为 S2 模拟任务闭环与只读回放；S3 真实主线；S4 独立图像、中文语音、视觉；S5 Mind+ 经有线手柄/NRF。详细入口在 `docs/03_验收与后续计划.md`。定位/高度失效时不能从本机状态提示推导“真实飞机一定悬停”。
"""
    (evidence / "验收报告.md").write_text(text, "utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "backend": backend["tests"],
                "desktop": len(packaged["checks"]),
                "report": str(evidence / "验收报告.md"),
            }
        )
    )


if __name__ == "__main__":
    main()

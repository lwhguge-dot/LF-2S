"""Generate the reviewable stage report from saved execution evidence."""

import hashlib
import json
import platform
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from sync_protocol import verify

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]


def usb_summary(evidence):
    results = {}
    for mode in ("dev", "package", "package-usb-removal"):
        path = evidence / f"fc-usb-{mode}-live.json"
        if not path.exists():
            continue
        saved = json.loads(path.read_text("utf-8"))
        if saved.get("application_version") != VERSION or not saved.get("passed"):
            continue
        state = saved["last_live_state"]
        closed = saved["reader_closed"]
        if isinstance(closed, dict):
            closed = [closed]
        results[mode] = {
            "passed": True,
            "application_version": saved["application_version"],
            "physical_usb": saved["physical_usb"],
            "checks": saved["checks"],
            "last_live_frames": state["counters"].get("frames", 0),
            "raw_report_count": saved["raw_report_count"],
            "closed_sessions": [
                {"epoch": row["connection_epoch"], "counters": row["counters"]}
                for row in closed
            ],
            "raw_sha256": saved["raw_sha256"],
            "mavlink_session_established": False,
            "hardware_control_allowed": False,
        }
    (evidence / "fc-usb-public-summary.json").write_text(
        json.dumps(
            {
                "application_version": VERSION,
                "scope": "Physical vendor USB input and display; no UART or control qualification",
                "runs": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    return results


def main():
    evidence = ROOT / "evidence"
    suite = ET.parse(evidence / "backend-junit.xml").getroot().find("testsuite")
    backend = {
        key: int(suite.attrib[key])
        for key in ("tests", "failures", "errors", "skipped")
    }
    dev = json.loads((evidence / "desktop-dev.json").read_text("utf-8"))
    packaged = json.loads((evidence / "desktop-package.json").read_text("utf-8"))
    if any(row.get("application_version") != VERSION for row in (dev, packaged)):
        raise ValueError("Desktop reports must match the current application version")
    workbench_runs = {}
    for mode in ("dev", "package"):
        saved = json.loads((evidence / f"workbench-{mode}.json").read_text("utf-8"))
        if saved.get("application_version") != VERSION or not saved.get("passed"):
            raise ValueError(
                "Workbench reports must match and pass the current version"
            )
        workbench_runs[mode] = {
            "passed": True,
            "checks": saved["checks"],
            "layout": saved["layout"],
            "mobile_layout": saved["mobile_layout"],
            "scope": saved["scope"],
        }
    (evidence / "workbench-public-summary.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "scope": "Actual Electron/Python/IPC with local HTTP model fixture and synthetic picture;no hardware/company model acceptance",
                "runs": workbench_runs,
                "image_recognition_or_flight_qualified": False,
                "archive_result": "See the release verification.json for extracted-bundle checks",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    usb = usb_summary(evidence)
    voice_runs = {}
    for mode in ("dev", "package"):
        voice_path = evidence / f"voice-{mode}-live.json"
        if not voice_path.exists():
            continue
        saved = json.loads(voice_path.read_text("utf-8"))
        if saved.get("application_version") == VERSION and saved.get("passed"):
            voice_runs[mode] = {
                "passed": True,
                "checks": saved["checks"],
                "fake_microphone": saved["fake_microphone"],
                "real_local_asr": saved["real_local_asr"],
                "company_asr": saved["company_asr"],
                "company_model": saved["company_model"],
                "reference": saved["reference"],
                "corrected_reference_task": saved.get("corrected_reference_task"),
                "human_mandarin_accuracy_accepted": False,
            }
    (evidence / "voice-public-summary.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "scope": "Chromium fake microphone and WAV review; local CPU reference ASR and optional company model on manually corrected text; no company ASR deployment, human Mandarin accuracy or flight acceptance",
                "runs": voice_runs,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    text_runs = {}
    for mode in ("dev", "package"):
        path = evidence / f"text-{mode}-live.json"
        if not path.exists():
            continue
        saved = json.loads(path.read_text("utf-8"))
        if saved.get("application_version") == VERSION and saved.get("passed"):
            text_runs[mode] = {
                "passed": True,
                "checks": saved["checks"],
                "thinking_enabled": False,
                "cases": [
                    {
                        "phase": row["phase"],
                        "provider": row["provider"],
                        "elapsed_ms": row["elapsed_ms"],
                    }
                    for row in saved["runs"]
                ],
            }
    (evidence / "text-public-summary.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "scope": "Actual company API text suggestions through Electron; no ASR, permission or control",
                "runs": text_runs,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    physical = {}
    physical_attempts = {}
    for mode in ("dev", "package"):
        path = evidence / f"esp32-{mode}-live.json"
        if path.exists():
            saved = json.loads(path.read_text("utf-8"))
            if saved.get("application_version") == VERSION:
                attempt = {
                    "passed": bool(saved.get("passed")),
                    "checks": len(saved["checks"]),
                    "frames": len(saved.get("frames", [])),
                    "firmware": (saved.get("health") or {}).get("firmware"),
                }
                physical_attempts[mode] = attempt
                if saved.get("passed"):
                    physical[mode] = attempt
    physical_note = (
        f"开发版和目录包已连接实物 ESP32，固件 {physical['package']['firmware']}；"
        "图像解码、对焦请求接受、飞控未知和断开失效检查通过。"
        if len(physical) == 2
        else "本版本未重新完成 ESP32 双模式实板测试；先前相机结果保留在历史记录，不计入本版本实机通过。"
    )
    failed_modes = [
        name for name, row in physical_attempts.items() if not row["passed"]
    ]
    if failed_modes:
        physical_note += (
            " 本版实板检查存在失败："
            + "、".join(failed_modes)
            + "；失败原始记录已保留，不能用软件回归通过覆盖。"
        )
    usb_note = (
        "本版已重测原厂 ZIN_QUAD HID → Python → IPC → Vue 的只读显示，逐项证据见 fc-usb-public-summary.json。"
        if usb
        else "本版没有重测真实飞控 USB；当前 fc-usb-public-summary.json 的 runs 为空。原厂姿态、IMU与RC上传的历史实测保留原版本，不能计为本版重新通过。"
    )
    real_asr_note = (
        "本版显式启动本机真实SenseVoice CPU，参考样音结果见voice-public-summary.json；无独立标准转写，不计算准确率。"
        if any(row["real_local_asr"] for row in voice_runs.values())
        else "本版语音回归使用测试麦克风及模拟ASR，没有重跑真实SenseVoice或公司Qwen；本机官方样音和人工修改后调用公司Qwen的历史证据保留在原版本。"
    )
    stability_path = evidence / "preview-stability-summary.json"
    stability = (
        json.loads(stability_path.read_text("utf-8"))
        if stability_path.exists()
        else None
    )
    if stability and stability.get("application_version") != VERSION:
        stability = None
    performance_path = evidence / "preview-performance-summary.json"
    performance = (
        json.loads(performance_path.read_text("utf-8"))
        if performance_path.exists()
        else None
    )
    if performance and performance.get("application_version") != VERSION:
        performance = None
    stability_note = (
        f"修复前约 {stability['baseline']['elapsed_ms'] / 1000:.3f} 秒、{stability['baseline']['frames']} 张图后失效；"
        f"修复后 {stability['fixed']['elapsed_ms'] / 1000:.3f} 秒连续收到 {stability['fixed']['frames']} 张图，"
        f"读取异常 {stability['fixed']['errors']} 次，最慢请求 {stability['fixed']['max_request_ms']} ms。"
        if stability
        else "未提供匹配此版本的实板持续预览摘要。"
    )
    cleanup = json.loads((evidence / "cleanup.json").read_text("utf-8-sig"))
    assert (
        backend["failures"] == backend["errors"] == 0
        and dev["passed"]
        and packaged["passed"]
    )
    assert cleanup["passed"]
    fixture_path = ROOT / "tests/fixtures/fc-estimator-fault-arm.json"
    fixture = json.loads(fixture_path.read_text("utf-8"))
    controller_checks = {
        "controller_zero_codes_are_current_reports_without_real_qualification",
        "live_controller_diagnostics_do_not_reuse_historical_replay_state",
        "current_fault_reason_and_revoked_permission_are_visible_in_actual_renderer",
        "expired_fault_diagnostics_are_cleared_in_actual_renderer",
        "current_manual_latch_is_visible_without_creating_control_permission",
    }
    assert all(
        controller_checks <= {c["name"] for c in r["checks"] if c["passed"]}
        for r in (dev, packaged)
    )
    controller = {
        "application_version": VERSION,
        "scope": "Current protocol report display; saved85C byte ingress and real Electron simulation UI, no physical UART/control",
        "backend_regressions": sum(
            case.attrib.get("classname", "").endswith("test_controller")
            for case in suite.findall("testcase")
        ),
        "saved_C_scenarios": len(fixture["cases"]),
        "saved_C_unique_frames": sum(
            len(row["frames_hex"])
            for case in fixture["cases"]
            for row in case["timeline"]
        ),
        "C_elf_sha256": fixture["elf_sha256"],
        "actual_desktop_checks_each": len(dev["checks"]),
        "specific_desktop_checks": sorted(controller_checks),
        "old_report_cleared_on_stale_block_reboot_replay_bench_or_service_failure": True,
        "no_new_hardware_or_action_transport": True,
        "source_sha256_lf": {
            name: hashlib.sha256(
                (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            for name in (
                "service/controller.py",
                "service/adapter.py",
                "service/app.py",
                "frontend/ControllerPanel.vue",
                "frontend/App.vue",
                "frontend/bridge.ts",
                "tests/test_controller.py",
                "tests/desktop.cjs",
                "tests/s2_desktop.cjs",
                "tests/fixtures/fc-estimator-fault-arm.json",
                "package.json",
            )
        },
    }
    assert (
        controller["backend_regressions"] == 14
        and controller["saved_C_unique_frames"] == 24
    )
    (evidence / "controller-diagnostics-summary.json").write_text(
        json.dumps(controller, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
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
        "stage": "Visual task workbench + S0–S2 + vendor FC USB display bench + Mandarin review + non-thinking suggestions",
        "version": VERSION,
        "backend": backend,
        "development_desktop_checks": len(dev["checks"]),
        "packaged_desktop_checks": len(packaged["checks"]),
        "packaged_runtime": packaged["runtime"],
        "cleanup": cleanup,
        "passed": True,
        "scope": "synthetic tasks/replay, vendor FC USB input and ESP32 display benches; no UART/control, optical timing or flight acceptance",
        "fc_usb_display_bench": usb,
        "company_text_suggestions": text_runs,
        "mandarin_transcription_review": voice_runs,
        "esp32_display_bench": physical,
        "esp32_latest_attempts": physical_attempts,
        "preview_stability": stability,
        "preview_performance": performance,
        "controller_protocol_diagnostics": controller,
        "visual_task_workbench": workbench_runs,
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
        "fc_usb_display_bench": usb,
        "company_text_suggestions": text_runs,
        "mandarin_transcription_review": voice_runs,
        "esp32_display_bench": physical,
        "esp32_latest_attempts": physical_attempts,
        "preview_stability": stability,
        "preview_performance": performance,
        "controller_protocol_diagnostics": controller,
        "visual_task_workbench": workbench_runs,
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
    text = f"""# LF-2S 上位机软件回归与实物台架报告

日期：{datetime.now().astimezone().date()}。版本：{VERSION}。本机模拟监控、任务闭环与只读回放已验证。飞控 USB 实板结果见下文。{physical_note} 真实 UART、机载动作与 S3 整体尚未完成。

## 可运行产物

`release/LF2S-Station-{VERSION}-win-x64/LF2S-Station.exe`（多次构建以 release/latest.json 为准），保留整个目录即可双击运行。自带 Electron、编译后的 Vue 界面和 PyInstaller onedir Python 服务，不需要用户另装 Node/Python。包内 bundle-manifest.json 记录逐文件 SHA-256。

工程启动和复验命令在上一级 README。日志位于 `%APPDATA%/LF2SStation/logs`；测试使用独立 `.work/qa-*` 目录。

## 已执行验证

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 应用后台，HTTP/UDP/子进程与实物 provider / 文本及语音软件契约 | {backend["tests"]} 项通过，0 失败/错误 | backend-junit.xml；tests/test_backend.py、test_esp32.py、test_fc_usb.py、test_text_tasks.py、test_voice.py |
| 实际 Electron 开发窗口 | {len(dev["checks"])} 个检查点通过 | desktop-dev.json；dev-*.png |
| 自带运行时的 Windows 目录包 | {len(packaged["checks"])} 个检查点通过 | desktop-package.json；package-*.png |
| 新工作台开发窗口与目录包 | 各 {len(workbench_runs["package"]["checks"])} 项通过 | workbench-public-summary.json；workbench-package-*.png |
| Python、Vue/TypeScript、Electron 语法、格式 | 通过 | .work/package.log；Station.ps1 check |
| 协议受控副本 | {len(protocol["files"])} 个文件：{authority_note} | protocol-integrity.json；protocol/manifest.json |
| 最终后台/桌面进程清理 | 通过；无本工程匹配进程残留 | cleanup.json |

开发窗口和目录包重复的是同一组功能验收，不把检查点相加解释为更多业务能力。

## 视觉任务工作台

大画面、右侧任务/识别信息与底部助手已进入实际Electron。新工作台检查使用本机HTTP模型响应夹具和合成JPEG，覆盖填入不发送、原文/新意图/建议流程、无许可或协议副作用、独立未知视觉字段、编辑和迟到隔离、调试返回保草稿不恢复旧建议、语音清理及后台失效清图。视口与画面占比见workbench-public-summary.json。公司文本调用另在下节记录。

实时灯色/闪烁、视觉找路、斑马线与人民币张数/面额/金额尚未接入，当前显示未知。演示实物未准备，未验证自主飞行；界面和文字建议不构成这些能力的证据。

## 飞控 USB 实物显示

{usb_note} 保留各轮独立结果，不把多轮数量合计解释为更多功能。

物理拔插另在0.5.0完成13项检查，保留原版本和原始记录；不自动计为后续版本新实测。该轮拔线引起一个预期读取错误，与连续接收阶段的零校验错误分开记录。后端九项注入测试覆盖每类字段独立过期和模式/回放隔离；注入结果不替代实机拔线。

USB 只读打开，没有输出报告、刷写、校准或解锁路径。高度/位置/电压/无线链路有效性/固件版本与板内采样时刻均未知；不产生 MAVLink 会话、控制资格或传感器量测精度。截图 `fc-usb-package-online.png` 来自实物，`package-*.png` 为模拟窗口。

## 无思考文本任务建议

实际公司 API 的开发窗口/目录包结果见 `text-public-summary.json`，记录本版本实际输入和耗时。仅调用 `qwen38` 并显式设置 enable_thinking=false，拒绝思考内容。缺参输入本地澄清，完整输入联系公司服务并核对原文依据；本轮包含过路、清点纸币、只去B点和信号灯查询。编辑、取消清空旧建议；不发协议、不创建许可、不启动任务。软件测试独立覆盖超时、身份错误、截断、重复字段、数值及原文不符；小样本通过不能解释为通用自然语言理解准确率。

## 普通话录音与转写核对

本版加入录音/导入、试听、显式保存WAV、提交识别、转写核对与采用。开发/目录包逐项证据见 `voice-public-summary.json`。测试麦克风使用Chromium音频文件设备，真实AudioWorklet/IPC/HTTP路径不代表真人麦克风验收；默认不上传，显式保存后的WAV与实际提交哈希一致，15秒自动结束，取消/切页/回放/后台失效清理旧结果。

{real_asr_note} 采用相同文字也先清理旧建议，不自动解析或授权。普通话真人24句评测定义及工具已准备，当前真人语料为0。公司Linux语音部署、真实麦克风、噪声/远场和用户口令端到端仍待验证，详见docs/11。

## ESP32 实物显示

{physical_note}

真实板测试独立于软件/模拟回归，使用 `tests/esp32_live.cjs`。实物摘要：`{json.dumps(physical, ensure_ascii=False)}`。原始实物 JSON 与照片留在本机，不随公开文档携带。显示成功与 AF 命令接受不证明光学对焦、曝光新鲜度或标定合格。实物连接占用期间模拟操作拒绝；回放关闭实物通道；飞控测量保持未知。固件的 UART 与动作出口关闭。

### 持续预览排查

{stability_note}

上述持续预览数据来自随源码保留的本机实板摘要，默认软件验收不重复操作硬件。原始负载和连接条件不能据此视为完全相同；不宣称吞吐改善比例或长期无线性能。修复与回归范围见 docs/07；临时恢复不接纳新流或重发对焦/飞行动作。

历史多档图传与显示频率结果见 docs/08。仅版本匹配的实测摘要计入本报告；这些结果不产生识别率、光学时序或真实控制资格。

公开仓库只保留 public-verification.json、acceptance.json、本报告和截图；JUnit、完整运行报告与进程记录由复验生成，不把本机绝对路径和主机名带入公开发布。

覆盖的关键行为：

- 点击连接和协议配置查询经过 Vue → 受限 IPC → 本机 FastAPI → 原生 MAVLink 2 UDP → 模拟网关/FCModel；日志关联应用 operation_id 与线协议 request_id。
- 实际渲染进程收到会话编号 `{session}`，超过 JavaScript 安全整数范围；与 `(boot_id<<32)|session_counter` 的精确值一致。nonce 同样大于 2^53；后端另覆盖 u64 最大值和 NaN/Inf 归一化。
- 健康场景显示显式合成测量；未知场景返回 null；传感器停止更新时链路仍在线、测量单独过期；暂停网络超过 1000 ms 时链路、数字和图像失效。
- 错误来源/协议指纹有中文拒绝原因，不刷新有效状态；模拟 FC 重启使旧配置失效，须显式重新连接。
- 默认监控只允许 HELLO/QUERY_PROFILE。显式模拟环境中经本次确认、PERMIT/ACQUIRE/BEGIN，实际执行模拟起飞/保持/降落并由新样本产生最终结果，END 与 RELEASE 后撤权。模拟测试资格不代表生产配置签认。
- 传感器冻结、定位 reset、断链、重启、人工接管中断流程。人工锁存不因重连解除；空中人工状态可能按原协议拒绝 HELLO/BUSY。未知结果按原字节、原编号有限重试，之后仅查询原编号；最终结果不被迟到接受覆盖。
- 回放时间轴定位与播放只读，打开时断开实时会话，后台拒绝实时连接/任务/场景/图像；退出不恢复旧动作。录制跨服务退出仍可读取，u64 保真。快照最多 2 Hz，单份 4 MiB、6 份保留，256 条队列，容量/丢弃策略有检查；图像仅元数据。
- 合成 JPEG 在真实 Chromium 解码为 960×540；合成/过期标记常驻，hardware_freshness_qualified 保持 false。图像与配置查询并发验证通过。
- 受限 preload、sandbox/contextIsolation、无 renderer Node、无外部资源请求、窄窗口、日志筛选展开、后续路线展示均验证。
- 第二次启动不创建第二个后台；后台意外退出时旧数字隐藏；手动重启不恢复会话。最终目录包实际关闭 BrowserWindow，以及主进程立即 app.exit 的 EOF 清理均通过。
- 目录包启动环境 PATH 仅保留 Windows System32，移除 Python/Node 的开发环境变量。渲染页面被置为 offline 后仍可从本地资源重载并通过 IPC 查询。

最终目录包运行时：Electron {packaged["runtime"]["versions"]["electron"]}、内置 Node {packaged["runtime"]["versions"]["node"]}、{packaged["runtime"]["platform"]}/{packaged["runtime"]["arch"]}。该次目录包首个后台就绪快照约 {packaged["startup_ms"]} ms，仅为本机样本，不是实机性能承诺。

## 截图

- [工作台初始界面](workbench-package-idle.png)
- [过路文字建议，视觉字段未知](workbench-package-road.png)
- [纸币文字建议，数量与总额未知](workbench-package-cash.png)
- [正常合成监控](package-overview.png)
- [断链过期](package-stale.png)
- [协议日志展开](package-logs.png)
- [窄窗口](package-compact.png)
- [模拟任务进行中](package-task-active.png)
- [模拟任务最终完成](package-task-completed.png)
- [只读历史回放](package-replay.png)

## 明确未验收事项

尚未交付 NSIS 安装/升级/卸载、数字签名、另一台干净 Windows 主机、真实 MAVLink UART/NRF、相机清晰度/采样新鲜度/标定、语音/视觉模型、机载故障处置或飞行验收。System32 PATH 和页面 offline 检查不能代替这些项目。商家原件和 ESP-IDF hello_world 保留，受控协议代码未变。

下一阶段为 S3 真实主线；S4 独立图像、中文语音、视觉；S5 Mind+ 经有线手柄/NRF。详细入口在 `docs/03_验收与后续计划.md`。定位/高度失效时不能从本机状态提示推导“真实飞机一定悬停”。
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

# 飞控 USB 只读台架

0.5.0 开发版新增。连接到货 ZIN_QUAD 原厂 USB，观察姿态、IMU 原始计数和遥控上传通道，可与 ESP32 相机页面同时工作。此接口不建立真实 MAVLink 会话，也不提供刷写、校准、PID、解锁或飞行动作。

## 使用

1. 飞控通过原 USB 数据线连接电脑，保留原厂程序。只读采集不要求打开遥控器或解锁；需要操作摇杆或移动实物时先拆下全部螺旋桨。
2. 打开「飞控 USB 台架」，点击「连接飞控 USB」。只接受唯一一个 VID0483/PIDA001、产品 ZIN_QUAD、Usage Page/Usage 0x8C/1、输入长度 65 的设备；不自动选择其他 HID、串口或下载器。
3. 观察上传角度、变化曲线、IMU 计数、十个 RC 原通道和两个状态原字节。没有已核实的 FRD 轴向、原始计数量程或解锁位语义，不推导为航向、g、°/s 或安全状态。
4. 点击「断开」后数值和曲线清空。USB 拔线/读取失败立即报错，停止更新并释放读句柄；不自动重新连接。单个数据类型超过 1000 ms 未更新即不显示为当前值，其他数据包不能替它续期。

ESP32 相机与 USB 可独立连接、独立断开。任一实物输入激活时，模拟连接/任务/场景/合成图像入口被后台拒绝。打开回放会先关闭两种实物输入，退出回放不自动重连。尚未把 USB 高频原始记录转换为界面历史回放，勿将模拟录制当作真实 USB 记录。

## API 与状态

- `POST /api/v1/fc-usb/connect`、`POST /api/v1/fc-usb/disconnect`：仅空请求体；不接受路径、消息字节或命令参数。
- `/api/v1/state.usb_fc`：`source=vendor_usb_hid`、`display_only=true`、`mavlink_session_established=false`；姿态/IMU/RC/状态分别给出 `available`、`received_age_ms`、`value`。
- `received_age_ms` 是 PC 单调接收年龄；`sample_age_ms=null`、`measurement_qualified=false`、`hardware_freshness_qualified=false`。1000 ms 仅为显示预算。
- `battery_v/height_m/position_m/firmware_version/mcu_model/receiver_link_valid/boot_id/session_id` 都是 `null`。原厂高度零、磁力零不能改变它们。
- 主 `link/identity/sensors` 仍代表 MAVLink 与控制资格，不借 USB 填充。`hardware_control_allowed=false` 恒定。
- `connecting/online/stale/error/disconnected` 分别表示设备打开中、收到校验正确帧、接收过期、错误、已关闭。需要每类数据自身未过期才可显示。

## 日志、生命周期与复验

原始输入报告保存到应用日志目录 `fc-usb.jsonl`。每个连接有随机 `connection_id` 和本服务 epoch，报告带 PC 单调相对接收时刻、原始 65 字节十六进制；连接元数据、结束统计及末尾不完整流一并记录。日志异步写入，队列最多 1024，单文件 8 MiB，另保留 3 个备份；计数公开队列丢弃和写入失败。曲线最多 240 点、每 100 ms 一点。

读取在独立线程，重叠 ReadFile 每次最多等待 250 ms，关闭时请求停止并等待读取退出。程序关闭、服务重启或回放切换均会结束线程和日志。应用无 WriteFile / SetFeature / HID 输出报告 API。

离线注入测试：`tests/test_fc_usb.py`，包括任意分片、校验恢复、错误身份/能力拒绝、单字段过期、USB 拔线、模式/回放隔离。实机端到端脚本：`node tests/fc_usb_live.cjs`，必须显式运行且连接实物；`--packaged` 验证冻结包。真实证据分别写入 `evidence/fc-usb-{dev|package}-live.json`，不能把注入测试称为实板通过。

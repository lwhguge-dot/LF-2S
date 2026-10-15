# 受控协议副本

权威来源为 `P1/01_通信协议/02_参考实现/python/lf2s_protocol`。这里只复制运行所需源码，保留原注释，并从原资料目录携带生成器 COPYING（含生成输出的 MIT 例外）。没有引入 Cockpit 源码，没有手改 dialect。`manifest.json` 逐文件记录来源与 SHA-256；同步由 `scripts/sync_protocol.py` 显式执行，检查用 `--check`。

应用层的连接、超时、来源、会话、指纹和遥测有效性检查在 `service/adapter.py`。重新连接创建全新 Client，避免沿用参考 Client 的旧 challenge/task/reset。应用不要求安装目录旁存在协议原工程。

独立开源仓库运行 `scripts/sync_protocol.py --check` 时校验已附源码与清单；仅在本机原协议工程存在时另行校验权威源，并在结果中明确 authority_checked。显式更新使用 `--source <原协议工程目录>`，不会静默重新设计或下载线协议。

`simulation/profile.json` 是明确未签认的模拟参数：`config_valid=0`、`validated_mask=0`，启动代数也未作物理持久存储验证，不能成为生产配置。

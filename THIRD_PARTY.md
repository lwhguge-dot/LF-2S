# 依赖与来源

本工程的自有代码采用 MIT，见源码根目录 LICENSE；Windows 包为 LF2S-LICENSE.txt。第三方代码保留各自许可证，不由该 MIT 声明替换。没有复制 Cockpit 源码；其布局和架构仅作参考。仓库只发布上位机和受控协议副本，不包含商家原始资料、ESP-IDF hello_world、工作区环境或模型。

| 组件 | 本轮固定版本 | 上游许可/用途 |
| --- | --- | --- |
| Electron | 44.7.2 | MIT；Chromium/Node 及运行时自带第三方许可见目录包 LICENSE、LICENSES.chromium.html |
| Vue | 3.5.44 | MIT，打包到界面 |
| TypeScript / vue-tsc | 5.9.3 / 3.3.13 | Apache-2.0 / MIT，构建工具 |
| Vite / plugin-vue | 8.3.4 / 6.0.10 | MIT，构建工具 |
| Playwright | 1.64.1 | Apache-2.0，开发/验收工具；不随运行包分发 |
| FastAPI / Starlette / Uvicorn | 0.143.1 / 1.8.0 / 0.54.0 | MIT / BSD-3-Clause / BSD-3-Clause |
| Pillow | 12.3.0 | HPND（PIL 许可），合成 JPEG |
| PyInstaller | 6.22.3 | GPL-2.0-or-later，带引导程序分发例外；构建工具 |
| Python | 3.12.14 | PSF，冻结解释器 |
| MAVLink 生成 dialect | 现有协议副本 | 携带上游 COPYING：生成器 (L)GPL v3，生成输出含 MIT 例外；来源/哈希见 protocol/manifest.json |

传递依赖的版本及包完整性在 package-lock.json、requirements.lock、requirements-dev.lock。目录包保留 Electron 原有 LICENSE、LICENSES.chromium.html；附加运行依赖许可由 scripts/licenses.py 生成到 THIRD_PARTY_LICENSES，包含 Python 发行许可、前端/后台各依赖声明，以及 MAVLink 原始 COPYING 和生成源码。生成器没有随应用运行；受控 dialect.py 的字节和注释未修改。

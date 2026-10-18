# 0.8.0 工作台设计对照

**Findings**

本轮没有未处理的 P0/P1/P2 视觉问题。对照的是用户已选定的 HTML 草案和实际 Windows 目录包；相机、任务、结果、助手的排列及明亮配色保留。通过范围为软件界面，不含真实视觉、硬件或飞行。

**比较对象与证据**

- source visual truth：研发工作区 `P1/最终演示目标/上位机设计/index.html`，由其原有 `style.css`、`app.js` 渲染。公开视觉快照：[桌面](evidence/design-source-desktop.png)、[窄屏](evidence/design-source-mobile.png)。
- implementation：最终 0.8.0 自带运行时目录包的 [桌面空状态](evidence/workbench-package-idle.png)、[窄屏空状态](evidence/workbench-package-mobile.png)、[过路建议](evidence/workbench-package-road.png)、[纸币建议](evidence/workbench-package-cash.png)。
- viewport：桌面 1440×1024 CSS px，双方 PNG 均为 1440×1024；窄屏 390×844 CSS px，15px 垂直滚动条使双方全页内容 PNG 宽度为 375px，草案高1326px、实现高1305px。deviceScaleFactor/devicePixelRatio=1，未对图像缩放；窄屏按内容顶边并排，不按不同页面高度拉伸。
- state：没有相机连接、没有任务建议或识别结果。实现的公司模型配置提示与草案的未接入提示不同；空输入解析按钮因此禁用，不是缺少交互。
- full-view comparison：[桌面并排](evidence/design-comparison-package.png)、[窄屏并排](evidence/design-comparison-mobile.png)。均已打开检查。
- focused comparison：[顶栏](evidence/design-detail-package-header.png)、[任务与结果](evidence/design-detail-package-right.png)、[助手](evidence/design-detail-package-assistant.png)。局部采用双方同一坐标裁切、原像素并排，已打开检查字号、行距、换行和边距。

**五项必查表面**

| 表面 | 观察与判定 |
| --- | --- |
| Fonts / typography | 双方均使用 Segoe UI / Microsoft YaHei / system-ui；标题和辅助文字层级、中文行高与换行可读。正式版保留原品牌文字，消除继承的紧字距；正文没有截断或挤压。 |
| Spacing / layout rhythm | 桌面相机宽1022px、工作区宽1376px，占74.3%；助手底边945.6px，在1024px视口内。保留白色卡片、边框、圆角与轻阴影。任务卡最小高度随正式内容调整，结果仍在同屏。窄屏依次堆叠并纵向滚动，没有横向溢出或控件遮挡。 |
| Colors / tokens | 背景 #f5f7f9、文字 #23343f、主色 #087e83、辅助色 #6c7d89 沿用草案。禁用解析按钮降低不透明度，实际输入后恢复；不是文字或状态丢失。 |
| Image quality / assets | 比较状态没有相机图像；没有用插画或虚构实拍替代图像。合成 JPEG 在独立测试中实际解码且标注来源。原草案品牌为排版文字，正式版沿用；未替换任何外部图像、标志或插画。真实相机清晰度与新鲜度不属于本次 QA。 |
| Copy / content | 正式版将“预览任务卡片”改为“解析任务建议”，明确核对后仍无执行权限；草案本地图片入口改为实物相机/本机模拟来源与相机设置。建议步骤和未知识别字段分开，模型已配置不写成飞机在线。产品页面没有暴露代码或凭据。 |

**允许的实现差异**

相机空状态解释实际连接入口；帧失效原因占一条额外状态行。语音按钮放到助手标题行，便于展开已有录音/审阅流程；清空按钮替代草案示例链接。设备与调试进入已有独立页面，返回保留草稿并清旧建议。它们由已实现功能和生命周期约束决定，未改变用户选定的信息层级。

**Comparison history**

初次窄屏比较使用历史草案截图，该截图含格式错误和测试原文，与正式版空状态不相同，因此不作视觉通过依据。已重新在同一 Electron 运行时、同一390×844 CSS视口渲染未修改草案并捕获干净空状态，再生成并检查当前并排图。本轮未提出需要修改界面源码的 P0/P1/P2；最后字距调整已包含在当前目录包并经局部图复核。

**Open Questions**

无阻挡本版软件交付的设计问题。真实灯具/斑马线/纸币、安装视角、实际识别、真人录音及飞行状态仍缺实物证据；它们不由本版布局推导。未进行完整屏幕阅读器、全键盘、色彩对比度或200%缩放认证。

**Implementation Checklist**

- 已核对桌面与窄屏、五项视觉表面、关键局部并排图。
- 工作台22项 Electron/IPC 检查覆盖示例填入、显式解析、两类建议、未知视觉字段、编辑/迟到隔离、调试返回、语音入口和后台失效；页面脚本错误为0。
- 目录包与 ZIP 解压的运行结果分别查看 `evidence/acceptance.json` 与 Release `verification.json`，不把开发版结果代替分发包验证。

**Follow-up Polish**

P3：取得真实演示器材后复核长识别结果、实际画面和常用窗口尺寸；这项不改变当前未知状态。

final result: passed

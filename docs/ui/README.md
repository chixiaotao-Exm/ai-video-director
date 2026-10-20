# T17 Web UI 截图与验证

日期：2026-10-20。Microsoft Edge 无头模式；桌面 1440×900，手机 390×844。

共 40 张 PNG：15 个页面/标签页的双尺寸截图、4 个长页区域的双尺寸截图，以及授权弹窗的双尺寸截图。页面截图来自独立的 `data/ui-capture` 离线真实媒体数据；弹窗来自独立 `data/ui-smoke`。所有内容均为合成演示数据，不包含服务密钥。

已逐页查看并修正：分镜页整页溢出、窄屏多列表格挤成竖排、项目标题对齐、手机预算显示、成片播放器尺寸、历史 SSE 通知重复及未保存表单被刷新。宽表格在面板内部横向滚动。

`report.json`：38/38 页面/区域检查通过，无页面溢出、JS/控制台错误、图片/视频加载错误或外部请求。`smoke-report.json`：10/10 浏览器交互流程通过，未调用付费服务或提交生成任务。

| 页面 / 区域 | 桌面 | 手机 |
| --- | --- | --- |
| 工作台 | [dashboard-desktop.png](dashboard-desktop.png) | [dashboard-mobile.png](dashboard-mobile.png) |
| 项目·概览 | [project-overview-desktop.png](project-overview-desktop.png) | [project-overview-mobile.png](project-overview-mobile.png) |
| 项目·导演对话 | [project-chat-desktop.png](project-chat-desktop.png) | [project-chat-mobile.png](project-chat-mobile.png) |
| 项目·分镜 | [project-storyboard-desktop.png](project-storyboard-desktop.png) | [project-storyboard-mobile.png](project-storyboard-mobile.png) |
| 项目·镜头 | [project-shots-desktop.png](project-shots-desktop.png) | [project-shots-mobile.png](project-shots-mobile.png) |
| 项目·成片 | [project-final-desktop.png](project-final-desktop.png) | [project-final-mobile.png](project-final-mobile.png) |
| 项目·分析 | [project-analysis-desktop.png](project-analysis-desktop.png) | [project-analysis-mobile.png](project-analysis-mobile.png) |
| 素材库 | [assets-desktop.png](assets-desktop.png) | [assets-mobile.png](assets-mobile.png) |
| 素材库·声音区域 | [assets-voices-desktop.png](assets-voices-desktop.png) | [assets-voices-mobile.png](assets-voices-mobile.png) |
| 任务队列 | [jobs-desktop.png](jobs-desktop.png) | [jobs-mobile.png](jobs-mobile.png) |
| 费用 | [costs-desktop.png](costs-desktop.png) | [costs-mobile.png](costs-mobile.png) |
| 费用·明细区域 | [costs-ledger-desktop.png](costs-ledger-desktop.png) | [costs-ledger-mobile.png](costs-ledger-mobile.png) |
| 设置·模型服务 | [settings-desktop.png](settings-desktop.png) | [settings-mobile.png](settings-mobile.png) |
| 设置·价格区域 | [settings-prices-desktop.png](settings-prices-desktop.png) | [settings-prices-mobile.png](settings-prices-mobile.png) |
| 设置·ComfyUI | [settings-comfy-desktop.png](settings-comfy-desktop.png) | [settings-comfy-mobile.png](settings-comfy-mobile.png) |
| 设置·通用 | [settings-general-desktop.png](settings-general-desktop.png) | [settings-general-mobile.png](settings-general-mobile.png) |
| 设置·高级 | [settings-advanced-desktop.png](settings-advanced-desktop.png) | [settings-advanced-mobile.png](settings-advanced-mobile.png) |
| 系统 | [system-desktop.png](system-desktop.png) | [system-mobile.png](system-mobile.png) |
| 系统·日志区域 | [system-logs-desktop.png](system-logs-desktop.png) | [system-logs-mobile.png](system-logs-mobile.png) |
| 素材授权弹窗 | [consent-desktop.png](consent-desktop.png) | [consent-mobile.png](consent-mobile.png) |

## 复现

在仓库虚拟环境中运行 `python scripts/prepare_ui_demo.py --data-dir data/ui-capture`。脚本完全离线生成 FFmpeg 媒体并写入 `ui-manifest.json`；已有 settings.json 保持不变。随后使用 manifest 中的数据目录和数据库启动 `avd serve --no-workers`，再运行：

```text
node scripts/capture_ui.mjs --base-url http://127.0.0.1:8001 --manifest data/ui-capture/ui-manifest.json
```

浏览器交互验证会写入测试项目、素材和设置；仅对独立离线数据运行 `scripts/smoke_ui.mjs`。它拒绝配置了服务密钥或仍有待处理任务的环境，且拦截生成/聊天/连接测试和外部请求。

真实模型、付费 API、GPU 和真实 ComfyUI 工作流未在这些验证中运行。

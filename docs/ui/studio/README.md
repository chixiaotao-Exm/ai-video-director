# Studio 0.2 截图与验收

2026-10-20，真实无头 Microsoft Edge，桌面 **1440×900**、手机 **390×844**。58 张截图来自独立 `data/studio-release` 离线 fixture；画面中的图片、视频、音频全部由本地 FFmpeg 合成。未使用真人身份、API 密钥或生成服务。

`report.json` 保存每张图的准备状态、主题、尺寸、媒体加载、控制台错误、横向溢出和聊天输入区位置。58/58 通过；四主题聊天输入区底部均为桌面 881px / 手机 827px。已逐图检查缩略总览，并放大检查聊天、概览、设置、版本中心和主题首页；桌面聊天侧栏独立滚动，手机辅助面板排在聊天区下方。

`smoke-report.json` 记录独立 `data/studio-preview` fixture 上的浏览器交互验收，包括模板持久化、保留聊天草稿、路线切换、草案恢复与导出、空项目创建/删除、快捷搜索、Git 元数据、手机导航和模拟断网恢复。真实资产授权、队列、费用和 worker 保持不变；脚本仅修改自己的隔离测试数据。

## 全部页面：Midnight Studio

| 页面 | 桌面 | 手机 |
| --- | --- | --- |
| 创作空间 | [desktop](midnight-home-desktop.png) | [mobile](midnight-home-mobile.png) |
| 唱跳演出 | [desktop](midnight-performance-desktop.png) | [mobile](midnight-performance-mobile.png) |
| 外观实验室 | [desktop](midnight-appearance-desktop.png) | [mobile](midnight-appearance-mobile.png) |
| 版本中心 | [desktop](midnight-versions-desktop.png) | [mobile](midnight-versions-mobile.png) |
| 动漫续作 | [desktop](midnight-anime-desktop.png) | [mobile](midnight-anime-mobile.png) |
| 导演对话 | [desktop](midnight-project-chat-desktop.png) | [mobile](midnight-project-chat-mobile.png) |
| 项目流程 | [desktop](midnight-project-overview-desktop.png) | [mobile](midnight-project-overview-mobile.png) |
| 思维导图 | [desktop](midnight-project-mindmap-desktop.png) | [mobile](midnight-project-mindmap-mobile.png) |
| 分镜 | [desktop](midnight-project-storyboard-desktop.png) | [mobile](midnight-project-storyboard-mobile.png) |
| 镜头 | [desktop](midnight-project-shots-desktop.png) | [mobile](midnight-project-shots-mobile.png) |
| 成片 | [desktop](midnight-project-final-desktop.png) | [mobile](midnight-project-final-mobile.png) |
| 分析 | [desktop](midnight-project-analysis-desktop.png) | [mobile](midnight-project-analysis-mobile.png) |
| 素材库 | [desktop](midnight-assets-desktop.png) | [mobile](midnight-assets-mobile.png) |
| 任务队列 | [desktop](midnight-jobs-desktop.png) | [mobile](midnight-jobs-mobile.png) |
| 费用 | [desktop](midnight-costs-desktop.png) | [mobile](midnight-costs-mobile.png) |
| 设置 | [desktop](midnight-settings-desktop.png) | [mobile](midnight-settings-mobile.png) |
| 系统 | [desktop](midnight-system-desktop.png) | [mobile](midnight-system-mobile.png) |

## 其他模板

每套额外覆盖首页、唱跳方案、外观与聊天的桌面/手机版本，共 24 张：

| 模板 | 首页桌面 | 首页手机 | 聊天桌面 | 聊天手机 |
| --- | --- | --- | --- | --- |
| Liquid Glass | [desktop](glass-home-desktop.png) | [mobile](glass-home-mobile.png) | [desktop](glass-project-chat-desktop.png) | [mobile](glass-project-chat-mobile.png) |
| Acid Edit | [desktop](acid-home-desktop.png) | [mobile](acid-home-mobile.png) | [desktop](acid-project-chat-desktop.png) | [mobile](acid-project-chat-mobile.png) |
| Sunday Editorial | [desktop](editorial-home-desktop.png) | [mobile](editorial-home-mobile.png) | [desktop](editorial-project-chat-desktop.png) | [mobile](editorial-project-chat-mobile.png) |

其他文件遵循 `<theme>-performance-<desktop|mobile>.png` 和 `<theme>-appearance-<desktop|mobile>.png` 命名。截图中的 Git 状态是拍摄时的真实本地状态，不代表发布后的远程同步结果。

## 复现

```text
python scripts/prepare_ui_demo.py --data-dir data/studio-release
npm run build --prefix frontend
```

读取新 fixture 的 `ui-manifest.json`，以其中的 `data_dir` 和 `database_url` 启动 `avd serve --no-workers --port 8011`，确认无 provider key 后运行：

```text
node scripts/capture_studio.mjs --base-url http://127.0.0.1:8011 --manifest data/studio-release/ui-manifest.json --output docs/ui/studio
```

交互脚本 `scripts/smoke_studio.mjs` 需要另一份可写的离线 fixture，不应用于日常项目。两种脚本都会拦截外站请求并关闭自己创建的 Edge；浏览器配置与临时数据保留在已忽略的 `data/` 内。

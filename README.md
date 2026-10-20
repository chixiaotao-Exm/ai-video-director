# AI Video Director

本地运行的有状态视频导演：从参考视频提取结构和风格，以已获授权的人物与声音制作新视频，并只重建修改影响的镜头。SQLite 保存项目、资产、不可变版本、费用和持久化任务。

## 安装

需要 Python 3.12。在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,api]"
Copy-Item .env.example .env
```

Linux/macOS 激活命令为 `source .venv/bin/activate`，复制配置用 `cp .env.example .env`。
UI 和离线测试不需要模型 SDK；实际 LLM/视频分析另外安装：

```text
python -m pip install -e ".[llm,analysis]"
```

`.[all,dev]` 安装全部运行时功能及测试工具。Whisper/场景检测依赖较大，模型仅在实际调用时加载或下载。

## 配置

启动配置可使用 `AVD_` 环境变量或 `.env`；浏览器设置保存到本地 settings.json 并覆盖环境层。完整变量说明见 [.env.example](.env.example)，优先级见下方“浏览器设置与本地安全”。本地凭据文件不提交 Git。

- `AVD_DATA_DIR`：媒体和 SQLite 存储位置，默认 `./data`。数据库默认是该目录下的 `state.db`；可用 `AVD_DATABASE_URL` 覆盖。
- `AVD_ANTHROPIC_API_KEY`、`AVD_OPENAI_API_KEY`、`AVD_GEMINI_API_KEY`：真实服务凭据。每家对应的 `AVD_*_BASE_URL` 支持 relay。
- `AVD_DIRECTOR_MODEL` 默认 `claude-opus-5-5`；`AVD_SCRIPT_MODEL`/`AVD_QA_MODEL` 默认 `gpt-6.1-sol`；`AVD_IMAGE_MODEL` 默认 `gpt-image-2`。这些是规格指定的配置默认值，需确认所用服务/relay 实际提供它们。`AVD_GEMINI_MODEL` 必须显式填写。
- `AVD_MODEL_PRICES`：按实际服务价格配置每百万 token 的输入/输出单价，键为 `provider/model`。未知价格会警告并以零估算，不能当作真实账单。
- `AVD_JOB_ESTIMATED_COSTS`：图像、ComfyUI 等执行器的单任务估算，例如 `{"image_gen":0.05,"image_to_video":0.1}`。
- `AVD_CONFIRM_THRESHOLD`：需要单独确认的金额阈值。项目预算 `0` 表示零额度，`null` 表示不限额；实际费用写入账本。

FFmpeg 和 ffprobe 必须在 PATH。Windows 可运行 `winget install Gyan.FFmpeg` 后重开终端；Ubuntu 用 `sudo apt-get install ffmpeg`。检查：

```text
ffmpeg -version
ffprobe -version
```

最终渲染需要 `libx264`、`subtitles` 和 `drawtext` 过滤器。缺少工具时真实媒体测试会跳过。

## ComfyUI

在 ComfyUI 中安装模型/节点，实际验证流程，然后使用 **Save (API format)** 导出。把 `workflows/` 中的占位文件替换为导出流程并保留约定占位符，或设置 `AVD_WORKFLOW_DIR` 指向自己的目录。占位文件本身不能生成视频，细节见 [workflows/README.md](workflows/README.md)。

通过配置切换本机或远端 GPU：

```dotenv
AVD_COMFYUI_ACTIVE_PROFILE=local
AVD_COMFYUI_PROFILES={"local":{"base_url":"http://127.0.0.1:8188","timeout":600},"remote":{"base_url":"http://127.0.0.1:8189","timeout":600}}
```

远端 profile 连接 SSH 隧道，例如 `ssh -L 8189:127.0.0.1:8188 user@gpu-host`。ComfyUI 保持绑定远端的 `127.0.0.1`。

## 启动

激活虚拟环境后运行 `avd serve`，然后打开 http://127.0.0.1:8000 。项目、素材授权、生成、版本回退、队列、费用和设置均可在浏览器操作。

```text
avd serve
```

默认启动 GPU/API/CPU 各一个受管 worker；系统页可分别启动、停止和查看状态。关闭服务时先给当前任务宽限期，再安全终止进程树并保留重试/隔离状态。界面只控制本服务启动的进程，不会终止另行启动的 worker。

已有独立 worker 或只想检查项目时：

```text
avd serve --no-workers
```

`--with-workers` 显式启用默认行为。独立 worker 仍可使用 `avd worker --lane gpu|api|cpu` 或 `scripts/run_worker.py`。共享 GPU lane 由数据库串行占用约束保护。新设置在受管 worker 下一次领取任务前加载，已排队任务的执行快照不变。

上次异常退出且确认所有旧 worker/子进程均已停止时，才使用 `--recover-stale` 强制恢复。进程清理不确定会隔离对应任务，不能通过反复启动 worker 绕过。API 文档在 `/docs`；此程序是回环地址上的本地单用户应用。

## 使用流程

1. 新建项目并设置预算，上传参考视频，运行分析，检查结构模板并批准。
2. 上传自己的正面参考图（可加侧面/半身/全身图），填写固定外观描述；上传清晰声音样本。分别确认有权使用该人物/声音，记录确认人，然后绑定到项目。参考视频的人脸与声音不会被当作授权资产。
3. 填写创作简报并生成脚本/分镜。**检查点 1：批准脚本。**
4. 生成首帧，在镜头网格检查角色与构图。**检查点 2：批准首帧。**
5. 执行重建，依次完成视频、配音、唇形、字幕和最终合成。**检查点 3：审阅最终视频。**最终产物带可见 AI-generated 标识及元数据。
6. 在聊天中要求修改台词、情绪、画面、运镜或时长。只影响相应链路；旧版本可选择回退，媒体文件不会被覆盖。

批准绑定具体版本；修改后过期批准需要重新确认。预算确认用页面的金额确认表单，不能由模型自行授权。需要增减镜头数量时新建项目；当前支持同数量分镜重写及单镜头字段修改。

## 测试与手动 smoke

```text
python -m pytest -q
```

测试使用 FakeProvider、假 SDK/ComfyUI transport 和本地生成的小文件。所有服务网络连接默认禁止；Windows asyncio 的进程内 socketpair 唤醒管道除外。

`scripts/smoke_comfyui.py`、`smoke_llm.py`、`smoke_analysis.py`、`smoke_image.py`、`smoke_media.py`、`smoke_director.py` 是显式手动检查入口。缺少配置时正常退出并说明原因；配置真实服务后运行可能产生费用。

完全离线的真实媒体演示不需要模型或网络：

```text
python -m avd.demo
```

它用 FFmpeg 生成文字卡片、慢速缩放、正弦音和字幕，走真实队列/重建引擎，修改第 2 镜头台词后打印只重跑的节点和缓存命中。演示数据库与不可变版本保存在 `data/demo/runs/`，便利导出为 `data/demo/final.mp4`。仓库自带的 `data/tools/ffmpeg/`（若已按进度下载）只用于本地验证，不提交二进制。

里程碑记录见 [docs/PROGRESS.md](docs/PROGRESS.md)，实现决策见 [docs/DECISIONS.md](docs/DECISIONS.md)。

## Quality checks and recovery

Run `./scripts/check.ps1` on Windows or `bash scripts/check.sh` on Linux/macOS.
They check Ruff lint/format, mypy (strict in state/jobs/engine), and the full offline pytest suite.
Known audit findings and external verification requirements are in [docs/REVIEW.md](docs/REVIEW.md).

The asset panel can also generate a fictional character sheet from text. Inspect the generated image in job results, then explicitly confirm consent before assigning it. A paid image smoke records its work in a private database under `data/smoke/image/`.

Workers now recover only expired leases automatically; an active heartbeat prevents live work from being reset. The explicit `--recover-stale` option forces recovery and should only be used after every previous worker has stopped. Use the UI's continue action (or director rebuild with `retry_failed`) to retry corrected failures.

## CLI

安装或更新 editable 包后，虚拟环境内可使用 `avd`（也可 `python -m avd.cli`）：

```text
avd init
avd project create --title "My video" --budget 10
avd project list
avd project status PROJECT_ID
avd analyze source.mp4 --project PROJECT_ID
avd analyze source.mp4 --budget 10
avd asset character create --name Hero --front uploads/front.png --appearance-prompt "Original character"
avd asset voice create --name Narrator --sample uploads/sample.wav --engine comfyui
avd consent confirm character CHARACTER_ID --confirmed-by "Owner"
avd consent confirm voice VOICE_ID --confirmed-by "Owner"
avd chat PROJECT_ID
avd chat PROJECT_ID --message "Summarize progress"
avd rebuild PROJECT_ID --retry-failed
avd versions PROJECT_ID NODE_ID
avd revert PROJECT_ID NODE_ID 1
avd jobs PROJECT_ID
avd worker --lane gpu
avd worker --lane api
avd worker --lane cpu
avd serve --host 127.0.0.1 --port 8000
```

全局 `--data-dir` / `--database-url` 位于子命令之前。`analyze` 会把本地视频复制进 uploads 后入队；不指定项目时新建项目，`--budget` 设置该项目额度。资产创建使用 `data_dir/uploads` 内的相对文件路径（可通过网页上传）。资产绑定与版本审批继续在网页完成；CLI 重建不会绕过这些检查点。`chat` 输入 `/quit` 退出。`worker --once` 执行一个任务，`--recover-stale` 是停止旧 worker 后的强制恢复选项。

## GPU 与容器部署

租用 Ubuntu GPU、安装 ComfyUI、SSH 隧道、模型清单、Docker/Compose 和关机停止计费步骤见 [deploy/gpu-server/README.md](deploy/gpu-server/README.md)。默认安装脚本只开回环地址；模型清单需在核对许可后选择。

```text
docker compose -f deploy/docker-compose.yml up --build -d
```

Compose 使用 host networking，面向 Linux 或启用了 host networking 的 Docker Desktop，以便访问主机上的 SSH 隧道。数据库与媒体在独立 named volume 中；不要用删除 volume 的方式停止服务。

## 运行状态、超时与退出

`GET /health` 检查应用数据库连接，正常返回 200，数据库不可用返回 503。它不探测收费模型或 GPU。`GET /metrics` 返回各 lane 的排队数量、正在运行的任务和各项目累计费用；Docker 健康检查使用 `/health`。

日志输出到 stderr，包含 `project_id`、`job_id` 和事件类型；设置 `AVD_LOG_FORMAT=json` 可供日志工具读取，`AVD_LOG_LEVEL` 控制级别。HTTP 日志记录方法、状态和耗时，不记录聊天正文或上传内容。

生产 CLI 和 `scripts/run_worker.py` 为每次任务启动独立执行进程。`AVD_JOB_TIMEOUT_SECONDS=3600` 是默认硬超时；`AVD_JOB_TIMEOUTS` 的 JSON 字典按 job_type 覆盖，例如 `{"tts":600,"lipsync":1800}`。超时先终止本地执行进程及其子进程，再记录失败并按 `max_attempts` 重试。内嵌 fake/demo Worker 保持同步执行；若配置硬超时，必须提供隔离执行器。

Ctrl+C / SIGTERM 停止领取新任务，给当前任务 `AVD_WORKER_SHUTDOWN_GRACE_SECONDS` 秒完成，默认 30 秒；超过宽限期后终止并按重试策略保存。Windows 使用 Job Object 管理进程树；Linux 使用独立进程组并监测监督进程退出。若不能确认进程树已停止，该任务保持 running 并记录隔离原因，GPU lane 不会继续领取。确认所有旧 worker、其子进程和相关远程工作已停止后，才使用 `--recover-stale` 强制恢复。

本地超时不会保证取消远程服务已经接受的推理；远程任务可能继续执行和计费。队列任务提供可重试的至少一次执行，不承诺外部调用恰好一次；强制中断时尚未返回的用量应与服务商账单核对。

同项目的预算决策、调用与记账互斥到事务提交。并发冲突返回 HTTP 409 / `BudgetBusy`，稍后重试即可；worker 冲突也遵循有限重试次数。数据库旁的 `.avd-budget-locks` 文件保存锁载体，锁由操作系统管理；运行期间不要删除它们。完成但尚未记账的生成尝试继续占用预算，避免回调间隙重复花费。

## 浏览器设置与本地安全

设置页可保存模型、服务地址、价格、预算默认值、ComfyUI profiles、日志和执行参数。配置优先级为 `.env`/环境变量 < `data/settings.json` < 显式启动/构造参数。字典字段整体覆盖下一层，保存 `{}` 会清空该层的映射。设置原子保存到当前 data_dir，运行时数据目录和数据库地址只读；已排队任务仍使用其已保存的执行配方。

按 T17 要求，界面输入的密钥保存在本地、Git 忽略的 settings.json；界面/API 仅显示是否设置和末四位，输入框留空保持原值，替换或清除必须显式操作。不要分享此本地文件。配置变更会清除此前连接测试的状态。

服务仅接受 localhost、127.0.0.1 或 ::1 的 Host；浏览器写请求的 Origin 必须与应用一致。命令行无 Origin 的请求仍可用。连接测试不自动运行；模型测试需要选择费用归属项目，最多请求八个输出 token，正常执行预算检查和费用记账。ComfyUI 测试只读取 /system_stats。

## 白色 Web UI 与截图验证

工作台、项目六个标签页、素材库、任务队列、费用、设置和系统均使用中文白色界面；手机导航在顶部菜单中。分镜/费用等宽表格可在面板内横向滚动。配置密钥仅写入本机，连接测试只由明确点击触发。

`scripts/check.ps1` / `scripts/check.sh` 现在也运行每个 JavaScript 模块的 `node --check`。界面截图索引与检查结果见 [docs/ui/README.md](docs/ui/README.md)：40 张桌面/手机 PNG、页面诊断报告和 10 项离线浏览器操作报告。

截图复现使用 `scripts/prepare_ui_demo.py --data-dir data/ui-capture` 创建独立离线媒体。读取该目录的 ui-manifest.json，以其中 data_dir/database_url 启动 `avd serve --no-workers`，然后运行 `node scripts/capture_ui.mjs --base-url http://127.0.0.1:8001 --manifest data/ui-capture/ui-manifest.json`。不要把交互冒烟脚本用于日常项目数据；它专用于可写的离线验证目录。已有本地设置不会被演示准备脚本覆盖。

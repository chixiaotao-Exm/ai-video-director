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

所有配置使用 `AVD_` 环境变量或 `.env`；完整变量说明见 [.env.example](.env.example)。`.env` 不提交 Git。

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

## 启动网页与 worker

从仓库根目录、激活虚拟环境后运行：

```text
uvicorn avd.api.app:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。API 文档在 `/docs`。这是本地单用户应用。

在另外的终端各启动一个 worker，使用同一份 `.env` 和数据库：

```text
python scripts/run_worker.py --lane gpu
python scripts/run_worker.py --lane api
python scripts/run_worker.py --lane cpu
```

GPU lane 启动一个 worker 串行执行；API lane 可以启动多个 worker。网页入队后不等待生成完成，SSE 推送任务完成事件，页面也刷新排队/运行状态。
关闭用 Ctrl+C。上次异常退出且所有旧 worker 已停止时，可先用 `--recover-stale` 启动一个 worker 恢复中断任务；不要对正在执行的 worker 使用该参数。

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

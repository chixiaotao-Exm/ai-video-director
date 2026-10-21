# 歌声模型版本：RVC 与 SeedVC

这是 S03 的正式工程接口。训练 RVC 模型和 SeedVC 零样本转换分别使用明确的引擎与参数。2026-10-21 已完成 SeedVC 正式后端 10 秒真实小样；目标音色与完整唱跳效果尚待人工验收，RVC 真实目标权重仍未完成验收。

## 官方契约基线

2026-10-21 直接读取 RVC 官方 GitHub API 与源文件核对：

```text
Repository: https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI
Checked HEAD: 81eed5e8f68b6bed1789f682fe78cdd324495afc
CLI: infer/cli.py
CLI Git blob: 190216d9c99b3ace708d614a0a6bb65dbed322f8
CLI SHA-256: 8e321cd606bfa1f4756685db897e90df2278e77d3e58869bc83d4735ff7b7387
Documentation: docs/en/cli.md
```

支持入口是 `infer/cli.py`。该基线支持 `--model`、`--input`、`--output`、`--speaker-id`、`--pitch`、`--f0-method`、`--index`、`--index-rate`、`--resample-sr`、`--rms-mix-rate`、`--protect` 和 `--format`。

`--f0-method` 只有 `pm`、`rmvpe`。该入口没有 `--device` 或 `--filter-radius`，也没有旧的 `tools/infer_cli.py`。`--list-speakers` 输出 `0-N` 或 `ID<TAB>名称`，不是 JSON。测试按上述实际格式验证。

设备和精度由 RVC 的 `configs.config.Config` 自动选择。适配器在独立子进程探测该对象；配置 `RVCInstallation.device="cuda"` 时，若它选到了 CPU，就明确拒绝继续，避免悄悄接受回退。实际 CLI 调用仍使用官方支持的参数。转换结果保存该探测得到的设备、精度，以及 CLI 和 `infer`/`configs` Python 源码树的哈希。安装目录的模型依赖仍须事先准备；不自动下载权重。

## 接口

`src/avd/performance/voice.py` 提供：

- `init_voice_models(engine)`：创建独立 `singing_voice_model_versions` 表及 SQLite 更新/删除保护。应用应在启动时显式注册；重复调用安全。
- `VoiceModelImport` / `import_voice_model(session, data_dir, request)`：要求已有且授权的 `Voice`，将 `.pth`、可选 `.index` 和训练配置复制为独立版本快照，保存 SHA-256、参数、来源和编号。同一内容/参数重复导入复用版本。样本文件和普通 TTS 的 `Voice.model_path` 不自动变成 RVC 模型。
- `voice_model_readiness(...)`：检查授权、快照完整性、运行时路径/哈希。`runtime_files_available` 只代表运行文件存在且 CLI 哈希校验通过，普通 GET 不加载模型、不执行子进程；`probe=True` 时用官方 CLI 加载说话人元数据，并探测运行时设备。只有实际探测成功才给出 `runtime_available`、`model_compatible` 和 `ready_for_inference=True`。该值只代表可以尝试推理，`quality_verified` 始终为 `False`。
- `rvc_installation_snapshot(installation)` / `validate_rvc_snapshot(installation, snapshot)`：只读取运行配置、CLI 与 `infer`/`configs` Python 源码哈希，不运行子进程、不加载模型。快照包含 `installation`、`cli_sha256`、`runtime_source_sha256`、`not_ready` 和 `reasons`。缺文件或固定 CLI 哈希不符时显式标记不可用；冻结时不可用、后来才安装的环境也必须重新生成配方。校验拒绝配置或源码变化。该快照不代表模型已探测或音色已验收。
- `RVCVoiceConverter(data_dir, installation).convert(session, version_id, input_relative, output_relative, parameters=None)`：输入为 PCM WAV 干声，输出为 WAV 与 `.conversion.json`。参数以 argv 列表传递，禁用 shell，不覆盖已有产物。默认不变调。输出采样率必须符合所选模型/显式重采样参数；时长误差最多为输入/输出中较长的一个采样周期（40 kHz 时为 25 微秒），`duration_tolerance_s` 只能进一步收紧，不能放宽。按精确采样数比较，超过立即报错，不做拉伸修补；清单保存实际误差与生效容差。转换前后重新校验运行时源码，拒绝处理中发生的替换。

数据库只保存模型相对路径。`RVCInstallation` 的 Python 和安装目录是运行环境配置，不写入模型版本行。外部训练配置作为来源快照；官方推理入口读取 `.pth` 中嵌入的结构配置。

同一输出路径使用独占 `.conversion.lock`，防止并发请求覆盖文件或在失败清理时删除另一任务的产物。正常退出会释放锁；若子进程/服务被强制终止，需确认该转换已停止后再移除遗留锁。

## 验证边界

离线测试使用假检查点、假 CLI 子进程和生成的 PCM WAV，验证不可变性、导入幂等、文件篡改、授权、参数契约、超时/失败、采样率与时长拒绝行为。它们不加载真实权重。

真实验收还需要可运行的 RVC 安装及 HuBERT/RMVPE 等运行依赖、目标模型/索引、独立目标声音和演唱干声；需要人工试听高音、长音、咬字及原声泄漏。即便模型可被 `--list-speakers` 读取，也不能据此宣称这些听感已达标。

## SeedVC 正式入口

`src/avd/performance/seed_vc.py` 负责版本导入、运行时冻结、readiness 与转换；`seed_vc_runner.py` 只由独立 SeedVC Python 执行，不把 CUDA Torch 装入主应用环境。官方 SeedVC 源码保持未修改，固定 revision 为 `51383efd921027683c89e5348211d93ff12ac2a8`。

本机 `Settings.seed_vc_installation` 配置如下；通过现有 settings API/SettingsStore 保存和重新加载，主库版本、任务配方另行保存完整运行时指纹：

```json
{
  "project_root": "D:/code/ai-video-director/data/tools/seed-vc",
  "python_executable": "D:/code/ai-video-director/data/tools/seed-env/Scripts/python.exe",
  "revision": "51383efd921027683c89e5348211d93ff12ac2a8",
  "model_manifest_path": "D:/code/ai-video-director/data/t21-performance-runtime-20261021/seedvc-models.json",
  "device": "cuda",
  "timeout_s": 600,
  "duration_tolerance_s": 0.01
}
```

`GET /voices/{voice_id}/singing-reference-samples` 仅列出选定声音资产登记的样本及其哈希/媒体信息。用 `POST /singing-models/import` 导入 SeedVC 参考版本：

```json
{
  "voice_id": "目标声音资产ID",
  "engine": "seed-vc",
  "reference_path": "assets/voices/目标声音资产ID/已登记样本.wav",
  "sample_rate": 44100,
  "parameters": {
    "diffusion_steps": 30,
    "inference_cfg_rate": 0.7,
    "pitch": 0,
    "auto_f0_adjust": false,
    "fp16": true,
    "seed": 20261021
  },
  "provenance": "目标参考样本与转换参数的来源说明"
}
```

参考必须是该声音资产登记的至少一秒 PCM WAV，且资产授权状态有效；不接受其他声音的文件或 RVC 的 model/index/config 路径。导入复制参考音频，冻结依赖环境 METADATA/RECORD、源码、全部权重和独立 runner。源码、参数或依赖发生变化时须创建新版本，不能修改旧版本。

`GET /singing-models/{version_id}/readiness` 不发起新探测；它校验当前文件与授权，并只在版本、模型指纹、运行配置和源码指纹一致时恢复最近一次真实成功探测。刷新页面或切回模型后仍可恢复，新的排队、失败、取消或不兼容探测不能借用更早成功结果。`POST` 同一路径将真实兼容探测排入 GPU 串行队列；202 仅表示已入队。SeedVC probe 使用 CPU 构建并逐值检查活跃 DiT 参数、检查 CUDA 可用性，不启动 GPU 歌声推理；`ready_for_inference` 与听感验收是不同状态。

转换通过正式 `singing_voice` 节点进入既有 GPU lane；`LocalPerformanceBackend` 根据不可变模型版本的 `engine` 选择转换器。F0 条件和长度倍率固定，参考最多使用前 25 秒；推理使用预先核验的本地模型路径并关闭网络，不提供原唱或 TTS 回退。raw FLOAT WAV 和经过峰值衰减/至多 10ms 尾部补零的 PCM24 WAV 分别保存，所有转换记录 `quality_verified=false`。

SeedVC 增加的 SQLite 列为 nullable 的 `reference_path`、`reference_sha256` 和 `runtime_snapshot`，旧 RVC 行无需重写；现有版本更新/删除触发器继续生效。普通失败或超时删除本次标准化输出和成功清单，保留请求/日志/raw 证据。强制终止时可保留该 attempt 的锁和半成品；任务不发布成功版本，后续重试使用新的 `attempt-N`，不能手工把半成品标为成功。

## 2026-10-21 真实适配器结果

通过正式 `LocalPerformanceBackend.run("singing_voice")`，主库声音 `704146bb-d3dc-444e-8698-cfaa99a9e31d` 的 SeedVC v1 为 `1eb9b842-ac19-4a67-91bb-8ebd91cd777e`。证据目录：

```text
data/t21-performance-runtime-20261021/seedvc-adapter-20261021-105307/
```

`adapter-result.json`、`converted.conversion.json` 和 worker 日志记录：RTX 5070 Ti Laptop，Torch/torchaudio 2.7.1+cu128；加载 10.982 秒、换声 25.788 秒，正式后端含 CPU probe 共 54.970 秒；显存峰值 allocated 2,635,046,400 bytes、reserved 2,770,337,792 bytes。`converted.wav` 为 44.1kHz mono、441000 个采样点、整 10 秒；raw 的 440832 个采样点仅尾补 168 点（3.81ms），没有拉伸。

raw 有约 0.0034% 采样达到满幅；FLOAT 保留与标准化衰减不能修复上游已有饱和。输出衰减 1dB，目标音色、高音/长音、咬字和泄漏仍待人工试听。此前的原唱/换声混音对照在 `seedvc-listening/`；它们是此前独立官方小样的派生产物，不冒充本次正式 backend 输出。

此次输入来自已保存的真实分轨文件，尚无主库源分轨 `Version`，所以 `source_version_id=null`。引擎已经实现传递真实依赖 `Version.id`，但主队列“真实分轨→换声→混音”的版本关联/发布/缓存、真实推理中断和完整唱跳视频仍待运行验收。SeedVC 25 项离线测试与 RVC 相关检查共 89 项通过；既有 CPU 进程树/超时回归另有 24 项通过。测试不能替代上述真实效果验收。

## 模型执行记录

任务的 `execution.steps/history` 保留每次尝试的执行身份。排队时的 `snapshot` 仅说明冻结配置；FFmpeg 的准备、混音、同步和导出也只显示工具快照。SeedVC、RVC 与 Demucs 只有原生子进程成功启动后才记 `actual`，探测记 `phase=probe`，转换记 `phase=inference`。启动后失败或超时仍保留记录，缺失执行文件不产生实际启动记录；显式注入的假 runner/backend 不会冒充原生模型执行。

ComfyUI 在原生客户端提交 `/prompt` 的边界记 `actual`，含工作流修订、模板哈希及声明的模型版本；这表示提交尝试，不证明远端已加载对应权重，`remote_weights_verified=false`。恢复已有持久 prompt 记 `source=resumed`、`phase=resume`、`calls=0`，不会制造一次新的模型调用。运行时在短事务内同时检查任务为 `RUNNING`、claim token 与尝试编号相符，过期 worker 不能给新尝试追加执行证据。

# S03：声音模型准备与歌声音色转换

状态：工程接入与真实 SeedVC 适配器短样本技术运行已验证；用户对原十秒换声试听明确反馈“音色还不够像，需要调整”，原音色验收未通过。已新增同一目标参考的 B/C/D 三组真实转换及 A 基准统一试听，并用用户指定两段歌曲制作原唱/B/C/D的原画对照片，新版本尚待人工判断。主队列真实分轨版本链及完整唱跳效果仍未验收。依赖：S02。与 S04 开发并行，共享 GPU 时执行串行。

## 2026-10-21 已完成

- 保留训练模型 `engine="rvc"` 的独立导入与官方 CLI；新增 `engine="seed-vc"` 零样本歌声音色转换。SeedVC 参考样本不是训练完成的 RVC 权重。
- SeedVC 只能选目标声音库已登记、已授权的 PCM WAV 样本；复制参考音频并记录 SHA-256、参数、来源及不可变版本。SQLite 旧表幂等增加参考路径、参考哈希和运行时快照列，继续禁止更新/删除版本。
- 正式 `LocalPerformanceBackend` 按引擎分派。SeedVC 冻结官方源码 revision、Python 源文件、独立 Python/依赖 METADATA/RECORD、九个模型文件及其不可变仓库 revision/hash；推理前后拒绝漂移。主环境不导入 CUDA Torch。
- 独立 worker 使用固定本地权重与本地模型目录，关闭 Hub 联网并阻断推理网络。先用 CPU 比较所有活跃 DiT 可学习参数，再探测 CUDA 可用性。F0 条件开启、长度倍率固定 1，参考最多使用 25 秒。
- 保存 raw FLOAT WAV、峰值衰减后的 PCM24 WAV、实际参数、哈希、显存、耗时和完整转换清单。只允许最多 10ms 的尾部静音补齐，不拉伸、不使用原唱回退；拒绝空、非有限、原唱/仅增益复制和超时结果。
- 参数 schema、配置图、运行时探测任务和依赖版本 ID 已进入正式服务；仅改场景保留冻结工作流和声音配置。模型与参数切换按实际引擎重新校验。

## 真实正式适配器证据

2026-10-21 10:53 UTC，通过正式 `LocalPerformanceBackend.run("singing_voice")` 完成一次 10 秒转换；不是将独立演示文件冒充正式输出。主库目标声音 `704146bb-d3dc-444e-8698-cfaa99a9e31d` 已登记 SeedVC v1 `1eb9b842-ac19-4a67-91bb-8ebd91cd777e`。导入前数据库备份、请求、worker 日志和原始/标准化产物保存在：

```text
data/t21-performance-runtime-20261021/seedvc-adapter-20261021-105307/
  adapter-result.json
  converted.wav
  converted.raw.wav
  converted.conversion.json
  converted.seed-result.json
  converted.seed-stdout.log
```

- 设备 RTX 5070 Ti Laptop；独立 Torch/torchaudio 2.7.1+cu128。
- 模型加载 10.982 秒、实际转换 25.788 秒；正式后端含 CPU 兼容 probe 总计 54.970 秒。峰值 allocated 2,635,046,400 bytes、reserved 2,770,337,792 bytes。
- raw 为 44.1kHz、440832 个采样点、9.996190476 秒；尾补 168 个采样点（3.81ms）后为 441000 个采样点、整 10 秒，没有拉伸。输出 SHA-256 `ae862fbfc9e35bb5db5663764447f485b46fc5505f53534a1acf81e52fed87ff`。
- raw 仍有约 0.0034% 采样达到满幅，必须保留这一质量限制；FLOAT 保存和输出衰减不能恢复上游已经饱和的峰。标准化输出衰减 1dB。`quality_verified` 保持 false，声线、高音、长音、辅音、换气和原唱泄漏等待人工试听。
- 本次直接使用已保存的真实分轨文件，源分轨尚不是主库 `Version`，故 `source_version_id=null`。主重建引擎已把依赖 `Version.id` 传入 backend，但“主队列真实分轨→换声→混音”发布/缓存与真实版本关联仍须单独验收，不能用本次直接调用替代。

此前独立官方演示及响度匹配的原唱/换声混音对照仍保留在 `data/t21-performance-runtime-20261021/seedvc-listening/`，其 RMVPE 音高/包络测量只能辅助检查旋律与时序，不代表已确认目标音色。真实 RVC 检查点训练/效果验收尚未完成。

## 原试听未通过后的真实参数对照

用户反馈原文与原试听哈希保存在`data/t23-seedvc-timbre-comparison-20261021/attempt-1/human-review-old-listening.json`，明确为`human_quality="rejected"`、`quality_verified=false`。旧试听与正式A的raw WAV文件哈希不同，但解码波形逐采样相同；不能将WAV封装差异解释为重新生成了不同声音。旧混音另有处理，不作为新混音的直接公平对照。

三次新转换使用正式后端、隔离数据库和现有独立CUDA环境，严格串行。所有组固定seed20261021、pitch0、auto_f0_adjust=false、fp16=true、F0开启、长度倍率1；A/B/C使用同一登记参考的最多25秒，D只用该参考的0–10秒，没有引入其他人的声音。

| 组 | 步数 | CFG | 参考长度 | 本轮实际后端耗时 |
| --- | --- | --- | --- | --- |
| A | 30 | 0.7 | 最多25秒 | 复用正式历史输出，新增调用0 |
| B | 50 | 0.7 | 最多25秒 | 60.622秒 |
| C | 30 | 1.0 | 最多25秒 | 47.092秒 |
| D | 30 | 0.7 | 前10秒 | 40.125秒 |

三次GPU推理均成功。最初试听后处理因FFmpeg诊断尾部混入JSON解析而失败；原脚本、失败清单和raw全部保留，另在`attempt-1/cpu-recovery-1/`仅CPU恢复，恢复新增模型调用0。完成清单为`comparison-completed-manifest.json`；8份干人声/同伴奏混音均48kHz双声道、480000采样点、整10秒，哈希及文件头另由`listening-root-audit-1.json`独立复核。原输入、模型快照、推理产物和主库所选声音模型行均未改动。

干人声按同一`loudnorm`输入测量方法静态配平到约-20LUFS；混音采用相同伴奏、0采样偏移和共同master增益，没有各自动态压缩或独立混音归一化。不同响度测量方法仍可有约0.3LU差异，不能宣称绝对等响。试听没有额外加入原唱，但伴奏中的原唱残留尚未人工评估。D的短参考未证明更干净或更像。新版本没有重新测量旋律/F0误差；0变调配置不是音高完全不变的实测证明。raw满幅附近采样与高音、长音、咬字等限制保留，新对照的`human_quality`仍为未审、`quality_verified=false`。

另外在`attempt-1/target-reference-listening-1/`保留同一登记目标参考精确前10秒的试听，按同一测量方法静态配平到-20LUFS、48kHz双声道480000采样点；该文件是参考声音，并非新换声输出。单独`manifest.json`记录来源哈希、裁片区间、静态增益和限制，没有覆盖原参考、8份对照或完成清单。

## 用户指定歌曲的直观对照

用户确认两段上传`f73cb0ee655e4a61843e2f5c0fe742c7`和`f27597179f574ac6a93aad8d70ba50d1`只作为歌曲输入，目标仍为声音704146bb。`data/t24-requested-song-comparison-20261021/attempt-2/comparison-manifest.json`合并两源各0–10秒B/C/D正式结果，参数和目标参考沿用上表；总计7次原生调用，6次成功，1次源2B因监控归属无法确认而中止，原失败证据保留，源1没有重跑。未下载新模型、未重跑Demucs、未改主库模型选择或审批。

新增`attempt-1/balanced-listening-1/`8份混音使用各源原唱静态人声增益同时衰减伴奏（源1 -9.84dB、源2 -8.75dB），保留原分轨的人声/伴奏比例；每源四组同伴奏、0偏移、共同master均0dB，不叠加旧preview master或逐组动态处理。原唱是分轨还原，不能称为原AAC逐采样保真；伴奏残留与音色仍待试听。

`visual-comparison-1/source-{1,2}/`各一段40秒MP4按原唱/B/C/D播放同一10秒原画，540×1080、30fps、1200帧、48kHz AAC；额外边栏标明组别与AI换声。未压缩PCM24拼接WAV另存；`assembly.json`绑定正式转换和balanced清单，`delivery-audit-1.json`核对PCM各段字节一致和两个MP4完整无错解码，实际标签帧已查看。此视频没有生成新人物或场景，也不是40秒长片模型执行；quality=false/not_reviewed，需用户指出哪组更像或继续调整。

## 自动检查与失败边界

后续用户明确选择“B”，其原话、参数与来源保存在 `data/t24-requested-song-comparison-20261021/human-selection-B-1.json`。主项目 `42c2fdbf` 已使用 SeedVC v1 的有效覆盖参数 50 步 / CFG 0.7、同一目标参考，真实完成分轨 Version `7aafd568`、换声 Version `7aa16900` 与混音 Version `dc93a057`，不再仅有独立数据库的声音样片。模型行的不可变默认仍为 30 步，页面现在显示项目实际保存的 50 步。组别选择不代表整段唱腔、口型或视频质量批准；主混音与 T24 balanced 试听的人声/伴奏比例另行复核，记录见 T25。

SeedVC 新增 25 项离线检查；与 RVC 生命周期和执行快照共 89 项通过，既有 CPU 进程树/超时检查另外 24 项通过，相关七个源码文件 mypy 与 Ruff 通过。假模型/假进程检查不等于模型效果验收。

异常和子进程超时会删除当前任务拥有的标准化输出及成功清单，保留失败请求、日志和 worker/raw 证据；不覆盖历史结果。普通异常释放独占锁。主队列通过现有 `ProcessRunner` 管理整个任务进程树，Windows Job Object/POSIX 进程组覆盖 SeedVC 子进程；重试写入新的 `attempt-N` 目录。强制终止来不及执行 Python finally 时，旧 attempt 可以保留锁和半成品，但不会发布成功 `Version`，也不由重试覆盖。本轮未执行真实 GPU 推理中断验收，仍应作为后续运行验收项。

接口和本机运行配置见 [歌声模型接入说明](../../docs/performance-rvc.md)。

原生模型执行身份现已接入任务 `execution.steps/history`：SeedVC/RVC/Demucs 在子进程成功创建后记录实际启动，probe 与 inference 分开；ComfyUI 记录真实提交边界，恢复持久 prompt 单独标记 `resumed` 且调用数为零。FFmpeg 只显示工具快照，假 runner/backend 不制造实际模型记录。追加证据的短事务严格检查 RUNNING、claim token、attempts；这些记录证明执行边界，不代表模型效果或远端权重加载已经验收。

## 工作包

1. 扩展 Voice 资产：区分原始声音样本、准备中、模型可用、准备失败；记录模型版本、权重/索引哈希、采样率及引擎配置。
2. 先提供已验证模型导入，再提供声音样本清洗/切分、训练、验证和版本登记任务。上传样本本身不能标记为可转换。
3. 分离参考视频的原人声与伴奏；复用现有 Demucs 路线，不达标样本再比较其他分离后端。
4. 增加 `singing_voice_conversion` 执行器，用输入人声与目标模型生成新人声；音高/F0、索引与前后处理等参数进入不可变快照。
5. 新增试听与混音输出，支持伴奏/人声音量及响度调整；保留干人声、伴奏和混音三个独立产物。
6. 训练与推理按资源占用进入队列，记录一次性模型准备成本与分段推理成本，支持取消、失败和恢复。

## 产物

- 声音资产状态和版本管理、模型导入/准备任务。
- 歌声转换后端与 fake，实现人声和混音预览 API。
- 固定角色的真实换声样本和参数记录。

## 验收

- 模型不就绪时给出具体原因，不悄悄回退为普通 TTS。
- 新人声保留目标歌曲的歌词/旋律与时间位置；检测长度变化和前后静音偏移，不默认强行变速。
- 人工试听高音、长音、辅音、换气和泄漏；声线可接受才进入成片。
- 换模型版本/音高参数只重做对应音频和真实依赖后继；调混音不触发视频重建。
- 取消和异常不能发布损坏的“成功”模型或音频；历史声音模型可选择回退。

主要模块：`assets/library.py`、`assets/media.py`、`state/models.py`、新增声音后端、`executors/`、`config.py`、队列和相关测试。

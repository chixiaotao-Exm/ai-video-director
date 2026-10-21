# T18 实施与验收记录

最新主项目进展单列于 `data/t25-main-performance-draft-20261021/README.md`：用户选择 B50/.7，并指定“天台晚风·虚构成年女主”；另确认普通舞台服装后，新场景图已生成，主准备/分轨/换声/混音均真实完成，动作视频仍在生成。主队列新增结果不覆盖下面历史技术样片的拒绝、失败或原质量结论；完整视频与长片尚未验收。

日期：2026-10-21。总目标仍是保留舞蹈、换人物、换歌声音色、换场景的完整唱跳视频。真实10秒AI动作视频已与SeedVC换声混音合成，技术核验通过；但开头约1秒人物明显出画，视觉质量未通过，用户也明确反馈换声音色不够像、需要调整。不能将技术导出成功或离线 fake 测试视为效果全部达标。

| 阶段 | 当前事实 | 主要证据 / 剩余项 |
| --- | --- | --- |
| S00 | 规格与 7 项复现问题已修复并回归 | SPEC 第10节；正式图片/设置/子Agent测试；第一轮完整质量门797通过3跳过 |
| S01 | 全身舞蹈源、10秒分轨、真实SeedVC换声、舞台参考图和10秒AI唱跳样片已产出 | 技术合成成功；开头出画问题未修复，目标音色及视觉效果未验收 |
| S02 | 新模式配置、状态图、队列、升级、时间映射和准备API已接入 | `tests/performance/`、`tests/api/test_performance.py`；模型版本指纹和重启/取消专项继续验证 |
| S03 | RVC/SeedVC不可变版本、正式后端、自动Demucs分轨和GPU探测已接入并真实运行；另产出B/C/D参数对照及用户指定两首歌的原画试听视频 | 原音色被用户拒绝；两段40秒对照片按原唱/B/C/D比较同一10秒，尚待人工试听，raw质量限制保留 |
| S04 | 正式backend/API、冻结/恢复与PTS原生采样已贯通；真实Wan原生161帧已生成并导出160帧 | 480×832、16fps、10秒时间契约已验证；开头约1秒出画，动作和身份质量仍待验收 |
| S05 | 已接严格同步、混音与最终合成执行器，真实模型输出已完成10秒MP4合成 | 160帧、音视频整10秒和AI标识已核验；嘴型、音色及视觉质量未验收 |
| S06 | 网页独立工作区、导演共用配置/调度服务、局部修改与草稿保护已接入；新增完整≤10秒视频预览与实际视频审批 | 本轮桌面/手机布局与按钮门禁已有检查记录；历史独立浏览器验证保留，主队列真实闭环仍待验收 |
| S07 | 已接入只读原生时钟分段规划，`execution_available=false` | 长视频执行、跨段衔接、恢复、真实长样片及发布验收仍未完成 |

## 可运行入口

源素材准备（不调用生成模型，新全身舞蹈素材）：

```powershell
.\.venv\Scripts\python.exe scripts\prepare_performance.py --import-source "test\微信视频2026-10-21_041845_325.mp4" --duration 10
```

API：`POST /projects`中指定`mode=performance_remix`；`PUT /projects/{id}/performance`保存完整配置，`PATCH`只修改提供的字段并保留其他冻结配置。`GET /projects/{id}/performance`返回配置和依赖图，通用项目状态/版本API可查看结果。制作入口使用`/projects/{id}/performance`前缀：

| 入口 | 当前合同 |
| --- | --- |
| `POST /prepare` | 仅排入源素材准备任务 |
| `POST /preview` | 输入审批后生成场景/人物参考、换声和混音，没有动作视频或最终MP4 |
| `POST /video-preview` | 输入审批与模型/工作流就绪后生成完整唱跳MP4，含动作、同步及启用的口型；明确区间必须≤10秒，超限拒绝且不改配置 |
| `POST /generate` | 需要实际视频的`performance_preview`审批，处理当前配置区间并可复用有效结果；不自动扩展全长 |
| `POST /segment-plan` | 读取当前准备与冻结动作工作流，提出更长区间的帧/采样分段计划；只读，不生成或排任务 |

`performance_inputs`只审阅当前输入。`performance_preview`必须在播放实际唱跳视频后审批，并绑定场景、歌声、动作、同步、混音、成片及启用口型的当前版本与相对最终视频路径；旧的图像/试听审批不能继续沿用。`performance_final`单独记录成片审阅。API/导演共用这些约束，缺少真实模型或工作流时明确拒绝。

`POST /singing-models/import` 支持已训练的 RVC 文件或 `engine=seed-vc` 的已登记声音参考样本；读取就绪状态不会推理。`POST /singing-models/{version_id}/readiness` 将真实探测排入串行 GPU 队列，202 响应只代表入队，后续读取 probe 任务得到实际探测结果。Seed 参考不是训练好的专属 RVC 权重。

配置示例中的路径必须已导入 `data_dir`，人物和声音ID必须来自资产库：

```json
{
  "preparation": {"source_video_path": "assets/performance-inputs/example.mp4", "start_s": 0, "duration_s": 10},
  "character_id": "角色ID",
  "voice_id": "声音ID",
  "scene_prompt": "深蓝背景的小型录音棚舞台，柔和轮廓光",
  "voice_model_version_id": null,
  "lipsync": false
}
```

当前未配置真实适配器的模型步骤会给出缺失配置原因，不返回假生成媒体。`voice_model_version_id=null` 仅允许保存/准备素材，不能证明声音可转换。

## 本轮短片审批与长片规划进展

新增`/video-preview`和导演`preview_performance_video`补全“输入批准→完整短片→观看后审批”的入口。原`/preview`及`preview_performance`保留为场景与换声参考，不再被视作完整视频预览。`tests/api/test_performance_video_preview.py`离线回归包含实际持久化队列在fake执行器下推进8/9节点、执行时审阅检查、超10秒拒绝和旧试听分支隔离；这些结果不代替真实模型样片验收。

新增`/segment-plan`和导演`plan_performance_segments`，从当前准备区间的起点规划拟处理总长度，不更改现有10秒配置。入口核对CLEAN准备Version、manifest请求、当前源哈希及冻结工作流；采用源视频流实测时长和原生帧/音频采样边界，返回分段、上下文、尾补和固定长度合同限制。规划模块和API的离线回归包含成功/失败后配置、节点、Version、审核、任务及输入文件不变，以及旧版list形状输出拒绝。所有成功计划仍返回`execution_available=false`、`model_generated=false`、`quality_verified=false`。长视频分段执行、跨段连续性、恢复和真实30–60秒样片尚未完成。

真实素材只读规划已单独记录于`data/t22-gateway-20261021/real-interval-plan-verification.json`，SHA-256为`824a86d8c9c0f016c3a98b16dee862b98424cef8ae888c9a187bb08ac93dc6f1`。复用真实源片、不可变准备manifest和冻结snapshot，20秒为320帧/960000采样、两个10秒区间；8帧上下文时的不兼容短尾明确标注，30秒超出源流范围而拒绝。未打开数据库，0模型调用、0排队任务，输入哈希前后不变。这不是新视频或长片连续性验收。

本轮1280px桌面/390px手机浏览器检查未发现横向溢出，新视频与审批按钮在当前条件下正确禁用，控制台无warn/error。截图为`data/qa-evidence/performance-video-preview-desktop-20261021.jpg`和`data/qa-evidence/performance-video-preview-mobile-20261021.jpg`；`performance-video-preview-approval-20261021.jpg`为可直接查看审批按钮的普通视口截图。基础界面观察不代表主队列真实生成、视频观看审批或整条浏览器制作流程已验收。

2026-10-21 13:31:05 UTC观察到本轮完整`scripts/check.ps1`退出码0：1473项Python通过、3项跳过（197.09秒），前端44项通过；Ruff、格式、mypy、构建及21个JavaScript模块语法检查全部通过。日志为`data/t22-gateway-20261021/check-video-preview-segment-plan-attempt4.log`，SHA-256为`e8ad22eaf2505a16276f40619d2fe83c71f8fbf8d0340728e0184cd17f08fac1`，同名`-completion.json`保留实际结束记录；此前三次格式/类型失败日志也保留，未覆盖历史结果。此次自动检查不调用外部模型。

后续只读核验为`data/t22-gateway-20261021/video-preview-release-verification.json`，SHA-256为`938822fc2a0ab31cbd6be67c3310f2043a250659a2292d3219fb650c46db70d0`，绑定这轮源码哈希、质量门、当前主服务和独立浏览器证据。13:31:32 UTC主8000健康且无活动任务，已有图像任务`7ffa9755`和导演`26c4da60`仍为真实成功，实际导演模型为`gpt-6-astra`；主服务尚无新PATCH、video-preview、segment-plan入口。独立8768新接口及页面检查成功后，于13:32:59 UTC确认自有进程和监听均已退出，关闭证据为`video-preview-qa-shutdown.json`。这份核验在用户随后拒绝音色前保存，是当时状态快照，不将其人工待审字段当作后续接受结论。

## 真实媒体证据

- `data/t18-real-media-20261021/dance-000-010/`：新增全身舞蹈源的10秒、600帧动作参考、音频及逐帧manifest。
- `data/t18-real-media-20261021/dance-stems-000-010/`：对应原唱人声与伴奏，每轨48kHz双声道、480000采样点；不是换声结果。
- `data/t18-real-media-20261021/queue-verification.json`：独立烟测数据库中的真实API/队列/Version发布与缓存验证；没有改动主素材库记录。
- `data/t18-environment-20261021/REPORT.md`：源片、人物、声音关系、GPU与模型服务证据。
- `data/t21-performance-runtime-20261021/performance-real-final-20261021-123347/final.mp4`：真实Wan动作与SeedVC换声混音合成，480×832、160帧、16fps，音视频均为10.0秒，带可见AI标识及元数据；同目录`render_manifest.json`记录输入哈希和时间核验。
- `data/t21-performance-runtime-20261021/wan-formal-ten-context21-uncached/`：原生执行耗时1505.253秒（25.08755分钟），raw保留161帧；`attempt-1-comfy-result-audit.json`、`attempt-1-result.json`与原始媒体保留成功执行和适配器拒绝的各自事实。`attempt-2-recovery-result.json`记录沿用同一journal与真实捕获的history/raw本地恢复，新增模型调用`calls=0`，没有重新推理。

原attempt1的模型已成功，但MP4 edit-list的stream时长为10.06201171875秒，比161/16秒少0.48828125ms，旧适配器将其误判为时间线变化。修复后仍严格检查帧数和逐帧PTS，使用完整帧时长验证呈现终点，仅对有充分证据的MP4容器尾部量化误差放行。只移除已声明的第161帧尾补，未拉伸视频或丢弃有效动作帧；原失败证据未覆盖。

技术合成不代表视觉通过。QA帧0和6的人物大幅出画到右侧，帧12仍半身偏右，帧18才回到中心，开头约1秒未保持全身。后续逐帧核实发现原上传素材自身第3帧（0.05秒）即从居中跳至右缘；准备和16fps采样保留了该变化，原生输入第1–9帧本就靠右。生成没有保住源第0帧的居中姿态，不能把整段开头都归为模型无依据漂移，也未宣称该首帧问题已修复；手指、嘴型及目标音色仍待审阅。

正式包的 `qa-startup-diagnostic-20261021/startup-diagnostic.json`、`opening-graph-audit.md`和`attempt-2-recovery-visual-observations.json`保存来源核对、源码边界和抽帧观察。未发现首帧误删、首pose偏移或横向错裁；后续context窗口参考槽差异只属于待验证假设，没有据此更改工作流或重新推理。

## 验收边界

原试听被用户明确拒绝后，`data/t23-seedvc-timbre-comparison-20261021/attempt-1/`新增B50步/CFG0.7、C30步/CFG1.0、D30步/CFG0.7+同参考前10秒三组正式后端转换，分别耗时60.622/47.092/40.125秒。A复用原正式输出。三次GPU调用串行成功；第一次CPU试听处理失败记录保留，另在`cpu-recovery-1/`以0新增模型调用恢复。8份48kHz、480000采样点的干人声与共同伴奏混音已核对，清单为`comparison-completed-manifest.json`，根代理另存`listening-root-audit-1.json`；主所选声音模型行及受保护输入未变。旧音色保持rejected，新版本均未审且quality=false，参数变化不能证明更像。完整参数、配平方式和限制见S03。

用户随后明确将`f73cb0ee655e4a61843e2f5c0fe742c7`及`f27597179f574ac6a93aad8d70ba50d1`作为歌曲输入，沿用目标声音704146bb。`data/t24-requested-song-comparison-20261021/`已完成两源各0–10秒的B/C/D六份正式换声，以及原唱/B/C/D顺序的两段40秒原画对照片。首次源2B被实验资源监控中止，失败清单和部分输出保留；新attempt-2只重试源2并合并源1，合计7次原生推理调用、6次正式成功。最终balanced混音伴奏采用原唱静态人声增益，各源四组共用伴奏与master，避免旧试听中伴奏相对人声过大的处理比例。视频完整解码、音视频40秒/1200帧、PCM拼接顺序及哈希通过；未改主库声音选择或审批。对照片循环原画，没有新生成人物/动作/场景，不能用40秒比较视频冒充长片完成；音色仍待用户判断。

S01完整10秒效果、S04出画问题和手指/转身/遮挡完整检查、S06主项目队列端到端及新完整视频预览的桌面/手机流程、S07长片执行与质量验收均未完成；S03现有音色已被用户拒绝，需要调整并再次试听。只读规划不改变这些状态。设备有 RTX5070Ti Laptop 12GB；主虚拟环境保留 CPU Torch。独立 ComfyUI 和 SeedVC CUDA 环境已建立，四个Wan官方模型均通过完整SHA-256。历史1.35秒Wan短片耗时545.935秒，其系统RAM峰值33.01GB、整卡GPU峰值11.43GB、系统commit峰值56.90GB、最低剩余commit304MB仅属于该次短片。完整10秒原生执行与资源记录另见正式包的`attempt-1-resource-guard.json`及`.jsonl`，不混用两次运行峰值，也不将差异归因于单一参数。独立真实10秒样片未改变主数据库的Version或审阅状态。总计划保持未完成状态。

用户最新报告的导演失败、模型选择和任务模型显示优先修复，记录见 [T19](../T19-director-routing.md)。

图像网关与任务复制的后续修复见 [T20](../T20-image-gateway-and-task-copy.md)。历史完整质量门为1276项Python通过、3项跳过，前端42项通过，日志`data/t22-gateway-20261021/check-final.log`，退出码0。MP4时长误判修复后的上一轮全量质量门为1298项Python通过、3项跳过、前端42项通过，Ruff、格式、mypy、构建及21个JavaScript模块语法检查均通过；日志为`data/t22-gateway-20261021/check-container-duration-final.log`，`check-container-duration-final-completion.json`记录12:31:13 UTC观察到退出码0。本轮新增视频审批/分段规划的针对性测试见上，后续全量集成与UI结果另行追加，不覆盖这些历史实测。测试不调用外部模型。

正式动作恢复路径不重复提交已有prompt；已发生的付费尝试仍保留账本。停止并重载主8000进程曾被自动审批策略拒绝（仅返回`blocked by policy`），新代码的浏览器验收使用8768独立数据库服务，不能把它视作主服务已重载。8768曾为完整10秒Wan验证释放资源而停止，本轮重新启动完成新页面/接口检查后又关闭；历史readiness、真实探测和浏览器截图保留各自观察记录，不以诊断脚本的新检查时间冒充新浏览器验收。用户说明没有原启动终端，已只读查明后台主服务没有热重载入口。手动恢复方案现已保存到`data/t22-gateway-20261021/manual-restart-main.ps1`及同目录`manual-main-restart.md`，由用户在新PowerShell本人执行；Agent没有执行脚本，包括默认检查路径，也没有重试被拒绝的等效终止操作。

用户第一次手动执行已停止三个空闲受管worker，但旧检查误将同次启动的系统`conhost.exe`视作额外子进程而中止；主8000未被终止，失败日志`manual-main-restart-20261021-094437-293.log`保留。只读确认控制台宿主的系统路径、微软签名、启动时间、无子进程及监听后，脚本增加严格附属进程识别和冻结身份复核；仍只终止原先已验证并持有句柄的main/launcher，不终止控制台宿主。修订已通过两次Parser及独立静态复审，尚待用户再次执行和后续只读发布核验，不能视为主服务已经更新。

之后用户本人第二次执行成功，13:49 UTC启动主服务PID66636，新PowerShell保持前台运行。只读核验`data/t22-gateway-20261021/main-after-manual-restart-20261021.json`（SHA-256 `81d31ff6ecf62277b6e38a1dcd69d3e0a59fcdc073e75e4d4d9f8998beb7c12d`）确认主8000健康、三个managed worker运行且空闲、全局queued/running为空；PATCH、video-preview、segment-plan入口已存在，当前导演仍为`openai/gpt-6-astra`。质量门记录中的8个相关源码文件哈希未变。Agent未执行终止/重启、未提交模型任务或审批；路由检查不能单独证明全部加载源码，主项目唱跳队列端到端与质量验收仍未完成。第二次前台日志持续写入，不将其当前哈希作为不可变证据。

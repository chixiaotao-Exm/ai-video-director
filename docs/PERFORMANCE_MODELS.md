# 唱跳视频：模型与算力选择

核查日期：**2026-10-20**。这是面向“原创歌曲 + 自己的人物唱跳”的选型建议；没有调用付费生成服务、下载权重或实测 GPU。2027 是界面设计方向，不是已发生的模型评测日期。

## 现在选哪套

**第一支样片选 API：Eleven Music v2.5 做歌曲，Kling VIDEO 3.0 Motion Control 做全身舞蹈，Kling Avatar 2.0 做唱歌近景。** 先做 15–30 秒副歌，把全身舞蹈与面部近景交替剪辑。我建议先验证人物一致性、节拍、歌词和口型，再扩展整首 MV；这是制作建议，不是保证一次生成成功的基准结论。

- Eleven 的正式 Music API 支持 `music_v2_5`，付费订阅可用，可生成歌声与伴奏；这不是普通 TTS。[E1][E2][E3]
- Kling 动作控制输入是人物图和参考动作视频。官方已提供 3.0 的 API；动作参考与人物朝向等参数影响支持长度，首轮只用 5–10 秒干净的单人舞蹈。[K1][K2]
- Avatar 2.0 官方展示了歌唱场景，适合评估歌唱近景。它不承诺复现指定全身编舞；也不能把动作控制输出直接等同于精准歌词口型。[K3]
- 固定为“我自己的歌声”时，最稳的初始输入是自己演唱、已获授权的干声，另配伴奏。模型生成某种歌手风格不等于精确克隆你的音色。若以后使用歌声音色转换（SVC），需要单独的歌唱数据、模型、音高测试与授权；`tts.json` 里的普通配音不能替代它。

**不急着购买 GPU。** 已有显卡先承担音乐草稿、编辑和局部口型；确实需要全部本地运行时，先租 48 GB 单卡试验 Wan Dancer，再根据实测显存、耗时和每月使用量决定购买。48 GB 是本项目的试验档位，**不是官方已公布的最低配置或运行保证**。[W1]

## 三条可选路线

| 路线 | 具体组合 | 适合谁 | 主要限制 |
| --- | --- | --- | --- |
| API 优先（首选） | Eleven Music v2.5 → Kling 3.0 Motion Control / Avatar 2.0 → 本地 FFmpeg | 先做出可看的原创唱跳样片 | 需要官方账户、预算和服务可用性；本仓库还没有这些 API 的生成适配器 |
| 已有 12 GB 显卡 | ACE-Step 1.5 2B turbo + 0.6B LM → Kling 动作控制 → MuseTalk 1.5 近景试验 | 反复改音乐，重视频交给 API | MuseTalk 歌唱质量需试听/逐帧验收；12 GB 不作为 Wan Dancer 完整流程的保证 |
| 24 / 48 GB 本地路线 | ACE-Step 1.5 XL → Wan Dancer → MuseTalk 1.5；按需评估 LatentSync 1.6 | 希望保留本地生成能力、愿意维护环境 | 24 GB 先用于音乐/口型，舞蹈先租 48 GB 评估；工作流、依赖、峰值内存均需实际验证 |

这些方案是**选型资料**。在浏览器选择方案不会购买、下载、调用服务，也不会偷偷改变项目模型或预算。

## 模型取舍与授权

| 模型 | 建议与证据 | 使用边界 |
| --- | --- | --- |
| **Eleven Music v2.5** | API 音乐首选，支持歌词/段落控制、音频参考和局部重做。[E1][E2][E3] | 参考音频用于引导风格，不保证指定歌手身份。商用范围取决于订阅和音乐条款；广告、发行等用途按对应权利表核对。[E4] |
| **ACE-Step 1.5（2B / XL）** | 本地音乐首选。上游已有 XL 4B；2B 更适合现有小显存显卡。XL 官方要求至少 12 GB（offload），推荐至少 20 GB；24 GB 档可优先试 XL。[A1] | 项目与所核查 XL turbo 权重为 MIT。音色定制与生成质量仍需样片验证；授权不能覆盖未获许可的输入歌词/录音。[A1][A2] |
| **Kling VIDEO 3.0 Motion Control** | 有可核查的正式 API，适合人物图 + 已授权舞蹈参考。[K1][K2] | 复杂快速动作可能失败或缩短；手、脚、遮挡、旋转仍需检查。不把普通 I2V 或原生音频称为精确编舞/歌唱保证。 |
| **Kling Avatar 2.0** | 作为唱歌近景候选，官方有歌曲表演案例；API 更新记录也包含 Avatar 升级。[K2][K3] | 近景唱歌与全身编舞分别验收。不能依赖厂商自评排名作跨模型质量保证。 |
| **Wan Dancer** | 本地舞蹈首选试验项；Comfy 官方提供人物图 + 音频原生模板，采用 Wan2.2 架构，全局/局部双模型，Apache-2.0。[W1] | 官方示例默认 5 秒/149 帧，只说明需要较多显存，未给明确最低 GB。不能推断 12/24/48 GB 一定可跑；主模型之外的辅助权重逐一核验。它根据音乐生成舞蹈，不保证复制指定编舞。 |
| **Wan2.2 Animate-14B** | 有参考舞蹈视频、想迁移动作时的本地备选；官方提供 animation / replacement 模式。[W2][W3] | 动作和人脸表现转移不等于歌词驱动口型；其显存要求不能套用 TI2V-5B 的低显存数据。 |
| **MuseTalk 1.5** | 本地近景口型首选试验项；官方有 4 GB fp16 示例，代码/作者权重允许商用。[M1] | 256² 面部区域；大幅侧脸、遮挡和快速唱词需验证。辅助 VAE、Whisper、姿态/人脸模型各有许可；旧 Comfy 节点不自动兼容 1.5。 |
| **LatentSync 1.6** | 24 GB 档的口型对比候选，官方最低显存 18 GB；1.5 为 8 GB。[L1] | 代码 Apache-2.0，权重 OpenRAIL++；默认依赖中的 InsightFace 预训练权重有非商用研究限制，未解决前不作为商用默认。[L2][L3] |
| **MiniMax Music 3** | 候选保留，但不推荐新用户购买音乐 API 接入。官方明确从 **2026-08-20** 起付费音乐 API 不再开放给新用户，已有付费用户可继续。[N1] | 官方推荐网页产品或开放权重；本轮没有验证其本地完整部署和许可证适用性。不能把旧 Music 2.5 教程当作当前可开通承诺。 |
| **CosyVoice3 / GPT-SoVITS** | 保留给对白和旁白，已有详细比较见 [MODELS.md](MODELS.md)。 | TTS 音色克隆不等于按旋律、音高和时值演唱。不要用它们替代整首歌曲生成或 SVC。 |
| **Suno 与第三方“兼容 API”** | 本轮未核实可以按公开文档直接集成的 Suno 官方 API，不加入自动化默认。 | 官网手工创作与第三方封装是两条路线；名称带 Suno 不代表官方授权、稳定性或商业权利。本次未购买或调用此类服务。 |

## 钱与硬件怎么安排

截至核查日，Eleven 官方 API 价目页列音乐 **US$0.15 / 分钟**；这是公开参考单价，不是整支 MV 的总价，也不代替账户套餐/计费确认。API compose 文档与营销价目页的最长时长描述不同，集成时应按所选模型的实时接口约束处理。[E3][E5]

Kling 官方 3.0 Motion Control 更新记录列标准 **0.9 units / 秒**、专业 **1.2 units / 秒**；这里故意保留原计费单位。网页会员的 credits 与 API units 不应互换，本轮没有核定账户现金兑换价。Avatar、重试、存储、GPU 租价和税费需另计，不能据此编造固定“每条视频多少钱”。[K2]

| 显存预算 | 我建议的用途 | 购买 / 租用决策 |
| --- | --- | --- |
| 12 GB | ACE-Step 1.5 2B + 小 LM；串行做 MuseTalk 试验，舞蹈用 API | 有现成卡就用；不要只为本项目再买一张同档卡 |
| 24 GB | ACE-Step XL；近景口型评测；独立试验动作迁移 | 优先短租实测，再比较本地卡整机成本；不把 24 GB 当所有 14B 视频模型的通行证 |
| 48 GB | Wan Dancer 短片试验与较大工作流，逐项记录峰值显存 | 推荐先租这档验证；是否需要更大卡由实测决定 |

上述档位是显存大小，不是把笔记本与桌面同名 GPU 等同；以实际可用 VRAM 为准。多个 GPU 的显存也不会自动合并。RTX 50 的 PyTorch/CUDA 和自定义节点兼容性沿用 [MODELS.md](MODELS.md) 的单独验证要求。

是否购买，用实测数据计算：`可接受的整机总成本 ÷（月租用时长 × 实时报价 + 存储流量费 − 本地月运行成本）`，只在分母为正时才有收回成本的月份。把维护时间和重试率一起记录；本轮没有获得可复核的具体 GPU 报价，所以不报虚构价格。

## 与当前程序的接线状态

当前代码证据：`src/avd/executors/handlers.py` 只向 Comfy 提供 `image_to_video`、`tts`、`lipsync` 三类模板；`workflows/README.md` 明确仓库 JSON 只是占位契约。它们分别传人物图/提示词、文本/语音样本、视频/音频。**没有歌曲生成、歌声转换、舞蹈动作参考、音乐驱动视频或 Kling/Eleven Music 的成品适配器。** 现有“填 Key”界面不意味着上述服务已经接入。

因此，推荐页应显示“方案待接入 / 可外部试片”，不能把选型按钮变成假装启动生成的按钮。后续真实接入至少需要：

1. 为歌曲与动作参考增加可持久化的输入、配方和任务类型；保持原片风格参考与获授权的表演素材分离。
2. 用独立音轨保存主唱、伴奏和最终混音。人声用于口型，混音用于成片；改歌词应只重建受影响段落。
3. 为官方 API 增加鉴权、异步查询、错误处理、费用预留/入账、重试去重；所有测试使用 fake。
4. 本地模型导出真正可执行的 Comfy API 图，记录节点/权重版本。不要覆盖现有通用配音契约来伪装支持唱歌。
5. 使用现有人物/声音授权门槛，另保存歌词、歌曲、编舞和动作参考的使用权记录。克隆脸/声继续要求 `consent_confirmed=True`，不因“换模型”绕过。人物首帧和歌声试听通过后再扩大预算，成片保留 AI 标识要求。

建议首个验收样片：原创 15–30 秒副歌、同一人物、三段 5–10 秒镜头；包含一个正脸唱歌近景和两个全身舞蹈镜头。检查歌词可辨、节拍落点、音画时长一致、人物稳定、手脚与口型；记录每段重试数、费用、生成耗时和峰值显存。该验收尚未实际执行。

## 一手来源

以下均于 **2026-10-20** 核查。页面会更新，正式付费前按当前账户和所选版本重新确认。

- [E1 — Eleven Music 能力与 v2.5](https://elevenlabs.io/docs/overview/capabilities/music)
- [E2 — Eleven Music API 开放范围](https://elevenlabs.io/docs/eleven-creative/products/music)
- [E3 — Eleven Music compose API](https://elevenlabs.io/docs/api-reference/music/compose)
- [E4 — Eleven Music 商业使用条款](https://elevenlabs.io/music-terms)
- [E5 — Eleven 官方 API 价目](https://elevenlabs.io/pricing/api)
- [A1 — ACE-Step 1.5 官方代码、硬件档位、许可](https://github.com/ace-step/ACE-Step-1.5)
- [A2 — ACE-Step XL turbo 官方权重卡](https://huggingface.co/ACE-Step/acestep-v15-xl-turbo)
- [K1 — Kling 3.0 Motion Control 官方指南](https://kling.ai/quickstart/motion-control-user-guide)
- [K2 — Kling API 版本与计费更新](https://kling.ai/document-api/apiReference/updateNotice?date=2026-02-11)
- [K3 — Kling Avatar 2.0 官方发布与歌唱案例](https://kling.ai/release-note/release-notes/9bbqv0a76z)
- [W1 — Comfy 官方 Wan Dancer 图与要求](https://docs.comfy.org/tutorials/video/wan/wan-dancer)
- [W2 — Wan2.2 官方代码与许可证](https://github.com/Wan-Video/Wan2.2)
- [W3 — Comfy 官方 Wan2.2 Animate](https://docs.comfy.org/tutorials/video/wan/wan2-2-animate)
- [M1 — MuseTalk 官方代码、许可与限制](https://github.com/TMElyralab/MuseTalk)
- [L1 — LatentSync 官方代码与显存要求](https://github.com/bytedance/LatentSync)
- [L2 — LatentSync 1.6 权重卡](https://huggingface.co/ByteDance/LatentSync-1.6)
- [L3 — InsightFace 代码与权重许可区别](https://github.com/deepinsight/insightface#license)
- [N1 — MiniMax 音乐 API 当前开放限制](https://platform.minimax.io/docs/api-reference/music-generation)

# Model choices and deployment evidence

Access date: **2026-10-20**. These are open/open-weight candidates, not a claim that every weight license is permissive or OSI-approved. No model weights were downloaded and no GPU inference benchmark was run. Published requirements, wrapper recommendations and our unmeasured planning estimates are distinguished below.

## Image to video

| Candidate | Memory evidence and tiers | ComfyUI availability | License / commercial use |
| --- | --- | --- | --- |
| **Wan2.2 TI2V-5B** | Native Comfy documentation reports **8GB with offload**; upstream CLI example separately specifies **24GB**. Recommended baseline for the 12GB Comfy offload tier and for 24/48GB. Resolution, frame count, host RAM and latency still matter. | Native support; official T2V/I2V example templates. | Apache-2.0; preferred permissive baseline. [W1][W2] |
| **Wan2.2 I2V-A14B** | Upstream command requires **80GB**. Paired high/low-noise FP8 Comfy weights exist, but a measured 12/24/48GB minimum was not established from the reviewed official page. Avoid on 12GB; treat 24/48GB offload as an **experimental, unmeasured** quality option. | Native paired expert loaders/sampling. | Apache-2.0. Use the Wan2.1 VAE for this variant, not the 5B VAE. [W1][W2] |
| **HunyuanVideo-1.5 (8.3B)** | Published minimum **14GB with offload**: 12GB below reference minimum, 24/48GB evaluation targets. Third-party low-memory routes are not the native baseline. | Upstream documents Comfy support. | Tencent community terms are conditional: territorial restrictions include EU/UK/South Korea, plus attribution/use terms and a 100M-MAU threshold requiring separate permission. Not the unrestricted default. [H1][H2] |
| **LTX-2.5 (22B distilled)** | Published minimum **32GB VRAM**, 32GB RAM, 100GB disk; 12/24GB below that baseline, 48GB an evaluation tier. Quantized artifacts do not prove a lower supported minimum. | Official ComfyUI-LTXVideo with 2.5 I2V examples. | Current LTX-2.x terms dated 2026-08-11 include a $10M revenue threshold and separate-license requirements for directly competing products. This video's application category needs applicability checked. [L1][L2][L3] |

## Voice cloning / TTS — Chinese and English

| Candidate | Memory evidence and tiers | ComfyUI availability | Code / weights and commercial suitability |
| --- | --- | --- | --- |
| **Fun-CosyVoice3-0.5B-2512** | Supports Chinese and English. Node author recommends **8GB+**; no universal upstream inference minimum. Preferred 12GB batch-one evaluation target; 24/48GB provides headroom. | FL-CosyVoice3 explicitly supports CV3. | Apache-2.0 code and official weights. Preferred permissive TTS baseline, with runtime and voice rights validation still required. [C1][C2][C3] |
| **GPT-SoVITS V5** | Current upstream V5 instructions; both languages. No official V5 minimum verified. **12GB batch-one and 8–10GB initial reservation are planning estimates**, not benchmark claims; 24/48GB is safer for evaluation. | AIFSH wrapper exists, but V5 support is **unverified**. Validate a local service or updated node. | MIT code and official weight metadata. Check auxiliary models separately. [G1][G2][G3] |
| **F5-TTS v1 Base** | Chinese/English lineage. No universal minimum in the reviewed upstream material; 12GB batch-one is an **estimate**, 24/48GB offers headroom. | Community F5-TTS node exists. | MIT code, **CC-BY-NC-4.0 official pretrained weights**. Excluded from commercial defaults. [F1][F2][F3] |
| **Fish Speech S2-Pro / newer Drama 3** | Official requirements: **S2-Pro ≥24GB**, **Drama 3 ≥48GB FP8**. Neither is a 12GB baseline. | S2 node available; Drama 3 node support unverified. | S2-Pro weights retain Research License. Current code/Drama 3 use Community License (2026-10-13): revenue and hosted-service restrictions apply. HF metadata and bundled license differ; preserve and read the actual license revision. Not an unrestricted default. [S1][S2][S3][S4][S5] |

## Lip synchronization

| Candidate | Memory evidence and tiers | ComfyUI availability | License / commercial suitability |
| --- | --- | --- | --- |
| **MuseTalk 1.5** | Upstream reports Windows fp16 inference on a **4GB RTX3050Ti**, about five minutes for an eight-second clip. This is evidence for one workload, not universal throughput. 12GB default evaluation candidate; 24/48GB adds headroom. Face crop is 256². | Officially linked wrapper documents legacy `musetalk/pytorch_model.bin`; **1.5 wrapper compatibility is not established**. Export/test an updated workflow before use. | MIT code/card and explicit commercial permission for authors' model. Audit auxiliary components separately. [M1][M2][M3] |
| **LatentSync 1.5 / 1.6** | Published minima **8GB for1.5**, **18GB for1.6 (512²)**; wrapper suggests roughly20GB for1.6. 12GB optional1.5; 24/48GB quality evaluation1.6. | Wrapper explicitly supports1.6. | Apache-2.0 code, OpenRAIL++ weight cards. Stock face detection uses InsightFace pretrained models whose downloads are noncommercial research; the full pipeline is **not commercially cleared by the code license**. [Y1][Y2][Y3][Y4][Y5] |
| **Wav2Lip / GAN** | No official inference minimum verified. Legacy research baseline; no tier is a commercial default. | Community wrapper exists. | Upstream restricts repository/pretrained model/result usage to personal/research/noncommercial use. Wrapper licensing does not override that. [V1][V2] |

## Recommended profiles and manifest

| Tier | Baseline selection | Optional evaluation |
| --- | --- | --- |
| 12GB laptop | Wan2.2 TI2V-5B native offload + CosyVoice3 + MuseTalk1.5 | Validate each node separately; TTS/lipsync compiled dependencies and MuseTalk1.5 wrapper are not certified on this machine. |
| 24GB rental | Same permissive baseline, less offload where supported | Hunyuan1.5 after license review; LatentSync1.6 for research/appropriately licensed dependencies. |
| 48GB rental | Same verified baseline first | Wan A14B FP8 workflow as unmeasured quality upgrade; LTX2.5 only after applicable commercial terms and kernel compatibility are cleared. |

The canonical [models.yaml](../deploy/gpu-server/models.yaml) provides **opt-in** `draft12`, `balanced24`, and `quality48` baseline downloads. The default profile is `none`; select `MODEL_PROFILE=draft12` etc. before setup. The names are VRAM planning tiers, not measured performance guarantees. No weight is downloaded by tests.

The manifest contains 19 entries. All three opt-in profiles select the same baseline; changing a profile does not rewrite workflows or change model recipes in the application. Each entry lists its supported profiles explicitly. Unknown profiles are rejected, and `none` skips downloads even when entries are present.

Entries pin repository revisions and include publisher LFS SHA-256 for every binary weight. Small configuration/tokenizer files are pinned by repository revision without a separate publisher SHA-256. Wan5B's three components total about 18.15GB decimal **on disk**, not VRAM. CosyVoice includes its configuration/tokenizer and core inference files; optional optimized/RL files are omitted. MuseTalk entries are its main 1.5 model/config only: follow upstream for VAE, Whisper, DWPose, face parsing and their licenses. These downloads are **not a complete runnable ComfyUI workflow**. The chosen node's exact directory expectations may require moving/configuring a model path.

| Download repository | Pinned revision |
| --- | --- |
| Comfy-Org/Wan_2.2_ComfyUI_Repackaged | `ee6f4a40737a995bf5818954cfce6d59443b0f04` |
| FunAudioLLM/Fun-CosyVoice3-0.5B-2512 | `29e01c4e8d000f4bcd70751be16fa94bf3d85a18` |
| TMElyralab/MuseTalk | `2bcb936e2fddb4d86db4c62fd45b387d0c061571` |

## RTX50 / Blackwell compatibility

PyTorch2.7 introduced Blackwell support with CUDA12.8. Current PyTorch guidance directs Blackwell to CUDA13.0+ (driver580.65.06+ Linux /580.88+ Windows). Framework support does not certify each custom CUDA extension. [P1][P2]

- The deployment kit selects configurable cu130. Keep an older pinned cu128 stack only when the selected packages explicitly support it.
- CosyVoice upstream requirements still pin torch2.3.1/cu121; MuseTalk documents torch2.0.1/cu118 and MMCV2.0.1; LatentSync pins torch2.5.1/cu121. Those stock environments do **not** establish RTX50 support. Use isolated, validated modern PyTorch environments and avoid optional acceleration until eager inference works. [C4][M1][Y6]
- GPT-SoVITS includes a2.7/cu128 tested environment; F5 examples include2.8/cu128. This still requires a node-specific smoke test. [G1][F1]
- Current LTX optimization instructions distinguish datacenter B200 from consumer sm120; its recent NATTEN/attention package stack is version-sensitive. Do not install a datacenter-only kernel on the laptop merely because both GPUs are called Blackwell. [L4]

## Sources

All links accessed2026-10-20. Memory facts are attributed to their original publisher or node author, not a third-party benchmark summary.

- [W1: official native Wan2.2 Comfy guide](https://docs.comfy.org/tutorials/video/wan/wan2_2)
- [W2: Wan2.2 upstream code/license](https://github.com/Wan-Video/Wan2.2)
- [W3: pinned-download metadata source](https://huggingface.co/api/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged?blobs=true)
- [H1: HunyuanVideo1.5](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)
- [H2: Hunyuan license](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5/blob/main/LICENSE)
- [L1: LTX2.5 requirements](https://docs.ltx.io/open-source-model/getting-started/system-requirements)
- [L2: official LTX2.5 Comfy workflows](https://github.com/Lightricks/ComfyUI-LTXVideo/blob/master/example_workflows/2.5/README.md)
- [L3: current LTX2.x license](https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x)
- [L4: LTX optimization](https://github.com/Lightricks/LTX-2/blob/main/docs/optimization.md)
- [C1: CosyVoice code/license](https://github.com/QwenAudio/CosyVoice)
- [C2: official CosyVoice3 weights/card](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)
- [C3: FL-CosyVoice3 node requirement](https://github.com/filliptm/ComfyUI_FL-CosyVoice3)
- [C4: CosyVoice requirements](https://github.com/QwenAudio/CosyVoice/blob/main/requirements.txt)
- [G1: GPT-SoVITS current README](https://github.com/RVC-Boss/GPT-SoVITS)
- [G2: official GPT-SoVITS weights](https://huggingface.co/lj1995/GPT-SoVITS)
- [G3: community GPT-SoVITS node](https://github.com/AIFSH/ComfyUI-GPT_SoVITS)
- [F1: F5-TTS code and weight-license statement](https://github.com/SWivid/F5-TTS)
- [F2: F5 official model card](https://huggingface.co/SWivid/F5-TTS)
- [F3: F5 Comfy node](https://github.com/niknah/ComfyUI-F5-TTS)
- [S1: Fish hardware requirements](https://speech.fish.audio/install/)
- [S2: Fish current code license](https://github.com/fishaudio/fish-speech/blob/main/LICENSE)
- [S3: Fish S2-Pro model card](https://huggingface.co/fishaudio/s2-pro)
- [S4: Drama3 bundled license](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE)
- [S5: S2 community node](https://github.com/Saganaki22/ComfyUI-FishAudioS2)
- [M1: MuseTalk README/dependencies/model permission](https://github.com/TMElyralab/MuseTalk)
- [M2: MuseTalk weight card](https://huggingface.co/TMElyralab/MuseTalk)
- [M3: officially linked community node](https://github.com/chaojie/ComfyUI-MuseTalk)
- [Y1: LatentSync requirements](https://github.com/bytedance/LatentSync)
- [Y2: LatentSync1.5 card](https://huggingface.co/ByteDance/LatentSync-1.5)
- [Y3: LatentSync1.6 card](https://huggingface.co/ByteDance/LatentSync-1.6)
- [Y4: InsightFace license policy](https://github.com/deepinsight/insightface#license)
- [Y5: LatentSync wrapper](https://github.com/ShmuelRonen/ComfyUI-LatentSyncWrapper)
- [Y6: LatentSync pinned dependencies](https://github.com/bytedance/LatentSync/blob/main/requirements.txt)
- [V1: Wav2Lip upstream restrictions](https://github.com/Rudrabha/Wav2Lip)
- [V2: Wav2Lip wrapper](https://github.com/ShmuelRonen/ComfyUI_wav2lip)
- [P1: PyTorch2.7 Blackwell announcement](https://pytorch.org/blog/pytorch-2-7/)
- [P2: PyTorch2.12 CUDA guidance](https://pytorch.org/blog/pytorch-2-12-release-blog/)

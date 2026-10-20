# ComfyUI workflow templates

The checked-in JSON files are **placeholder contracts**, not runnable model graphs. On your
ComfyUI server, install the required models and custom nodes, build and verify each workflow,
then export it with **Save (API format)**. Replace the corresponding file with that export and
replace the indicated input values with the placeholders below. Keep the exact placeholder
names so the executors can supply the right inputs.

| Template | Required placeholders | Types |
| --- | --- | --- |
| `image_to_video.json` | `image`, `prompt`, `duration_s`, `seed` | uploaded image name, string, number, integer |
| `tts.json` | `text`, `voice_sample`, `speed`, `emotion` | string, uploaded audio name, number, string |
| `lipsync.json` | `video`, `audio` | uploaded video name, uploaded audio name |

Write placeholders as JSON strings, for example `"seed": "{{seed}}"`. A placeholder that fills
an entire string retains the supplied value type, so a Python integer becomes a JSON number.
An embedded placeholder, such as `"prefix_{{seed}}"`, becomes text. `Path` and `FileRef` values
become POSIX path strings. Missing and unused values both raise `WorkflowTemplateError`.
File inputs must use the server-side names returned by `ComfyUIClient.upload_file`; local
absolute paths on the laptop are not visible to a remote ComfyUI server.

```python
from avd.backends import ComfyUIClient, load_workflow
from avd.config import Settings

settings = Settings()
profile = settings.comfyui_profile
with ComfyUIClient(profile.base_url, profile.timeout) as client:
    image_name = client.upload_file("frame.png")
    workflow = load_workflow(
        "image_to_video",
        {
            "image": image_name,
            "prompt": "Slow camera push toward the character",
            "duration_s": 3.0,
            "seed": 42,
        },
    )
    prompt_id = client.submit(workflow)
    client.wait(prompt_id, timeout=profile.timeout)
    output_files = client.download_outputs(prompt_id, "data/example-output")
```

Choose the server using `AVD_COMFYUI_ACTIVE_PROFILE=local` or `remote`. Set profiles with
`AVD_COMFYUI_PROFILES` as JSON, or use nested environment variables such as
`AVD_COMFYUI_PROFILES__REMOTE__BASE_URL=http://127.0.0.1:8189`. A rented server should remain
bound to loopback behind an SSH tunnel. Profile defaults both use loopback; choosing `remote`
does not expose or automatically provision a server.

## Recommended starting models (checked October 20, 2026)

Use the same baseline for the 12 GB laptop and initial 24 / 48 GB rental evaluation. Larger
cards provide room to reduce offload; these are planning recommendations, not measured
performance guarantees. Candidate comparisons, licenses and primary-source links are in
[`docs/MODELS.md`](../docs/MODELS.md).

| Workflow | Recommended baseline | Required integration work |
| --- | --- | --- |
| `image_to_video.json` | Native Wan2.2 TI2V-5B; `wan2.2_ti2v_5B_fp16.safetensors`, `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan2.2_vae.safetensors` | Start from the official native Comfy I2V graph, use offload on 12 GB, and connect uploaded image/prompt/seed. Convert `duration_s` into a valid frame count at the graph's output frame rate. |
| `tts.json` | Fun-CosyVoice3-0.5B-2512; FL-CosyVoice3 node | Configure the downloaded model directory, wire the uploaded reference sample and text, and expose speed/emotion through nodes or a supported instruction mode. Verify Chinese and English output. Upstream's older pinned PyTorch stack is not a certified RTX 50 environment. |
| `lipsync.json` | MuseTalk 1.5 evaluation baseline | The linked community wrapper documents an older checkpoint: verify or adapt 1.5 support before exporting. Install upstream VAE, Whisper, DWPose and face-parsing auxiliaries separately and review each license. The manifest's main weights alone are insufficient. |

The [GPU manifest](../deploy/gpu-server/models.yaml) pins these baseline components and
provides publisher weight checksums. GPU setup defaults to `MODEL_PROFILE=none`; opt into
`draft12`, `balanced24`, or `quality48` to download them. `MODEL_PROFILE` controls downloads
only; it does not select nodes, rewrite workflows, or change application model recipes.
`CUSTOM_NODES` also defaults to empty. Review node dependencies and compatibility before
adding their repository URLs; installing an old node can replace CUDA/PyTorch packages.

After connecting all required placeholders, run each exported graph directly in ComfyUI
with an authorized sample and confirm its saved output format. Video and lip-sync outputs
must contain video; TTS must save audio. Retain the node/model revisions with the exported
graph and point `AVD_WORKFLOW_DIR` at that directory. Downloading a profile does not make
the shipped placeholder graphs executable.

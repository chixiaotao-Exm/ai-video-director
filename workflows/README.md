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
    workflow = load_workflow("image_to_video", {
        "image": image_name,
        "prompt": "Slow camera push toward the character",
        "duration_s": 3.0,
        "seed": 42,
    })
    prompt_id = client.submit(workflow)
    client.wait(prompt_id, timeout=profile.timeout)
    output_files = client.download_outputs(prompt_id, "data/example-output")
```

Choose the server using `AVD_COMFYUI_ACTIVE_PROFILE=local` or `remote`. Set profiles with
`AVD_COMFYUI_PROFILES` as JSON, or use nested environment variables such as
`AVD_COMFYUI_PROFILES__REMOTE__BASE_URL=http://127.0.0.1:8189`. A rented server should remain
bound to loopback behind an SSH tunnel. Profile defaults both use loopback; choosing `remote`
does not expose or automatically provision a server.

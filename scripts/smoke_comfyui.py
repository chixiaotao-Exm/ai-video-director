"""Manual smoke: AVD_COMFYUI_URL and AVD_SMOKE_WORKFLOW must be explicitly set."""

import json
import os
from pathlib import Path

from avd.backends.comfyui import ComfyUIClient


def main() -> None:
    url = os.environ.get("AVD_COMFYUI_URL")
    workflow_path = os.environ.get("AVD_SMOKE_WORKFLOW")
    if not url or not workflow_path:
        print("Skipped: set AVD_COMFYUI_URL and AVD_SMOKE_WORKFLOW (exported API JSON).")
        return
    workflow = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    with ComfyUIClient(url, timeout=60) as client:
        prompt_id = client.submit(workflow)
        client.wait(prompt_id, poll_interval=1, timeout=600)
        output_dir = Path(os.environ.get("AVD_DATA_DIR", "data")) / "smoke" / prompt_id
        print(client.download_outputs(prompt_id, output_dir))


if __name__ == "__main__":
    main()

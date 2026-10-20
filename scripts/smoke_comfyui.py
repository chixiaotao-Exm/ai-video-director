"""Manual smoke: AVD_COMFYUI_URL and AVD_SMOKE_WORKFLOW must be explicitly set."""

import json
from pathlib import Path

from avd.backends.comfyui import ComfyUIClient
from avd.config import Settings


def main() -> None:
    settings = Settings()
    url = settings.comfyui_url
    workflow_path = settings.smoke_workflow
    if not url or not workflow_path:
        print("Skipped: set AVD_COMFYUI_URL and AVD_SMOKE_WORKFLOW (exported API JSON).")
        return
    workflow = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    with ComfyUIClient(url, timeout=60) as client:
        prompt_id = client.submit(workflow)
        client.wait(prompt_id, poll_interval=1, timeout=600)
        output_dir = settings.data_dir / "smoke" / prompt_id
        outputs = client.download_outputs(prompt_id, output_dir)
        if not outputs:
            raise RuntimeError("Workflow finished without output artifacts")
        print(outputs)


if __name__ == "__main__":
    main()

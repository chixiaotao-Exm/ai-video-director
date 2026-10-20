"""Explicit manual director turn using an existing project and configured key."""

import argparse

from avd.config import Settings
from avd.director import DirectorAgent
from avd.runtime import Runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project")
    parser.add_argument("--message", default="Summarize the current project status.")
    args = parser.parse_args()
    settings = Settings()
    provider = settings.llm_route("director").provider
    if not args.project or not getattr(settings, f"{provider}_api_key", None):
        print("Skipped: provide --project and the director provider API key.")
        return
    runtime = Runtime(settings)
    try:
        print(DirectorAgent(runtime).chat(args.project, args.message))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()

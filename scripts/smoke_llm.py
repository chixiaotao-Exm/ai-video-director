"""Explicit paid-service smoke check; exits normally when credentials are absent."""

from __future__ import annotations

import argparse

from avd.config import Settings
from avd.llm import CostLedger, LLMRouter
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.repo import ProjectRepo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", choices=("director", "script", "qa", "analysis"), default="script"
    )
    parser.add_argument("--budget", type=float, default=1.0)
    args = parser.parse_args()
    settings = Settings()
    route = settings.llm_route(args.role)
    if not route.model:
        print("Skipped: set AVD_GEMINI_MODEL before the analysis smoke check.")
        return 0
    key = getattr(settings, f"{route.provider}_api_key", None)
    if not key:
        print(f"Skipped: set AVD_{route.provider.upper()}_API_KEY before this live smoke check.")
        return 0
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url)
    init_db(engine)
    with create_session_factory(engine)() as session:
        project = ProjectRepo(session).create_project("LLM manual smoke", budget_limit=args.budget)
        router = LLMRouter(
            settings, ledger=CostLedger(session, settings.model_prices), project=project
        )
        try:
            response = router.chat(
                args.role, [{"role": "user", "content": "Reply with exactly: smoke OK"}]
            )
        finally:
            # Persist any completed billed responses even if later validation fails.
            session.commit()
        print(response.content)
        print(f"project={project.id}; estimated spend=${project.spent:.6f}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

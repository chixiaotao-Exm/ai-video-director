"""Model selection is local information and does not advertise unimplemented execution."""

import json
from pathlib import Path


def test_performance_catalog_has_actionable_presets_and_cited_sources() -> None:
    root = Path(__file__).resolve().parents[2]
    data = json.loads(
        (root / "src/avd/api/static/performance-models.json").read_text(encoding="utf-8")
    )
    assert data["updated_at"]
    assert len(data["presets"]) >= 3
    assert len({preset["id"] for preset in data["presets"]}) == len(data["presets"])
    assert {preset["mode"] for preset in data["presets"]} == {"api", "local", "hybrid"}
    for preset in data["presets"]:
        assert preset["hardware"] and preset["next_steps"] and preset["stages"]
        for stage in preset["stages"]:
            assert stage["model"] and stage["limitations"]
            assert stage["source_url"].startswith("https://")
    assert len(data["sources"]) >= 10
    assert all(source["url"].startswith("https://") for source in data["sources"])

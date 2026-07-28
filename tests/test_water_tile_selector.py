from __future__ import annotations

from forecaster.water_tile_selector import (
    CDSECredentialSet,
    TileConfig,
    WaterTileSelector,
)


def test_water_statistics_tries_every_credential_before_exhausting_retries(
    tmp_path,
    monkeypatch,
):
    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def json(self):
            return {"data": []}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"Unexpected HTTP failure: {self.status_code}")

    responses = iter(
        [Response(403), Response(403), Response(403), Response(403), Response(200)]
    )
    monkeypatch.setattr(
        "forecaster.water_tile_selector.requests.post",
        lambda *args, **kwargs: next(responses),
    )

    selector = WaterTileSelector(
        geojson_path=tmp_path / "tiles.geojson",
        cache_path=tmp_path / "water.json",
        water_check_interval=("2026-06-01", "2026-06-30"),
    )
    selector._credential_sets_cache = [
        CDSECredentialSet(label, f"{label}-id", f"{label}-secret")
        for label in ("primary", "backup", "backup_2", "backup_3", "backup_4")
    ]
    monkeypatch.setattr(
        selector,
        "_get_access_token",
        lambda: f"token-{selector._credential_index}",
    )

    scenes = selector._query_tile(
        TileConfig("tile_0", [22.0, 38.0, 22.1, 38.1])
    )

    assert scenes == []
    assert selector._credential_index == 4

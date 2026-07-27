"""Deterministic defaults for offline tests.

The public test suite must never read a developer's real storage settings from
``.env``.  Tests that exercise storage explicitly override these values with
``monkeypatch``.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _offline_storage_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERRA_STORAGE_ENABLED", "false")
    monkeypatch.setenv("TERRA_AOI_ID", "test-aoi")
    for name in (
        "MONGO_URI",
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_BUCKET_NAME",
        "MINIO_VERIFY_TLS",
        "MINIO_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)
    # Keep the fixture independent of a user's local .env file.  load_dotenv
    # does not overwrite these explicit environment values.
    os.environ["TERRA_STORAGE_ENABLED"] = "false"

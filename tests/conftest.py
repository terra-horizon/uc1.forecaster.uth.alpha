"""Deterministic defaults for offline tests.

The public test suite must never read a developer's real storage settings from
``.env``.  Tests that exercise storage explicitly override these values with
``monkeypatch``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_storage_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TERRA_AOI_ID",
        "MONGO_URI",
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_BUCKET_NAME",
        "MINIO_VERIFY_TLS",
        "MINIO_CA_BUNDLE",
        "CDSE_CLIENT_ID",
        "CDSE_CLIENT_SECRET",
        "CDSE_BACKUP_CLIENT_ID",
        "CDSE_BACKUP_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    for index in range(2, 10):
        monkeypatch.delenv(f"CDSE_BACKUP_{index}_CLIENT_ID", raising=False)
        monkeypatch.delenv(f"CDSE_BACKUP_{index}_CLIENT_SECRET", raising=False)

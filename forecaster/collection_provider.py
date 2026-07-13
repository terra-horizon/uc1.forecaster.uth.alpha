from __future__ import annotations

from typing import Protocol

from data_collection_bootstrap import ensure_data_collection_importable

ensure_data_collection_importable()

from data_collection import CollectionRequest, CollectionResult, CollectionService  # noqa: E402


class CollectionProvider(Protocol):
    def collect(self, request: CollectionRequest) -> CollectionResult: ...


class LocalCollectionProvider:
    def __init__(self, service: CollectionService | None = None):
        self.service = service or CollectionService()

    def collect(self, request: CollectionRequest) -> CollectionResult:
        return self.service.collect(request)


__all__ = ["CollectionProvider", "CollectionRequest", "CollectionResult", "LocalCollectionProvider"]

from data_collection_bootstrap import ensure_data_collection_importable

ensure_data_collection_importable()

from data_collection import credentials as _credentials  # noqa: E402

_load_local_env_if_present = _credentials._load_local_env_if_present


def get_credential_sets():
    _credentials._load_local_env_if_present = _load_local_env_if_present
    return _credentials.get_credential_sets()

import os
from pathlib import Path

from dotenv import load_dotenv

_LOCAL_ENV_LOADED = False

def _load_local_env_if_present() -> None:
    global _LOCAL_ENV_LOADED
    if _LOCAL_ENV_LOADED:
        return
    _LOCAL_ENV_LOADED = True
    
    config_dir = Path(__file__).resolve().parent
    repo_root = config_dir.parent
    
    # Prioritize loading from repo root .env, and override existing env vars 
    # to ensure .env is the single source of truth.
    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)


def _get_required_env(var_name: str) -> str:
    _load_local_env_if_present()
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(
            f"Environment variable '{var_name}' is required but not set. "
            "Configure it before running the Copernicus data collection pipeline."
        )
    return value


def _get_optional_env(*var_names: str) -> str | None:
    _load_local_env_if_present()
    for var_name in var_names:
        value = os.getenv(var_name)
        if value:
            return value
    return None


def get_credential_sets() -> list[dict[str, str]]:
    client_id = _get_required_env("CDSE_CLIENT_ID")
    client_secret = _get_required_env("CDSE_CLIENT_SECRET")
    credentials = [
        {
            "label": "primary",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ]
    
    # Check for primary backup (CDSE_BACKUP_CLIENT_ID)
    bk_id = _get_optional_env("CDSE_BACKUP_CLIENT_ID", "CDSE_FALLBACK_CLIENT_ID")
    bk_sec = _get_optional_env("CDSE_BACKUP_CLIENT_SECRET", "CDSE_FALLBACK_CLIENT_SECRET")
    if bk_id and bk_sec:
        credentials.append({
            "label": "backup",
            "client_id": bk_id,
            "client_secret": bk_sec,
        })
        
    # Dynamically check for multiple backups (e.g. CDSE_BACKUP_2_CLIENT_ID)
    for i in range(2, 10):
        bk_id = os.getenv(f"CDSE_BACKUP_{i}_CLIENT_ID")
        bk_sec = os.getenv(f"CDSE_BACKUP_{i}_CLIENT_SECRET")
        if bk_id and bk_sec:
            credentials.append({
                "label": f"backup_{i}",
                "client_id": bk_id,
                "client_secret": bk_sec,
            })
            
    return credentials

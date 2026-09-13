from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet

from .config_store import load_config, save_config

logger = logging.getLogger("meesman")
_warned_mismatch = False


def env_master_key() -> str:
    return (os.environ.get("MASTER_KEY") or "").strip()


def master_key_source() -> str:
    """'env' (omgevingsvariabele), 'config' (config.yaml) of 'none'."""
    if env_master_key():
        return "env"
    if (load_config().get("master_key") or "").strip():
        return "config"
    return "none"


def get_or_create_master_key(create: bool = False) -> str:
    """
    De master key komt bij voorkeur uit de omgevingsvariabele MASTER_KEY
    (staat dan niet naast de versleutelde waarden in config.yaml). Ontbreekt
    die, dan uit config.yaml; met create=True wordt daar een nieuwe aangemaakt.
    """
    global _warned_mismatch
    env_key = env_master_key()
    cfg = load_config()
    cfg_key = (cfg.get("master_key") or "").strip()

    if env_key:
        if cfg_key and cfg_key != env_key and not _warned_mismatch:
            logger.warning("MASTER_KEY (env) verschilt van master_key in config.yaml — "
                           "de env-waarde wordt gebruikt; bestaande geheimen zijn dan "
                           "mogelijk niet te ontsleutelen.")
            _warned_mismatch = True
        return env_key

    if cfg_key:
        return cfg_key

    if not create:
        raise RuntimeError("No master key. Set MASTER_KEY or generate one via the UI button.")

    new_key = Fernet.generate_key().decode("utf-8")
    cfg["master_key"] = new_key
    save_config(cfg)
    return new_key


def get_fernet() -> Fernet:
    key = get_or_create_master_key(create=False)
    return Fernet(key.encode("utf-8"))


def encrypt_str(s: str) -> str:
    return get_fernet().encrypt(s.encode("utf-8")).decode("utf-8")


def decrypt_str(s: str) -> str:
    return get_fernet().decrypt(s.encode("utf-8")).decode("utf-8")

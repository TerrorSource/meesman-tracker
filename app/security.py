from __future__ import annotations
from cryptography.fernet import Fernet
from .config_store import load_config, save_config


def get_or_create_master_key(create: bool = False) -> str:
    cfg = load_config()
    key = (cfg.get("master_key") or "").strip()
    if key:
        return key

    if not create:
        raise RuntimeError("No master_key in config.yaml. Generate one via the UI button.")

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

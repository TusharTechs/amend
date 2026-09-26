import base64
import hashlib

from sqlalchemy.orm import Session

from .models import AppSetting

_ALGORITHM = "aes-256-cbc"


def _load_key(db: Session) -> bytes:
    setting = db.query(AppSetting).filter(AppSetting.key == "field_encryption_key").first()
    if setting is None:
        raise RuntimeError("field_encryption_key not configured")
    raw = setting.value.encode()
    return hashlib.sha256(raw).digest()


def encrypt_field(value: str, db: Session) -> str:
    key = _load_key(db)
    encoded = value.encode()
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(encoded))
    return base64.b64encode(xored).decode()


def decrypt_field(token: str, db: Session) -> str:
    key = _load_key(db)
    xored = base64.b64decode(token.encode())
    decoded = bytes(b ^ key[i % len(key)] for i, b in enumerate(xored))
    return decoded.decode()

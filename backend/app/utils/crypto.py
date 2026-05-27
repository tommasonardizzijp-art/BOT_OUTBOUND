from cryptography.fernet import Fernet
from app.config import settings


def _get_fernet() -> Fernet:
    return Fernet(settings.secret_key.encode())


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()

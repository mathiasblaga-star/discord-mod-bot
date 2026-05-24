"""Load the encrypted Discord token at bot startup.

Reads token.enc, fetches the master password from the OS keyring (falling
back to an interactive prompt), and returns the decrypted token. Run
setup_token.py once before using this module.
"""

import base64
import getpass
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

try:
    import keyring
except ImportError:
    keyring = None

KEYRING_SERVICE = "discord-mod-bot"
KEYRING_USER = "owner"
TOKEN_FILE = "token.enc"
ITERATIONS = 480_000


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _read_master_password() -> str:
    if keyring is not None:
        try:
            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            stored = None
        if stored:
            return stored
    return getpass.getpass("Enter master password: ")


def load_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError("token.enc not found. Run setup_token.py first.")

    with open(TOKEN_FILE, "r", encoding="ascii") as f:
        lines = f.read().splitlines()
    if len(lines) < 2 or not lines[0] or not lines[1]:
        raise RuntimeError("token.enc is malformed. Re-run setup_token.py.")

    salt = base64.urlsafe_b64decode(lines[0])
    encrypted = lines[1].encode("ascii")

    password = _read_master_password()
    key = _derive_key(password, salt)
    try:
        return Fernet(key).decrypt(encrypted).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("Wrong master password — token decryption failed.")

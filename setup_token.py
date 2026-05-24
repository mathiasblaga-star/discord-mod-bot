"""One-time setup: encrypt the Discord bot token with a master password.

Run this once after creating your bot. The plaintext token is never written
to disk — only an encrypted blob (token.enc) protected by a password-derived
Fernet key.
"""

import base64
import getpass
import os
import sys

from cryptography.fernet import Fernet
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
SALT_BYTES = 16


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def main() -> None:
    token = getpass.getpass("Enter your DISCORD_TOKEN: ").strip()
    if not token:
        print("Error: token cannot be empty.")
        sys.exit(1)

    password = getpass.getpass("Create a master password: ")
    confirm = getpass.getpass("Confirm master password: ")
    if password != confirm:
        print("Error: passwords do not match.")
        sys.exit(1)
    if not password:
        print("Error: master password cannot be empty.")
        sys.exit(1)

    salt = os.urandom(SALT_BYTES)
    key = _derive_key(password, salt)
    encrypted = Fernet(key).encrypt(token.encode("utf-8"))

    with open(TOKEN_FILE, "w", encoding="ascii") as f:
        f.write(base64.urlsafe_b64encode(salt).decode("ascii") + "\n")
        f.write(encrypted.decode("ascii") + "\n")

    if keyring is not None:
        answer = input("Save master password to OS keyring? (y/n): ").strip().lower()
        if answer in ("y", "yes"):
            try:
                keyring.set_password(KEYRING_SERVICE, KEYRING_USER, password)
                print("Master password saved to OS keyring.")
            except Exception as e:
                print(f"Could not save to keyring: {e}")

    print("Token encrypted and saved to token.enc. "
          "You can now delete DISCORD_TOKEN from your .env file.")


if __name__ == "__main__":
    main()

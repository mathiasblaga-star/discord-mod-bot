"""
Token & Encryption Security Tests
==================================
Tests token_store.py and setup_token.py (encryption layer from Prompt 2).
Verifies: encryption works, wrong password fails, token never touches disk
in plaintext, and the encrypted file cannot be trivially decoded.
"""

import base64
import os
import sys
import pytest

# conftest.py handles sys.path and DB patching before this runs.


# ── Fernet / PBKDF2 round-trip ───────────────────────────────────────────────

class TestEncryptionRoundTrip:
    """Directly test the cryptographic primitives used by setup_token.py."""

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        import base64
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
        return base64.urlsafe_b64encode(kdf.derive(password.encode()))

    def _encrypt(self, token: str, password: str, salt: bytes) -> bytes:
        from cryptography.fernet import Fernet
        key = self._derive_key(password, salt)
        f = Fernet(key)
        return f.encrypt(token.encode())

    def _decrypt(self, encrypted: bytes, password: str, salt: bytes) -> str:
        from cryptography.fernet import Fernet
        key = self._derive_key(password, salt)
        f = Fernet(key)
        return f.decrypt(encrypted).decode()

    def test_correct_password_decrypts(self):
        salt = os.urandom(16)
        token = "MTA0MDAxMjM0NTY3ODkwMTIz.fake.token_for_testing_only"
        password = "my_super_secret_password_123"
        encrypted = self._encrypt(token, password, salt)
        result = self._decrypt(encrypted, password, salt)
        assert result == token

    def test_wrong_password_fails(self):
        from cryptography.fernet import InvalidToken
        salt = os.urandom(16)
        token = "MTA0MDAxMjM0NTY3ODkwMTIz.fake.token"
        encrypted = self._encrypt(token, "correct_password", salt)
        with pytest.raises(InvalidToken):
            self._decrypt(encrypted, "wrong_password", salt)

    def test_wrong_salt_fails(self):
        from cryptography.fernet import InvalidToken
        salt1 = os.urandom(16)
        salt2 = os.urandom(16)
        token = "MTA0MDAxMjM0NTY3ODkwMTIz.fake.token"
        encrypted = self._encrypt(token, "password", salt1)
        with pytest.raises(InvalidToken):
            self._decrypt(encrypted, "password", salt2)

    def test_encrypted_bytes_not_plaintext(self):
        """The encrypted blob must not contain the plaintext token."""
        salt = os.urandom(16)
        token = "SUPER_SECRET_TOKEN_DO_NOT_EXPOSE"
        encrypted = self._encrypt(token, "password", salt)
        assert token.encode() not in encrypted, "Token found in plaintext in encrypted blob!"
        assert b"SUPER_SECRET" not in encrypted

    def test_each_encryption_produces_different_ciphertext(self):
        """Fernet uses random IVs — same plaintext must produce different ciphertext."""
        salt = os.urandom(16)
        token = "same_token_every_time"
        enc1 = self._encrypt(token, "password", salt)
        enc2 = self._encrypt(token, "password", salt)
        assert enc1 != enc2, "Deterministic encryption detected — IV not random!"


# ── token.enc file security ──────────────────────────────────────────────────

class TestTokenEncFile:
    """Verify properties of the token.enc file if it exists."""

    ENC_PATH = os.path.join(os.path.dirname(__file__), "..", "token.enc")

    def test_token_enc_exists(self):
        if not os.path.exists(self.ENC_PATH):
            pytest.skip("token.enc not present — run setup_token.py first")

    def test_token_not_in_plaintext_in_enc_file(self):
        if not os.path.exists(self.ENC_PATH):
            pytest.skip("token.enc not present")
        with open(self.ENC_PATH, "rb") as f:
            raw = f.read()
        # Discord tokens contain a dot-separated structure and start with MTI/MTA/OD etc.
        # Check that raw bytes don't contain a typical token pattern
        import re
        token_pattern = rb"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"
        matches = re.findall(token_pattern, raw)
        assert not matches, f"Possible plaintext token found in token.enc: {matches}"

    def test_token_enc_has_two_lines(self):
        if not os.path.exists(self.ENC_PATH):
            pytest.skip("token.enc not present")
        with open(self.ENC_PATH) as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        assert len(lines) == 2, f"token.enc should have exactly 2 lines, got {len(lines)}"

    def test_salt_line_is_valid_base64(self):
        if not os.path.exists(self.ENC_PATH):
            pytest.skip("token.enc not present")
        with open(self.ENC_PATH) as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        salt_b64 = lines[0]
        try:
            salt = base64.urlsafe_b64decode(salt_b64 + "==")
            assert len(salt) == 16, f"Salt should be 16 bytes, got {len(salt)}"
        except Exception as e:
            pytest.fail(f"Salt line is not valid base64: {e}")

    def test_env_file_does_not_contain_token(self):
        """
        After running setup_token.py, .env must NOT contain a live DISCORD_TOKEN.
        FAIL here means your token is stored in plaintext — remove DISCORD_TOKEN
        from .env immediately (the encrypted copy in token.enc is sufficient).
        """
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if not os.path.exists(env_path):
            pytest.skip(".env file not present")
        with open(env_path) as f:
            content = f.read()
        import re
        match = re.search(r"DISCORD_TOKEN\s*=\s*([^\s#]+)", content)
        if not match:
            return  # no DISCORD_TOKEN line at all — safe
        value = match.group(1)
        safe_placeholders = ("", '""', "''", "your_token_here", "REPLACE_ME", "None")
        if value in safe_placeholders:
            return  # empty / placeholder — safe
        # Value looks real — check token pattern (24+ chars . 6 chars . 27+ chars)
        token_pattern = re.compile(
            r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"
        )
        if token_pattern.match(value):
            pytest.fail(
                f"\n\n*** SECURITY ISSUE ***\n"
                f"DISCORD_TOKEN is still stored in plaintext in .env\n"
                f"  Token starts with: {value[:12]}...\n\n"
                f"ACTION REQUIRED:\n"
                f"  1. Run: python setup_token.py  (if you haven't already)\n"
                f"  2. Delete DISCORD_TOKEN= line from .env\n"
                f"  3. The encrypted token in token.enc is used instead\n"
            )


# ── .env and .gitignore hygiene ──────────────────────────────────────────────

class TestFileHygiene:

    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def test_gitignore_excludes_env(self):
        gi_path = os.path.join(self.ROOT, ".gitignore")
        assert os.path.exists(gi_path), ".gitignore not found"
        with open(gi_path) as f:
            content = f.read()
        assert ".env" in content, ".env not listed in .gitignore!"

    def test_gitignore_excludes_token_enc(self):
        gi_path = os.path.join(self.ROOT, ".gitignore")
        if not os.path.exists(gi_path):
            pytest.skip(".gitignore not found")
        with open(gi_path) as f:
            content = f.read()
        assert "token.enc" in content, "token.enc not listed in .gitignore — it will be committed!"

    def test_gitignore_excludes_database(self):
        gi_path = os.path.join(self.ROOT, ".gitignore")
        if not os.path.exists(gi_path):
            pytest.skip(".gitignore not found")
        with open(gi_path) as f:
            content = f.read()
        assert "*.db" in content or "moderation.db" in content or "data/" in content, \
            "Database file not in .gitignore — it may be committed!"

    def test_no_hardcoded_token_in_source(self):
        """Scan all .py files for anything that looks like a Discord token."""
        import re
        token_pattern = re.compile(r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}")
        found = []
        for dirpath, _, filenames in os.walk(self.ROOT):
            if ".venv" in dirpath or "__pycache__" in dirpath:
                continue
            for fname in filenames:
                if fname.endswith(".py"):
                    fpath = os.path.join(dirpath, fname)
                    with open(fpath, errors="ignore") as f:
                        content = f.read()
                    for match in token_pattern.finditer(content):
                        found.append((fpath, match.group()))
        assert not found, f"Possible hardcoded token(s) found: {found}"

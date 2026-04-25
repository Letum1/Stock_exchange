"""
Decrypt a password from the encrypted vault.

Usage (interactive):
    python3 decrypt_password.py

You will be asked to paste:
  1. The base64 ciphertext copied from the vault page.
  2. The path to your private key file (vault_private_key.pem).

Or pass arguments:
    python3 decrypt_password.py <ciphertext_b64> <path/to/vault_private_key.pem>

Requires: pip install cryptography
"""
import base64
import sys
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def decrypt(ciphertext_b64: str, private_key_path: str) -> str:
    with open(private_key_path, "rb") as f:
        priv = serialization.load_pem_private_key(f.read(), password=None)
    blob = base64.b64decode(ciphertext_b64)
    pad = padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=None,
    )
    return priv.decrypt(blob, pad).decode("utf-8")


def main():
    if len(sys.argv) == 3:
        ct, key = sys.argv[1], sys.argv[2]
    else:
        ct  = input("Paste the encrypted blob (base64): ").strip()
        key = input("Path to your vault_private_key.pem: ").strip() or "vault_private_key.pem"
    print()
    print("Password:", decrypt(ct, key))


if __name__ == "__main__":
    main()

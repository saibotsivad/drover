#!/usr/bin/env python3
"""Generate a Drover API key and its SHA-256 hash.

Usage:
    # Generate a random key and its hash:
    python scripts/generate_api_key.py

    # Hash an existing key:
    python scripts/generate_api_key.py --key "my-secret-key"

The hash is the value you set as the DROVER_API_KEY environment variable.
The plain-text key is what callers pass in the Authorization header.
"""

import argparse
import hashlib
import secrets


def hash_key(plain_key: str) -> str:
    return hashlib.sha256(plain_key.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate or hash a Drover API key."
    )
    parser.add_argument(
        "--key",
        default=None,
        help="Plain-text key to hash. If omitted, a random key is generated.",
    )
    args = parser.parse_args()

    plain_key = args.key if args.key else secrets.token_urlsafe(32)
    hashed = hash_key(plain_key)

    print(f"Plain-text key : {plain_key}")
    print(f"SHA-256 hash   : {hashed}")
    print()
    print("Set the hash as your environment variable:")
    print(f'  export DROVER_API_KEY="{hashed}"')
    print()
    print("Pass the plain-text key in API requests:")
    print(f"  curl -H 'Authorization: Bearer {plain_key}' http://localhost:8000/images")


if __name__ == "__main__":
    main()

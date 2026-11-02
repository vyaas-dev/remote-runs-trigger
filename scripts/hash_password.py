#!/usr/bin/env python3
"""Generate a bcrypt hash for ADMIN_PASSWORD_HASH (Coolify env var)."""

from __future__ import annotations

import getpass
import sys

import bcrypt


def main() -> int:
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if not password:
        print("Password must not be empty.", file=sys.stderr)
        return 1
    if len(password.encode("utf-8")) > 72:
        print("Password exceeds bcrypt's 72-byte limit.", file=sys.stderr)
        return 1

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
    print(hashed.decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

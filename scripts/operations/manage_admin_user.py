#!/usr/bin/env python3
"""Bootstrap or reset an Argus administrator without putting passwords in argv."""

from __future__ import annotations

import argparse
import getpass
import time
from pathlib import Path

from argus.auth import AdminAuth
from argus.database import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap an EyeOfSauron admin user")
    parser.add_argument("username")
    parser.add_argument("--display-name", default="Administrator")
    parser.add_argument("--database", type=Path, default=Path("/var/lib/argus/state.db"))
    parser.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    password = (
        args.password_file.read_text(encoding="utf-8").rstrip("\r\n")
        if args.password_file
        else getpass.getpass("New password: ")
    )
    database = Database(args.database)
    try:
        auth = AdminAuth(database)
        username = auth.validate_username(args.username)
        existing = database.get_admin_user_for_auth(username)
        now = int(time.time())
        encoded = auth.hash_password(password)
        if existing is None:
            database.create_admin_user(
                username, args.display_name, encoded, "admin", "local-bootstrap", now
            )
            print(f"created administrator: {username}")
        else:
            database.set_admin_user_password(
                int(existing["id"]), encoded, "local-bootstrap", now
            )
            print(f"reset administrator password: {username}")
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

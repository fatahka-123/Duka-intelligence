"""Emergency password reset for whoever runs the server (e.g. Render → Shell).

    python -m app.reset_password owner@example.com

Prompts for a new password, signs the account out everywhere and writes an audit entry.
"""
from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from .db import SessionLocal, User
from .security import audit, hash_password


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    email = argv[1].lower().strip()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            print(f"No account with email {email}")
            return 1
        pw = getpass.getpass("New password (min 10 characters): ")
        if len(pw) < 10 or pw != getpass.getpass("Repeat it: "):
            print("Passwords must match and be at least 10 characters")
            return 1
        user.password_hash = hash_password(pw)
        user.active = True
        user.token_version = (user.token_version or 0) + 1
        audit(db, user, "auth.password_reset_admin", "user", user.id, "reset from server console")
        db.commit()
    print(f"Password changed for {email}. They can sign in now.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

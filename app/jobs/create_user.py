from __future__ import annotations

import argparse
import getpass

from app.db import init_db, session_scope
from app.models import AppUser
from app.services.auth import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description='Create or update a Tender Assistant user.')
    parser.add_argument('--username', required=True)
    parser.add_argument('--password', default='')
    parser.add_argument('--full-name', default='')
    parser.add_argument('--email', default='')
    parser.add_argument('--role', choices=['admin', 'user'], default='user')
    parser.add_argument('--update', action='store_true', help='Update existing user instead of failing.')
    args = parser.parse_args()

    password = args.password or getpass.getpass('Password: ')
    if not password:
        raise SystemExit('Password is required.')

    init_db()
    with session_scope() as db:
        user = db.query(AppUser).filter(AppUser.username == args.username.strip()).one_or_none()
        if user is not None and not args.update:
            raise SystemExit(f'User {args.username} already exists. Use --update to change it.')
        if user is None:
            user = AppUser(username=args.username.strip(), is_active=True)
            db.add(user)
        user.password_hash = hash_password(password)
        user.full_name = args.full_name.strip() or user.full_name or user.username
        user.email = args.email.strip() or user.email
        user.role = args.role
        user.is_active = True
        db.flush()
        print(f'User {user.username} ready with role {user.role}.')


if __name__ == '__main__':
    main()

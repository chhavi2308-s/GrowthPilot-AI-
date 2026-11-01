"""Command line: python -m growthpilot <command>."""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from .config import PROJECT_ROOT, ConfigError


def _settings():
    from .config import get_settings

    try:
        return get_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_serve(args) -> None:
    import uvicorn

    _settings()
    uvicorn.run("growthpilot.asgi:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


def cmd_worker(_args) -> None:
    from .db import check_or_stamp_mode, init_engine, migrate
    from .worker import run_forever

    s = _settings()
    engine = init_engine(s)
    migrate(engine)
    check_or_stamp_mode(s)
    run_forever()


def cmd_migrate(_args) -> None:
    from .db import init_engine, migrate

    migrate(init_engine(_settings()))
    print("Database is at the latest migration.")


def cmd_revision(args) -> None:
    from alembic import command

    from .db import alembic_config, init_engine

    command.revision(alembic_config(init_engine(_settings())), message=args.message, autogenerate=True)


def cmd_init_env(args) -> None:
    """Create .env from .env.example with fresh secrets. Never prints the secret values."""
    target = Path(args.path) if args.path else PROJECT_ROOT / ".env"
    if target.exists() and not args.force:
        print(f"{target} already exists; leaving it unchanged (use --force to overwrite).")
        return
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in example.splitlines():
        if line.startswith("SECRET_KEY="):
            line = f"SECRET_KEY={secrets.token_urlsafe(48)}"
        elif line.startswith("ENCRYPTION_KEY="):
            line = f"ENCRYPTION_KEY={Fernet.generate_key().decode()}"
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
    print(f"Wrote {target} with new SECRET_KEY and ENCRYPTION_KEY (values not shown). Add API keys there when you "
          "have them.")


def cmd_demo_setup(args) -> None:
    """Create a demo owner account (if needed) and load a demo brand (Poojaloka by default). Demo mode only."""
    from sqlalchemy import select

    from .db import check_or_stamp_mode, init_engine, migrate, session_scope
    from .demo import fixtures
    from .models import Membership, Role, User, Workspace
    from .security import hash_password

    s = _settings()
    if not s.is_demo:
        print("demo-setup only runs in demo mode (APP_MODE=demo).", file=sys.stderr)
        sys.exit(2)
    engine = init_engine(s)
    migrate(engine)
    check_or_stamp_mode(s)
    password = None
    with session_scope() as db:
        user = db.execute(select(User).where(User.email == args.email)).scalar_one_or_none()
        if user is None:
            password = secrets.token_urlsafe(9) + "A1"
            user = User(email=args.email, name="Demo Owner", password_hash=hash_password(password))
            ws = Workspace(name="Poojaloka (demo)" if args.brand == "poojaloka" else "Aranya Living (demo)",
                           timezone="Asia/Kolkata")
            db.add_all([user, ws])
            db.flush()
            db.add(Membership(user_id=user.id, workspace_id=ws.id, role=Role.OWNER))
            db.flush()
            ws_id = ws.id
        else:
            ws_id = db.execute(select(Membership.workspace_id).where(Membership.user_id == user.id)).scalars().first()
            ws = db.get(Workspace, ws_id)
            if ws.name.endswith("(demo)"):
                ws.name = "Poojaloka (demo)" if args.brand == "poojaloka" else "Aranya Living (demo)"
        result = fixtures.seed(db, ws_id, user.id, args.brand)
    print(f"Demo data loaded: {args.brand} ({result['products']} products, history campaign "
          f"#{result['history_campaign_id']}).")
    if password:
        cred = PROJECT_ROOT / "data" / "demo-login.txt"
        cred.write_text(f"email: {args.email}\npassword: {password}\n", encoding="utf-8")
        cred.chmod(0o600)
        print(f"Demo login created for {args.email}. Password saved to {cred} (local, gitignored).")
    else:
        print(f"Existing account {args.email} kept; its password is unchanged.")


def cmd_sync_catalogue(args) -> None:
    """Import a brand's catalogue from its connector (Poojaloka storefront API). Live mode, or demo with
    DEMO_ALLOW_CATALOGUE_SYNC=true. Reads only public product data; prints counts, never secrets."""
    from sqlalchemy import select

    from . import catalogue
    from .db import check_or_stamp_mode, init_engine, migrate, session_scope
    from .models import Brand

    s = _settings()
    engine = init_engine(s)
    migrate(engine)
    check_or_stamp_mode(s)
    with session_scope() as db:
        b = db.execute(select(Brand).where(Brand.id == args.brand_id)).scalar_one_or_none() if args.brand_id else \
            db.execute(select(Brand).order_by(Brand.id)).scalars().first()
        if b is None:
            print("No brand found.", file=sys.stderr)
            sys.exit(2)
        if not (b.catalogue_source or {}).get("kind"):
            b.catalogue_source = {"kind": "poojaloka_store_api", "base_url": s.poojaloka_api_base}
        src = catalogue.get_source(b)
        if src is None:
            print("Catalogue sync isn't allowed in demo mode unless DEMO_ALLOW_CATALOGUE_SYNC=true.", file=sys.stderr)
            sys.exit(2)
        res = catalogue.sync(db, b, src, full=args.full, limit=args.limit)
        try:
            content = src.brand_content()
            b.catalogue_source = dict(b.catalogue_source or {}) | {k: content[k] for k in ("festivals", "articles")
                                                                   if content.get(k)}
        except catalogue.CatalogueError as exc:
            print(f"Brand content not refreshed: {exc}")
    print(f"Catalogue sync: {res.created} created, {res.updated} updated, {res.unchanged} unchanged, "
          f"{res.deactivated} deactivated, {res.photos_downloaded} product photo(s) stored, {len(res.errors)} error(s).")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="growthpilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="Run the API server")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true")
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("worker", help="Run the background worker").set_defaults(fn=cmd_worker)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(fn=cmd_migrate)
    sr = sub.add_parser("revision", help="Autogenerate a migration")
    sr.add_argument("-m", "--message", required=True)
    sr.set_defaults(fn=cmd_revision)
    se = sub.add_parser("init-env", help="Create .env with fresh secrets")
    se.add_argument("--force", action="store_true")
    se.add_argument("--path")
    se.set_defaults(fn=cmd_init_env)
    sd = sub.add_parser("demo-setup", help="Create a demo login and load demo data (demo mode only)")
    sd.add_argument("--email", default="demo@growthpilot.local")
    sd.add_argument("--brand", default="poojaloka", choices=["poojaloka", "aranya"])
    sd.set_defaults(fn=cmd_demo_setup)
    sc = sub.add_parser("sync-catalogue", help="Import the brand catalogue from its connector (live mode)")
    sc.add_argument("--brand-id", type=int)
    sc.add_argument("--full", action="store_true", help="Deactivate products no longer listed at the source")
    sc.add_argument("--limit", type=int, help="Import at most N products")
    sc.set_defaults(fn=cmd_sync_catalogue)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

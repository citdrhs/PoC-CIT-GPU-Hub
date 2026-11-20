"""Run the loopback API or provision an expiring operator-approved token."""
import argparse
import os
import re
import sys
from pathlib import Path

from .catalog import Catalog


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in {"issue-token", "revoke-tokens"}:
        action = argv[0]
        parser = argparse.ArgumentParser(prog=f"python -m services.api {action}")
        parser.add_argument("--identity", required=True)
        parser.add_argument("--db", default=os.getenv("GPUHUB_DB", ".gpuhub/hub.sqlite3"))
        parser.add_argument("--data", default=os.getenv("GPUHUB_DATA", ".gpuhub/data"))
        if action == "issue-token":
            parser.add_argument("--alias", required=True)
            parser.add_argument("--role", choices=["student","admin"], default="student")
            parser.add_argument("--hours", type=int, default=8)
            parser.add_argument("--read-only", action="store_true", help="Limit the token to GET requests.")
        args = parser.parse_args(argv[1:])
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}",args.identity):
            parser.error("Use an identity between 1 and 80 letters, numbers, dots, underscores or hyphens.")
        catalog = Catalog(args.db, Path(args.data), False)
        if action == "issue-token":
            if not args.alias.strip() or not args.alias.isprintable() or len(args.alias)>96:
                parser.error("Choose a printable alias of up to 96 characters.")
            if not 1 <= args.hours <= 720:
                parser.error("Token lifetime must be between 1 and 720 hours.")
            print(catalog.issue(args.identity,args.alias,args.role,"operator",args.hours*3600,
                                read_only=args.read_only)["token"])
        else:
            with catalog.connect() as db:
                count = db.execute("DELETE FROM auth_tokens WHERE user_id=?",(args.identity,)).rowcount
            print(f"Revoked {count} token(s).")
        return
    parser = argparse.ArgumentParser(prog="python -m services.api")
    parser.add_argument("--host", choices=["127.0.0.1","::1","localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.getenv("GPUHUB_DB", ".gpuhub/hub.sqlite3"))
    parser.add_argument("--data", default=os.getenv("GPUHUB_DATA", ".gpuhub/data"))
    parser.add_argument("--dev-auth", action="store_true", help="Enable explicit local sample identities.")
    args = parser.parse_args(argv)
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(args.db,args.data,dev_auth=args.dev_auth),host=args.host,
                port=args.port,proxy_headers=False,access_log=False)


if __name__ == "__main__":
    main()

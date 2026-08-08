"""Unified command line entry point.

    python3 -m launchbox <command> [options]

Every module was previously invoked separately (``python3 -m launchbox.builder``
and so on), which meant deployment ordering lived in a bash hook. One entry
point keeps that ordering in one place.
"""

import argparse
import sys
from typing import List, Optional

from launchbox import runner
from launchbox.builder import build
from launchbox.deploy import (
    deploy, remove_app, rollback, update_env, configure_database,
    detach_database, DATABASE_ENGINES,
)
from launchbox.init import init
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.state import StateStore

logger = setup_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launchbox",
        description="Self-hosted, git-push-to-deploy platform.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="register an application")
    p_init.add_argument("app_name")
    p_init.add_argument("--branch", default="main",
                        help="branch that triggers deployment (default: main)")

    p_build = sub.add_parser("build", help="build an image without deploying")
    p_build.add_argument("app_name")
    p_build.add_argument("--source", default=None)
    p_build.add_argument("--tag", default=None)

    p_deploy = sub.add_parser("deploy", help="build and deploy an application")
    p_deploy.add_argument("app_name")
    p_deploy.add_argument("--source", default=None,
                          help="directory to build (default: apps/<app>)")
    p_deploy.add_argument("--commit", default=None,
                          help="commit SHA being deployed")

    p_rollback = sub.add_parser("rollback", help="restore a previous version")
    p_rollback.add_argument("app_name")
    p_rollback.add_argument("--commit", default=None,
                            help="target commit (default: last good version)")

    p_env = sub.add_parser(
        "env", help="update environment variables without rebuilding")
    p_env.add_argument("app_name")
    p_env.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="set (or overwrite) an environment variable; repeatable")
    p_env.add_argument(
        "--unset", action="append", default=[], metavar="KEY",
        help="remove an environment variable; repeatable")

    p_db = sub.add_parser(
        "db", help="provision or detach an application's database")
    p_db.add_argument("app_name")
    db_action = p_db.add_mutually_exclusive_group(required=True)
    db_action.add_argument(
        "--engine", choices=sorted(DATABASE_ENGINES),
        help="provision a database of this engine (or reconfigure the "
             "existing one)")
    db_action.add_argument(
        "--detach", action="store_true",
        help="stop injecting database credentials; the database and its "
             "data are kept")
    p_db.add_argument(
        "--version", default=None,
        help="engine version (default: a sensible per-engine version)")
    p_db.add_argument(
        "--name", default=None, dest="db_name",
        help="database name (default: <app_name>_db)")

    p_remove = sub.add_parser(
        "remove", help="remove an application, its containers and its images")
    p_remove.add_argument("app_name")
    p_remove.add_argument(
        "--with-database", action="store_true",
        help="also destroy the application's database container and its "
             "recorded credentials (data is lost)")

    p_history = sub.add_parser("history", help="show deployment history")
    p_history.add_argument("app_name")
    p_history.add_argument("--limit", type=int, default=20)

    sub.add_parser("list", help="list known applications")

    p_logs = sub.add_parser("logs", help="show container logs")
    p_logs.add_argument("app_name")
    p_logs.add_argument("--tail", type=int, default=200)

    sub.add_parser("dashboard", help="start the web dashboard")

    p_sup = sub.add_parser("supervisor", help="start the health supervisor")
    p_sup.add_argument("--interval", type=float, default=None)

    return parser


def _cmd_history(app_name: str, limit: int) -> int:
    with StateStore() as store:
        rows = store.list_deployments(app_name, limit=limit)
    if not rows:
        print(f"No deployments recorded for {app_name}")
        return 0

    print(f"{'ID':>4}  {'COMMIT':<10} {'STATUS':<12} {'STARTED':<22} ERROR")
    for row in rows:
        print(
            f"{row['id']:>4}  {(row['commit_sha'] or '-')[:9]:<10} "
            f"{row['status']:<12} {row['started_at']:<22} {row['error'] or ''}"
        )
    return 0


def _cmd_list() -> int:
    with StateStore() as store:
        apps = store.list_apps()
    if not apps:
        print("No applications deployed yet")
        return 0

    print(f"{'APP':<20} {'CONTAINER':<28} {'HEALTH':<10} RESTARTS")
    for app in apps:
        print(
            f"{app['app_name']:<20} {(app['current_container'] or '-'):<28} "
            f"{(app['health_state'] or 'unknown'):<10} {app['restart_attempts']}"
        )
    return 0


def _cmd_logs(app_name: str, tail: int) -> int:
    with StateStore() as store:
        container = store.current_container(app_name)
    if not container:
        if runner.container_exists(app_name):
            container = app_name
        else:
            print(f"No container recorded for {app_name}", file=sys.stderr)
            return 1
    print(runner.container_logs(container, tail=tail))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "init":
            init(args.app_name, default_branch=args.branch)
            return 0

        if args.command == "build":
            print(build(args.app_name, source_dir=args.source, tag=args.tag))
            return 0

        if args.command == "deploy":
            print(deploy(args.app_name, source_dir=args.source,
                         commit_sha=args.commit))
            return 0

        if args.command == "rollback":
            print(rollback(args.app_name, commit_sha=args.commit))
            return 0

        if args.command == "env":
            set_vars = {}
            for item in args.set:
                if "=" not in item:
                    logger.error(f"--set expects KEY=VALUE, got: {item!r}")
                    return 1
                key, value = item.split("=", 1)
                set_vars[key] = value
            print(update_env(args.app_name, set_vars=set_vars,
                            unset_vars=args.unset))
            return 0

        if args.command == "db":
            if args.detach:
                print(detach_database(args.app_name))
            else:
                print(configure_database(
                    args.app_name, args.engine,
                    version=args.version, db_name=args.db_name,
                ))
            return 0

        if args.command == "remove":
            summary = remove_app(
                args.app_name, remove_database=args.with_database
            )
            print(
                f"Removed {args.app_name}: "
                f"route={'yes' if summary['route_removed'] else 'no'}, "
                f"containers={len(summary['containers'])}, "
                f"images={len(summary['images'])}"
            )
            return 0

        if args.command == "history":
            return _cmd_history(args.app_name, args.limit)

        if args.command == "list":
            return _cmd_list()

        if args.command == "logs":
            return _cmd_logs(args.app_name, args.tail)

        if args.command == "dashboard":
            from launchbox.dashboard import main as dashboard_main

            dashboard_main()
            return 0

        if args.command == "supervisor":
            from launchbox.supervisor import run_forever

            run_forever(interval=args.interval)
            return 0

    except (LaunchboxError, ValueError) as exc:
        # validate_app_name (and the branch-name check inside init()) raise
        # plain ValueError rather than a LaunchboxError subclass, since they
        # live in launchbox.config / launchbox.init and validate CLI input
        # before any deployment machinery runs. Catching it here alongside
        # LaunchboxError is what keeps an invalid app name a one-line error
        # instead of a raw traceback.
        logger.error(str(exc))
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())

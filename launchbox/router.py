"""Traefik dynamic-route generation.

Routes are registered by writing a small YAML document into the directory
Traefik's file provider watches, rather than by attaching labels to the
application container.

The distinction matters: Docker labels are fixed at container creation, so a
label-routed container is reachable from the instant it exists. Writing the
route as a separate file lets the orchestrator create a container, verify it is
healthy, and only then send traffic to it -- and lets a failed deployment be
discarded without the route ever having moved.
"""

import os
from typing import Any, Dict, Optional

import yaml

from launchbox.config import BASE_DIR, validate_app_name
from launchbox.logger import setup_logger

logger = setup_logger("router")

DYNAMIC_DIR = os.path.join(BASE_DIR, "traefik", "dynamic")


def route_file_path(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> str:
    # App names become filenames under DYNAMIC_DIR. Restricting them to a safe
    # character set (no `/`, `..`, or other path separators) prevents a crafted
    # app_name from escaping DYNAMIC_DIR -- an arbitrary file write via
    # write_route or an arbitrary file delete via remove_route.
    validate_app_name(app_name)
    return os.path.join(dynamic_dir, f"{app_name}.yml")


def build_route_document(
    app_name: str,
    backend_host: str,
    port: int,
    https: bool = False,
    redirect_http: bool = True,
) -> Dict[str, Any]:
    """Build the Traefik dynamic configuration for one application."""
    service_name = f"{app_name}-svc"
    host_rule = f"Host(`{app_name}.localhost`)"

    web_router: Dict[str, Any] = {
        "rule": host_rule,
        "service": service_name,
        "entryPoints": ["web"],
    }

    http_section: Dict[str, Any] = {
        "routers": {app_name: web_router},
        "services": {
            service_name: {
                "loadBalancer": {
                    "servers": [{"url": f"http://{backend_host}:{port}"}]
                }
            }
        },
    }

    if https:
        http_section["routers"][f"{app_name}-secure"] = {
            "rule": host_rule,
            "service": service_name,
            "entryPoints": ["websecure"],
            "tls": {},
        }
        if redirect_http:
            middleware_name = f"{app_name}-redirect"
            web_router["middlewares"] = [middleware_name]
            http_section["middlewares"] = {
                middleware_name: {"redirectScheme": {"scheme": "https"}}
            }

    return {"http": http_section}


def write_route(
    app_name: str,
    backend_host: str,
    port: int,
    https: bool = False,
    redirect_http: bool = True,
    dynamic_dir: str = DYNAMIC_DIR,
) -> str:
    """Write the route file atomically and return its path.

    The document is written to a temporary file in the same directory and then
    renamed over the target, so Traefik's watcher never observes a partial
    document.
    """
    os.makedirs(dynamic_dir, exist_ok=True)
    document = build_route_document(
        app_name, backend_host, port, https=https, redirect_http=redirect_http
    )

    target = route_file_path(app_name, dynamic_dir)
    tmp_target = f"{target}.tmp"

    try:
        with open(tmp_target, "w") as handle:
            yaml.safe_dump(
                document, handle, default_flow_style=False, sort_keys=False
            )
        os.replace(tmp_target, target)
    except Exception:
        if os.path.exists(tmp_target):
            os.remove(tmp_target)
        raise

    logger.info(
        f"Route registered: {app_name}.localhost -> {backend_host}:{port}"
        f"{' (https)' if https else ''}"
    )
    return target


def read_route(
    app_name: str, dynamic_dir: str = DYNAMIC_DIR
) -> Optional[Dict[str, Any]]:
    path = route_file_path(app_name, dynamic_dir)
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return yaml.safe_load(handle)


def remove_route(app_name: str, dynamic_dir: str = DYNAMIC_DIR) -> bool:
    """Delete an application's route file. Returns True if one was removed."""
    path = route_file_path(app_name, dynamic_dir)
    if not os.path.exists(path):
        return False
    os.remove(path)
    logger.info(f"Route removed: {app_name}")
    return True

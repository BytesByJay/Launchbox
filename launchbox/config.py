import os
import re

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPS_DIR = os.path.join(BASE_DIR, "apps")
REPOS_DIR = os.path.join(BASE_DIR, "repos")

APP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def validate_app_name(app_name: str) -> str:
    """Return app_name if it is safe to interpolate into paths and image tags.

    Application names reach the platform from CLI arguments, HTTP path
    segments and directory names, and are used to build filesystem paths and
    Docker image tags. Anything outside this character set is rejected rather
    than escaped, because there is no legitimate application name that needs it.
    """
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise ValueError(
            f"Invalid application name: {app_name!r}. "
            "Names must start with a letter or digit and contain only "
            "letters, digits, hyphens and underscores."
        )
    return app_name

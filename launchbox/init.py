"""Application registration.

Creates the bare repository that receives pushes and installs the post-receive
hook that triggers a deployment.
"""

import os
import re
import shlex
import subprocess
import sys

from launchbox.config import BASE_DIR, REPOS_DIR, validate_app_name
from launchbox.logger import setup_logger, LaunchboxError
from launchbox.state import StateStore

logger = setup_logger("init")

BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _validate_branch_name(branch: str) -> str:
    """Reject branch names that would not survive being baked into the hook.

    The generated hook embeds this value literally inside a shell test, so a
    name carrying quotes or shell metacharacters would execute as code when
    the hook runs. Git's own rules are looser than this, but nothing this
    platform deploys needs a branch name outside it.
    """
    if not isinstance(branch, str) or ".." in branch or not BRANCH_NAME_RE.fullmatch(branch):
        raise ValueError(f"Invalid branch name: {branch!r}")
    return branch


HOOK_TEMPLATE = """#!/bin/bash
# Launchbox post-receive hook -- generated, do not edit by hand.
set -euo pipefail

APP_NAME="{app_name}"
BASE_DIR={base_dir}

if [ -x {venv_python} ]; then
    PYTHON_BIN={venv_python}
else
    PYTHON_BIN="python3"
fi

GIT_REPO_DIR="$(git rev-parse --absolute-git-dir)"

while read -r oldrev newrev refname; do
    if [ "$refname" != "refs/heads/{default_branch}" ]; then
        echo "[Launchbox] Ignoring push to $refname"
        continue
    fi

    if [[ "$newrev" =~ ^0+$ ]]; then
        echo "[Launchbox] Branch deleted, nothing to deploy"
        continue
    fi

    echo "[Launchbox] Deploying $APP_NAME @ ${{newrev:0:7}}"

    WORKDIR="$(mktemp -d)"
    trap 'rm -rf "$WORKDIR"' EXIT

    git --git-dir="$GIT_REPO_DIR" --work-tree="$WORKDIR" \\
        checkout -f "$newrev" -- .

    cd "$BASE_DIR"
    PYTHONPATH="$BASE_DIR" "$PYTHON_BIN" -m launchbox deploy "$APP_NAME" \\
        --source "$WORKDIR" --commit "$newrev"

    rm -rf "$WORKDIR"
    trap - EXIT
done
"""


def render_hook(app_name: str, base_dir: str, default_branch: str = "main") -> str:
    """Render the post-receive hook for an application.

    Three properties matter, and each fixes a defect in the previous hook:

    * ``set -euo pipefail`` means a failing deployment fails the push visibly,
      rather than being swallowed.
    * The branch filter stops a push to a feature branch deploying to
      production.
    * The temporary worktree means what is built is the commit that was
      actually pushed, rather than whatever happens to be sitting on disk.

    ``default_branch`` is validated before it is spliced into the script: it
    is embedded literally inside a double-quoted shell string, so an
    unvalidated value could break out and execute arbitrary commands when the
    hook runs. ``base_dir`` is run through ``shlex.quote`` for the same
    reason.
    """
    _validate_branch_name(default_branch)
    venv_python = shlex.quote(os.path.join(base_dir, ".venv", "bin", "python"))
    return HOOK_TEMPLATE.format(
        app_name=app_name,
        base_dir=shlex.quote(base_dir),
        default_branch=default_branch,
        venv_python=venv_python,
    )


def init(app_name: str, default_branch: str = "main") -> str:
    """Register an application. Returns the bare repository path.

    Safe to re-run: an existing repository is kept and only its hook is
    refreshed, so applications registered under an older Launchbox pick up the
    current pipeline.
    """
    validate_app_name(app_name)
    _validate_branch_name(default_branch)

    repo_path = os.path.join(REPOS_DIR, f"{app_name}.git")
    hooks_dir = os.path.join(repo_path, "hooks")
    hook_path = os.path.join(hooks_dir, "post-receive")

    if os.path.isdir(repo_path):
        logger.info(f"Repository already exists: {repo_path}")
    else:
        os.makedirs(REPOS_DIR, exist_ok=True)
        result = subprocess.run(
            ["git", "init", "--bare", repo_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise LaunchboxError(f"git init --bare failed: {result.stderr.strip()}")
        logger.info(f"Created bare repo at {repo_path}")

    os.makedirs(hooks_dir, exist_ok=True)
    with open(hook_path, "w") as handle:
        handle.write(render_hook(app_name, BASE_DIR, default_branch))
    os.chmod(hook_path, 0o755)
    logger.info(f"Hook installed at {hook_path}")

    app_dir = os.path.join(BASE_DIR, "apps", app_name)
    if not os.path.isdir(app_dir):
        logger.warning(
            f"No application directory at {app_dir}. "
            "Create it with a Dockerfile before pushing."
        )

    # Registration alone previously left the app invisible everywhere: no
    # local apps/<name> directory (git-push apps never get one) and no
    # deployments row (that's only written by an actual deploy attempt). A
    # dashboard or `launchbox list` reading either source found nothing until
    # the first push succeeded. Recording the row here makes the app visible
    # immediately, showing "not deployed" until it is.
    try:
        with StateStore() as store:
            store.register(app_name)
    except Exception as e:
        logger.warning(f"Failed to record registration for {app_name}: {e}")

    return repo_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python3 -m launchbox.init <app_name>")
        sys.exit(1)
    init(sys.argv[1])

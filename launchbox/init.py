import os
import sys
from pathlib import Path

def init(app_name):
    repo_path = Path(f"./repos/{app_name}.git")
    hook_path = repo_path / "hooks" / "post-receive"

    if repo_path.exists():
        print(f"[!] Repo {repo_path} already exists.")
        return

    os.system(f"git init --bare {repo_path}")
    print(f"[✓] Created bare repo at {repo_path}")

    hook_content = f"""#!/bin/bash
APP_NAME={app_name}
echo "[Launchbox] Deploying $APP_NAME"
export BASE_DIR=$(pwd)
export PYTHONPATH=$(pwd)
python3 -m launchbox.builder $APP_NAME
python3 -m launchbox.runner $APP_NAME
"""

    hook_path.write_text(hook_content)
    hook_path.chmod(0o755)
    print(f"[✓] Hook created at {hook_path}")

if __name__ == "__main__":
    init(sys.argv[1])

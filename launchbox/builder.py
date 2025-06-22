import subprocess
import sys
from launchbox.config import APPS_DIR

def build(app_name: str):
    app_path = f"{APPS_DIR}/{app_name}"
    image_name = f"launchbox-{app_name}"
    print(f"Building image: {image_name}")
    subprocess.run(["docker", "build", "-t", image_name, app_path], check=True)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: builder.py <app_name>")
        sys.exit(1)
    build(sys.argv[1])

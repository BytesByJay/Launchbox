import subprocess
import sys


def run(app_name):
    subprocess.run(
        ["docker", "rm", "-f", app_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            app_name,
            "--network",
            "traefik_default",  # This line is crucial
            "--label",
            f"traefik.enable=true",
            "--label",
            f"traefik.http.routers.{app_name}.rule=Host(`{app_name}.localhost`)",
            "--label",
            f"traefik.http.services.{app_name}.loadbalancer.server.port=3000",
            f"launchbox-{app_name}",
        ],
        check=True,
    )


if __name__ == "__main__":
    run(sys.argv[1])

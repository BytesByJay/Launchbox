import os
import subprocess

import pytest

from launchbox import builder
from launchbox.logger import BuildError


@pytest.fixture
def app_dir(tmp_path):
    """A minimal application directory with a Dockerfile and config."""
    d = tmp_path / "myapp"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "launchbox.yaml").write_text("app:\n  port: 3000\n")
    return str(d)


def _ok(*_args, **_kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_build_returns_tagged_image_reference(mocker, app_dir):
    mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    ref = builder.build("myapp", source_dir=app_dir, tag="abc1234")

    assert ref == "launchbox-myapp:abc1234"


def test_build_without_tag_uses_latest(mocker, app_dir):
    mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    assert builder.build("myapp", source_dir=app_dir) == "launchbox-myapp:latest"


def test_build_tags_both_sha_and_latest(mocker, app_dir):
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    builder.build("myapp", source_dir=app_dir, tag="abc1234")

    build_cmd = run.call_args_list[0][0][0]
    assert "-t" in build_cmd
    tags = [build_cmd[i + 1] for i, a in enumerate(build_cmd) if a == "-t"]
    assert "launchbox-myapp:abc1234" in tags
    assert "launchbox-myapp:latest" in tags


def test_build_uses_the_given_source_directory(mocker, app_dir):
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    builder.build("myapp", source_dir=app_dir, tag="abc1234")

    cmd = run.call_args_list[0][0][0]
    assert cmd[-1] == app_dir
    assert os.path.join(app_dir, "Dockerfile") in cmd


def test_build_raises_when_source_directory_missing(tmp_path):
    with pytest.raises(BuildError, match="not found"):
        builder.build("myapp", source_dir=str(tmp_path / "nope"))


def test_build_raises_when_dockerfile_missing(tmp_path):
    d = tmp_path / "myapp"
    d.mkdir()
    with pytest.raises(BuildError, match="Dockerfile not found"):
        builder.build("myapp", source_dir=str(d))


def test_build_raises_with_docker_stderr_on_failure(mocker, app_dir):
    mocker.patch(
        "launchbox.builder.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="step 3 failed"
        ),
    )

    with pytest.raises(BuildError, match="step 3 failed"):
        builder.build("myapp", source_dir=app_dir, tag="abc1234")


def test_build_rejects_traversing_app_name(mocker, app_dir):
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    with pytest.raises(ValueError):
        builder.build("../../etc", source_dir=app_dir)

    run.assert_not_called()


def test_build_rejects_embedded_separator_in_app_name(mocker, app_dir):
    mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    with pytest.raises(ValueError):
        builder.build("a/b", source_dir=app_dir)


@pytest.mark.parametrize("name", ["demo_app", "test_app", "my-app-2"])
def test_build_accepts_legal_app_names(mocker, tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "Dockerfile").write_text("FROM scratch\n")
    mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    ref = builder.build(name, source_dir=str(d), tag="abc1234")

    assert ref == f"launchbox-{name}:abc1234"


def test_build_honours_custom_dockerfile_and_context(mocker, tmp_path):
    d = tmp_path / "myapp"
    (d / "docker").mkdir(parents=True)
    (d / "docker" / "Dockerfile.prod").write_text("FROM scratch\n")
    (d / "src").mkdir()
    (d / "launchbox.yaml").write_text(
        "app:\n"
        "  build:\n"
        "    dockerfile: docker/Dockerfile.prod\n"
        "    context: src\n"
    )
    run = mocker.patch("launchbox.builder.subprocess.run", side_effect=_ok)

    builder.build("myapp", source_dir=str(d), tag="abc1234")

    cmd = run.call_args_list[0][0][0]
    assert str(d / "docker" / "Dockerfile.prod") in cmd
    assert cmd[-1] == str(d / "src")

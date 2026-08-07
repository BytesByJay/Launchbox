import os
import stat
import subprocess

import pytest

from launchbox import init as init_module


def test_hook_sets_strict_bash_flags():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert hook.startswith("#!/bin/bash")
    assert "set -euo pipefail" in hook


def test_hook_reads_the_push_from_stdin():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "while read -r oldrev newrev refname" in hook


def test_hook_filters_to_the_default_branch():
    hook = init_module.render_hook("myapp", "/opt/launchbox", default_branch="main")
    assert 'refs/heads/main' in hook
    assert "Ignoring push" in hook


def test_hook_honours_a_custom_default_branch():
    hook = init_module.render_hook("myapp", "/opt/launchbox",
                                   default_branch="production")
    assert "refs/heads/production" in hook


def test_hook_checks_out_into_a_temporary_worktree():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "mktemp -d" in hook
    assert "--work-tree=" in hook
    assert 'checkout -f "$newrev"' in hook
    assert "rm -rf" in hook, "the temporary worktree must be cleaned up"


def test_hook_calls_the_orchestrator_with_source_and_commit():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "-m launchbox deploy" in hook
    assert "--source" in hook
    assert "--commit" in hook


def test_hook_does_not_call_builder_and_runner_separately():
    """The two-invocation pipeline is what allowed a failed build through."""
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "launchbox.builder" not in hook
    assert "launchbox.runner" not in hook


def test_hook_prefers_the_project_virtualenv():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    assert "/opt/launchbox/.venv/bin/python" in hook


def test_hook_is_valid_bash():
    hook = init_module.render_hook("myapp", "/opt/launchbox")
    result = subprocess.run(
        ["bash", "-n"], input=hook, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_init_creates_a_bare_repo_with_an_executable_hook(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    repo_path = init_module.init("myapp")

    assert os.path.isdir(repo_path)
    assert repo_path == str(repos / "myapp.git")
    # A bare repository has no working tree.
    assert os.path.exists(os.path.join(repo_path, "HEAD"))

    hook_path = os.path.join(repo_path, "hooks", "post-receive")
    assert os.path.exists(hook_path)
    assert os.stat(hook_path).st_mode & stat.S_IXUSR


def test_init_is_idempotent(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    first = init_module.init("myapp")
    second = init_module.init("myapp")

    assert first == second


def test_init_rewrites_the_hook_on_reinitialisation(tmp_path, monkeypatch):
    """Existing apps must pick up the new hook without recreating the repo."""
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    repo_path = init_module.init("myapp")
    hook_path = os.path.join(repo_path, "hooks", "post-receive")
    with open(hook_path, "w") as handle:
        handle.write("#!/bin/bash\necho stale\n")

    init_module.init("myapp")

    with open(hook_path) as handle:
        assert "launchbox deploy" in handle.read()


# --- Fix round 1: default_branch injection and deleted-branch handling ----


@pytest.mark.parametrize(
    "bad_branch",
    [
        'main"; touch /tmp/PWNED; echo "',
        "main; rm -rf /",
        "main$(touch /tmp/PWNED)",
        "main`touch /tmp/PWNED`",
        "-x",
        "main..other",
        "main\n",
        "main\nx",
    ],
)
def test_render_hook_rejects_malicious_branch_names(bad_branch):
    with pytest.raises(ValueError):
        init_module.render_hook("myapp", "/opt/launchbox", default_branch=bad_branch)


@pytest.mark.parametrize(
    "branch", ["main", "master", "release/1.0", "feature-x", "v1.2.3"]
)
def test_render_hook_accepts_legal_branch_names(branch):
    hook = init_module.render_hook("myapp", "/opt/launchbox", default_branch=branch)
    assert f"refs/heads/{branch}" in hook

    result = subprocess.run(
        ["bash", "-n"], input=hook, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_init_rejects_malicious_branch_and_creates_no_repo(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(init_module, "REPOS_DIR", str(repos))
    monkeypatch.setattr(init_module, "BASE_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        init_module.init("myapp", default_branch='main"; touch /tmp/PWNED; echo "')

    assert not os.path.exists(str(repos / "myapp.git"))


def _run_hook(hook_path, cwd, stdin_line):
    return subprocess.run(
        ["bash", str(hook_path)],
        input=stdin_line,
        text=True,
        capture_output=True,
        cwd=str(cwd),
        check=False,
    )


def _install_hook(tmp_path, app_name="myapp", default_branch="main"):
    """Real bare repo + rendered hook, base_dir has no .venv (falls back to python3)."""
    repo = tmp_path / f"{app_name}.git"
    subprocess.run(
        ["git", "init", "--bare", str(repo)], check=True, capture_output=True
    )

    base_dir = tmp_path / "base"
    base_dir.mkdir()

    hook = init_module.render_hook(
        app_name, str(base_dir), default_branch=default_branch
    )
    hook_path = repo / "hooks" / "post-receive"
    hook_path.write_text(hook)
    hook_path.chmod(0o755)

    return repo, hook_path


def test_hook_skips_a_deleted_branch_cleanly(tmp_path):
    """A branch delete sends an all-zero newrev; the hook must not try to
    check that out, and must not let set -e turn it into a raw git error."""
    repo, hook_path = _install_hook(tmp_path)
    zero = "0" * 40

    result = _run_hook(hook_path, repo, f"{zero} {zero} refs/heads/main\n")

    assert result.returncode == 0, result.stderr
    assert "Branch deleted" in result.stdout + result.stderr


def test_hook_ignores_non_default_branch_without_running_deploy(tmp_path):
    """The branch filter must short-circuit before any git or python work,
    so a push to a feature branch never needs the deploy CLI to exist."""
    repo, hook_path = _install_hook(tmp_path)
    sha = "a" * 40

    result = _run_hook(hook_path, repo, f"{sha} {sha} refs/heads/somebranch\n")

    assert result.returncode == 0, result.stderr
    assert "Ignoring push" in result.stdout + result.stderr

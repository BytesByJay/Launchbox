import pytest

from launchbox import supervisor
from launchbox.state import StateStore


@pytest.fixture
def store(tmp_state_db):
    s = StateStore(tmp_state_db)
    yield s
    s.close()


def _deploy(store, app="myapp", sha="abc1234"):
    dep = store.record_start(app, sha)
    store.record_success(dep, f"{app}-{sha}", f"launchbox-{app}:{sha}")
    return f"{app}-{sha}"


def test_healthy_container_resets_restart_attempts(store):
    _deploy(store)
    store.increment_restart_attempts("myapp")
    store.increment_restart_attempts("myapp")

    actions = supervisor.supervise_once(
        store, health_fn=lambda _c: "healthy", restart_fn=lambda _c: True
    )

    assert actions == [
        {"app": "myapp", "action": "healthy", "container": "myapp-abc1234"}
    ]
    assert store.restart_attempts("myapp") == 0
    assert store.get_app("myapp")["health_state"] == "healthy"


def test_unhealthy_container_is_restarted_within_the_bound(store):
    _deploy(store)
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "unhealthy",
        restart_fn=lambda c: restarted.append(c) or True,
        max_attempts=3,
    )

    assert restarted == ["myapp-abc1234"]
    assert actions[0]["action"] == "restarted"
    assert store.restart_attempts("myapp") == 1
    assert store.is_marked_failed("myapp") is False


def test_repeated_failures_eventually_mark_the_app_failed(store):
    _deploy(store)
    restarted = []

    for _ in range(4):
        supervisor.supervise_once(
            store,
            health_fn=lambda _c: "unhealthy",
            restart_fn=lambda c: restarted.append(c) or True,
            max_attempts=3,
        )

    assert len(restarted) == 3, "must stop restarting once the bound is reached"
    assert store.is_marked_failed("myapp") is True
    assert store.get_app("myapp")["health_state"] == "failed"


def test_a_failed_app_is_skipped_on_later_passes(store):
    _deploy(store)
    store.mark_failed("myapp")
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "unhealthy",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert actions[0]["action"] == "skipped"


def test_starting_container_is_left_alone(store):
    _deploy(store)
    restarted = []

    supervisor.supervise_once(
        store,
        health_fn=lambda _c: "starting",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert store.restart_attempts("myapp") == 0


def test_container_without_a_probe_is_not_restarted(store):
    """An app that declares no health_check must not be churned."""
    _deploy(store)
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "none",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert actions[0]["action"] == "healthy"


def test_missing_container_is_reported_not_restarted(store):
    _deploy(store)
    restarted = []

    actions = supervisor.supervise_once(
        store,
        health_fn=lambda _c: "missing",
        restart_fn=lambda c: restarted.append(c) or True,
    )

    assert restarted == []
    assert actions[0]["action"] == "missing"
    assert store.get_app("myapp")["health_state"] == "unknown"


def test_apps_with_no_current_container_are_ignored(store):
    store.record_start("neverdeployed", "abc1234")  # no success recorded

    actions = supervisor.supervise_once(
        store, health_fn=lambda _c: "healthy", restart_fn=lambda _c: True
    )

    assert actions == []


def test_multiple_apps_are_each_evaluated(store):
    _deploy(store, app="alpha", sha="1111111")
    _deploy(store, app="beta", sha="2222222")

    statuses = {"alpha-1111111": "healthy", "beta-2222222": "unhealthy"}
    actions = supervisor.supervise_once(
        store, health_fn=lambda c: statuses[c], restart_fn=lambda _c: True
    )

    by_app = {a["app"]: a["action"] for a in actions}
    assert by_app == {"alpha": "healthy", "beta": "restarted"}


def test_a_failed_restart_still_counts_as_an_attempt(store):
    _deploy(store)

    actions = supervisor.supervise_once(
        store, health_fn=lambda _c: "unhealthy", restart_fn=lambda _c: False
    )

    assert actions[0]["action"] == "restart_failed"
    assert store.restart_attempts("myapp") == 1


def test_start_background_thread_is_a_daemon(mocker):
    mocker.patch.object(supervisor, "run_forever")

    thread = supervisor.start_background_thread(interval=1)

    assert thread.daemon is True, "must not block interpreter shutdown"

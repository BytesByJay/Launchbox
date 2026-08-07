import os

import pytest
import yaml

from launchbox import router


def test_route_document_http_only():
    doc = router.build_route_document("myapp", "myapp-abc1234", 3000)

    assert doc["http"]["routers"]["myapp"]["rule"] == "Host(`myapp.localhost`)"
    assert doc["http"]["routers"]["myapp"]["service"] == "myapp-svc"
    assert doc["http"]["routers"]["myapp"]["entryPoints"] == ["web"]
    assert "tls" not in doc["http"]["routers"]["myapp"]

    servers = doc["http"]["services"]["myapp-svc"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://myapp-abc1234:3000"}]


def test_route_document_https_adds_secure_router():
    doc = router.build_route_document(
        "myapp", "myapp-abc1234", 8080, https=True, redirect_http=False
    )
    secure = doc["http"]["routers"]["myapp-secure"]

    assert secure["rule"] == "Host(`myapp.localhost`)"
    assert secure["entryPoints"] == ["websecure"]
    assert secure["tls"] == {}
    assert secure["service"] == "myapp-svc"
    # Both routers share one backend service definition.
    assert len(doc["http"]["services"]) == 1


def test_route_document_https_with_redirect_adds_middleware():
    doc = router.build_route_document(
        "myapp", "myapp-abc1234", 3000, https=True, redirect_http=True
    )

    assert doc["http"]["routers"]["myapp"]["middlewares"] == ["myapp-redirect"]
    middleware = doc["http"]["middlewares"]["myapp-redirect"]
    assert middleware["redirectScheme"]["scheme"] == "https"


def test_route_document_without_https_has_no_middlewares_key():
    doc = router.build_route_document("myapp", "myapp-abc1234", 3000)
    assert "middlewares" not in doc["http"]


def test_write_route_creates_parseable_yaml(tmp_dynamic_dir):
    path = router.write_route(
        "myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir
    )

    assert path == os.path.join(tmp_dynamic_dir, "myapp.yml")
    assert os.path.exists(path)

    with open(path) as handle:
        parsed = yaml.safe_load(handle)
    assert parsed["http"]["services"]["myapp-svc"]["loadBalancer"]["servers"] == [
        {"url": "http://myapp-abc1234:3000"}
    ]


def test_write_route_overwrites_previous_backend(tmp_dynamic_dir):
    router.write_route("myapp", "myapp-1111111", 3000, dynamic_dir=tmp_dynamic_dir)
    router.write_route("myapp", "myapp-2222222", 3000, dynamic_dir=tmp_dynamic_dir)

    doc = router.read_route("myapp", dynamic_dir=tmp_dynamic_dir)
    servers = doc["http"]["services"]["myapp-svc"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://myapp-2222222:3000"}]


def test_write_route_leaves_no_temp_files_behind(tmp_dynamic_dir):
    router.write_route("myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir)

    assert sorted(os.listdir(tmp_dynamic_dir)) == ["myapp.yml"]


def test_write_route_creates_missing_directory(tmp_path):
    target = str(tmp_path / "not-yet-there")
    router.write_route("myapp", "myapp-abc1234", 3000, dynamic_dir=target)

    assert os.path.exists(os.path.join(target, "myapp.yml"))


def test_remove_route_deletes_the_file(tmp_dynamic_dir):
    router.write_route("myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir)

    assert router.remove_route("myapp", dynamic_dir=tmp_dynamic_dir) is True
    assert not os.path.exists(os.path.join(tmp_dynamic_dir, "myapp.yml"))


def test_remove_route_is_safe_when_absent(tmp_dynamic_dir):
    assert router.remove_route("ghost", dynamic_dir=tmp_dynamic_dir) is False


def test_read_route_returns_none_when_absent(tmp_dynamic_dir):
    assert router.read_route("ghost", dynamic_dir=tmp_dynamic_dir) is None


# --- Finding 1: path traversal in route_file_path -----------------------


def test_route_file_path_rejects_parent_traversal():
    with pytest.raises(ValueError):
        router.route_file_path("../escape")


def test_route_file_path_rejects_embedded_separator():
    with pytest.raises(ValueError):
        router.route_file_path("a/b")


def test_write_route_rejects_traversing_name_and_writes_nothing(tmp_dynamic_dir):
    with pytest.raises(ValueError):
        router.write_route(
            "../../etc/cron.d/x", "backend", 3000, dynamic_dir=tmp_dynamic_dir
        )

    # Nothing was written inside the intended directory...
    assert os.listdir(tmp_dynamic_dir) == []
    # ...nor did the traversal escape and land somewhere on disk.
    escaped = os.path.abspath(
        os.path.join(tmp_dynamic_dir, "..", "..", "etc", "cron.d", "x.yml")
    )
    assert not os.path.exists(escaped)


def test_remove_route_rejects_traversing_name(tmp_dynamic_dir):
    with pytest.raises(ValueError):
        router.remove_route("../escape", dynamic_dir=tmp_dynamic_dir)


@pytest.mark.parametrize("name", ["demo_app", "test_app", "myapp", "my-app-2"])
def test_route_file_path_accepts_legal_names(name):
    path = router.route_file_path(name, dynamic_dir="/tmp/dynamic")
    assert path == os.path.join("/tmp/dynamic", f"{name}.yml")


# --- Finding 2: leftover temp file on write failure ----------------------


def test_write_route_cleans_up_temp_file_on_write_failure(tmp_dynamic_dir, mocker):
    mocker.patch.object(
        router.yaml, "safe_dump", side_effect=RuntimeError("boom")
    )

    with pytest.raises(RuntimeError):
        router.write_route(
            "myapp", "myapp-abc1234", 3000, dynamic_dir=tmp_dynamic_dir
        )

    assert os.listdir(tmp_dynamic_dir) == []

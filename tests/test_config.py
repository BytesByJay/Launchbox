import pytest

from launchbox.config import validate_app_name


@pytest.mark.parametrize(
    "name", ["demo_app", "test_app", "myapp", "my-app-2", "a", "app2"]
)
def test_validate_app_name_accepts_legal_names(name):
    assert validate_app_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../x",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "",
        ".",
        "-leading",
        "_leading",
        "myapp\n",
        "demo_app\n",
        "myapp\n\n",
        "my\napp",
    ],
)
def test_validate_app_name_rejects_illegal_names(name):
    with pytest.raises(ValueError, match=r"Invalid application name"):
        validate_app_name(name)


def test_validate_app_name_rejects_non_string():
    with pytest.raises(ValueError, match=r"Invalid application name"):
        validate_app_name(None)


def test_validate_app_name_message_names_the_offending_value():
    with pytest.raises(ValueError, match=r"\.\./x"):
        validate_app_name("../x")

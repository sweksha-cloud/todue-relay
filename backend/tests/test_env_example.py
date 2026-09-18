"""backend/.env.example is the documented list of every setting. It drifted
once already: it said STALE_CLAIM_MINUTES=15 while the code default (and the
decided value) was 3, and anyone copying it silently got 5x slower crash
recovery. These tests fail whenever config.py and the example disagree.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def _config_settings() -> dict[str, str | None]:
    """{env var name: literal default, or None if the default isn't a plain
    string literal (e.g. a computed path)} for every os.getenv in config.py.
    """
    settings: dict[str, str | None] = {}
    for node in ast.walk(ast.parse((BACKEND / "app" / "config.py").read_text())):
        is_getenv = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        )
        if not is_getenv:
            continue
        name = node.args[0].value
        default = node.args[1] if len(node.args) > 1 else None
        settings[name] = default.value if isinstance(default, ast.Constant) else None
    return settings


def _example_settings() -> dict[str, str]:
    values = {}
    for line in (BACKEND / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_the_check_itself_finds_settings():
    """Guard against a vacuous pass if the parsing ever silently breaks."""
    assert len(_config_settings()) >= 20
    assert len(_example_settings()) >= 20


def test_every_setting_in_config_is_listed_in_the_example():
    missing = sorted(set(_config_settings()) - set(_example_settings()))
    assert missing == [], f"add these to backend/.env.example: {missing}"


def test_the_example_lists_nothing_config_does_not_read():
    extra = sorted(set(_example_settings()) - set(_config_settings()))
    assert extra == [], f"in .env.example but not read by config.py: {extra}"


def test_example_values_match_the_code_defaults():
    example = _example_settings()
    mismatched = {
        name: (default, example[name])
        for name, default in _config_settings().items()
        if default is not None and name in example and example[name] != default
    }
    assert mismatched == {}, f"(code default, .env.example) differ: {mismatched}"

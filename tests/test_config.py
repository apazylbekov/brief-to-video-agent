from pathlib import Path

import pytest

from app import config


@pytest.mark.parametrize(
    ("shell", "expected"),
    [(None, "from-file"), ("", "from-file"), ("from-shell", "from-shell")],
    ids=["unset", "blank, as .mcp.json passes it", "set"],
)
def test_env_file_fills_unset_and_blank_variables_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str | None, expected: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BTV_TEST_KEY=from-file\nBTV_TEST_EMPTY=\n")
    if shell is None:
        monkeypatch.delenv("BTV_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("BTV_TEST_KEY", shell)
    monkeypatch.delenv("BTV_TEST_EMPTY", raising=False)

    config.load_env(env_file)

    assert config.os.environ["BTV_TEST_KEY"] == expected
    assert "BTV_TEST_EMPTY" not in config.os.environ  # an empty .env value sets nothing

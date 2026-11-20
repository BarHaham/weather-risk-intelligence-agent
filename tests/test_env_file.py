""".env loading. Every test writes its own file in pytest's tmp_path: no test reads or
writes the repository's real .env."""
import os
import warnings

import anthropic
import pytest

from app import config

KEY = "sk-ant-test-0000000000000000000000"


def write(tmp_path, text, encoding):
    p = tmp_path / ".env"
    p.write_bytes(text.encode(encoding))
    return p


def test_tests_never_target_the_repo_env_file(tmp_path):
    assert tmp_path not in config.ENV_FILE.parents and config.ENV_FILE != tmp_path / ".env"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le-bom", "cp1252"])
def test_key_is_read_whatever_the_file_encoding(tmp_path, encoding):
    body = f"# written on Windows\r\nANTHROPIC_API_KEY={KEY}\r\nWRIA_NOTE=café\r\n"
    if encoding == "utf-16-le-bom":  # what Windows PowerShell 5.1's `>` / Out-File writes
        p = tmp_path / ".env"
        p.write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
    else:
        p = write(tmp_path, body, encoding)
    loaded = config.load_env_file(p)
    assert os.environ["ANTHROPIC_API_KEY"] == KEY and loaded["ANTHROPIC_API_KEY"] == KEY
    assert os.environ["WRIA_NOTE"] == "café"
    assert config.api_key_source == ".env"


def test_empty_value_is_skipped(tmp_path):
    p = write(tmp_path, "ANTHROPIC_API_KEY=\nNO_VALUE_LINE\n", "utf-8")
    assert config.load_env_file(p) == {}
    assert "ANTHROPIC_API_KEY" not in os.environ and config.api_key_source == "missing"


def test_environment_variable_wins_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-shell")
    config.load_env_file(write(tmp_path, f"ANTHROPIC_API_KEY={KEY}\n", "utf-8"))
    assert os.environ["ANTHROPIC_API_KEY"] == "from-the-shell"
    assert config.api_key_source == "environment"


def test_empty_environment_variable_does_not_block_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config.load_env_file(write(tmp_path, f"ANTHROPIC_API_KEY={KEY}\n", "utf-8"))
    assert os.environ["ANTHROPIC_API_KEY"] == KEY and config.api_key_source == ".env"


def test_values_are_not_interpolated(tmp_path):
    config.load_env_file(write(tmp_path, "ANTHROPIC_API_KEY=abc$HOME\n", "utf-8"))
    assert os.environ["ANTHROPIC_API_KEY"] == "abc$HOME"


def test_missing_file_is_a_no_op(tmp_path):
    assert config.load_env_file(tmp_path / ".env") == {}
    assert config.api_key_source == "missing"


def test_warns_about_notepad_txt_extension(tmp_path):
    (tmp_path / ".env.txt").write_text(f"ANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config.load_env_file(tmp_path / ".env")
    assert any(".env.txt" in str(x.message) for x in w)
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_sdk_client_picks_up_the_key_from_a_utf16_env_file(tmp_path):
    config.load_env_file(write(tmp_path, f"ANTHROPIC_API_KEY={KEY}\n", "utf-16"))
    assert anthropic.Anthropic().api_key == KEY

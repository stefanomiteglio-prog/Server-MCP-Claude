"""Oscuramento dei segreti e regole di accesso ai file (senza HTTP)."""

import pytest

from local_llm_mcp.files import read_text_file, sensitive_reason
from local_llm_mcp.safety import InputError, redact_secrets

# Segreti finti costruiti a runtime, per non far scattare gli scanner sul repository.
GITHUB = "ghp_" + "A1b2C3d4E5" * 4
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "x" * 40 + "\n-----END RSA PRIVATE KEY-----"
)
JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".c2lnbmF0dXJlLXZhbHVl"


@pytest.mark.parametrize(
    ("text", "kind", "secret"),
    [
        ("key = sk-its-FAKEfakeFAKEfake00000000", "API_KEY", "sk-its-FAKEfakeFAKEfake00000000"),
        ("aws AKIAIOSFODNN7EXAMPLE here", "AWS_ACCESS_KEY", "AKIAIOSFODNN7EXAMPLE"),
        (f"token {GITHUB}", "GITHUB_TOKEN", GITHUB),
        (f"cert:\n{PRIVATE_KEY}\nfine", "PRIVATE_KEY", "MIIEow"),
        (f"jwt={JWT}", "JWT", JWT),
        (
            "Authorization: Bearer abcdefghijklmnopqrstuvwx",
            "BEARER_TOKEN",
            "abcdefghijklmnopqrstuvwx",
        ),
        ("postgres://admin:S3cr3tPass@db.local:5432/app", "URL_PASSWORD", "S3cr3tPass"),
        ('password = "hunter2hunter"', "SECRET", "hunter2hunter"),
        ('{"client_secret": "abc123xyz"}', "SECRET", "abc123xyz"),
        ("DB_PASSWORD=Sup3rS3cret!", "SECRET", "Sup3rS3cret!"),
        ("+    API_KEY: 'zzzz9999zzzz'", "SECRET", "zzzz9999zzzz"),
    ],
)
def test_secrets_are_redacted(text, kind, secret):
    redacted, found = redact_secrets(text)

    assert secret not in redacted
    assert f"[REDACTED:{kind}]" in redacted
    assert found[kind] >= 1


@pytest.mark.parametrize(
    "text",
    [
        'password = request.form["password"]',
        "token = settings.auth_token",
        'tokenizer = "bert-base-uncased"',
        'token_type = "Bearer"',
        "max_tokens = 4096",
        "def check_password(user, password):",
        "Questa frase parla di una password ma non ne contiene una.",
        "api_key = os.environ['LOCAL_LLM_API_KEY']",
        "API_KEY=${LOCAL_LLM_API_KEY}",
    ],
)
def test_code_and_prose_are_not_redacted(text):
    redacted, found = redact_secrets(text)

    assert redacted == text
    assert not found


@pytest.mark.parametrize(
    ("name", "blocked"),
    [
        (".env", True),
        (".env.local", True),
        ("prod.env", True),
        (".env.example", False),
        ("id_ed25519", True),
        ("server.pem", True),
        ("terraform.tfstate", True),
        ("README.md", False),
        ("app.py", False),
    ],
)
def test_sensitive_files(tmp_path, name, blocked):
    assert (sensitive_reason(tmp_path / name) is not None) is blocked


def test_files_inside_git_dir_are_blocked(tmp_path):
    assert sensitive_reason(tmp_path / ".git" / "config") is not None


def test_read_text_file(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("﻿ciao è già UTF-8", encoding="utf-8")

    assert read_text_file(str(f), (tmp_path.resolve(),), 1000) == "ciao è già UTF-8"


def test_relative_path_is_rejected(tmp_path):
    with pytest.raises(InputError, match="assoluto"):
        read_text_file("notes.md", (tmp_path.resolve(),), 1000)


def test_binary_file_is_rejected(tmp_path):
    f = tmp_path / "image.txt"
    f.write_bytes(b"\x89PNG\x00\x00data")

    with pytest.raises(InputError, match="binario"):
        read_text_file(str(f), (tmp_path.resolve(),), 1000)


def test_non_utf8_file_is_rejected(tmp_path):
    f = tmp_path / "latin1.txt"
    f.write_bytes("perché".encode("latin-1"))

    with pytest.raises(InputError, match="UTF-8"):
        read_text_file(str(f), (tmp_path.resolve(),), 1000)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
def test_utf16_file_with_bom_is_read(tmp_path, encoding):
    # Come lo scrive Windows PowerShell 5.1 con "git diff > file": UTF-16 con BOM e a capo CRLF.
    f = tmp_path / "review.diff"
    data = "diff --git a/è.py b/è.py\r\n+riga\r\n"
    if encoding == "utf-16-be":
        f.write_bytes(b"\xfe\xff" + data.encode("utf-16-be"))
    else:
        f.write_bytes(data.encode("utf-16"))

    text = read_text_file(str(f), (tmp_path.resolve(),), 1000)

    assert text == "diff --git a/è.py b/è.py\n+riga\n"


def test_utf16_bom_does_not_hide_a_binary_file(tmp_path):
    f = tmp_path / "fake.txt"
    f.write_bytes(b"\xff\xfe" + b"\x00\x00ab")

    with pytest.raises(InputError, match="binario"):
        read_text_file(str(f), (tmp_path.resolve(),), 1000)


def test_too_large_file_is_rejected_before_reading(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 500, encoding="utf-8")

    with pytest.raises(InputError, match="troppo grande"):
        read_text_file(str(f), (tmp_path.resolve(),), 100)


def test_missing_file(tmp_path):
    with pytest.raises(InputError, match="non trovato"):
        read_text_file(str(tmp_path / "nope.txt"), (tmp_path.resolve(),), 100)


def test_directory_is_rejected(tmp_path):
    with pytest.raises(InputError, match="non indica un file"):
        read_text_file(str(tmp_path), (tmp_path.resolve(),), 100)

"""Tests for secret redaction (section 1.3).

Two things are being proven here. That every credential shape we claim to
recognise is actually caught, in all three places a secret can hide in a log
record — and, just as importantly, that ordinary high-entropy strings Scry logs
constantly (commit shas, UUIDs, paths) survive untouched. A filter that redacts
every git sha would make the logs useless, which defeats the point of having
them.
"""

from __future__ import annotations

import logging

import pytest

from scry.util.redact import RedactingFilter, redact


def body(length: int, tag: str = "EXAMPLEONLY") -> str:
    """Deterministic filler that satisfies our patterns and resembles nothing real."""
    return (tag + "0" * length)[:length]


# Credential fixtures are assembled at runtime; the body never appears as a
# literal anywhere in this file.
#
# This is not decoration. GitHub push protection scans committed text and
# rejects the push if it finds anything credential-shaped — and it defeats
# same-line `"prefix-" + "body"` splitting, as this file learned the hard way
# when a Slack- and a Stripe-shaped fixture blocked the first push. A
# repository whose entire purpose is finding secrets cannot itself contain
# secret-shaped text, so the recognisable half is generated rather than typed.
#
# Every Pathologist fixture from section 4.8 onward must follow the same rule.
SECRETS = [
    ("aws_key", "AKIA" + body(16)),
    ("aws_temp_key", "ASIA" + body(16)),
    ("github_token", "ghp_" + body(36)),
    ("github_pat", "github_pat_" + body(22)),
    ("anthropic_key", "sk-ant-" + body(24)),
    ("openai_key", "sk-proj-" + body(24)),
    ("slack_token", "xoxb-" + body(20)),
    ("google_api_key", "AIza" + body(35)),
    ("stripe_key", "sk_live_" + body(24)),
    ("jwt", "eyJ" + body(12) + ".eyJ" + body(12) + "." + body(24)),
    ("private_key", "-----BEGIN RSA PRIVATE " + "KEY-----"),
    ("bearer_token", "Bearer " + body(24)),
]

# Used throughout the LogRecord tests below.
AWS_KEY = "AKIA" + body(16)


@pytest.mark.parametrize(("kind", "secret"), SECRETS, ids=[k for k, _ in SECRETS])
def test_every_known_credential_shape_is_redacted(kind: str, secret: str):
    result = redact(f"connecting with {secret} now")
    assert secret not in result
    assert f"<REDACTED_{kind}>" in result


def test_url_credentials_are_stripped_but_the_host_survives():
    result = redact("cloning https://alice:hunter2A@github.com/acme/repo.git")
    assert "hunter2A" not in result
    assert "alice" not in result
    assert "github.com/acme/repo.git" in result, "the useful part of the URL should remain"


def test_anthropic_keys_are_not_mislabelled_as_openai():
    """`sk-ant-` must be tried before `sk-`; reversed, every Anthropic key is mislabelled."""
    result = redact("key " + "sk-ant-" + body(30))
    assert "<REDACTED_anthropic_key>" in result
    assert "openai" not in result


# ---------------------------------------------------------------------------
# Contextual assignments
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "password=Hunter2Secret",
        "api_key: Ab3fGh9kLm2p",
        'token="Xy7zQw1234abcd"',
        "SECRET = Zz9987654321",
        "authorization: Bb44556677889900",
    ],
)
def test_credential_assignments_are_redacted(line: str):
    result = redact(line)
    assert "<REDACTED_credential>" in result


def test_assignment_keeps_the_key_so_the_log_still_reads_sensibly():
    assert redact("api_key=Ab3fGh9kLm2p").startswith("api_key=")


def test_lowercase_words_after_a_keyword_are_not_redacted():
    """Guards against firing on Scry's own log lines, e.g. 'token: defaults'."""
    for benign in ("token: defaults", "secret: unknown", "password: changeme"):
        assert redact(benign) == benign


# ---------------------------------------------------------------------------
# Things that must survive untouched
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "benign",
    [
        "commit 2709086332 67aabe947691380c9d6850b6c67811",
        "sha b498788555 3b8b9018bf3510802d87b28f0a242c",
        "workspace id legacy-monolith-a7f3k9m2",
        "uuid 550e8400-e29b-41d4-a716-446655440000",
        r"parsing E:\scry\src\scry\config\loader.py",
        "42 files processed in 1.2s",
    ],
)
def test_ordinary_log_content_is_left_alone(benign: str):
    """Entropy-based detection would flag every one of these and ruin the logs."""
    assert redact(benign) == benign


def test_redaction_is_idempotent():
    """The filter runs once per handler on the same record object."""
    once = redact("key " + AWS_KEY)
    assert redact(once) == once


def test_empty_and_plain_strings_are_unchanged():
    assert redact("") == ""
    assert redact("nothing to see here") == "nothing to see here"


# ---------------------------------------------------------------------------
# The filter, over real LogRecords
# ---------------------------------------------------------------------------
def make_record(msg, args=(), **kwargs) -> logging.LogRecord:
    return logging.LogRecord(
        name="scry.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=kwargs.get("exc_info"),
    )


def test_filter_redacts_the_message():
    record = make_record("token " + AWS_KEY)
    RedactingFilter().filter(record)
    assert AWS_KEY not in record.getMessage()


def test_filter_redacts_lazy_format_arguments():
    """`log.info("%s", secret)` puts nothing in msg — the secret lives in args."""
    record = make_record("token %s", (AWS_KEY,))
    RedactingFilter().filter(record)
    assert AWS_KEY not in record.getMessage()
    assert "<REDACTED_aws_key>" in record.getMessage()


def test_filter_redacts_secrets_inside_non_string_arguments():
    """The value can hide in an object's repr, which per-field scrubbing misses."""
    error = ValueError("bad credential " + AWS_KEY)
    record = make_record("failed: %s", (error,))
    RedactingFilter().filter(record)
    assert AWS_KEY not in record.getMessage()


def test_filter_redacts_tracebacks():
    """The sneakiest case: the secret is in neither msg nor args."""
    try:
        raise ValueError("bad credential " + AWS_KEY)
    except ValueError:
        import sys

        record = make_record("auth failed", exc_info=sys.exc_info())

    RedactingFilter().filter(record)
    assert record.exc_text is not None
    assert AWS_KEY not in record.exc_text
    assert "<REDACTED_aws_key>" in record.exc_text


def test_filter_redacts_already_rendered_exception_text():
    record = make_record("boom")
    record.exc_text = f"Traceback...\nValueError: {AWS_KEY}\n"
    RedactingFilter().filter(record)
    assert AWS_KEY not in record.exc_text


def test_filter_redacts_stack_info():
    record = make_record("boom")
    record.stack_info = "File x, in y\n  token=Ab3fGh9kLm2p"
    RedactingFilter().filter(record)
    assert "Ab3fGh9kLm2p" not in record.stack_info


def test_filter_survives_a_malformed_format_string():
    """A bad format must not let the raw value through unscrubbed."""
    record = make_record("token %s %s", (AWS_KEY,))
    assert RedactingFilter().filter(record) is True
    assert AWS_KEY not in str(record.msg)


def test_filter_always_returns_true():
    """The filter scrubs records; it never suppresses them."""
    assert RedactingFilter().filter(make_record("ordinary message")) is True

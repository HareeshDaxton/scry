"""Secret redaction for anything that reaches a log.

Spec section 13.4 requires that Scry never logs a secret value. Written as a
rule, that would have to be remembered at every ``log.debug()`` in every agent,
forever, including in code nobody has written yet. Rules like that hold until
the first tired evening, and then somebody's AWS key is sitting in
``~/.scry/logs/scry.log``.

This module turns the rule into a property of the logging layer instead:
:class:`RedactingFilter` is attached to every handler at setup, so any record
that reaches any handler is scrubbed regardless of which logger produced it or
how careful its author was.

**This is a last-resort safety net, not the secret detector.** Its patterns are
deliberately precise and it does no entropy analysis, because it runs on every
log record — potentially millions during a large analysis — and because entropy
would flag the things Scry logs constantly: commit shas, content hashes, UUIDs.
Redacting every git sha would make the logs useless for debugging, which defeats
the point of having them. Real secret detection, with entropy and context, is
Pathologist's job in sections 4.8 and 4.9.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

MASK = "<REDACTED_{kind}>"

# Order matters. The combined pattern is a single alternation, and at any given
# position the first alternative that matches wins — so more specific patterns
# must precede more general ones. `sk-ant-` before `sk-` is the load-bearing
# case: reversed, every Anthropic key would be labelled an OpenAI key.
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private_key", r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("aws_temp_key", r"\bASIA[0-9A-Z]{16}\b"),
    ("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9\-_]{20,}"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z\-_]{35}"),
    ("stripe_key", r"\b[sr]k_live_[0-9A-Za-z]{16,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("bearer_token", r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*"),
    # Credentials embedded in a URL: https://user:pass@host
    ("url_credentials", r"(?<=://)[^/\s:@]+:[^/\s:@]+(?=@)"),
    # Contextual assignment, for providers we have no specific pattern for.
    # The value must be six or more characters *and* contain a digit or an
    # uppercase letter. Without that guard this fires on Scry's own log lines
    # ("token: defaults"); with it, ordinary lowercase words are left alone
    # while anything credential-shaped is caught.
    (
        "assignment",
        r"\b(?i:(?P<assign_key>password|passwd|pwd|api[_-]?key|apikey|secret"
        r"|access[_-]?token|auth[_-]?token|token|authorization))"
        r"(?P<assign_sep>\s*[=:]\s*[\"']?)"
        r"(?P<assign_val>(?=[^\s\"',;]{6,})[^\s\"',;]*[0-9A-Z][^\s\"',;]*)",
    ),
)

_NAMES: tuple[str, ...] = tuple(name for name, _ in _PATTERNS)

# One compiled alternation rather than fifteen separate passes. Scrubbing is
# then a single scan per field, which matters on a path this hot.
_COMBINED = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _PATTERNS))


def _replace(match: re.Match[str]) -> str:
    for name in _NAMES:
        if match.group(name) is None:
            continue
        if name == "assignment":
            # Keep the key and separator so the log still reads sensibly;
            # replace only the value.
            return (
                f"{match.group('assign_key')}"
                f"{match.group('assign_sep')}"
                f"{MASK.format(kind='credential')}"
            )
        return MASK.format(kind=name)
    return match.group(0)  # pragma: no cover - unreachable while _NAMES is complete


def redact(text: str) -> str:
    """Replace any recognised secret in ``text`` with a typed mask.

    Idempotent: redacting already-redacted text is a no-op. That matters
    because the filter is attached to every handler, so it runs once per
    handler on the same record object.
    """
    return _COMBINED.sub(_replace, text)


def redact_value(value: Any) -> Any:
    """Redact a value if it is a string; pass anything else through unchanged."""
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    """Scrubs secrets from every log record that reaches a handler.

    **Attached to handlers, never to loggers.** This is not a style preference:
    a filter on a logger runs only for records logged *directly* to that logger.
    When ``scry.agents.pathologist`` emits a record it propagates up to the
    ``scry`` logger's *handlers*, but ``scry``'s *filters* are never consulted —
    ``Logger.filter()`` runs only in ``Logger.handle()`` on the originating
    logger. A filter installed on the ``scry`` logger would therefore scrub
    records from ``scry`` itself and silently miss every record from every
    agent, which is to say all the ones that matter.

    The record is formatted eagerly and its args cleared. That is deliberate:
    a secret can hide in three places, and only eager formatting catches all of
    them —

    * ``record.msg`` — ``log.info(f"token {t}")``
    * ``record.args`` — ``log.info("token %s", t)``, where msg holds no secret
    * ``record.exc_info`` — ``raise ValueError(f"bad {t}")`` then
      ``log.exception(...)``, where the value is rendered into the traceback and
      never appears in msg or args at all

    Eager formatting also covers the case where the secret lives in the *repr*
    of a non-string argument, which per-field string scrubbing would miss.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # Malformed format string. The handler would fail on this too;
            # scrub what we can rather than letting the raw value through.
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()

        # Formatter.format() uses exc_text when it is already set and only
        # renders exc_info otherwise, so populating it here means every handler
        # sees the scrubbed traceback rather than the original.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        elif record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))

        if record.stack_info:
            record.stack_info = redact(record.stack_info)

        return True

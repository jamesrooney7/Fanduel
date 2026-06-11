"""Turn user input (event URL or raw ID) into a numeric FanDuel event id."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .errors import InvalidEventRefError

_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")

_EXAMPLES = (
    "  33840322\n"
    "  https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322"
)


def parse_event_ref(ref: str) -> int:
    """Accept a raw numeric event id or any FanDuel event-page URL.

    URLs may carry query strings, fragments, trailing slashes, percent-encoded
    characters, or state-specific subdomains — the event id is the run of
    digits at the end of the last path segment.
    """
    text = (ref or "").strip()
    if not text:
        raise InvalidEventRefError(
            "No event given.",
            hint="Pass a FanDuel game URL or event id, e.g.:\n" + _EXAMPLES,
        )
    if text.isdigit():
        return int(text)

    # urlsplit drops ?query and #fragment for us.
    path = unquote(urlsplit(text).path).rstrip("/")
    last_segment = path.rsplit("/", 1)[-1]
    match = _TRAILING_DIGITS_RE.search(last_segment)
    if not match:
        raise InvalidEventRefError(
            f"Could not find an event id in {ref!r}.",
            hint=(
                "Open the game page in your browser and copy the URL exactly — "
                "the event id is the number at the end. Valid examples:\n" + _EXAMPLES
            ),
        )
    return int(match.group(1))

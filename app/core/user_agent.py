"""Coarse User-Agent classification for first-party analytics (§6.3).

Deliberately tiny and heuristic: we only need a device bucket
(``desktop`` / ``mobile`` / ``tablet`` / ``bot``) and a bot flag for aggregate
traffic stats — not a full UA-parsing dependency. The raw UA string is never
stored; only the derived class is (privacy minimization, spec §5).
"""

# Substrings that mark automated / crawler traffic (lower-cased match).
_BOT_MARKERS: tuple[str, ...] = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "crawling",
    "curl",
    "wget",
    "python-requests",
    "httpx",
    "headless",
    "monitor",
    "preview",
)

# Substrings marking tablets (checked before phones — an iPad also matches none
# of the phone markers, but Android tablets can be ambiguous; keep it simple).
_TABLET_MARKERS: tuple[str, ...] = ("ipad", "tablet")

# Substrings marking phones / small mobile devices.
_MOBILE_MARKERS: tuple[str, ...] = ("mobi", "iphone", "android", "ipod")


def classify_device(user_agent: str | None) -> tuple[str, bool]:
    """Return ``(device_class, is_bot)`` for a User-Agent string.

    Args:
        user_agent: The raw User-Agent header value, or ``None``.

    Returns:
        tuple[str, bool]: The device bucket (``desktop`` / ``mobile`` /
            ``tablet`` / ``bot``) and whether it looks automated. A missing UA is
            treated as a bot (server-to-server / scripted traffic).
    """
    if not user_agent:
        return "bot", True
    ua = user_agent.lower()
    if any(marker in ua for marker in _BOT_MARKERS):
        return "bot", True
    if any(marker in ua for marker in _TABLET_MARKERS):
        return "tablet", False
    if any(marker in ua for marker in _MOBILE_MARKERS):
        return "mobile", False
    return "desktop", False

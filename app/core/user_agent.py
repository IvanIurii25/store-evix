"""Coarse User-Agent classification for first-party analytics (§6.3).

Deliberately tiny and heuristic: we only need a device bucket
(``desktop`` / ``mobile`` / ``tablet`` / ``bot``) and a bot flag for aggregate
traffic stats — not a full UA-parsing dependency. The raw UA string is never
stored; only the derived class is (privacy minimization, spec §5).
"""

# Substrings that mark automated / crawler traffic (lower-cased match).
#
# Grouped for maintainability; order is irrelevant (any match ⇒ bot). Generic
# tokens ("bot", "spider", "crawler") already catch most named crawlers
# (Googlebot, Bingbot, AhrefsBot, TwitterBot, …); the explicit vendor entries
# below cover monitors, HTTP libraries and preview fetchers whose UA carries no
# generic marker. Bare vendor names that also appear in real browser UAs
# (notably "yandex" — Yandex Browser) are deliberately NOT listed to avoid
# flagging humans; those crawlers are caught by "bot"/"spider" instead.
_BOT_MARKERS: tuple[str, ...] = (
    # Generic automation / crawler tokens.
    "bot",
    "crawler",
    "spider",
    "slurp",
    "crawling",
    "headless",
    "preview",
    "scrapy",
    # Command-line / HTTP client libraries (scripted traffic).
    "curl",
    "wget",
    "python-requests",
    "httpx",
    "aiohttp",
    "urllib",
    "go-http-client",
    "okhttp",
    "java/",
    "libwww",
    "guzzle",
    "axios",
    "node-fetch",
    "postman",
    "insomnia",
    # Uptime / performance monitors (some carry no "bot"/"monitor" token).
    "monitor",
    "pingdom",
    "statuscake",
    "site24x7",
    "datadog",
    "newrelic",
    "checkly",
    "hetrix",
    "gtmetrix",
    "lighthouse",
    "phantomjs",
    # Social / link-preview fetchers.
    "facebookexternalhit",
    "whatsapp",
    "embedly",
    "vkshare",
    "pinterest",
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

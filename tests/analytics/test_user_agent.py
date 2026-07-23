"""Unit tests for coarse User-Agent classification (§6.3).

``classify_device`` is a pure function, so these tests need no fixtures: they
assert the device bucket and bot flag for representative real-world UAs. The
bot-marker list is the analytics anti-inflation guard (monitors/crawlers must
not count as human uniques), so the crawler/monitor cases are the important
coverage here.
"""

import pytest

from app.core.user_agent import classify_device

# Real browser UAs that must classify as human (device, not bot).
_HUMAN_CASES: list[tuple[str, str]] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "desktop",
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 "
        "Safari/604.1",
        "mobile",
    ),
    (
        "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "tablet",
    ),
    (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "mobile",
    ),
    # Yandex Browser — must NOT be flagged as bot (RU/RO/MD audience). It shares
    # the "yandex" token with YandexBot, which is why bare "yandex" is not a
    # marker; the real browser here stays human.
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/118.0.0.0 YaBrowser/23.11.0.0 Yowser/2.5 "
        "Safari/537.36",
        "desktop",
    ),
]

# Automated traffic that must be flagged as a bot (the inflation source).
_BOT_CASES: list[str] = [
    # Search / SEO crawlers (caught by generic tokens).
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)",
    "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)",
    # Uptime / performance monitors (the pre-launch offenders on /ro).
    "Mozilla/5.0 (compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)",
    "Pingdom.com_bot_version_1.4_(http://www.pingdom.com/)",
    "Mozilla/5.0 (compatible; StatusCake)",
    "Mozilla/5.0 (compatible; Site24x7)",
    "Datadog/Synthetics",
    "Better Uptime Bot Mozilla/5.0",
    # HTTP client libraries / scripted traffic.
    "curl/8.4.0",
    "Wget/1.21.3",
    "python-requests/2.31.0",
    "Go-http-client/2.0",
    "okhttp/4.12.0",
    "Java/17.0.9",
    "axios/1.6.2",
    "node-fetch/1.0",
    "PostmanRuntime/7.36.0",
    # Social / link-preview fetchers.
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "WhatsApp/2.23.20.0",
    # Headless browsers.
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/120.0.0.0 Safari/537.36",
]


@pytest.mark.parametrize(("user_agent", "expected_device"), _HUMAN_CASES)
def test_human_uas_classify_as_device(user_agent: str, expected_device: str) -> None:
    device, is_bot = classify_device(user_agent)
    assert is_bot is False
    assert device == expected_device


@pytest.mark.parametrize("user_agent", _BOT_CASES)
def test_bot_uas_are_flagged(user_agent: str) -> None:
    device, is_bot = classify_device(user_agent)
    assert is_bot is True
    assert device == "bot"


def test_missing_ua_is_bot() -> None:
    # No UA ⇒ server-to-server / scripted traffic, treated as a bot.
    assert classify_device(None) == ("bot", True)
    assert classify_device("") == ("bot", True)

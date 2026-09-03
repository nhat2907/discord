from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import shutil
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


APP_NAME = "OXY Sniper"
APP_VERSION = "2.1.1"
API_URL = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"

LETTERS = "abcdefghijklmnopqrstuvwxyz"
NUMBERS = "0123456789"
ALL_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"
LETTERS_NUMBERS = "abcdefghijklmnopqrstuvwxyz0123456789"
ALLOWED_USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_.")

MIN_USERNAME_LENGTH = 2
MAX_USERNAME_LENGTH = 32
MAX_WORKERS = 250
MAX_RETRIES = 4
MAX_RETRY_DELAY = 60.0
RECENT_DEDUPE_LIMIT = 500_000
WEBHOOK_QUEUE_LIMIT = 1_000


def application_directory() -> Path:
    """Return the directory beside the script or bundled executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = application_directory()
CONFIG_PATH = APP_DIR / "config.json"
LEGACY_SETTINGS_PATH = APP_DIR / "settings.json"
STATS_PATH = APP_DIR / "runtime_stats.json"
PROXY_PATH = APP_DIR / "proxies.txt"
WORDLIST_PATH = APP_DIR / "wordlist.txt"
LEGACY_WORDLIST_PATH = APP_DIR / "words.txt"


def ensure_runtime_files() -> list[str]:
    """Create empty runtime data files without altering existing content."""
    warnings: list[str] = []
    try:
        if not PROXY_PATH.exists():
            PROXY_PATH.touch()
    except OSError as exc:
        warnings.append(f"Could not create proxies.txt: {exc.__class__.__name__}.")

    try:
        if not WORDLIST_PATH.exists():
            if LEGACY_WORDLIST_PATH.exists():
                shutil.copyfile(LEGACY_WORDLIST_PATH, WORDLIST_PATH)
            else:
                WORDLIST_PATH.touch()
    except OSError as exc:
        warnings.append(f"Could not create wordlist.txt: {exc.__class__.__name__}.")
    return warnings


PALETTES: dict[str, dict[str, str]] = {
    "ghoul": {
        "primary": "bright_green",
        "secondary": "green",
        "accent": "bright_cyan",
        "success": "bright_green",
        "warning": "yellow",
        "error": "bright_red",
        "muted": "grey62",
    },
    "crimson": {
        "primary": "bright_red",
        "secondary": "red",
        "accent": "bright_magenta",
        "success": "bright_green",
        "warning": "yellow",
        "error": "bright_red",
        "muted": "grey62",
    },
    "neon": {
        "primary": "bright_magenta",
        "secondary": "magenta",
        "accent": "bright_cyan",
        "success": "bright_green",
        "warning": "yellow",
        "error": "bright_red",
        "muted": "grey62",
    },
    "ocean": {
        "primary": "bright_blue",
        "secondary": "blue",
        "accent": "bright_cyan",
        "success": "bright_green",
        "warning": "yellow",
        "error": "bright_red",
        "muted": "grey62",
    },
    "amber": {
        "primary": "bright_yellow",
        "secondary": "yellow",
        "accent": "bright_red",
        "success": "bright_green",
        "warning": "yellow",
        "error": "bright_red",
        "muted": "grey62",
    },
}


@dataclass(frozen=True)
class GeneratorMode:
    label: str
    alphabet: str
    force_underscore: bool = False


GENERATOR_MODES: dict[str, GeneratorMode] = {
    "1": GeneratorMode("Letters", LETTERS),
    "2": GeneratorMode("Numbers", NUMBERS),
    "3": GeneratorMode("Letters + Numbers", LETTERS_NUMBERS),
    "4": GeneratorMode("Letters + _", LETTERS, force_underscore=True),
    "5": GeneratorMode("Numbers + _", NUMBERS, force_underscore=True),
}


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


@dataclass
class AppConfig:
    webhook_url: str = ""
    user_id: str = ""
    ping_on_hit: bool = True
    workers: int = 250
    theme: str = "neon"
    animate: bool = True
    mode: str = "Letters + Numbers"
    length: int = 3
    random_dot: bool = False
    dot_at_end: bool = False
    request_delay_min: float = 1.0
    request_delay_max: float = 2.0
    generate_count: int = 0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AppConfig:
        defaults = cls()
        webhook = raw.get("webhook_url", raw.get("webhook", defaults.webhook_url))
        workers = raw.get("workers", raw.get("threads", defaults.workers))
        theme = str(raw.get("theme", defaults.theme)).strip().lower()
        if theme not in PALETTES:
            theme = defaults.theme

        try:
            delay_min = float(raw.get("request_delay_min", defaults.request_delay_min))
        except (TypeError, ValueError):
            delay_min = defaults.request_delay_min
        try:
            delay_max = float(raw.get("request_delay_max", defaults.request_delay_max))
        except (TypeError, ValueError):
            delay_max = defaults.request_delay_max
        delay_min = min(10.0, max(0.25, delay_min))
        delay_max = min(15.0, max(delay_min, delay_max))

        mode = str(raw.get("mode", defaults.mode))
        if mode == "Letters + Numbers + _":
            mode = "Letters + Numbers"

        return cls(
            webhook_url=str(webhook or "").strip(),
            user_id=str(raw.get("user_id", defaults.user_id) or "").strip(),
            ping_on_hit=True,
            workers=250,
            theme=theme,
            animate=True,
            mode=mode,
            length=coerce_int(raw.get("length", defaults.length), defaults.length, MIN_USERNAME_LENGTH, MAX_USERNAME_LENGTH),
            random_dot=coerce_bool(raw.get("random_dot", defaults.random_dot), defaults.random_dot),
            dot_at_end=coerce_bool(raw.get("dot_at_end", defaults.dot_at_end), defaults.dot_at_end),
            request_delay_min=delay_min,
            request_delay_max=delay_max,
            generate_count=coerce_int(raw.get("generate_count", defaults.generate_count), defaults.generate_count, 0, 10_000_000),
        )

    @classmethod
    def load(cls) -> tuple[AppConfig, list[str]]:
        warnings: list[str] = []
        source: Path | None = None
        if CONFIG_PATH.exists():
            source = CONFIG_PATH
        elif LEGACY_SETTINGS_PATH.exists():
            source = LEGACY_SETTINGS_PATH

        if source is None:
            return cls(), warnings

        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("configuration root must be an object")
            return cls.from_mapping(raw), warnings
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"Could not load {source.name}: {exc.__class__.__name__}. Defaults are active.")
            return cls(), warnings

    def save(self) -> None:
        payload = json.dumps(asdict(self), indent=4, ensure_ascii=False) + "\n"
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, CONFIG_PATH)


@dataclass
class LifetimeStats:
    total_checks: int = 0
    total_available: int = 0
    total_taken: int = 0
    total_errors: int = 0
    total_rate_limits: int = 0
    total_runtime_s: float = 0.0
    sessions: int = 0

    @classmethod
    def load(cls) -> LifetimeStats:
        candidates = [STATS_PATH, LEGACY_SETTINGS_PATH]
        for path in candidates:
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                return cls(
                    total_checks=max(0, int(raw.get("total_checks", 0))),
                    total_available=max(0, int(raw.get("total_available", raw.get("total_hits", 0)))),
                    total_taken=max(0, int(raw.get("total_taken", 0))),
                    total_errors=max(0, int(raw.get("total_errors", 0))),
                    total_rate_limits=max(0, int(raw.get("total_rate_limits", 0))),
                    total_runtime_s=max(0.0, float(raw.get("total_runtime_s", 0.0))),
                    sessions=max(0, int(raw.get("sessions", 0))),
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return cls()

    def add_session(self, stats: SessionStats) -> None:
        self.total_checks += stats.checked
        self.total_available += stats.available
        self.total_taken += stats.taken
        self.total_errors += stats.errors
        self.total_rate_limits += stats.rate_limits
        self.total_runtime_s += stats.elapsed
        self.sessions += 1

    def save(self) -> None:
        payload = json.dumps(asdict(self), indent=4, ensure_ascii=False) + "\n"
        temporary = STATS_PATH.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, STATS_PATH)


@dataclass
class SessionStats:
    checked: int = 0
    available: int = 0
    taken: int = 0
    errors: int = 0
    rate_limits: int = 0
    retries: int = 0
    invalid: int = 0
    duplicates: int = 0
    webhook_errors: int = 0
    last_http_status: int | None = None
    last_error: str = ""
    error_reasons: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    recent: deque[tuple[str, str, str]] = field(default_factory=lambda: deque(maxlen=9))

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    @property
    def cps(self) -> float:
        elapsed = self.elapsed
        return self.checked / elapsed if elapsed > 0 else 0.0

    def record(self, status: str, username: str, detail: str = "") -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.recent.appendleft((now, status, username if not detail else f"{username} — {detail}"))

    def record_failure(self, username: str, detail: str) -> None:
        reason = compact_error_text(detail) or "Unknown error"
        self.errors += 1
        self.last_error = f"{username} — {reason}"
        self.error_reasons[reason] = self.error_reasons.get(reason, 0) + 1
        self.record("ERROR", username, reason)

    @property
    def error_summary(self) -> str:
        if not self.error_reasons:
            return "None"
        ranked = sorted(self.error_reasons.items(), key=lambda item: (-item[1], item[0]))
        return "; ".join(f"{reason} ×{count}" for reason, count in ranked[:4])


@dataclass
class RunOptions:
    source: str
    workers: int
    ping_enabled: bool
    mode: GeneratorMode | None = None
    length: int = 0
    add_random_dot: bool = False
    add_dot_end: bool = False
    words: list[str] = field(default_factory=list)
    generate_count: int = 0

    @property
    def display_mode(self) -> str:
        if self.mode is not None:
            suffix = " + random dot" if self.add_random_dot else ""
            limit = f" [{self.generate_count:,}]" if self.generate_count > 0 else ""
            return f"{self.mode.label} / {self.length}{suffix}{limit}"
        suffix = " + trailing dot" if self.add_dot_end else ""
        return f"Wordlist{suffix}"


class RecentDeduper:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._values: set[str] = set()
        self._order: deque[str] = deque()

    def add(self, value: str) -> bool:
        if value in self._values:
            return False
        self._values.add(value)
        self._order.append(value)
        if len(self._order) > self.limit:
            oldest = self._order.popleft()
            self._values.discard(oldest)
        return True

    def __len__(self) -> int:
        return len(self._values)


def load_proxies(path: Path = PROXY_PATH) -> tuple[list[str], int]:
    """Load proxies using the original accepted formats without testing or pruning them."""
    if not path.exists():
        return [], 0

    proxies: list[str] = []
    skipped = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("http"):
                    proxies.append(line)
                    continue
                parts = line.split(":")
                if len(parts) == 2:
                    host, port = parts
                    proxies.append(f"http://{host}:{port}")
                elif len(parts) == 4:
                    host, port, user, password = parts
                    proxies.append(f"http://{user}:{password}@{host}:{port}")
                else:
                    skipped += 1
    except (OSError, UnicodeError):
        return [], 0
    return proxies, skipped


def load_wordlist(path: Path = WORDLIST_PATH) -> tuple[list[str], str | None]:
    if not path.exists():
        return [], f"{path.name} was not found beside the application."
    try:
        with path.open("r", encoding="utf-8") as handle:
            words = [line.strip().lower() for line in handle if line.strip()]
        return words, None
    except (OSError, UnicodeError) as exc:
        return [], f"Could not read {path.name}: {exc.__class__.__name__}."


def validate_username(username: str) -> tuple[bool, str]:
    if not MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH:
        return False, f"length must be {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH}"
    if any(character not in ALLOWED_USERNAME_CHARS for character in username):
        return False, "contains unsupported characters"
    if ".." in username:
        return False, "contains consecutive periods"
    return True, ""


def prepare_wordlist(words: list[str], add_dot_end: bool) -> tuple[list[str], int, int]:
    prepared: list[str] = []
    seen: set[str] = set()
    invalid = 0
    duplicates = 0

    for raw_word in words:
        username = raw_word.strip().lower()
        if add_dot_end and not username.endswith("."):
            username += "."
        valid, _ = validate_username(username)
        if not valid:
            invalid += 1
            continue
        if username in seen:
            duplicates += 1
            continue
        seen.add(username)
        prepared.append(username)
    return prepared, invalid, duplicates


def generate_username(mode: GeneratorMode, length: int, add_random_dot: bool) -> str:
    if mode.force_underscore:
        characters = [random.choice(mode.alphabet) for _ in range(length)]
        characters[random.randrange(length)] = "_"
    else:
        characters = [random.choice(mode.alphabet) for _ in range(length)]

    if add_random_dot:
        characters[random.randrange(length)] = "."
    return "".join(characters)


def username_space_size(mode: GeneratorMode, length: int, add_random_dot: bool) -> int:
    base = len(mode.alphabet)
    if mode.force_underscore:
        if add_random_dot:
            dot_replaces_underscore = length * (base ** (length - 1))
            both_symbols = length * (length - 1) * (base ** max(0, length - 2))
            return dot_replaces_underscore + both_symbols
        return length * (base ** (length - 1))
    if add_random_dot:
        return length * (base ** (length - 1))
    return base**length


def mask_webhook(url: str) -> str:
    if not url:
        return "Not configured"
    try:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        webhook_index = parts.index("webhooks")
        webhook_id = parts[webhook_index + 1]
        if webhook_id:
            return f"{parsed.netloc}/api/webhooks/{webhook_id}/••••••••"
    except (ValueError, IndexError):
        pass
    return "Configured (hidden)"


def clean_webhook_url(url: str) -> str:
    cleaned = "".join(char for char in url if ord(char) >= 32)
    if "https://" in cleaned:
        parts = cleaned.split("https://")
        if len(parts) > 1:
            cleaned = "https://" + parts[1]
    return cleaned.strip()


def valid_webhook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and "/api/webhooks/" in parsed.path


def valid_user_id(user_id: str) -> bool:
    return not user_id or (user_id.isdigit() and 15 <= len(user_id) <= 22)


async def sleep_or_stop(stop_event: asyncio.Event, delay: float) -> bool:
    """Sleep for delay seconds; return False when a stop was requested."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, delay))
        return False
    except TimeoutError:
        return True


def compact_error_text(value: Any, limit: int = 160) -> str:
    """Normalize untrusted error text for one safe terminal line."""
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


async def describe_http_error(response: aiohttp.ClientResponse) -> str:
    """Return a concise status/code/message without dumping HTML or secrets."""
    parts = [f"HTTP {response.status}"]
    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ClientError, json.JSONDecodeError, ValueError, TypeError):
        parts.append("non-JSON response")
        return " | ".join(parts)

    if not isinstance(payload, dict):
        parts.append(f"unexpected {type(payload).__name__} body")
        return " | ".join(parts)

    code = payload.get("code")
    message = compact_error_text(payload.get("message"))
    if code is not None and code != "":
        parts.append(f"code {compact_error_text(code, 40)}")
    if message:
        parts.append(message)
    elif "retry_after" in payload:
        parts.append(f"retry after {compact_error_text(payload.get('retry_after'), 40)}s")
    else:
        safe_keys = ", ".join(sorted(str(key) for key in payload)[:6])
        parts.append(f"response keys: {safe_keys or 'none'}")
    return compact_error_text(" | ".join(parts))


async def response_retry_delay(response: aiohttp.ClientResponse, fallback: float) -> float:
    raw_value: Any = response.headers.get("Retry-After")
    if raw_value is None:
        with contextlib.suppress(aiohttp.ClientError, json.JSONDecodeError, ValueError, TypeError):
            payload = await response.json(content_type=None)
            if isinstance(payload, dict):
                raw_value = payload.get("retry_after")
    try:
        delay = float(raw_value)
    except (TypeError, ValueError):
        delay = fallback
    return min(MAX_RETRY_DELAY, max(0.25, delay))


class WebhookNotifier:
    def __init__(self, config: AppConfig, stats: SessionStats) -> None:
        self.config = config
        self.stats = stats
        self.queue: asyncio.Queue[tuple[str, bool] | None] = asyncio.Queue(maxsize=WEBHOOK_QUEUE_LIMIT)
        self.task: asyncio.Task[None] | None = None
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if not self.config.webhook_url:
            return
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json"
        }
        self.session = aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=False)
        self.task = asyncio.create_task(self._run(), name="webhook-notifier")

    def enqueue(self, username: str, ping: bool) -> None:
        if self.task is None:
            return
        try:
            self.queue.put_nowait((username, ping))
        except asyncio.QueueFull:
            self.stats.webhook_errors += 1
            self.stats.record("WEBHOOK", username, "notification queue full")

    async def _run(self) -> None:
        assert self.session is not None
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                username, ping = item
                await self._send(username, ping)
            finally:
                self.queue.task_done()

    async def _send(self, username: str, ping: bool) -> None:
        assert self.session is not None
        content = f"<@{self.config.user_id}>" if ping and self.config.user_id else None
        allowed_mentions: dict[str, Any]
        if content:
            allowed_mentions = {"parse": [], "users": [self.config.user_id]}
        else:
            allowed_mentions = {"parse": []}
        payload = {
            "content": content,
            "allowed_mentions": allowed_mentions,
            "embeds": [
                {
                    "title": "✅ Username available",
                    "description": f"`{username}`",
                    "color": 0x00FF66,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {"text": APP_NAME},
                }
            ],
        }

        backoff = 1.0
        for attempt in range(3):
            try:
                async with self.session.post(self.config.webhook_url, json=payload) as response:
                    if response.status in {200, 204}:
                        return
                    if response.status == 429:
                        delay = await response_retry_delay(response, backoff)
                        await asyncio.sleep(delay)
                        backoff = min(MAX_RETRY_DELAY, backoff * 2)
                        continue
                    self.stats.webhook_errors += 1
                    detail = await describe_http_error(response)
                    self.stats.record("WEBHOOK", username, detail)
                    return
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                if attempt == 2:
                    self.stats.webhook_errors += 1
                    self.stats.record("WEBHOOK", username, f"delivery failed: {exc.__class__.__name__}")
                    return
                await asyncio.sleep(backoff)
                backoff = min(MAX_RETRY_DELAY, backoff * 2)

    async def close(self, flush_timeout: float = 20.0) -> None:
        if self.task is None:
            return
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)
        try:
            await asyncio.wait_for(self.task, timeout=flush_timeout)
        except TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        finally:
            if self.session is not None:
                await self.session.close()
            self.task = None
            self.session = None


class DiscordUsernameChecker:
    def __init__(
        self,
        config: AppConfig,
        options: RunOptions,
        proxies: list[str],
        stats: SessionStats,
        notifier: WebhookNotifier,
        stop_event: asyncio.Event,
    ) -> None:
        self.config = config
        self.options = options
        self.proxies = proxies
        self.stats = stats
        self.notifier = notifier
        self.stop_event = stop_event
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://discord.com/",
            "Origin": "https://discord.com",
        }

    async def check_username(self, session: aiohttp.ClientSession, username: str) -> None:
        valid, reason = validate_username(username)
        if not valid:
            self.stats.invalid += 1
            self.stats.record("INVALID", username, reason)
            return

        proxy = random.choice(self.proxies) if self.proxies else None
        backoff = 1.0

        for attempt in range(MAX_RETRIES):
            if self.stop_event.is_set():
                return
            try:
                async with session.post(API_URL, json={"username": username}, proxy=proxy) as response:
                    self.stats.last_http_status = response.status
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        if not isinstance(payload, dict) or not isinstance(payload.get("taken"), bool):
                            raise ValueError("unexpected response payload")
                        taken = payload["taken"]
                        self.stats.checked += 1
                        if taken:
                            self.stats.taken += 1
                            self.stats.record("TAKEN", username)
                        else:
                            self.stats.available += 1
                            self.stats.record("AVAILABLE", username)
                            self.notifier.enqueue(username, self.options.ping_enabled)
                        return

                    if response.status == 429:
                        self.stats.rate_limits += 1
                        delay = await response_retry_delay(response, backoff)
                        if attempt == MAX_RETRIES - 1:
                            self.stats.record_failure(
                                username,
                                f"HTTP 429 | rate limited after {MAX_RETRIES} attempts | Retry-After {delay:.1f}s",
                            )
                            return
                        self.stats.record("429", username, f"retrying in {delay:.1f}s")
                        if not await sleep_or_stop(self.stop_event, delay):
                            return
                        self.stats.retries += 1
                        backoff = min(MAX_RETRY_DELAY, backoff * 1.8)
                        continue

                    if 500 <= response.status < 600:
                        self.stats.retries += 1
                        if attempt == MAX_RETRIES - 1:
                            self.stats.record_failure(username, await describe_http_error(response))
                            return
                        if not await sleep_or_stop(self.stop_event, backoff):
                            return
                        backoff = min(15.0, backoff * 1.5)
                        continue

                    self.stats.record_failure(username, await describe_http_error(response))
                    return
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                self.stats.retries += 1
                if attempt == MAX_RETRIES - 1:
                    self.stats.record_failure(
                        username,
                        f"Proxy/network failure: {exc.__class__.__name__}",
                    )
                    return
                if not await sleep_or_stop(self.stop_event, backoff):
                    return
                backoff = min(15.0, backoff * 1.5)
            except json.JSONDecodeError:
                self.stats.record_failure(username, "HTTP 200 | invalid JSON response")
                return
            except (ValueError, TypeError) as exc:
                self.stats.record_failure(
                    username,
                    f"HTTP {self.stats.last_http_status or '?'} | invalid payload: {exc.__class__.__name__}",
                )
                return

    async def wordlist_worker(self, queue: asyncio.Queue[str | None]) -> None:
        timeout = aiohttp.ClientTimeout(total=8)
        connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300)
        async with aiohttp.ClientSession(
            headers=self.headers,
            timeout=timeout,
            connector=connector,
            trust_env=False,
        ) as session:
            while not self.stop_event.is_set():
                username = await queue.get()
                try:
                    if username is None:
                        return
                    await self.check_username(session, username)
                    delay = random.uniform(self.config.request_delay_min, self.config.request_delay_max)
                    if not await sleep_or_stop(self.stop_event, delay):
                        return
                finally:
                    queue.task_done()

    async def generator_worker(self, deduper: RecentDeduper, namespace_size: int) -> None:
        assert self.options.mode is not None
        timeout = aiohttp.ClientTimeout(total=8)
        connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300)
        async with aiohttp.ClientSession(
            headers=self.headers,
            timeout=timeout,
            connector=connector,
            trust_env=False,
        ) as session:
            while not self.stop_event.is_set():
                username = generate_username(self.options.mode, self.options.length, self.options.add_random_dot)
                if not deduper.add(username):
                    self.stats.duplicates += 1
                    if namespace_size <= RECENT_DEDUPE_LIMIT and len(deduper) >= namespace_size:
                        self.stats.record("DONE", "generator", "username space exhausted")
                        self.stop_event.set()
                        return
                    await asyncio.sleep(0)
                    continue
                await self.check_username(session, username)
                delay = random.uniform(self.config.request_delay_min, self.config.request_delay_max)
                if not await sleep_or_stop(self.stop_event, delay):
                    return


class TerminalUI:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.console = Console(highlight=False, soft_wrap=True)
        with contextlib.suppress(Exception):
            self.console.set_window_title(f"{APP_NAME} v{APP_VERSION}")

    @property
    def colors(self) -> dict[str, str]:
        return PALETTES.get(self.config.theme, PALETTES["neon"])

    def color(self, name: str) -> str:
        return self.colors[name]

    def clear(self) -> None:
        import os
        os.system("cls" if os.name == "nt" else "clear")

    def header(self, subtitle: str) -> None:
        pass

    def print(self, *args: Any, **kwargs: Any) -> None:
        import sys
        import time
        with self.console.capture() as capture:
            self.console.print(*args, **kwargs)
        rendered = capture.get()
        
        i = 0
        n = len(rendered)
        delay = 0.005
        while i < n:
            if rendered[i] == '\x1b':
                start = i
                i += 1
                if i < n and rendered[i] == '[':
                    i += 1
                    while i < n and not (0x40 <= ord(rendered[i]) <= 0x7E):
                        i += 1
                    if i < n:
                        i += 1
                sys.stdout.write(rendered[start:i])
                sys.stdout.flush()
            else:
                sys.stdout.write(rendered[i])
                sys.stdout.flush()
                if rendered[i] not in ('\r', '\n', '\t'):
                    time.sleep(delay)
                i += 1

    def section(self, title: str) -> None:
        width = max(8, min(34, self.console.width - len(title) - 8))
        self.print(
            f"[{self.color('muted')}]┌──[/] [bold white]{title.upper()}[/] "
            f"[{self.color('muted')}]{'─' * width}[/]"
        )

    def key_values(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.section(title)
        for key, value in rows:
            self.print(
                f"  [{self.color('muted')}]{key:<18}[/] "
                f"[{self.color('muted')}]│[/] {value}"
            )
        self.print()

    def loading(self, message: str, duration: float = 0.55) -> None:
        if not self.config.animate:
            return
        with self.console.status(f"[{self.color('accent')}]{message}[/]", spinner="dots12"):
            time.sleep(duration)

    def menu(self, title: str, options: list[tuple[str, str, str]]) -> None:
        self.section(title)
        for key, label, description in options:
            self.print(
                f"  [bold {self.color('primary')}]{key}[/] "
                f"[{self.color('muted')}]│[/] [bold white]{label}[/]"
            )
            self.print(f"    [{self.color('muted')}]└─ {description}[/]")
        self.print()

    def ask_choice(self, valid_choices: list[str], prompt: str = "Select") -> str:
        label = "Select option" if prompt == "Select" else prompt
        while True:
            value = self.console.input(
                f"[bold white]{label}[/] [bold {self.color('primary')}]›[/] "
            ).strip()
            if value in valid_choices:
                return value
            self.error(f"Choose one of: {', '.join(valid_choices)}")

    def pause(self) -> None:
        self.console.input(f"\n[{self.color('muted')}]Press Enter to continue[/] ")

    def success(self, message: str) -> None:
        self.print(f"[bold {self.color('success')}]✓ {message}[/]")

    def warning(self, message: str) -> None:
        self.print(f"[bold {self.color('warning')}]! {message}[/]")

    def error(self, message: str) -> None:
        self.print(f"[bold {self.color('error')}]✗ {message}[/]")

    def build_dashboard(
        self,
        stats: SessionStats,
        options: RunOptions,
        proxy_count: int,
        stopping: bool,
    ) -> Group:
        summary = Table(expand=True, box=None, padding=(0, 1))
        for heading in ["Checked", "Available", "Taken", "Errors", "429s", "CPS"]:
            summary.add_column(heading, justify="center")
        summary.add_row(
            f"[bold]{stats.checked:,}[/]",
            f"[bold {self.color('success')}]{stats.available:,}[/]",
            f"[{self.color('error')}]{stats.taken:,}[/]",
            f"[{self.color('warning')}]{stats.errors:,}[/]",
            str(stats.rate_limits),
            f"{stats.cps:.2f}",
        )

        diagnostics = Text(no_wrap=True, overflow="ellipsis")
        diagnostics.append("  Latest HTTP ", style=self.color("muted"))
        diagnostics.append(
            str(stats.last_http_status) if stats.last_http_status is not None else "waiting",
            style="bold white",
        )
        diagnostics.append("  │  Latest error ", style=self.color("muted"))
        diagnostics.append(
            stats.last_error or "none",
            style=self.color("warning") if stats.last_error else self.color("success"),
        )

        activity = Table(expand=True, box=None, padding=(0, 1))
        activity.add_column("Status", width=10, no_wrap=True)
        activity.add_column("Username", ratio=1, no_wrap=True, overflow="ellipsis")
        activity.add_column("Error / detail", ratio=2, no_wrap=True, overflow="ellipsis")
        status_colors = {
            "AVAILABLE": self.color("success"),
            "TAKEN": self.color("error"),
            "ERROR": self.color("warning"),
            "429": self.color("warning"),
            "INVALID": self.color("warning"),
            "WEBHOOK": self.color("warning"),
            "DONE": self.color("accent"),
        }
        console_height = self.console.height or 24
        max_rows = max(1, console_height - 12)
        if stats.recent:
            for timestamp, status, text in list(stats.recent)[:max_rows]:
                style = status_colors.get(status, self.color("accent"))
                username, separator, detail = text.partition(" — ")
                activity.add_row(
                    Text(status, style=f"bold {style}"),
                    Text(username, no_wrap=True, overflow="ellipsis"),
                    Text(detail if separator else "—", no_wrap=True, overflow="ellipsis"),
                )
        else:
            activity.add_row(
                Text("READY", style=self.color("accent")),
                Text("Waiting for results…"),
                Text("—"),
            )

        state = "stopping cleanly…" if stopping else "running — Ctrl+C to stop"
        section_live = Text("┌── LIVE SNIPER ───────────────────────", style=self.color("muted"))
        section_activity = Text("┌── RECENT ACTIVITY ───────────────────", style=self.color("muted"))
        footer = Text(f"  └─ {state}", style=self.color("muted"))
        return Group(
            section_live,
            summary,
            diagnostics,
            Text(""),
            section_activity,
            activity,
            footer,
        )


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


async def test_webhook(config: AppConfig) -> tuple[bool, str]:
    if not config.webhook_url:
        return False, "No webhook is configured."
    timeout = aiohttp.ClientTimeout(total=10)
    payload = {
        "content": None,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "✅ OXY connection test",
                "description": "Webhook notifications are configured correctly.",
                "color": 0x00FF66,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=False) as session:
            async with session.post(config.webhook_url, json=payload) as response:
                if response.status in {200, 204}:
                    return True, "Webhook test delivered."
                return False, f"Webhook returned HTTP {response.status}."
    except (aiohttp.ClientError, TimeoutError) as exc:
        return False, f"Webhook test failed: {exc.__class__.__name__}."


class OxyApplication:
    def __init__(self) -> None:
        file_warnings = ensure_runtime_files()
        self.config, config_warnings = AppConfig.load()
        self.startup_warnings = file_warnings + config_warnings
        self.lifetime_stats = LifetimeStats.load()
        self.ui = TerminalUI(self.config)

    def run(self) -> None:
        alternate_screen = self.ui.console.set_alt_screen(True)
        try:
            self.ui.loading("Booting OXY interface")
            while True:
                self.ui.clear()
                self.ui.header("Main menu")
                for warning in self.startup_warnings:
                    self.ui.warning(warning)
                self.startup_warnings.clear()
                self.ui.menu(
                    "Main menu",
                    [
                        ("1", "Start checker", "Configure a generator or wordlist session"),
                        ("2", "Settings", "Webhook, proxy file, workers, and appearance"),
                        ("3", "Information", "Runtime totals, files, and checker behavior"),
                        ("4", "Exit", "Close the application"),
                    ],
                )
                choice = self.ui.ask_choice(["1", "2", "3", "4"])
                if choice == "1":
                    self.start_menu()
                elif choice == "2":
                    self.settings_menu()
                elif choice == "3":
                    self.info_menu()
                else:
                    return
        finally:
            if alternate_screen:
                self.ui.console.set_alt_screen(False)

    def save_config(self) -> bool:
        try:
            self.config.save()
            return True
        except OSError as exc:
            self.ui.error(f"Could not save config.json: {exc.__class__.__name__}.")
            return False

    def settings_menu(self) -> None:
        while True:
            self.ui.clear()
            self.ui.header("Settings")
            proxies, malformed = load_proxies()
            words, _ = load_wordlist()
            self.ui.key_values(
                "Current setup",
                [
                    ("Webhook", mask_webhook(self.config.webhook_url)),
                    ("Discord user ID", self.config.user_id or "Not configured"),
                    ("Proxy file", f"{len(proxies):,} loaded / {malformed:,} malformed"),
                    ("Wordlist file", f"{len(words):,} entries"),
                    ("Theme", self.config.theme.title()),
                ],
            )
            self.ui.menu(
                "Settings",
                [
                    ("1", "Webhook", "Set, replace, or clear the notification URL"),
                    ("2", "Discord user ID", "Set the optional mention target"),
                    ("3", "Proxy file", "Paste, append, replace, or clear proxies.txt"),
                    ("4", "Wordlist file", "Paste, append, replace, or clear wordlist.txt"),
                    ("5", "Theme", "Choose the terminal color palette"),
                    ("6", "Test webhook", "Send one connection-test notification"),
                    ("0", "Back", "Return to the main menu"),
                ],
            )
            choice = self.ui.ask_choice(["0", "1", "2", "3", "4", "5", "6"])
            if choice == "1":
                self.change_webhook()
            elif choice == "2":
                self.change_user_id()
            elif choice == "3":
                self.manage_text_file(PROXY_PATH, "Proxy file", "proxy")
            elif choice == "4":
                self.manage_text_file(WORDLIST_PATH, "Wordlist file", "wordlist")
            elif choice == "5":
                self.change_theme()
            elif choice == "6":
                self.run_webhook_test()
            else:
                return

    def manage_text_file(self, path: Path, title: str, kind: str) -> None:
        while True:
            self.ui.clear()
            self.ui.header(title)
            if kind == "proxy":
                entries, malformed = load_proxies(path)
                status_rows = [
                    ("File", path.name),
                    ("Loaded entries", f"{len(entries):,}"),
                    ("Malformed lines", f"{malformed:,}"),
                    ("Handling", "Stored exactly as pasted; no probing or removal"),
                ]
            else:
                entries, error = load_wordlist(path)
                status_rows = [
                    ("File", path.name),
                    ("Non-empty lines", f"{len(entries):,}"),
                    ("Status", error or "Ready"),
                    ("Handling", "Normalized only when a session starts"),
                ]
            self.ui.key_values("File status", status_rows)
            self.ui.menu(
                "File actions",
                [
                    ("1", "Replace from paste", f"Replace all content in {path.name}"),
                    ("2", "Append from paste", f"Add pasted lines to {path.name}"),
                    ("3", "Clear file", f"Empty {path.name} after confirmation"),
                    ("4", "Back", "Return to Settings"),
                ],
            )
            choice = self.ui.ask_choice(["1", "2", "3", "4"])
            if choice == "4":
                return
            if choice == "3":
                if Confirm.ask(f"Clear every line in {path.name}?", default=False):
                    try:
                        path.write_text("", encoding="utf-8")
                        self.ui.success(f"{path.name} cleared.")
                    except OSError as exc:
                        self.ui.error(f"Could not clear {path.name}: {exc.__class__.__name__}.")
                    self.ui.pause()
                continue

            replace = choice == "1"
            if replace and not Confirm.ask(f"Replace all existing content in {path.name}?", default=False):
                continue
            pasted = self.collect_pasted_lines(path.name, hide_input=kind == "proxy")
            if pasted is None:
                continue
            if not pasted:
                self.ui.warning("Nothing was pasted; the file was not changed.")
                self.ui.pause()
                continue
            try:
                self.write_pasted_lines(path, pasted, append=not replace)
                action = "appended to" if not replace else "written to"
                self.ui.success(f"{len(pasted):,} lines {action} {path.name}.")
            except OSError as exc:
                self.ui.error(f"Could not update {path.name}: {exc.__class__.__name__}.")
            self.ui.pause()

    def collect_pasted_lines(self, filename: str, hide_input: bool = False) -> list[str] | None:
        self.ui.clear()
        self.ui.header(f"Paste into {filename}")
        self.ui.section("Paste input")
        self.ui.print(
            f"  [{self.ui.color('muted')}]Paste as many lines as needed. On a new line type "
            f"[bold {self.ui.color('primary')}]::done[/] to save or "
            f"[bold {self.ui.color('warning')}]::cancel[/] to abort."
            f"{' Proxy values are hidden while entered.' if hide_input else ''}[/]\n"
        )
        lines: list[str] = []
        while True:
            try:
                line = self.ui.console.input(
                    f"[{self.ui.color('muted')}]{len(lines) + 1:>5} │[/] ",
                    password=hide_input,
                )
            except EOFError:
                return None
            command = line.strip().lower()
            if command == "::done":
                return lines
            if command == "::cancel":
                return None
            lines.append(line)

    @staticmethod
    def write_pasted_lines(path: Path, lines: list[str], append: bool) -> None:
        payload = "\n".join(lines) + "\n"
        if not append:
            path.write_text(payload, encoding="utf-8", newline="\n")
            return

        separator = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) not in {b"\n", b"\r"}:
                    separator = "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(separator + payload)

    def change_webhook(self) -> None:
        self.ui.clear()
        self.ui.header("Settings — Webhook")
        self.ui.print(
            f"[{self.ui.color('muted')}]Paste a Discord webhook URL. Enter [bold]clear[/] to remove it or leave blank to cancel.[/]"
        )
        value = Prompt.ask("Webhook URL", default="", show_default=False).strip()
        if not value:
            return
        if value.lower() == "clear":
            self.config.webhook_url = ""
            if self.save_config():
                self.ui.success("Webhook cleared.")
                time.sleep(0.45)
            return
        value = clean_webhook_url(value)
        if not valid_webhook_url(value):
            self.ui.error("That is not a supported Discord webhook URL.")
            self.ui.pause()
            return
        self.config.webhook_url = value
        if self.save_config():
            self.ui.success("Webhook saved and hidden from the interface.")
            time.sleep(0.45)

    def change_user_id(self) -> None:
        self.ui.clear()
        self.ui.header("Settings — Discord User ID")
        value = Prompt.ask("Discord user ID (blank clears it)", default="", show_default=False).strip()
        if not valid_user_id(value):
            self.ui.error("The user ID must contain 15-22 digits.")
            self.ui.pause()
            return
        self.config.user_id = value
        if self.save_config():
            self.ui.success("Discord user ID updated.")
            time.sleep(0.45)

    def change_theme(self) -> None:
        self.ui.clear()
        self.ui.header("Theme gallery")
        rows: list[tuple[str, str, str]] = []
        theme_names = list(PALETTES)
        for index, name in enumerate(theme_names, start=1):
            marker = "current" if name == self.config.theme else "palette"
            rows.append((str(index), name.title(), marker))
        rows.append((str(len(theme_names) + 1), "Back", "Keep the current theme"))
        self.ui.menu("Themes", rows)
        valid = [str(number) for number in range(1, len(rows) + 1)]
        choice = self.ui.ask_choice(valid)
        if choice == str(len(rows)):
            return
        self.config.theme = theme_names[int(choice) - 1]
        if self.save_config():
            self.ui.success(f"Theme changed to {self.config.theme.title()}.")
            time.sleep(0.45)

    def run_webhook_test(self) -> None:
        if not self.config.webhook_url:
            self.ui.error("Configure a webhook first.")
            self.ui.pause()
            return
        if not Confirm.ask("Send one test notification now?", default=False):
            return
        self.ui.loading("Contacting webhook", duration=0.7)
        try:
            success, message = asyncio.run(test_webhook(self.config))
        except KeyboardInterrupt:
            self.ui.warning("Webhook test cancelled.")
            self.ui.pause()
            return
        if success:
            self.ui.success(message)
        else:
            self.ui.error(message)
        self.ui.pause()

    def info_menu(self) -> None:
        proxies, skipped_proxies = load_proxies()
        words, word_error = load_wordlist()
        self.ui.clear()
        self.ui.header("Information")
        self.ui.key_values(
            "Components",
            [
                ("Application", f"{APP_NAME} v{APP_VERSION}"),
                ("Python runtime", sys.version.split()[0]),
                ("HTTP runtime", f"aiohttp {getattr(aiohttp, '__version__', 'bundled')}"),
                ("Terminal UI", "Rich bundled runtime"),
                ("proxies.txt", f"{len(proxies):,} loaded / {skipped_proxies:,} malformed"),
                ("wordlist.txt", f"{len(words):,} entries" if not word_error else word_error),
                ("Webhook", mask_webhook(self.config.webhook_url)),
            ],
        )

        totals = Table(expand=True, box=None)
        totals.add_column("Sessions", justify="center")
        totals.add_column("Checks", justify="center")
        totals.add_column("Available", justify="center")
        totals.add_column("Taken", justify="center")
        totals.add_column("Errors", justify="center")
        totals.add_column("Runtime", justify="center")
        totals.add_row(
            f"{self.lifetime_stats.sessions:,}",
            f"{self.lifetime_stats.total_checks:,}",
            f"[{self.ui.color('success')}]{self.lifetime_stats.total_available:,}[/]",
            f"{self.lifetime_stats.total_taken:,}",
            f"{self.lifetime_stats.total_errors:,}",
            format_duration(self.lifetime_stats.total_runtime_s),
        )
        self.ui.section("Lifetime totals")
        self.ui.print(totals)
        self.ui.print()
        self.ui.key_values(
            "Behavior",
            [
                ("Username rules", "2-32 lowercase a-z, 0-9, underscore, and period; no consecutive periods"),
                ("Proxy behavior", "Random existing entry per username; no probing, scoring, removal, or quarantine"),
                ("Rate limits", "Retry-After is honored and bounded; retry attempts are finite"),
                ("Shutdown", "Ctrl+C closes workers/sessions, flushes notifications, and saves totals"),
            ],
        )
        self.ui.pause()

    def start_menu(self) -> None:
        while True:
            self.ui.clear()
            self.ui.header("Session setup")
            self.ui.menu(
                "Source",
                [
                    ("1", "Generator", "Create usernames from selected character sets"),
                    ("2", "Wordlist", "Read candidates from wordlist.txt"),
                    ("3", "Back", "Return to the main menu"),
                ],
            )
            source_choice = self.ui.ask_choice(["1", "2", "3"])
            if source_choice == "3":
                return
            options = self.configure_generator() if source_choice == "1" else self.configure_wordlist()
            if options is None:
                continue
            if not self.confirm_session(options):
                continue
            self.run_session(options)

    def ask_username_length(self) -> int:
        while True:
            value = IntPrompt.ask(
                f"Username length ({MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH})",
                default=self.config.length,
            )
            if MIN_USERNAME_LENGTH <= value <= MAX_USERNAME_LENGTH:
                return value
            self.ui.error(f"Length must be between {MIN_USERNAME_LENGTH} and {MAX_USERNAME_LENGTH}.")

    def configure_generator(self) -> RunOptions | None:
        self.ui.clear()
        self.ui.header("Generator setup")
        rows = [(key, mode.label, "Generated continuously until Ctrl+C") for key, mode in GENERATOR_MODES.items()]
        rows.append(("6", "Back", "Return to source selection"))
        self.ui.menu("Character mode", rows)
        choice = self.ui.ask_choice(["1", "2", "3", "4", "5", "6"])
        if choice == "6":
            return None
        mode = GENERATOR_MODES[choice]
        
        self.ui.clear()
        self.ui.header("Generator setup — Parameters")
        self.ui.key_values("Selected mode", [("Mode", mode.label)])
        
        length = self.ask_username_length()
        add_random_dot = Confirm.ask("Replace one random position with a period?", default=self.config.random_dot)
        generate_count = max(0, IntPrompt.ask("Amount to generate (0 for continuous)", default=self.config.generate_count))
        workers = 250

        self.config.mode = mode.label
        self.config.length = length
        self.config.random_dot = add_random_dot
        self.config.workers = workers
        self.config.generate_count = generate_count
        self.save_config()
        return RunOptions(
            source="generator",
            workers=workers,
            ping_enabled=self.config.ping_on_hit,
            mode=mode,
            length=length,
            add_random_dot=add_random_dot,
            generate_count=generate_count,
        )

    def configure_wordlist(self) -> RunOptions | None:
        self.ui.clear()
        self.ui.header("Wordlist setup")
        self.ui.loading("Loading wordlist.txt")
        words, error = load_wordlist()
        if error:
            self.ui.error(error)
            self.ui.pause()
            return None
            
        self.ui.clear()
        self.ui.header("Wordlist setup — Parameters")
        self.ui.key_values("Loaded wordlist", [("Total raw words", f"{len(words):,}")])
        
        add_dot_end = Confirm.ask("Add a period to entries that do not already end with one?", default=self.config.dot_at_end)
        prepared, invalid, duplicates = prepare_wordlist(words, add_dot_end)
        if not prepared:
            self.ui.error("No valid usernames remain after validation.")
            self.ui.pause()
            return None
        if invalid:
            self.ui.warning(f"{invalid:,} invalid entries will be skipped.")
        if duplicates:
            self.ui.warning(f"{duplicates:,} duplicate entries were removed.")
        workers = 250
        self.config.dot_at_end = add_dot_end
        self.config.workers = workers
        self.save_config()
        return RunOptions(
            source="wordlist",
            workers=workers,
            ping_enabled=self.config.ping_on_hit,
            add_dot_end=add_dot_end,
            words=prepared,
        )

    def confirm_session(self, options: RunOptions) -> bool:
        self.ui.clear()
        self.ui.header("Session confirmation")
        proxies, skipped = load_proxies()
        self.ui.key_values(
            "Session summary",
            [
                ("Mode", options.display_mode),
                ("Candidates", f"{len(options.words):,}" if options.source == "wordlist" else "Continuous"),
                ("Workers", str(options.workers)),
                ("Proxies", f"{len(proxies):,}" if proxies else "Direct connection"),
                ("Malformed proxies", f"{skipped:,} skipped"),
                ("Webhook", "Enabled" if self.config.webhook_url else "Disabled"),
                ("Ping available hits", "Yes" if options.ping_enabled and self.config.user_id else "No"),
            ],
        )
        return Confirm.ask("Start this session?", default=True)

    def run_session(self, options: RunOptions) -> None:
        self.ui.clear()
        self.ui.header("Starting session")
        proxies, skipped = load_proxies()
        if skipped:
            self.ui.warning(f"Skipped {skipped:,} malformed proxy lines; valid entries remain untouched.")

        if options.source == "generator" and options.generate_count > 0:
            assert options.mode is not None
            namespace = username_space_size(options.mode, options.length, options.add_random_dot)
            target_count = min(options.generate_count, namespace)
            self.ui.loading(f"Generating {target_count:,} unique usernames", duration=0.45)
            
            seen: set[str] = set()
            generated_list: list[str] = []
            while len(seen) < target_count:
                username = generate_username(options.mode, options.length, options.add_random_dot)
                valid, _ = validate_username(username)
                if valid and username not in seen:
                    seen.add(username)
                    generated_list.append(username)
            options.words = generated_list
            options.source = "wordlist"

        self.ui.loading("Preparing workers", duration=0.7)
        self.ui.clear()
        try:
            stats, cancelled = asyncio.run(self.execute_session(options, proxies))
        except KeyboardInterrupt:
            self.ui.warning("Session interrupted.")
            self.ui.pause()
            return

        self.ui.clear()
        self.ui.header("Session results")
        if cancelled:
            self.ui.warning("Session stopped cleanly.")
        else:
            self.ui.success("Session completed.")
        self.ui.key_values(
            "Final session totals",
            [
                ("Checked", f"{stats.checked:,}"),
                ("Available", f"{stats.available:,}"),
                ("Taken", f"{stats.taken:,}"),
                ("Errors", f"{stats.errors:,}"),
                ("Rate limits", f"{stats.rate_limits:,}"),
                ("Latest HTTP", str(stats.last_http_status) if stats.last_http_status is not None else "None"),
                ("Latest error", escape(stats.last_error or "None")),
                ("Error breakdown", escape(stats.error_summary)),
                ("Webhook errors", f"{stats.webhook_errors:,}"),
                ("Runtime", format_duration(stats.elapsed)),
            ],
        )
        self.ui.pause()

    async def execute_session(self, options: RunOptions, proxies: list[str]) -> tuple[SessionStats, bool]:
        stats = SessionStats()
        stop_event = asyncio.Event()
        notifier = WebhookNotifier(self.config, stats)
        checker = DiscordUsernameChecker(self.config, options, proxies, stats, notifier, stop_event)
        tasks: list[asyncio.Task[None]] = []
        refresh_task: asyncio.Task[None] | None = None
        cancelled = False

        await notifier.start()
        if options.source == "wordlist":
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            for username in options.words:
                queue.put_nowait(username)
            for _ in range(options.workers):
                queue.put_nowait(None)
            tasks = [
                asyncio.create_task(checker.wordlist_worker(queue), name=f"wordlist-worker-{index + 1}")
                for index in range(options.workers)
            ]
        else:
            assert options.mode is not None
            namespace = username_space_size(options.mode, options.length, options.add_random_dot)
            deduper = RecentDeduper(min(namespace, RECENT_DEDUPE_LIMIT))
            tasks = [
                asyncio.create_task(checker.generator_worker(deduper, namespace), name=f"generator-worker-{index + 1}")
                for index in range(options.workers)
            ]

        with Live(
            self.ui.build_dashboard(stats, options, len(proxies), False),
            console=self.ui.console,
            refresh_per_second=8,
            screen=False,
            transient=True,
        ) as live:
            async def refresh_dashboard() -> None:
                while not stop_event.is_set():
                    live.update(self.ui.build_dashboard(stats, options, len(proxies), False))
                    await asyncio.sleep(0.125)
                live.update(self.ui.build_dashboard(stats, options, len(proxies), True), refresh=True)

            refresh_task = asyncio.create_task(refresh_dashboard(), name="dashboard-refresh")
            try:
                await asyncio.gather(*tasks)
            except (asyncio.CancelledError, KeyboardInterrupt):
                cancelled = True
                stop_event.set()
            finally:
                stop_event.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if refresh_task is not None:
                    refresh_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await refresh_task
                await notifier.close()
                stats.finished_at = time.monotonic()
                live.update(self.ui.build_dashboard(stats, options, len(proxies), True), refresh=True)

        self.lifetime_stats.add_session(stats)
        try:
            self.lifetime_stats.save()
        except OSError:
            stats.record("ERROR", "runtime_stats.json", "could not save totals")
        return stats, cancelled


def run_self_test() -> int:
    failures: list[str] = []
    for mode in GENERATOR_MODES.values():
        for length in (MIN_USERNAME_LENGTH, 3, MAX_USERNAME_LENGTH):
            for add_dot in (False, True):
                for _ in range(100):
                    candidate = generate_username(mode, length, add_dot)
                    valid, reason = validate_username(candidate)
                    if not valid:
                        failures.append(f"generator produced invalid value: {reason}")
                        break
    sample_secret = "https://discord.com/api/webhooks/123456789012345678/secret-token-value"
    masked = mask_webhook(sample_secret)
    if "secret-token-value" in masked:
        failures.append("webhook masking exposed a token")
    if not valid_webhook_url(sample_secret):
        failures.append("valid webhook URL was rejected")
    if validate_username("a..b")[0]:
        failures.append("consecutive periods were accepted")
    if not validate_username("a_b.1")[0]:
        failures.append("valid username was rejected")

    if failures:
        print("SELF-TEST FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"{APP_NAME} v{APP_VERSION} self-test passed (offline; no requests sent).")
    return 0


def print_help() -> None:
    print(f"{APP_NAME} v{APP_VERSION}")
    print("Usage: OXY_Sniper.exe [--help | --version | --self-test]")
    print("Run without arguments to open the interactive menu.")


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print_help()
        return 0
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if "--self-test" in sys.argv:
        return run_self_test()

    try:
        OxyApplication().run()
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nStopped by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
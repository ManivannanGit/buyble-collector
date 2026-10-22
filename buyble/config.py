"""Load and validate .env settings and items.yaml.

Secrets are never printed. Use Settings.status() to see which are set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = {"food_drink", "apparel", "auto_parts", "electronics", "handmade", "service", "other"}
SECRET_KEYS = (
    "DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME",
    "REDDIT_PASSWORD",
    "REDDIT_REFRESH_TOKEN",
    "AUTHOR_HASH_SALT",
)
ENV_KEYS = {*SECRET_KEYS, "EXTRACT_MODEL", "REDDIT_USER_AGENT", "MAX_LLM_CALLS_PER_RUN",
            "MAX_POSTS_PER_ITEM", "DEFAULT_CITY"}
# Values copied unchanged from .env.example count as missing.
PLACEHOLDERS = {"postgresql://...", "linux:buyble-collector:0.1 (by /u/YOUR_USERNAME)"}
PASSWORD_PLACEHOLDER = "YOUR-PASSWORD"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    anthropic_api_key: str | None
    extract_model: str
    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_username: str | None
    reddit_password: str | None
    reddit_refresh_token: str | None
    reddit_user_agent: str | None
    author_hash_salt: str | None
    max_llm_calls_per_run: int
    max_posts_per_item: int
    default_city: str

    def status(self) -> dict[str, str]:
        """Map each variable to 'set' or 'missing'. Safe to print."""
        return {
            key: "set" if getattr(self, key.lower()) else "missing"
            for key in (*SECRET_KEYS, "REDDIT_USER_AGENT")
        }

    def __repr__(self) -> str:  # never leak secrets through logs or tracebacks
        return f"Settings({self.status()})"

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not getattr(self, k.lower())]
        if missing:
            raise ConfigError(f"missing in .env: {', '.join(missing)}")

    @property
    def reddit_ready(self) -> bool:
        if not (self.reddit_client_id and self.reddit_client_secret and self.reddit_user_agent):
            return False
        return bool(self.reddit_refresh_token or (self.reddit_username and self.reddit_password))


def env_path() -> Path:
    return Path(os.environ.get("ENV_FILE") or ROOT / ".env")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value in PLACEHOLDERS or PASSWORD_PLACEHOLDER in value or "[YOUR-PASSWORD]" in value:
        return None
    return value


def _int(raw: dict, key: str, default: int) -> int:
    value = _clean(raw.get(key))
    if value is None:
        return default
    try:
        n = int(value)
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None
    if n <= 0:
        raise ConfigError(f"{key} must be positive")
    return n


def load_settings(path: Path | None = None) -> Settings:
    path = path or env_path()
    raw = dotenv_values(path) if path.exists() else {}
    # Real environment variables win over the file.
    raw = {**raw, **{k: v for k, v in os.environ.items() if k in ENV_KEYS}}
    get = lambda k: _clean(raw.get(k))  # noqa: E731
    return Settings(
        database_url=get("DATABASE_URL"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        extract_model=get("EXTRACT_MODEL") or "claude-haiku-5-5",
        reddit_client_id=get("REDDIT_CLIENT_ID"),
        reddit_client_secret=get("REDDIT_CLIENT_SECRET"),
        reddit_username=get("REDDIT_USERNAME"),
        reddit_password=get("REDDIT_PASSWORD"),
        reddit_refresh_token=get("REDDIT_REFRESH_TOKEN"),
        reddit_user_agent=get("REDDIT_USER_AGENT"),
        author_hash_salt=get("AUTHOR_HASH_SALT"),
        max_llm_calls_per_run=_int(raw, "MAX_LLM_CALLS_PER_RUN", 400),
        max_posts_per_item=_int(raw, "MAX_POSTS_PER_ITEM", 40),
        default_city=get("DEFAULT_CITY") or "Bengaluru",
    )


@dataclass(frozen=True)
class Item:
    key: str
    category: str
    canonical: str
    queries: tuple[str, ...]
    attrs: dict[str, tuple[str, ...]]
    bounds: tuple[float, float]
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalog:
    city: str
    nearby_cities: tuple[str, ...]
    channels: dict[str, tuple[str, ...]]
    items: dict[str, Item]
    subreddits: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def by_canonical(self, canonical: str) -> Item | None:
        return next((i for i in self.items.values() if i.canonical == canonical), None)

    def allowed_attrs(self, item: Item) -> dict[str, tuple[str, ...]]:
        """Composition attrs plus the category's channels."""
        out = dict(item.attrs)
        if self.channels.get(item.category):
            out["channel"] = self.channels[item.category]
        return out

    def subreddits_for(self, item: Item) -> list[str]:
        """Category subreddits first, then general ones, deduped case-insensitively."""
        seen, out = set(), []
        for sub in (*self.subreddits.get(item.category, ()), *self.subreddits.get("general", ())):
            if sub.lower() not in seen:
                seen.add(sub.lower())
                out.append(sub)
        return out


def _tuple_of_str(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return tuple(value)


def parse_catalog(data: dict) -> Catalog:
    if not isinstance(data, dict):
        raise ConfigError("items.yaml must be a mapping")
    city = data.get("city")
    if not isinstance(city, str) or not city:
        raise ConfigError("items.yaml: city is required")

    channels = {}
    for cat, spec in (data.get("categories") or {}).items():
        if cat not in CATEGORIES:
            raise ConfigError(f"items.yaml: unknown category {cat!r}")
        channels[cat] = _tuple_of_str((spec or {}).get("channels"), f"categories.{cat}.channels")

    items, canonicals = {}, set()
    for key, spec in (data.get("items") or {}).items():
        where = f"items.{key}"
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a mapping")
        cat = spec.get("category")
        if cat not in channels:
            raise ConfigError(f"{where}.category {cat!r} is not under categories")
        canonical = spec.get("canonical")
        if not isinstance(canonical, str) or canonical != canonical.lower() or not 1 <= len(canonical.split()) <= 3:
            raise ConfigError(f"{where}.canonical must be lowercase, 1-3 words")
        if canonical in canonicals:
            raise ConfigError(f"{where}.canonical {canonical!r} is duplicated")
        canonicals.add(canonical)
        attrs = spec.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise ConfigError(f"{where}.attrs must be a mapping")
        if "channel" in attrs:
            raise ConfigError(f"{where}.attrs must not define channel (it comes from the category)")
        bounds = spec.get("bounds")
        if (not isinstance(bounds, list) or len(bounds) != 2
                or not all(isinstance(b, (int, float)) for b in bounds) or not 0 < bounds[0] < bounds[1]):
            raise ConfigError(f"{where}.bounds must be [min, max] with 0 < min < max")
        items[key] = Item(
            key=key,
            category=cat,
            canonical=canonical,
            queries=_tuple_of_str(spec.get("queries"), f"{where}.queries"),
            attrs={k: _tuple_of_str(v, f"{where}.attrs.{k}") for k, v in attrs.items()},
            bounds=(float(bounds[0]), float(bounds[1])),
            models=_tuple_of_str(spec["models"], f"{where}.models") if "models" in spec else (),
        )
    if not items:
        raise ConfigError("items.yaml: no items")

    subs = {k: _tuple_of_str(v, f"subreddits.{k}") for k, v in (data.get("subreddits") or {}).items()}
    nearby = data.get("nearby_cities") or []
    return Catalog(
        city=city,
        nearby_cities=tuple(nearby) if nearby else (),
        channels=channels,
        items=items,
        subreddits=subs,
    )


def load_catalog(path: Path | None = None) -> Catalog:
    path = path or ROOT / "items.yaml"
    with open(path) as f:
        return parse_catalog(yaml.safe_load(f))


def set_db_password(path: Path | None = None) -> None:
    """Prompt (hidden) for the database password and write it into DATABASE_URL in .env."""
    import getpass
    from urllib.parse import quote

    path = path or env_path()
    lines = path.read_text().splitlines(keepends=True)
    idx = next((i for i, l in enumerate(lines) if l.startswith("DATABASE_URL=")), None)
    if idx is None or PASSWORD_PLACEHOLDER not in lines[idx]:
        raise ConfigError(f"DATABASE_URL in {path} has no {PASSWORD_PLACEHOLDER} placeholder to fill")
    password = getpass.getpass("Supabase database password (hidden): ")
    if not password:
        raise ConfigError("no password entered")
    lines[idx] = lines[idx].replace(PASSWORD_PLACEHOLDER, quote(password, safe=""), 1)
    path.write_text("".join(lines))
    path.chmod(0o600)
    print("DATABASE_URL: set")


if __name__ == "__main__":
    import sys

    if "--set-db-password" in sys.argv:
        try:
            set_db_password()
        except ConfigError as e:
            sys.exit(f"error: {e}")
        sys.exit(0)
    s = load_settings()
    c = load_catalog()
    print(f".env: {env_path()}")
    for k, v in s.status().items():
        print(f"  {k}: {v}")
    print(f"items.yaml: {len(c.items)} items, city {c.city}")

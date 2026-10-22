import copy

import pytest
import yaml

from buyble.config import ROOT, ConfigError, load_catalog, load_settings, parse_catalog


@pytest.fixture
def seed():
    with open(ROOT / "items.yaml") as f:
        return yaml.safe_load(f)


def test_real_items_yaml_loads():
    c = load_catalog()
    assert c.city == "Bengaluru"
    assert c.items["spark-plug"].bounds == (50.0, 2000.0)
    assert c.by_canonical("hot chocolate").key == "hot-chocolate"
    assert c.allowed_attrs(c.items["spark-plug"])["channel"] == ("authorised", "garage", "online")


def test_subreddits_dedupe_case_insensitive():
    c = load_catalog()
    subs = c.subreddits_for(c.items["spark-plug"])
    assert [s.lower() for s in subs].count("indianbikes") == 1
    assert subs[0] == "IndianBikes"


@pytest.mark.parametrize("mutate, msg", [
    (lambda d: d["items"]["spark-plug"].update(category="toys"), "category"),
    (lambda d: d["items"]["spark-plug"].update(canonical="Spark Plug"), "lowercase"),
    (lambda d: d["items"]["spark-plug"].update(bounds=[100, 50]), "bounds"),
    (lambda d: d["items"]["spark-plug"].pop("bounds"), "bounds"),
    (lambda d: d["items"]["spark-plug"].update(queries=[]), "queries"),
    (lambda d: d["items"]["air-filter"].update(canonical="spark plug"), "duplicated"),
    (lambda d: d["items"]["spark-plug"]["attrs"].update(channel=["x"]), "channel"),
])
def test_invalid_catalog(seed, mutate, msg):
    data = copy.deepcopy(seed)
    mutate(data)
    with pytest.raises(ConfigError, match=msg):
        parse_catalog(data)


def test_settings_placeholders_are_missing(tmp_path, monkeypatch):
    for k in ("DATABASE_URL", "REDDIT_USER_AGENT", "MAX_LLM_CALLS_PER_RUN"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text((ROOT / ".env.example").read_text())
    s = load_settings(env)
    assert s.database_url is None
    assert s.reddit_user_agent is None
    assert s.max_llm_calls_per_run == 400
    assert s.extract_model == "claude-haiku-5-5"
    assert set(s.status().values()) == {"missing"}
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        s.require("DATABASE_URL")


def test_settings_bad_int(tmp_path, monkeypatch):
    monkeypatch.delenv("MAX_POSTS_PER_ITEM", raising=False)
    env = tmp_path / ".env"
    env.write_text("MAX_POSTS_PER_ITEM=lots\n")
    with pytest.raises(ConfigError, match="integer"):
        load_settings(env)


def test_status_never_contains_values(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-secret-value\n")
    s = load_settings(env)
    assert s.status()["ANTHROPIC_API_KEY"] == "set"
    assert "sk-secret-value" not in repr(s.status())

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from lp_v2.models import Market, Token
from lp_v2.scoring import eligible_market, score_market


DATA = Path(__file__).parent / "data"
SNAPSHOT = json.loads((DATA / "markets_snapshot.json").read_text())
EXPECTED = json.loads((DATA / "scores_expected.json").read_text())


def load_market(row):
    values = dict(row["market"])
    values["tokens"] = tuple(Token(**token) for token in values["tokens"])
    for name in ("tick_size", "min_order_size", "rewards_min_size"):
        values[name] = Decimal(values[name])
    return Market(**values)


@pytest.mark.parametrize("row", SNAPSHOT["markets"], ids=lambda row: row["fixture_id"])
def test_scores_match_upstream(row):
    result = score_market(load_market(row))

    assert result is not None
    assert asdict(result) == EXPECTED["scores"][row["fixture_id"]]


@pytest.mark.parametrize("row", [row for row in SNAPSHOT["markets"] if "eligible" in row],
                         ids=lambda row: row["fixture_id"])
def test_hard_filters(row):
    control = next(row for row in SNAPSHOT["markets"] if row["fixture_id"] == "synthetic:positive_control")

    assert eligible_market(load_market(control)) is True
    assert eligible_market(load_market(row)) is row["eligible"]


def test_snapshot_provenance():
    metadata = SNAPSHOT["metadata"]
    oracle = EXPECTED["metadata"]
    commit = "4f32103591c9582ccd012bdf10f77d86e5879444"
    assert metadata["upstream_commit"] == oracle["upstream_commit"] == commit
    assert metadata["upstream_repository"] == oracle["upstream_repository"] == "https://github.com/warproxxx/poly-maker"
    assert metadata["source_sha256"]["scoring.py"] == oracle["original_source_sha256"] == "b6e829e1c78ac2de12e8e28d38f6a201e9c4826bf302a7d16b469e79df73bb9e"
    assert oracle["snapshot_sha256"] == hashlib.sha256((DATA / "markets_snapshot.json").read_bytes()).hexdigest()
    assert metadata["generator_command"] == ".venv/bin/python .task-evidence/M2-C04/build_snapshot.py"
    assert oracle["generator_command"] == ".venv/bin/python .task-evidence/M2-C04/generate_scores.py"
    assert len(oracle["generator_script_sha256"]) == 64
    assert "AST" in oracle["method"] and "No lp_v2 imports" in oracle["method"]
    assert metadata["normalization"]
    captures = {entry["name"]: entry for entry in metadata["captures"]}
    for name in ("politics-markets-raw.json", "sampling-markets-raw.json"):
        assert captures[name]["exit_code"] == 0
        assert captures[name]["url"].startswith("https://")
        assert datetime.fromisoformat(captures[name]["time"]).utcoffset() is not None
        assert len(captures[name]["sha256"]) == 64
    assert "tag_id=2" in captures["politics-markets-raw.json"]["url"]
    assert captures["sampling-markets-raw.json"]["url"] == "https://clob.polymarket.com/sampling-markets"

    rows = {row["fixture_id"]: row for row in SNAPSHOT["markets"]}
    assert len(rows) == len(SNAPSHOT["markets"])
    assert set(EXPECTED["scores"]) == set(rows)
    real = [row for row in rows.values() if row["provenance"]["synthetic"] is False]
    assert len(real) >= 20
    assert len({row["market"]["condition_id"] for row in real}) == len(real)
    for row in real:
        market, source = row["market"], row["provenance"]
        assert row["fixture_id"] == source["gamma_condition_id"] == source["clob_condition_id"] == market["condition_id"]
        assert row["question"] and source["gamma_market_id"]
        assert source["gamma_index"] >= 0 and source["sampling_index"] >= 0
        assert market["political"] is True and "Politics" in source["clob_tags"]
        assert source["matched_reward"]["asset_address"].lower() == "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
        assert market["rewards_daily_rate"] == source["matched_reward"]["rewards_daily_rate"] > 0
        assert source["gamma_accepting_orders"] is source["clob_accepting_orders"] is market["accepting_orders"] is True
        assert market["in_play"] is None
    for row in rows.values():
        source = row["provenance"]
        assert isinstance(source["synthetic"], bool)
        assert EXPECTED["scores"][row["fixture_id"]]["condition_id"] == row["market"]["condition_id"]
        if source["synthetic"]:
            assert row["fixture_id"].startswith("synthetic:") and source["reason"]
            base = rows[source["base_fixture_id"]]["market"]
            changes = source["changed_fields"]
            assert row["market"] == base | changes
            assert changes == {key: value for key, value in row["market"].items() if base[key] != value}
            assert len(changes) == 1 or set(changes) == {"best_bid", "best_ask"}
    required_cases = {
        "positive_control", "rewards_closed", "nonbinary", "mid_lower_endpoint", "mid_just_below",
        "mid_upper_endpoint", "mid_just_above", "liquidity_endpoint", "liquidity_just_below",
        "nonpolitical", "in_play", "accepting_orders_false", "in_play_unknown",
        "no_positive_reward", "no_reward_band", "missing_bid", "missing_ask",
    }
    assert {"synthetic:" + name for name in required_cases} <= set(rows)

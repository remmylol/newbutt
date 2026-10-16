"""Offline validations for the clean Railway profile and synthetic paper behavior."""
import os
import subprocess
import sys
from pathlib import Path
import clean_boot


def test_legacy_variables_are_scrubbed_and_new_profile_is_paper_only():
    env = {
        "MIN_SCORE": "999", "MOMENTUM_MIN_LIQUIDITY_USD": "999999999",
        "LIVE_TRADING_ENABLED": "true", "PAPER_MODE": "false",
        "STATE_FILE": "/old/halted.json", "RAILWAY_VOLUME_MOUNT_PATH": "/data",
        "FOMO_API_KEY": "private-do-not-log",
    }
    ignored, path = clean_boot.apply_clean_profile(env)
    assert "MIN_SCORE" in ignored
    assert "MIN_SCORE" in env and env["MIN_SCORE"] == "62"
    assert env["MOMENTUM_MIN_LIQUIDITY_USD"] == "15000"
    assert env["LIVE_TRADING_ENABLED"] == "false"
    assert env["PAPER_MODE"] == "true"
    assert env["STATE_FILE"] == "/data/clean_paper_state.json"
    assert "FOMO_API_KEY" not in env
    assert path == env["STATE_FILE"]


def test_custom_namespaced_overrides_only():
    env = {"CLEAN_STARTING_CAPITAL_USD": "50", "STARTING_CAPITAL_USD": "1",
           "CLEAN_POSITION_SIZE_USD": "8", "POSITION_SIZE_USD": "10000"}
    clean_boot.apply_clean_profile(env)
    assert env["STARTING_CAPITAL_USD"] == "50"
    assert env["POSITION_SIZE_USD"] == "8"


def test_invalid_caps_fail_closed():
    env = {"CLEAN_MARKET_CAP_MIN_USD": "2000000", "CLEAN_MARKET_CAP_MAX_USD": "1000000"}
    try:
        clean_boot.apply_clean_profile(env)
    except ValueError as exc:
        assert "cannot exceed" in str(exc)
    else:
        raise AssertionError("invalid configuration accepted")


def test_offline_smoke():
    # Fresh process, independent of other test modules' monkeypatch state.
    env = dict(os.environ, MIN_SCORE="999", LIVE_TRADING_ENABLED="true")
    result = subprocess.run([sys.executable, "clean_boot.py", "--self-test"],
                            cwd=Path(__file__).parent, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLEAN SELF TEST PASS" in result.stdout
    assert "LEGACY_CONFIG_IGNORED" in result.stdout

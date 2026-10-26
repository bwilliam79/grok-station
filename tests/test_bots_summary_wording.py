"""Bots page header: '1 bot', '1 needs attention', '2 need attention'."""
import pathlib
import re

APP = (pathlib.Path(__file__).resolve().parents[1] / "admin" / "web" / "app.js").read_text()


def test_attention_count_is_pluralized():
    assert '" needs attention" : n + " bots"' not in APP
    assert re.search(r'attn === 1 \? " needs attention" : " need attention"', APP)


def test_bot_count_is_pluralized():
    assert re.search(r'n === 1 \? " bot" : " bots"', APP)

import pytest
from conftest import OWO

from waifu_bot.parser import normalize, parse_caption, parse_value


def test_exact_user_supplied_caption():
    parsed = parse_caption(OWO)
    assert parsed.ready, parsed.issues
    assert parsed.fields == {
        "anime": "Chainsaw man",
        "external_card_id": "5672",
        "name": "Yoru",
        "rarity": "Legendary",
        "theme": "American Native",
    }
    assert "value" not in parsed.fields


@pytest.mark.parametrize("emoji", ["🔥", "🌸", "👩🏽‍🎤", "⚔️", "🟣", ""])
@pytest.mark.parametrize("multiline", [True, False])
def test_emoji_and_linebreak_variants(emoji, multiline):
    caption = f"OwO! Check out this character!\nAttack on Titan\n00123: Mikasa Ackerman [{emoji}]\n({emoji} 𝙍𝘼𝙍𝙄𝙏𝙔: Divine Performance)\n{emoji}𝑺𝒑𝒆𝒄𝒊𝒂𝒍{emoji}"
    if not multiline:
        caption = caption.replace("\n", " ")
    result = parse_caption(caption)
    assert result.ready, result.issues
    assert result.fields["anime"] == "Attack on Titan"
    assert result.fields["external_card_id"] == "00123"
    assert result.fields["name"] == "Mikasa Ackerman"
    assert result.fields["rarity"] == "Divine Performance"
    assert result.fields["theme"] == "Special"


def test_numeric_anime_title_and_punctuation():
    result = parse_caption(
        "OwO! Check out this character! 86 Eighty-Six 77: Vladilena Milizé [✨] (🟡 RARITY: Rare)"
    )
    assert result.ready
    assert result.fields["anime"] == "86 Eighty-Six"
    assert result.fields["name"] == "Vladilena Milizé"


@pytest.mark.parametrize(
    "caption",
    [
        "OwO! Check out this character! Chainsaw man: Yoru [🔥] (RARITY: Legendary)",
        "OwO! Check out this character! Chainsaw man 123: Yoru",
        "OwO! Check out this character! Chainsaw man 123: [🔥] (RARITY: Legendary)",
    ],
)
def test_malformed_template_goes_to_review(caption):
    assert not parse_caption(caption).ready


def test_labelled_manual_caption_and_conflicts():
    caption = "🌸 Name: Yoru\nAnime: Chainsaw man\nCard ID: 001\nRarity: Legendary\nValue: 25K"
    result = parse_caption(caption)
    assert result.ready
    assert result.fields["value"] == 25000
    assert not parse_caption(caption + "\nName: Another").ready
    assert normalize("  𝙍𝘼𝙍𝙄𝙏𝙔   ") == "rarity"


@pytest.mark.parametrize("raw", ["-1", "1.2", "$500", "9999999999999999999999999999"])
def test_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_value(raw)

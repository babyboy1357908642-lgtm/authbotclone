import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


ALIASES = {
    "name": "name",
    "character": "name",
    "character name": "name",
    "anime": "anime",
    "series": "anime",
    "rarity": "rarity",
    "tier": "rarity",
    "value": "value",
    "price": "value",
    "card id": "external_card_id",
    "id": "external_card_id",
}
REQUIRED = ("name", "anime", "rarity", "external_card_id")


def strip_decoration(value: str) -> str:
    """Remove outside emoji/decoration, preserving punctuation inside actual names."""
    value = value.strip()
    while value and not (value[0].isalnum() or value[0] in "\"'"):
        value = value[1:].lstrip()
    while value and not (value[-1].isalnum() or value[-1] in "\"'"):
        value = value[:-1].rstrip()
    return value


def parse_owo(caption: str) -> "ParsedCard":
    """Parse the supplied OwO template without depending on any particular emoji."""
    canonical = " ".join(unicodedata.normalize("NFKC", caption).split())
    prefix = re.search(r"\bOwO!\s*Check out this character!\s*", canonical, re.IGNORECASE)
    result = ParsedCard()
    if not prefix:
        result.issues.append("Missing OwO card header")
        return result
    body = canonical[prefix.end() :]
    rarity = re.search(r"\([^()]*?\bRARITY\s*:\s*([^()]+)\)", body, re.IGNORECASE)
    if not rarity:
        result.issues.append("Missing or malformed (RARITY: ...) section")
        return result
    identity = body[: rarity.start()].strip()
    # The [emoji] decoration belongs to the template, not the character's name.
    identity = re.sub(r"\s*\[[^\]]*\]\s*$", "", identity)
    match = re.fullmatch(r"(.+?)\s+([0-9]+)\s*:\s*(.+)", identity)
    if not match:
        result.issues.append("Expected Anime <numeric card ID>: Character [emoji]")
        return result
    result.fields = {
        "anime": match[1].strip(),
        "external_card_id": match[2],
        "name": match[3].strip(),
        "rarity": strip_decoration(rarity[1]),
    }
    theme = strip_decoration(body[rarity.end() :])
    if theme:
        result.fields["theme"] = theme
    for key, value in result.fields.items():
        if not value or len(value) > 500:
            result.issues.append(f"Invalid {key}")
    return result


def parse_value(raw: str) -> int:
    value = raw.strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", value)
    if not match:
        raise ValueError("Value must be a nonnegative integer or a K/M abbreviation.")
    try:
        amount = Decimal(match[1]) * {"": 1, "k": 1000, "m": 1_000_000}[match[2].lower()]
    except InvalidOperation as exc:
        raise ValueError("Invalid value.") from exc
    if amount != amount.to_integral_value() or amount > 2**63 - 1:
        raise ValueError("Value must be an integer within signed 64-bit range.")
    return int(amount)


@dataclass
class ParsedCard:
    fields: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.issues and all(self.fields.get(key) for key in REQUIRED)


def parse_caption(caption: str, aliases: dict | None = None) -> ParsedCard:
    """Parse OwO cards first; labelled lines support manual additions and other sources."""
    caption = unicodedata.normalize("NFKC", caption)
    if re.search(r"\bOwO!\s*Check out this character!", caption, re.IGNORECASE):
        return parse_owo(caption)
    mapping = {**ALIASES, **{normalize(k): v for k, v in (aliases or {}).items()}}
    result = ParsedCard()
    for line in caption.splitlines():
        match = re.match(r"^\W*([\w ]+?)\s*[:：]\s*(.*?)\s*$", line, re.UNICODE)
        if not match:
            continue
        key = mapping.get(normalize(match[1]))
        if key not in {*REQUIRED, "value"}:
            continue
        raw = match[2].strip()
        if not raw:
            continue
        if len(raw) > 500:
            result.issues.append(f"{key}: exceeds 500 characters")
            continue
        try:
            value = parse_value(raw) if key == "value" else raw
        except ValueError as exc:
            result.issues.append(str(exc))
            continue
        if key in result.fields and result.fields[key] != value:
            result.issues.append(f"Conflicting {key} labels")
        else:
            result.fields[key] = value
    for key in REQUIRED:
        if not result.fields.get(key):
            result.issues.append(f"Missing {key}")
    return result

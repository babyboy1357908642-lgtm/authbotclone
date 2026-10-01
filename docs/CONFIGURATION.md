# Configuration

Copy `.env.example` to `.env`. Python loads this file from the working directory. Compose passes it as
environment variables. Never commit or export a populated .env.

| Variable | Default | Meaning |
|---|---|---|
| TELEGRAM_BOT_TOKEN | required | New bot token from BotFather |
| ADMIN_IDS | required | Comma-separated numeric personal Telegram IDs |
| MONGO_URI | mongodb://localhost:27017 | MongoDB/Atlas URI; URI credentials stay secret |
| MONGO_DATABASE | waifu_cards | Database name |
| ALLOWED_CHANNEL_IDS | empty | Comma-separated source channel IDs used on first registration |
| DEFAULT_CATALOG | default | Namespace for manual adds/bootstrap channels; use owo for supplied cards |
| COLLECTOR_ENABLED | true | Initial global setting; /collector persists later changes |
| MAX_DOWNLOAD_MB | 15 | Max Telegram download size for photos/documents |
| MAX_IMAGE_PIXELS | 24000000 | Decoded image area limit |
| HASH_DISTANCE | 8 | Maximum of 64-bit pHash/dHash distances for candidate acceptance |
| HASH_MARGIN | 3 | Required gap to the next distinct card before suggesting one card |
| LOOKUP_CONCURRENCY | 4 | Number of background lookup workers |
| LOOKUP_COOLDOWN_SECONDS | 2 | Per-user submission cooldown |
| CACHE_TTL_SECONDS | 300 | Exact lookup cache lifetime; DB generation changes also invalidate |
| CACHE_MAX_ENTRIES | 5000 | Bound for exact lookup cache |
| JOB_MAX_ATTEMPTS | 3 | Attempts before a job is marked failed |
| LOG_LEVEL | INFO | App log level; HTTP/Mongo wire logs remain suppressed |

Photo and image-document support: JPEG, PNG, WebP. Animated media is not a separate collection mode.
Input documents are checked by Pillow after download, not trusted by their MIME label alone. Telegram
cloud Bot API imposes its own download limits; raising MAX_DOWNLOAD_MB does not remove those limits.

Use the same catalog for source channels that forward cards from the same original card system.
Use different catalogs when different systems reuse card IDs. The source channel ID is not the catalog.

## Parser format

OwO header, anime, numeric card ID, colon, character name, optional bracket decoration,
parenthesized RARITY field, and optional trailing theme. Line breaks/whitespace and Unicode styled
letters are normalized. The header and RARITY marker are case insensitive. Caption raw text stays in
source_messages. The supplied example is covered verbatim by tests.

Manual labelled format:

    Name: Yoru
    Anime: Chainsaw man
    Card ID: 5672
    Rarity: Legendary
    Value: 25K

Unknown currency/value syntax is sent for review rather than guessed. Values are optional nonnegative
integers; K/M suffixes are supported. Leading zeroes in Card ID are preserved. The current OwO grammar
requires a numeric external ID; manual labelled/import records can use string IDs.

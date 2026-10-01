# Oschet — Waifu Card Collector & Lookup Bot

သီးခြား Python + MongoDB Telegram bot project ဖြစ်ပါတယ်။ **AI မသုံးပါ။**
သတ်မှတ်ထားတဲ့ channel ထဲ card photo နဲ့ caption ကို forward လုပ်လိုက်ရင်
Name / Anime / Card ID / Rarity ကို parser နဲ့ထုတ်ပြီး MongoDB ထဲ သိမ်းပါတယ်။
User က photo ပို့လာရင် Telegram identifier နဲ့ အရင်ရှာပြီး၊ မတွေ့မှ image matching လုပ်ပါတယ်။

## ပေးထားတဲ့ caption ပုံစံ

```text
OwO! Check out this character!
Chainsaw man
5672: Yoru [🪶]
(🟡 𝙍𝘼𝙍𝙄𝙏𝙔: Legendary)
🪶𝑨𝒎𝒆𝒓𝒊𝒄𝒂𝒏 𝑵𝒂𝒕𝒊𝒗𝒆🪶
```

အပေါ်ကပုံစံကို တစ်ကြောင်းတည်းရေးထားလည်း လက်ခံပါတယ်။ Emoji အမျိုးအစားကို မှီခိုမထားပါ။
Unicode အလှစာလုံးတွေကို ပုံမှန်စာလုံးအဖြစ် normalize လုပ်ပါတယ်။

```text
🎴 CARD FOUND — Exact Telegram file

Name: Yoru
Anime: Chainsaw man
Rarity: Legendary
Card ID: 5672
Catalog: owo
Theme: American Native
```

`American Native` ကို optional Theme အဖြစ် သီးခြားသိမ်းပါတယ်။ Caption ထဲ Value မပါရင်
ခန့်မှန်းမဖြည့်ပါ။ မူရင်း caption၊ caption entities၊ channel/message IDs နဲ့ Telegram
photo sizes အားလုံးရဲ့ file IDs ကိုလည်း သိမ်းပါတယ်။ ပုံ binary ကို MongoDB ထဲ မသိမ်းပါ။

## ပါဝင်တဲ့လုပ်ဆောင်ချက်များ

- Allowed channels အများကြီးမှ auto collection; global/per-channel ON/OFF
- OwO caption parser + labelled caption parser; incomplete/conflicting data review queue
- Indexed file_unique_id lookup; exact match မှာ **ပုံ download မလုပ်**
- New reference image တစ်ခုကို background မှာ download လုပ်ပြီး SHA-256 / pHash / dHash index တည်ဆောက်
- Identifier မကိုက်မှ input image download; resize/compression နဲ့ uniform border fallback
- ပုံတစ်ပုံတည်းက card အများနဲ့ချိတ်နေရင် candidates ပြ; edition တစ်ခုကို ခန့်မှန်းမရွေး
- Duplicate source/file/card protection; edited channel captions ကို handle
- Admin commands၊ confirmation buttons၊ audit log၊ soft delete
- JSONL / CSV imports၊ JSONL exports၊ bulk writes၊ resumable import jobs
- Opt-in broadcast with pause/cancel/resume and rate-limit handling
- Async HTTP / PyMongo Async connection pooling၊ bounded lookup workers၊ exact-result cache
- Persistent MongoDB job queue၊ retry၊ leases၊ single-instance guard
- AI API key သို့မဟုတ် AI service လုံးဝမလိုပါ

## လိုအပ်ချက်များ

- Python **3.12+** (စမ်းသပ်ထားတဲ့ version: 3.12.14)
- MongoDB **8.0** သို့မဟုတ် compatible Atlas MongoDB deployment
- @BotFather မှ token ရယူထားတဲ့ bot အသစ်
- မိမိ Telegram **numeric user ID**
- Collector channel ရဲ့ numeric ID (`-100...`)

## Railway မှာတင်မယ်ဆိုရင်

**[Railway setup အဆင့်ဆင့်](docs/RAILWAY.md)** ကိုဖတ်ပါ။ `railway.json` နဲ့ `.env.simple` ပါပြီးသားပါ။
`.env.simple` ထဲ values တွေဖြည့်ပြီး Railway bot service ရဲ့ Variables → RAW Editor ထဲ import လုပ်ပါ။
MongoDB ကို Railway database service သို့မဟုတ် Atlas နဲ့ချိတ်ပါ။ ဒီ bot က background worker ဖြစ်လို့
HTTP port/domain မလိုပါ။

## အမြန်စတင်ရန် — Docker Compose

Docker Engine နဲ့ Docker Compose plugin ရှိရပါမယ်။

```bash
cp .env.example .env
```

`.env` မှာ အနည်းဆုံး ဒီ fields တွေဖြည့်ပါ။

```dotenv
TELEGRAM_BOT_TOKEN=<your BotFather token>
ADMIN_IDS=<your numeric Telegram user ID>
ALLOWED_CHANNEL_IDS=<your collector channel ID>
DEFAULT_CATALOG=owo
```

Token ကို README၊ public repository၊ screenshot ထဲ မထည့်ပါနဲ့။

```bash
docker compose up -d --build
docker compose logs -f bot
```

Compose က private MongoDB service ကို အလိုအလျောက်သုံးပါတယ်။ Host Mongo port မဖွင့်ပါ။
Compose volume ထဲမှာ data ကျန်နေပါတယ်။ `docker compose down` က volume မဖျက်ပါ။
`docker compose down -v` က database volume ကို ဖျက်တာဖြစ်ပါတယ်။

Atlas သို့မဟုတ် ကိုယ့် MongoDB ကိုသုံးချင်ရင် အောက်က Python နည်းလမ်းသုံးပါ။
Compose ကို ပြောင်းသုံးမယ်ဆိုရင် compose.yaml ရဲ့ `MONGO_URI` override နဲ့ mongo dependency
ကို ပြင်ရပါမယ်; `.env` ထဲပြောင်းတာတစ်ခုတည်းနဲ့ Compose override မပြောင်းပါ။

## Python ဖြင့် run ရန်

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps .
cp .env.example .env
```

`.env` ထဲ token/admin/channel IDs နဲ့ `MONGO_URI` ဖြည့်ပြီး—

```bash
waifu-bot --check
waifu-bot
```

`--check` က database indexes/bootstrap settings ဖန်တီးပြီး Telegram identity/channel
membership စစ်ပါတယ်။ Bot ကို Windows မှာ run ရင် activation command က
`.venv\Scripts\activate` ဖြစ်ပါတယ်။

## Telegram မှာ setup လုပ်ရန်

1. Bot ကို collector channel ထဲ admin/member အဖြစ် ထည့်ပါ။ Channel post တွေကို bot လက်ခံနိုင်ရပါမယ်။
2. Bot ကို private chat မှာ `/start` ပို့ပါ။ Admin IDs မှန်ရင် admin help ပြပါမယ်။
3. `.env` ထဲ channel မထည့်ထားရင် `/sources add -1001234567890 owo` သုံးပါ။
4. Card photo + original caption ကို channel ထဲ forward လုပ်ပါ။
5. `/stats` မှာ Active cards / Indexed assets ကြည့်ပါ။
6. ပုံတူကို bot private chat ထဲ ပို့ပြီး lookup စမ်းပါ။

Channel အဟောင်းထဲက message history အကုန်ကို bot က အလိုအလျောက် မဆွဲပါ။
Old cards တွေကို ပြန် forward သို့မဟုတ် `/import` လုပ်ရပါမယ်။ Protected content ကို
Telegram က forwarding တားထားရင် ဒီ bot က အဲဒီကန့်သတ်ချက်ကို မကျော်ပါ။
Channel ရှိရုံနဲ့ မရပါ—bot က posts တွေကို အမှန်တကယ် receive လုပ်နိုင်ဖို့လိုပါတယ်။

## Admin Commands

Admin commands နဲ့ confirmations တွေကို **private chat + numeric admin ID** နှစ်ခုလုံးနဲ့ စစ်ပါတယ်။
`/find` ကို user အားလုံး သုံးနိုင်ပါတယ်။

| Command | အသုံးပြုပုံ |
|---|---|
| `/stats` | Cards/assets/jobs/subscribers/lookup counts |
| `/find Yoru` | Exact normalized name / anime / card ID search |
| `/add` | Photo ကို reply လုပ်; caption ကို parse |
| `/edit owo 5672 rarity="Legendary"` | Metadata ပြင် |
| `/delete owo 5672` | Confirm ပြီး soft delete |
| `/sources` | Sources ကြည့် |
| `/sources add -1001234567890 owo` | Channel ကို catalog နဲ့ချိတ် |
| `/sources off -1001234567890` | Channel collection ရပ် |
| `/sources on -1001234567890` | ပြန်ဖွင့် |
| `/sources remove -1001234567890` | Allowlist ကဖယ်; existing data မဖျက် |
| `/collector off` | Global collector ရပ် |
| `/collector on` | Global collector ပြန်ဖွင့် |
| `/review` | Recent malformed/conflicting source captions |
| `/resolve -1001234567890 123` | Reviewed source image ကို ရှိပြီးသား canonical card နဲ့ approve ချိတ် |
| `/reindex all` | Reference image hashes ပြန်တွက်ရန် queue ထည့် |
| `/reindex owo 5672` | Card တစ်ခုရဲ့ images ပြန် index |
| `/import` | JSONL/CSV document ကို reply; validate + confirm |
| `/export` | All active catalogs ကို JSONL parts အဖြစ် export |
| `/export owo` | Catalog တစ်ခု export |
| `/broadcast Announcement text` | Subscribers ကို preview + confirm ပြီးပို့ |
| `/jobs` | Recent background jobs |
| `/jobs retry <job-id>` | Failed job ပြန်စမ်း |
| `/jobs pause <job-id>` | Import/broadcast ရပ်ထား |
| `/jobs resume <job-id>` | Paused job ပြန်ဆက် |
| `/jobs cancel <job-id>` | Import/broadcast cancel |

Manual add example (photo ကို reply လုပ်ပါ):

```text
/add catalog=owo id=5672 name="Yoru" anime="Chainsaw man" rarity="Legendary"
```

Search aliases:

```text
/edit owo 5672 name_aliases="War Devil|Yoru"
```

Labelled captions အတွက် source-specific alias example:

```text
/sources aliases -1001234567890 {"character name":"name","show":"anime"}
```

Malformed source ကို ပြင်ချင်ရင် caption ပြင်ပြီး channel post edit update ဝင်အောင်လုပ်ပါ၊
သို့မဟုတ် photo ကို bot ဆီ forward လုပ်ပြီး `/add` နဲ့ corrected fields ထည့်ပါ။
Metadata conflict တွေမှာ existing card data မ overwrite လုပ်ပါ။ `/edit` နဲ့ canonical
metadata ကို စစ်ပြင်ပြီး `/resolve` နဲ့ image association ကို admin က approve လုပ်နိုင်ပါတယ်။

## Import / Export

- `examples/cards.jsonl`: full portable catalog record example (metadata + optional Telegram image references)
- `examples/cards.csv`: metadata-only CSV example
- UTF-8; import တစ်ဖိုင် **5 MiB / 10,000 cards** အများဆုံး
- Export ကို **4 MiB** parts အဖြစ်ခွဲပေးပါတယ်; exported parts ကိုတစ်ခုချင်း import လုပ်နိုင်ပါတယ်
- Catalog + card ID တူရင် canonical metadata တူမှ merge; မတူရင် conflict အဖြစ် skip
- Imported image hashes ကို မယုံဘဲ bot အသုံးပြုနိုင်တဲ့ reference image ရှိမှ ပြန်တွက်ပါတယ်
- တခြား bot ရဲ့ file_id ကို ဒီ bot က download လုပ်နိုင်မယ်လို့ မယူဆပါ။ Bot identity နဲ့တွဲသိမ်းပါတယ်
- Portable export ထဲ image binaries၊ source-message history၊ user subscriptions၊ admin audit မပါပါ
- Full operational backup အတွက် MongoDB `mongodump`/`mongorestore` သို့မဟုတ် Atlas backup သုံးပါ

## Broadcast

`/start` လုပ်သူတိုင်းကို broadcast မပို့ပါ။ User က `/subscribe` လုပ်မှ လက်ခံပါမယ်။
`/unsubscribe` နဲ့ ရပ်နိုင်ပါတယ်။ Worker က sending မတိုင်ခင် subscription ကို ပြန်စစ်ပါတယ်။
Pause/cancel လုပ်ချိန် စတင်ပို့ပြီးသား request တစ်ခုက ပြီးသွားနိုင်ပါတယ်။
Telegram send နဲ့ Mongo checkpoint ကြား process ပျက်ရင် message တစ်ခု ထပ်ပို့နိုင်ပါတယ်;
Telegram API မှာ ဒီပုံစံအတွက် transactional exactly-once delivery မရှိလို့ပါ။

## Matching အကန့်အသတ်များ

- file_unique_id က image visual identity အာမခံမဟုတ်ပါ။ Reupload/screenshot မှာ ပြောင်းနိုင်ပါတယ်။
- Hash match ကို **similar** လို့သာပြပါတယ်။ Character/edition ကို AI နဲ့ ခန့်မှန်းမရွေးပါ။
- Screenshot ထဲ Telegram UI၊ collage၊ watermark၊ crop အများကြီးပါရင် original/tightly cropped image ပို့ပါ။
- Uniform outer border ကို fallback မှာ crop စမ်းပါတယ်; arbitrary UI segmentation မပါပါ။
- `HASH_DISTANCE=8`, `HASH_MARGIN=3` က starting values ဖြစ်ပါတယ်။ ကိုယ့် real cards dataset နဲ့ calibrate လုပ်ပါ။
- Distinct editions က image တစ်ပုံတည်းသုံးရင် candidates ပြပါတယ်။
- Reference cards များလာရင် in-memory hash arrays ကိုသုံးပါတယ်; exact lookup က Mongo indexed query ဖြစ်ပါတယ်။
- Single bot process အတွက် design လုပ်ထားပါတယ်။ Lookup worker concurrency ကိုတိုးနိုင်ပေမယ့်
  same bot ကို processes အများကြီး long-poll မလုပ်ပါနဲ့။
- Database collection က scalar indexes သုံးပါတယ်; hashes ကို Mongo string index နဲ့ nearest-neighbor ရှာတာမဟုတ်ပါ။

## Testing

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
```

Default `pytest` က external credentials မလိုတဲ့ tests တွေ run လုပ်ပြီး Mongo integration
tests တွေကို skip ပါမယ်။ Local disposable MongoDB နဲ့ full tests:

```bash
docker run --rm -d --name waifu-test-mongo -p 127.0.0.1:27019:27017 mongo:8.0
TEST_MONGO_URI=mongodb://127.0.0.1:27019 pytest -q
docker stop waifu-test-mongo
```

Tests က random `waifu_test_*` database တွေဖန်တီးပြီး ကိုယ်ဖန်တီးထားတဲ့ database တွေကိုပဲဖျက်ပါတယ်။
Production database URI မသုံးပါနဲ့။ Verified results နဲ့ live-test limitations ကို `VALIDATION.md` မှာကြည့်ပါ။

## အသေးစိတ်

- [Architecture and operational notes](docs/ARCHITECTURE.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Validation results](VALIDATION.md)

ဒီ package မှာ credentials မပါပါ။ ကိုယ့် token၊ admin ID၊ source channel နဲ့ MongoDB ကို
ဖြည့်ပြီး live Telegram flow စမ်းဖို့လိုပါတယ်။

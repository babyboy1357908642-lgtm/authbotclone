# Railway မှာ deploy လုပ်ရန်

## ၁။ လက်ရှိ repository မှ deploy လုပ်ပါ

Railway service ကို `babyboy1357908642-lgtm/authbotclone` repository နဲ့ချိတ်ပါ။
ဒီ project အသစ်က repository root မှာရှိပါတယ်။ ဒီ task ရဲ့ branch က
`coderabbit/detect-waifu-bot-automation/d053a9dd` ဖြစ်ပါတယ်။ အဲဒီ branch publish ဖြစ်ပြီးနောက်
Railway deployment branch အဖြစ်ရွေးပါ; main ထဲ merge ပြီးမှ main ကိုရွေးနိုင်ပါတယ်။

Service settings မှာ:

- **Root Directory:** repository root (`/`)
- **Railway Config File:** `/railway.json`
- ယခင် project ရဲ့ custom start command ရှိရင် ဖယ်ပါ သို့မဟုတ် `waifu-bot` သုံးပါ။
- ယခင် HTTP healthcheck path ရှိရင် ဖယ်ပါ; ဒီ bot က polling worker ဖြစ်ပါတယ်။
- Environment variables အသစ်ကို `.env.simple` အတိုင်းထည့်ပါ။ ယခင် `BOT_TOKEN` နာမည်အစား
  ဒီ project က `TELEGRAM_BOT_TOKEN` ကိုသုံးပါတယ်။

Railway က root ထဲက Dockerfile နဲ့ build လုပ်ပါမယ်။ Compose မသုံးပါ။
ဒီပြင်ဆင်မှုက Git repository နဲ့ deployment files ကိုသာ ပြင်ဆင်ထားတာဖြစ်ပြီး Railway account ထဲ deploy မလုပ်ထားပါ။
[Railway config reference](https://docs.railway.com/config-as-code/reference)

## ၂။ MongoDB ချိတ်ပါ

### Railway MongoDB service သုံးလျှင်

တူညီတဲ့ Railway project/environment ထဲမှာ MongoDB service ထည့်ပါ။ Bot service ရဲ့ Variables ထဲမှာ
`MONGO_URI` ကို database service ရဲ့ `MONGO_URL` သို့ reference ချိတ်ပါ။

Database service နာမည် **MongoDB** ဖြစ်တယ်ဆိုရင် example:

```dotenv
MONGO_URI=${{MongoDB.MONGO_URL}}
```

Service နာမည်မတူရင် `MongoDB` နေရာမှာ ကိုယ့် service နာမည်သုံးပါ။ Railway Variables UI ရဲ့ reference
selector ကနေရွေးတာ ပိုလွယ်ပါတယ်။ Public connection URL မလိုပါ; project အတွင်း private connection ကိုသုံးပါ။
MongoDB service က persistent volume သုံးနေကြောင်းစစ်ပါ။
[Railway MongoDB docs](https://docs.railway.com/databases/mongodb)

### Atlas / external MongoDB သုံးလျှင်

`MONGO_URI` ထဲမှာ ကိုယ့် MongoDB connection string အပြည့်ထည့်ပါ။ Railway က database ဆီ ချိတ်နိုင်တဲ့
network access နဲ့ database-user permission ရှိရပါမယ်။ URI password ထဲ reserved characters ပါရင် URL encode လုပ်ပါ။

**Railway မှာ `mongodb://localhost:27017` မသုံးပါနဲ့။** Bot container အတွင်း database မပါပါ။

## ၃။ Simple env ကို ဖြည့်ပါ

Package ထဲက `.env.simple` ကိုဖွင့်ပါ။ အောက်က template ထဲ values တွေကို ကိုယ့်အချက်အလက်နဲ့ အစားထိုးပြီး
**Bot service → Variables → RAW Editor** ထဲ paste/import လုပ်ပါ။

```dotenv
TELEGRAM_BOT_TOKEN=PASTE_BOTFATHER_TOKEN_HERE
ADMIN_IDS=123456789
ALLOWED_CHANNEL_IDS=-1001234567890
MONGO_URI=PASTE_MONGODB_CONNECTION_URL_HERE
MONGO_DATABASE=waifu_cards
DEFAULT_CATALOG=owo
```

- `ADMIN_IDS`: ကိုယ့် numeric Telegram user ID; admin အများဆို comma ခွဲပါ။
- `ALLOWED_CHANNEL_IDS`: forward cards တွေသိမ်းမယ့် channel ID; အများဆို comma ခွဲပါ။
- `MONGO_URI`: အပေါ်က Railway reference သို့မဟုတ် Atlas URI နဲ့အစားထိုးပါ။
- `.env.simple` ကို app က အလိုအလျောက်မဖတ်ပါ။ Railway Variables ထဲ import လုပ်ဖို့ template ပါ။
- Local Python run အတွက်တော့ `.env.simple` ကို `.env` အဖြစ် copy/rename လုပ်ပြီး values ဖြည့်ပါ။
- Secrets ဖြည့်ထားတဲ့ env file ကို GitHub မှာ မတင်ပါနဲ့။ Railway Variables ထဲမှာပဲ သိမ်းပါ။

[Railway variables / RAW Editor docs](https://docs.railway.com/variables)

## ၄။ Deploy settings

`railway.json` ထဲမှာ ပါပြီးသား:

- Builder: Dockerfile
- Start command: `waifu-bot`
- Replicas: **1**
- Serverless/sleep: disabled
- Restart: on failure, up to 10 retries
- Deployment overlap: 0 seconds
- SIGTERM draining window: 30 seconds

ဒါက Telegram long-polling **worker service** ဖြစ်ပါတယ်။ Public domain၊ PORT variable၊ HTTP healthcheck path
မလိုပါ။ Railway UI မှာ HTTP healthcheck path ကို မဖြည့်ပါနဲ့။
[Railway config reference](https://docs.railway.com/config-as-code/reference)

Redeploy အချိန်မှာ bot က SIGTERM ကိုလက်ခံပြီး workers ကို ရပ်၊ database client ပိတ်၊ process lease ကိုလွှတ်ပါတယ်။
Replacement process က previous instance lease လွတ်တဲ့အထိ အများဆုံး 120 seconds စောင့်နိုင်ပါတယ်။
တူညီတဲ့ token ကို အခြား server/project မှာ တပြိုင်နက် မ run ပါနဲ့။

## ၅။ Deploy ပြီး စစ်ရန်

1. MongoDB service ready ဖြစ်နေကြောင်းစစ်ပါ။
2. Bot service Deploy Logs မှာ `Database ready; Telegram bot ID=...` ကိုကြည့်ပါ။
3. Telegram collector channel ထဲ bot ကိုထည့်ပြီး channel posts လက်ခံနိုင်ကြောင်းစစ်ပါ။
4. Bot ကို private chat မှာ `/start` ပို့ပါ။
5. Card + OwO caption ကို collector channel ထဲ forward လုပ်ပါ။
6. `/stats` မှာ Active cards / Indexed assets တက်လာတာကြည့်ပါ။
7. ပုံကို bot private chat ထဲပို့ပြီး lookup စမ်းပါ။

Startup failure ရင် token၊ numeric admin IDs၊ MONGO_URI၊ database connectivity ကိုအရင်စစ်ပါ။
Restart retry limit ကုန်သွားရင် settings ပြင်ပြီး redeploy လုပ်ပါ။
ပိုအသေးစိတ် env options တွေကို `.env.example` နဲ့ `docs/CONFIGURATION.md` မှာကြည့်နိုင်ပါတယ်။

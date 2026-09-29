"""Telegram presentation and automatic lifecycle for shared H/L PvP rounds."""
import asyncio
import html
import logging
import math
import time
from collections import OrderedDict

from telegram import ChatPermissions, InputMediaPhoto
from telegram.error import BadRequest, RetryAfter, TelegramError
from pymongo.errors import PyMongoError

from .domain import RuleError, cents, money
from .group_pvp_store import BET_SECONDS

log = logging.getLogger(__name__)
PHOTO_COMMANDS = {"startphoto": "start", "stopphoto": "stop", "resultphoto": "result"}
SEND_PERMISSIONS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews",
)



def retry_seconds(exc):
    value = exc.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else value


def round_open_text(row, now):
    remaining = max(0, math.ceil(row["ends_at"] - now)) if row.get("ends_at") else BET_SECONDS
    return (f'⚔️ PVP GAME #{row["id"]}\n\n⏳ လောင်းရန် — {remaining} sec\n\n'
            f'🟦 H — {row["h_count"]} ယောက် | {money(row["h_amount"])}\n'
            f'🟥 L — {row["l_count"]} ယောက် | {money(row["l_amount"])}\n\n'
            '250–30000 coin · တစ်ပွဲတစ်ကြိမ်\n'
            '/pvp 1000 h\n/pvp 500 l\n\n'
            '🔒 နောက်ဆုံး 2 sec မှ result ပြပြီးချိန်အထိ chat ပိတ်ထားပါမယ်။')


def round_animation_text(row, frame):
    shown = (42, 71)[frame % 2]
    blue = round(shown * 12 / 100)
    return (f'⚔️ PVP #{row["id"]}\n🔒 လောင်းကြေးပိတ်ပါပြီ။\n\n'
            f'🟦 H {shown}% — 🟥 L {100-shown}%\n\n'
            + '🟦' * blue + '🟥' * (12-blue) + '\n\n⏳ ရလဒ်ထုတ်နေပါပြီ…')


def result_pages(row, bets, limit=900):
    """Bound HTML pages conservatively for both photo captions and text messages."""
    header = (f'⚔️ PVP RESULT #{row["id"]}\n'
              f'🟦 H {row["h_percent"]}% — 🟥 L {100-row["h_percent"]}%\n\n'
              f'🏆 Winner: {row["winner"].upper()}\n\n')
    lines = []
    for bet in bets:
        name = html.escape(bet["name"][:60])
        if bet["win_amount"]:
            lines.append(f'{name} {bet["side"].upper()} - {money(bet["amount"])}\n'
                         f'Win - {money(bet["amount"])} + {row["winner_percent"]}% = '
                         f'{money(bet["win_amount"])}\n\n')
        else:
            lines.append(f'{name} {bet["side"].upper()} - {money(bet["amount"])}\nLose - 0coin\n\n')
    if not lines:
        lines.append('ဒီပွဲမှာ လောင်းထားသူ မရှိပါ။\n\n')
    lines.append(f'💰 စုစုပေါင်းလောင်းကြေး — {money(row["total_bet"])}\n'
                 f'🎉 စုစုပေါင်းပြန်ပေးငွေ — {money(row["total_win"])}')
    pages, current = [], header
    for line in lines:
        # UTF-16 units match Telegram limits, including emoji surrogate pairs.
        if len((current + line).encode("utf-16-le")) // 2 > limit:
            pages.append(current.rstrip())
            current = header
        current += line
    pages.append(current.rstrip())
    return pages


class GroupPvP:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.call = service.store_call
        self.output_lock = asyncio.Lock()
        self.output_after = 0.0
        self.permission_after = 0.0
        self.render_task = None
        self.last_render = None
        self.migrated = False
        self.last_group = None
        self.receipts = OrderedDict()
        self.receipt_task = None

    async def send(self, operation):
        # Shared by game announcements, edits, and bet receipts: <=15/minute.
        async with self.output_lock:
            await asyncio.sleep(max(0, self.output_after - time.monotonic()))
            try:
                return await operation()
            except RetryAfter as exc:
                self.output_after = time.monotonic() + retry_seconds(exc) + 1
                raise
            finally:
                self.output_after = max(self.output_after, time.monotonic() + 4)

    async def bet(self, args, message, user, create_task=asyncio.create_task):
        game_id = None
        try:
            if not user or user.is_bot:
                return
            if len(args) != 2:
                raise RuleError("/pvp 1000 h သို့ /pvp 500 l ပုံစံရေးပါ။")
            row = await self.call(self.store.current_group_round, message.chat_id)
            if not row:
                raise RuleError("ဖွင့်ထားတဲ့ပွဲ မရှိသေးပါ။ ခဏစောင့်ပါ။")
            side = args[1].lower()
            await self.call(self.store.place_group_bet, row["id"], message.chat_id,
                user.id, f"@{user.username}" if user.username else user.full_name, side,
                cents(args[0]), message.message_id, message.date.timestamp())
            game_id = row["id"]
            text = (f'✅ #{row["id"]} လောင်းပြီးပါပြီ။\n'
                    f'{side.upper()} — {money(cents(args[0]))}')
        except RuleError as exc:
            text = str(exc)
        # One receipt worker batches confirmations; update handlers never wait
        # for Telegram's group send budget before accepting the next player's bet.
        key = (message.chat_id, user.id, game_id)
        self.receipts[key] = (message, f"{user.full_name[:40]} — {text}", time.monotonic())
        if self.receipt_task is None or self.receipt_task.done():
            self.receipt_task = create_task(self.send_receipts())

    async def send_receipts(self):
        while self.receipts:
            batch = []

            async def deliver():
                while self.receipts and len(batch) < 6:
                    key, entry = self.receipts.popitem(last=False)
                    # Every bet also appears in the result. Avoid stale receipts
                    # spilling into later rounds, and expire repeated error replies.
                    if time.monotonic() - entry[2] > 30:
                        continue
                    if key[2] is not None:
                        row = await self.call(self.store.group_round, key[2])
                        if not row or row["status"] in ("closed", "cancelled"):
                            continue
                    batch.append((key, entry))
                if batch:
                    await batch[0][1][0].reply_text("\n\n".join(entry[1] for _, entry in batch),
                                                    allow_sending_without_reply=True)
            try:
                await self.send(deliver)
            except RetryAfter:
                for key, entry in reversed(batch):
                    self.receipts[key] = entry
                    self.receipts.move_to_end(key, last=False)
            except (TelegramError, PyMongoError):
                log.warning("PvP receipt unavailable; accepted bets remain in the round result")

    async def tick(self, context):
        if not self.migrated:
            await self.call(self.store.retire_legacy_games)
            self.migrated = True
        group_id = int(self.service.pvp_group_id or 0)
        if group_id != self.last_group:
            await self.call(self.store.refund_moved_group_rounds, group_id)
            self.last_group = group_id
        # Permission restoration is independent of message throttling and survives restart.
        for old in await self.call(self.store.pending_permission_restores):
            if not await self.restore_permissions(context.bot, old):
                return
        if not group_id:
            return
        row = await self.call(self.store.ensure_group_round, group_id)
        if not row:
            return
        now = time.time()
        if row["status"] == "open":
            if now >= row["ends_at"] - 2:
                await self.mute_group(context.bot, row)
            if now >= row["ends_at"]:
                row = await self.call(self.store.lock_group_round, row["id"])
        if row["status"] == "rolling":
            if not row.get("muted"):
                await self.mute_group(context.bot, row)
            row = await self.call(self.store.settle_group_round, row["id"])
        if self.render_task is None or self.render_task.done():
            self.render_task = context.application.create_task(self.render(context.bot, row["id"]))

    async def mute_group(self, bot, row):
        if row.get("muted") or time.monotonic() < self.permission_after:
            return
        try:
            original = row.get("saved_permissions")
            if original is None:
                chat = await bot.get_chat(row["group_id"])
                if chat.permissions is None:
                    raise RuleError("Group permissions မရနိုင်ပါ။")
                original = chat.permissions.to_dict()
                await self.call(self.store.save_group_permissions, row["id"], original)
            locked = dict(original)
            locked.update({key: False for key in SEND_PERMISSIONS})
            await bot.set_chat_permissions(row["group_id"], ChatPermissions.de_json(locked, bot),
                                           use_independent_chat_permissions=True)
            await self.call(self.store.mark_group_permissions, row["id"])
        except (TelegramError, RuleError) as exc:
            self.permission_after = time.monotonic() + (retry_seconds(exc) + 1 if isinstance(exc, RetryAfter) else 5)
            log.warning("PvP chat lock failed (%s); Restrict Members permission is required", type(exc).__name__)

    async def restore_permissions(self, bot, row):
        if time.monotonic() < self.permission_after:
            return False
        try:
            await bot.set_chat_permissions(row["group_id"],
                ChatPermissions.de_json(row["saved_permissions"], bot), use_independent_chat_permissions=True)
            await self.call(self.store.mark_group_permissions, row["id"], restored=True)
            return True
        except TelegramError as exc:
            self.permission_after = time.monotonic() + (retry_seconds(exc) + 1 if isinstance(exc, RetryAfter) else 10)
            log.warning("PvP chat restore failed (%s); will retry", type(exc).__name__)
            return False

    async def render(self, bot, game_id):
        try:
            await self.send(lambda: self.render_current(bot, game_id))
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                log.warning("PvP message rejected (%s)", type(exc).__name__)
        except (TelegramError, PyMongoError):
            log.warning("PvP presentation delayed; will retry")

    async def render_current(self, bot, game_id):
        # Re-read after waiting for a send slot; never render an outdated open state.
        row = await self.call(self.store.group_round, game_id)
        if not row or not row["active"] or str(row["group_id"]) != self.service.pvp_group_id:
            return
        stage = {"publishing": "start", "open": "start", "rolling": "stop", "closed": "result"}[row["status"]]
        photo = await self.call(self.store.get, f"pvp_{stage}_photo")
        now = time.time()
        if row["status"] in ("publishing", "open"):
            text = round_open_text(row, now)
            sig = (game_id, "open", max(0, math.ceil((row.get("ends_at", now+20)-now)/5)), row["h_count"], row["l_count"])
        elif row["status"] == "rolling":
            frame = int((now - row["ends_at"]) // 4)
            text = round_animation_text(row, frame)
            sig = (game_id, "rolling", frame)
        else:
            if row["announced_at"] is not None:
                return
            pages = result_pages(row, await self.call(self.store.group_bets, game_id))
            page = row["result_page"]
            text = pages[page]
            sig = (game_id, "result", page)
        if sig == self.last_render:
            return
        if row["status"] == "publishing":
            # Fail before accepting stakes when the required admin privilege is missing.
            member = await bot.get_chat_member(row["group_id"], bot.id)
            if member.status != "creator" and not getattr(member, "can_restrict_members", False):
                raise BadRequest("PvP requires Restrict Members permission")
            if photo:
                message = await bot.send_photo(row["group_id"], photo, caption=text, parse_mode="HTML")
            else:
                message = await bot.send_message(row["group_id"], text, parse_mode="HTML")
            await self.call(self.store.open_group_round, game_id, message.message_id, bool(photo))
        elif row["message_id"] is None:
            if photo:
                replacement = await bot.send_photo(row["group_id"], photo, caption=text, parse_mode="HTML")
            else:
                replacement = await bot.send_message(row["group_id"], text, parse_mode="HTML")
            await self.call(self.store.replace_group_message, game_id, replacement.message_id, bool(photo))
        elif row["status"] == "closed" and row["result_page"] > 0:
            await bot.send_message(row["group_id"], text, parse_mode="HTML")
        else:
            try:
                if photo:
                    await bot.edit_message_media(chat_id=row["group_id"], message_id=row["message_id"],
                        media=InputMediaPhoto(photo, caption=text, parse_mode="HTML"))
                elif row["media"]:
                    await bot.edit_message_caption(chat_id=row["group_id"], message_id=row["message_id"],
                        caption=text, parse_mode="HTML")
                else:
                    await bot.edit_message_text(chat_id=row["group_id"], message_id=row["message_id"],
                        text=text, parse_mode="HTML")
            except BadRequest as exc:
                error = str(exc).lower()
                if "message to edit not found" in error or "message can't be edited" in error:
                    # An admin may delete the announcement. Deliver the result on a
                    # replacement message, otherwise the saved chat lock cannot clear.
                    await self.call(self.store.replace_group_message, game_id, None, False)
                    return
                elif "message is not modified" not in error:
                    raise
            if photo and not row["media"]:
                await self.call(self.store.group_rendered, game_id, media=True)
        if row["status"] == "closed":
            await self.call(self.store.group_rendered, game_id, page=row["result_page"]+1,
                            complete=row["result_page"]+1 == len(pages))
        self.last_render = sig

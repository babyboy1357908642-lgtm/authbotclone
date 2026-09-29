"""Run with PVP_TEST_MONGO_URI pointing at a disposable MongoDB replica set."""
import asyncio
import os
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import ChatPermissions
from telegram.error import BadRequest, RetryAfter

from auction_bot.bot import AuctionBot, PVP_GROUP_COMMANDS, OWNER_ONLY_COMMANDS
from auction_bot.domain import RuleError, cents
from auction_bot.group_pvp import GroupPvP, SEND_PERMISSIONS, result_pages, round_open_text
from auction_bot.group_pvp_store import winning_payout
from auction_bot.mongo_store import MongoStore

GROUP = -100123
URI = os.environ.get("PVP_TEST_MONGO_URI")


class PresentationTests(unittest.TestCase):
    def test_requested_payouts_and_fractional_rounding(self):
        self.assertEqual(winning_payout(cents("1000"), 64), cents("1640"))
        self.assertEqual(winning_payout(cents("500"), 64), cents("820"))
        self.assertEqual(winning_payout(cents("250.01"), 64), cents("410.01"))

    def test_result_pages_escape_names_and_keep_every_player_and_actual_totals(self):
        row = dict(id=100001, h_percent=64, winner="h", winner_percent=64,
                   total_bet=cents("1750"), total_win=cents("2460"))
        bets = [dict(name="<Alice & Bob>", side="h", amount=cents("1000"), win_amount=cents("1640")),
                dict(name="@Bob", side="h", amount=cents("500"), win_amount=cents("820")),
                dict(name="@Loser", side="l", amount=cents("250"), win_amount=0)]
        text = "\n".join(result_pages(row, bets))
        self.assertIn("&lt;Alice &amp; Bob&gt; H - 1000coin", text)
        self.assertIn("1000coin + 64% = 1640coin", text)
        self.assertIn("500coin + 64% = 820coin", text)
        self.assertIn("2460coin", text)
        self.assertIn("Lose - 0coin", text)
        pages = result_pages(row, [dict(bets[0], name=f"{i}:" + "😀<&" * 20) for i in range(100)])
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 900 for p in pages))
        self.assertEqual(sum(p.count("Win -") for p in pages), 100)

    def test_commands_remove_boom_and_owner_photos_are_protected(self):
        names = [cmd.command for cmd in PVP_GROUP_COMMANDS]
        self.assertIn("pvp", names)
        self.assertNotIn("boom", names)
        self.assertTrue({"startphoto", "stopphoto", "resultphoto"} <= OWNER_ONLY_COMMANDS)

    def test_application_schedules_independent_game_worker_at_five_seconds(self):
        service = object.__new__(AuctionBot)
        service.config = SimpleNamespace(token="12345:synthetic-test-token")
        app = service.application()
        jobs = app.job_queue.jobs()
        self.assertEqual({job.callback.__name__ for job in jobs}, {"tick", "tick_pvp"})


@unittest.skipUnless(URI, "Set PVP_TEST_MONGO_URI to a disposable replica set")
class MongoRoundTests(unittest.TestCase):
    def setUp(self):
        self.db_name = "pvp_test_" + uuid.uuid4().hex
        self.store = MongoStore(URI, self.db_name)
        self.store.set("group_id", GROUP)

    def tearDown(self):
        self.store.client.drop_database(self.db_name)
        self.store.close()

    def credit(self, uid, amount="5000"):
        self.store.adjust_wallet(uid, cents(amount), 99, f"credit:{uid}", "Synthetic")

    def opened(self, now=100):
        row = self.store.ensure_group_round(GROUP, now=now)
        return self.store.open_group_round(row["id"], 123, False, now=now)

    def bet(self, row, uid, side="h", amount="1000", now=101, message_id=None):
        return self.store.place_group_bet(row["id"], GROUP, uid, f"@user{uid}", side,
            cents(amount), message_id or uid, 101, now=now)

    def close(self, row, percent=64):
        with patch("auction_bot.group_pvp_store.secrets.choice", return_value=percent):
            row = self.store.lock_group_round(row["id"], now=120)
        return self.store.settle_group_round(row["id"], now=128)

    def test_auto_round_is_unique_under_concurrency_and_counter_persists(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.store.ensure_group_round(GROUP, now=100), range(12)))
        self.assertEqual({r["id"] for r in rows}, {100001})
        row = self.opened()
        self.close(row)
        self.store.group_rendered(row["id"], complete=True)
        self.assertEqual(self.store.ensure_group_round(GROUP, now=time.time()+9)["id"], 100001)
        self.assertEqual(self.store.ensure_group_round(GROUP, now=time.time()+11)["id"], 100002)

    def test_actual_payout_and_ledger_are_idempotent_after_restart(self):
        row = self.opened()
        for uid in (1, 2, 3):
            self.credit(uid)
        self.bet(row, 1)
        self.bet(row, 2, amount="500")
        self.bet(row, 3, side="l", amount="250")
        result = self.close(row)
        self.assertEqual(result["total_bet"], cents("1750"))
        self.assertEqual(result["total_win"], cents("2460"))
        self.assertEqual(result["owner_profit"], -cents("710"))
        self.assertEqual(self.store.wallet_balance(1)["total"], cents("5640"))
        self.assertEqual(self.store.wallet_balance(2)["total"], cents("5320"))
        self.assertEqual(self.store.wallet_balance(3)["total"], cents("4750"))
        reopened = MongoStore(URI, self.db_name)
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _: reopened.settle_group_round(row["id"], now=200), range(12)))
            self.assertEqual(reopened.db.wallet_events.count_documents({"kind": "pvp_win"}), 2)
            self.assertEqual(reopened.wallet_balance(1)["total"], cents("5640"))
        finally:
            reopened.close()

    def test_duplicate_bets_and_competing_users_never_overdraw(self):
        row = self.opened()
        self.credit(1, "1000")
        def attempt(_):
            try:
                self.bet(row, 1)
                return True
            except RuleError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(16)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.wallet_balance(1)["total"], 0)
        self.assertEqual(len(self.store.group_bets(row["id"])), 1)

    def test_deadline_group_hold_and_limits_are_enforced(self):
        row = self.opened()
        self.credit(1)
        for amount in ("249.99", "30000.01"):
            with self.assertRaises(RuleError):
                self.bet(row, 1, amount=amount)
        for bad_side in ("t", "heads", "H", ""):
            with self.assertRaises(RuleError):
                self.bet(row, 1, side=bad_side)
        with self.assertRaises(RuleError):
            self.bet(row, 1, now=120)
        with self.assertRaises(RuleError):
            self.store.place_group_bet(row["id"], -999, 1, "User", "h", cents("500"), 1, 101, now=101)
        self.store.db.holds.insert_one({"_id": 1, "user_id": 1, "amount": cents("4800")})
        with self.assertRaises(RuleError):
            self.bet(row, 1, amount="250")
        self.assertEqual(self.store.db.wallet_events.count_documents({"kind": "pvp_stake"}), 0)
        self.assertEqual(self.store.group_bets(row["id"]), [])

    def test_lock_is_immutable_and_l_side_uses_its_own_percentage(self):
        row = self.opened()
        self.credit(1)
        self.bet(row, 1, side="l", amount="500")
        with patch("auction_bot.group_pvp_store.secrets.choice", return_value=36):
            self.store.lock_group_round(row["id"], now=120)
        with patch("auction_bot.group_pvp_store.secrets.choice", return_value=90):
            self.store.lock_group_round(row["id"], now=121)
        self.assertEqual(self.store.group_round(row["id"])["h_percent"], 36)
        self.assertEqual(self.store.settle_group_round(row["id"], now=127)["status"], "rolling")
        self.assertEqual(self.store.settle_group_round(row["id"], now=128)["total_win"], cents("820"))

    def test_atomic_rollback_keeps_wallet_and_bet_consistent(self):
        row = self.opened()
        self.credit(1)
        write_wallet = self.store._round_wallet
        def write_then_abort(*args, **kwargs):
            write_wallet(*args, **kwargs)
            raise RuntimeError("transaction aborted after wallet write")
        with patch.object(self.store, "_round_wallet", side_effect=write_then_abort):
            with self.assertRaises(RuntimeError):
                self.bet(row, 1)
        self.assertEqual(self.store.wallet_balance(1)["total"], cents("5000"))
        self.assertEqual(self.store.group_bets(row["id"]), [])

    def test_group_move_refunds_once_and_keeps_permissions_for_restoration(self):
        row = self.opened()
        self.credit(1)
        self.bet(row, 1)
        self.store.save_group_permissions(row["id"], {"can_send_messages": True, "can_send_photos": True})
        self.store.set("group_id", -100999)
        self.store.refund_moved_group_rounds(-100999)
        self.store.refund_moved_group_rounds(-100999)
        self.assertEqual(self.store.wallet_balance(1)["total"], cents("5000"))
        pending = self.store.pending_permission_restores()
        self.assertEqual([r["id"] for r in pending], [row["id"]])
        self.assertTrue(pending[0]["saved_permissions"]["can_send_photos"])
        self.assertEqual(self.store.group_bets(row["id"])[0]["status"], "refunded")

    def test_old_games_cancel_and_refund_only_debited_players_once(self):
        self.credit(1)
        self.credit(2)
        self.store.db.pvp_games.insert_one(dict(_id="old", id="old", status="running",
            requester_id=1, target_id=2, amount=cents("500")))
        self.store.db.boom_games.insert_one(dict(_id="pending", id="pending", status="pending",
            requester_id=1, target_id=2, amount=cents("500")))
        self.store.retire_legacy_games()
        self.store.retire_legacy_games()
        self.assertEqual(self.store.db.wallet_events.count_documents({"kind": "game_refund"}), 2)
        self.assertEqual(self.store.db.boom_games.find_one()["status"], "cancelled")


@unittest.skipUnless(URI, "Set PVP_TEST_MONGO_URI to a disposable replica set")
class TelegramRoundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_name = "pvp_test_" + uuid.uuid4().hex
        self.store = MongoStore(URI, self.db_name)
        self.store.set("group_id", GROUP)
        self.service = object.__new__(AuctionBot)
        self.service.store = self.store
        self.service.group_id = self.service.pvp_group_id = str(GROUP)
        self.service.config = SimpleNamespace(owners={99})
        self.ui = self.service.group_pvp = GroupPvP(self.service)
        self.permissions = ChatPermissions(can_send_messages=True, can_send_photos=True,
            can_send_videos=True, can_send_polls=False, can_invite_users=False)
        self.bot = SimpleNamespace(id=88, username="pvp_test_bot",
            get_chat=AsyncMock(return_value=SimpleNamespace(permissions=self.permissions)),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator", can_restrict_members=True)),
            set_chat_permissions=AsyncMock(), send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)), edit_message_media=AsyncMock(),
            edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
        self.tasks = []
        def create_task(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(create_task=create_task))

    async def asyncTearDown(self):
        if self.ui.receipt_task:
            self.tasks.append(self.ui.receipt_task)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.client.drop_database(self.db_name)
        self.store.close()

    async def tick(self, now):
        self.ui.output_after = 0
        self.ui.permission_after = 0
        with patch("auction_bot.group_pvp.time.time", return_value=now):
            await self.ui.tick(self.context)
            if self.ui.render_task:
                await self.ui.render_task

    async def test_full_loop_mutes_messages_and_media_at_18_and_restores_after_result(self):
        await self.tick(100)
        row = self.store.current_group_round(GROUP)
        self.assertEqual(row["ends_at"], 120)
        self.assertNotIn("reply_markup", self.bot.send_message.call_args.kwargs)
        await self.tick(117)
        self.bot.set_chat_permissions.assert_not_called()
        await self.tick(118)
        locked = self.bot.set_chat_permissions.call_args.args[1].to_dict()
        expected = self.permissions.to_dict()
        expected.update({key: False for key in SEND_PERMISSIONS})
        self.assertEqual(locked, expected)
        self.assertTrue(self.bot.set_chat_permissions.call_args.kwargs["use_independent_chat_permissions"])
        await self.tick(120)
        self.assertEqual(self.store.current_group_round(GROUP)["status"], "rolling")
        await self.tick(128)
        self.assertIsNotNone(self.store.current_group_round(GROUP)["announced_at"])
        await self.tick(129)
        self.assertEqual(self.bot.set_chat_permissions.call_args.args[1].to_dict(), self.permissions.to_dict())
        await self.tick(139)
        self.assertEqual(self.store.current_group_round(GROUP)["id"], 100002)

    async def test_restart_restores_even_when_mute_call_ack_was_lost(self):
        row = self.store.ensure_group_round(GROUP, now=100)
        self.store.open_group_round(row["id"], 1, False, now=100)
        self.store.save_group_permissions(row["id"], self.permissions.to_dict())
        self.store.lock_group_round(row["id"], now=120)
        self.store.settle_group_round(row["id"], now=128)
        self.store.group_rendered(row["id"], complete=True)
        self.bot.set_chat_permissions.side_effect = [RetryAfter(1), None]
        await self.tick(130)
        self.assertFalse(self.store.group_round(row["id"])["permissions_restored"])
        await self.tick(132)
        self.assertTrue(self.store.group_round(row["id"])["permissions_restored"])
        self.assertEqual(self.bot.set_chat_permissions.call_args.args[1].to_dict(), self.permissions.to_dict())

    async def test_photos_use_same_message_and_no_betting_buttons(self):
        for stage in ("start", "stop", "result"):
            self.store.set(f"pvp_{stage}_photo", stage+"-file-id")
        await self.tick(100)
        self.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.bot.send_photo.call_args.args[1], "start-file-id")
        await self.tick(120)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["media"].media, "stop-file-id")
        await self.tick(128)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["media"].media, "result-file-id")
        self.assertNotIn("reply_markup", self.bot.edit_message_media.call_args.kwargs)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["message_id"], 1)

    async def test_text_start_can_change_to_result_photo(self):
        self.store.set("pvp_result_photo", "result-file")
        await self.tick(100)
        await self.tick(120)
        await self.tick(128)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["media"].media, "result-file")
        self.assertTrue(self.store.current_group_round(GROUP)["media"])

    async def test_missing_admin_privilege_never_opens_betting(self):
        self.bot.get_chat_member.return_value.can_restrict_members = False
        await self.tick(100)
        self.assertEqual(self.store.current_group_round(GROUP)["status"], "publishing")
        self.bot.send_message.assert_not_called()

    async def test_rate_limit_delays_transport_without_reopening_or_repaying_round(self):
        await self.tick(100)
        await self.tick(120)
        self.bot.edit_message_text.side_effect = RetryAfter(12)
        await self.tick(128)
        row = self.store.current_group_round(GROUP)
        self.assertEqual(row["status"], "closed")
        self.assertIsNone(row["announced_at"])
        self.assertGreater(self.ui.output_after, time.monotonic()+12)
        self.bot.edit_message_text.side_effect = None
        await self.tick(129)
        self.assertIsNotNone(self.store.current_group_round(GROUP)["announced_at"])

    async def test_owner_photo_upload_and_nonowner_cannot_change_settings(self):
        message = SimpleNamespace(photo=[SimpleNamespace(file_id="uploaded-file")], reply_text=AsyncMock(),
            text="", sender_chat=None, reply_to_message=None)
        context = SimpleNamespace(user_data={}, bot=self.bot)
        await self.service.owner_command("startphoto", [], message, context)
        await self.service.pvp_photo_input(message, context)
        self.assertEqual(self.store.get("pvp_start_photo"), "uploaded-file")
        await self.service.owner_command("startphoto", ["clear"], message, context)
        self.assertEqual(self.store.get("pvp_start_photo"), "")
        message.text = "/stopphoto"
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=5, is_bot=False),
            effective_chat=SimpleNamespace(id=5, type="private"))
        await self.service.message(update, context)
        self.assertNotIn("pvp_photo_edit", context.user_data)

    async def test_command_bet_and_wrong_chat_routing(self):
        await self.tick(100)
        self.store.adjust_wallet(5, cents("2000"), 99, "seed", "Synthetic")
        user = SimpleNamespace(id=5, username="alice", full_name="Alice", is_bot=False)
        message = SimpleNamespace(text="/pvp 1000 h", sender_chat=None, reply_to_message=None,
            chat_id=GROUP, message_id=5, date=datetime.fromtimestamp(101, timezone.utc), reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=user,
            effective_chat=SimpleNamespace(id=GROUP, type="supergroup"))
        self.ui.output_after = 0
        with patch("auction_bot.group_pvp_store.time.time", return_value=101):
            await self.service.message(update, SimpleNamespace(bot=self.bot, user_data={}))
        self.assertEqual(self.store.wallet_balance(5)["total"], cents("1000"))
        await self.ui.receipt_task
        self.assertIn("လောင်းပြီး", message.reply_text.call_args.args[0])
        update.effective_chat.id = -100999
        message.reply_text.reset_mock()
        await self.service.message(update, SimpleNamespace(bot=self.bot, user_data={}))
        message.reply_text.assert_not_called()

    async def test_deleted_result_message_is_replaced_before_chat_restores(self):
        await self.tick(100)
        await self.tick(120)
        self.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        await self.tick(128)
        self.assertIsNone(self.store.current_group_round(GROUP)["message_id"])
        self.assertIsNone(self.store.current_group_round(GROUP)["announced_at"])
        await self.tick(129)
        self.assertIn("PVP RESULT", self.bot.send_message.call_args.args[1])
        await self.tick(130)
        self.assertTrue(self.store.current_group_round(GROUP)["permissions_restored"])

    async def test_slow_receipts_do_not_block_more_than_16_players_from_betting(self):
        await self.tick(100)
        self.ui.output_after = time.monotonic() + 60
        with patch("auction_bot.group_pvp_store.time.time", return_value=101):
            for uid in range(1, 21):
                self.store.adjust_wallet(uid, cents("1000"), 99, f"seed:{uid}", "Synthetic")
                user = SimpleNamespace(id=uid, username=None, full_name=f"User {uid}", is_bot=False)
                message = SimpleNamespace(chat_id=GROUP, message_id=uid, reply_text=AsyncMock(),
                    date=datetime.fromtimestamp(101, timezone.utc))
                await asyncio.wait_for(self.ui.bet(["250", "h"], message, user), timeout=1)
        row = self.store.current_group_round(GROUP)
        self.assertEqual(row["h_count"], 20)
        self.assertEqual(row["total_bet"], cents("5000"))
        self.assertEqual(len(self.ui.receipts), 20)

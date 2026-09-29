"""Shared PvP rounds, using MongoStore's serialized wallet transactions."""
import secrets
import time

from .domain import MAX_PVP_WAGER, MIN_PVP_WAGER, RuleError

BET_SECONDS = 20
ROUND_PAUSE_SECONDS = 10
ANIMATION_SECONDS = 8


def winning_payout(amount, percent):
    # Wallets use hundredths of a coin; discard fractions of a subunit.
    return amount + amount * percent // 100


class GroupPvPStore:
    def init_group_pvp(self):
        self.db.group_pvp_rounds.create_index(
            "group_id", unique=True, partialFilterExpression={"active": True})
        self.db.group_pvp_bets.create_index([("game_id", 1), ("user_id", 1)], unique=True)
        self.db.group_pvp_bets.create_index("event_key", unique=True)

    def _round_wallet(self, session, user_id, delta, kind, key, note, now):
        self.db.wallets.update_one({"_id": user_id}, {"$inc": {"balance": delta}},
                                   upsert=True, session=session)
        eid = self._next("wallet_events", session)
        self.db.wallet_events.insert_one(dict(
            _id=eid, id=eid, user_id=user_id, delta=delta, kind=kind,
            note=note, actor_id=None, auction_id=None, event_key=key, created=now), session=session)

    def retire_legacy_games(self):
        """Cancel old invitations and return outstanding stakes once on upgrade."""
        def retire(s):
            if self.get("group_pvp_migrated", session=s) == "1":
                return
            for name in ("pvp_games", "boom_games"):
                collection = self.db[name]
                for row in collection.find({"status": {"$in": ["pending", "running"]}}, session=s):
                    if row["status"] == "running":
                        players = [row["requester_id"]]
                        if row.get("mode") != "solo":
                            players.append(row["target_id"])
                        for uid in players:
                            self._round_wallet(s, uid, row["amount"], "game_refund",
                                f"retire:{name}:{row['_id']}:{uid}", "PvP upgrade refund", time.time())
                    collection.update_one({"_id": row["_id"]}, {"$set": {
                        "status": "cancelled", "next_at": None, "turn_at": None}}, session=s)
            self.db.settings.update_one({"_id": "group_pvp_migrated"},
                {"$set": {"value": "1"}}, upsert=True, session=s)
        return self._tx(retire)

    def group_round(self, game_id):
        return self._clean(self.db.group_pvp_rounds.find_one({"_id": game_id}))

    def current_group_round(self, group_id):
        return self._clean(self.db.group_pvp_rounds.find_one({"group_id": group_id, "active": True}))

    def ensure_group_round(self, group_id, now=None):
        def ensure(s):
            at = time.time() if now is None else now
            if str(group_id) != self.get("group_id", session=s):
                return None
            row = self.db.group_pvp_rounds.find_one({"group_id": group_id, "active": True}, session=s)
            if row:
                if (row["status"] != "closed" or row.get("announced_at") is None
                        or row.get("permissions_restored") is False
                        or at < row["announced_at"] + ROUND_PAUSE_SECONDS):
                    return self._clean(row)
                self.db.group_pvp_rounds.update_one({"_id": row["_id"]}, {"$set": {"active": False}}, session=s)
            game_id = 100000 + self._next("group_pvp_rounds", s)
            row = dict(_id=game_id, id=game_id, group_id=group_id, active=True,
                       status="publishing", created_at=at, message_id=None, media=False,
                       total_bet=0, total_win=0, h_count=0, l_count=0, h_amount=0, l_amount=0,
                       result_page=0, announced_at=None)
            self.db.group_pvp_rounds.insert_one(row, session=s)
            return self._clean(row)
        return self._tx(ensure)

    def open_group_round(self, game_id, message_id, media, now=None):
        def open_round(s):
            at = time.time() if now is None else now
            self.db.group_pvp_rounds.update_one({"_id": game_id, "status": "publishing"},
                {"$set": {"status": "open", "message_id": message_id, "media": media,
                          "opened_at": at, "ends_at": at + BET_SECONDS}}, session=s)
        self._tx(open_round)
        return self.group_round(game_id)

    def place_group_bet(self, game_id, group_id, user_id, name, side, amount, message_id, sent_at, now=None):
        if side not in ("h", "l"):
            raise RuleError("/pvp 1000 h သို့ /pvp 500 l ပုံစံရေးပါ။")
        if type(amount) is not int or not MIN_PVP_WAGER <= amount <= MAX_PVP_WAGER:
            raise RuleError("PvP လောင်းကြေးကို 250 မှ 30000 coin အတွင်းထားပါ။")
        def bet(s):
            at = time.time() if now is None else now
            if str(group_id) != self.get("group_id", session=s):
                raise RuleError("သတ်မှတ်ထားတဲ့ group မှာပဲ လောင်းနိုင်ပါတယ်။")
            row = self.db.group_pvp_rounds.find_one({"_id": game_id, "group_id": group_id}, session=s)
            if not row or row["status"] != "open" or at >= row["ends_at"] or sent_at < int(row["opened_at"]):
                raise RuleError("လောင်းကြေးပိတ်ပါပြီ။ နောက်ပွဲမှာ လောင်းပါ။")
            key = f"group-pvp:{group_id}:{message_id}"
            if self.db.group_pvp_bets.find_one({"$or": [
                    {"game_id": game_id, "user_id": user_id}, {"event_key": key}]}, session=s):
                raise RuleError("ဒီပွဲမှာ လောင်းပြီးပါပြီ။ တစ်ပွဲတစ်ကြိမ်သာ လောင်းနိုင်ပါတယ်။")
            if self.wallet_balance(user_id, s)["available"] < amount:
                raise RuleError("လက်ကျန်မလုံလောက်ပါ။ /bal ကိုစစ်ပါ။")
            self._round_wallet(s, user_id, -amount, "pvp_stake", key,
                               f"PvP #{game_id} · {side.upper()}", at)
            self.db.group_pvp_bets.insert_one(dict(
                _id=f"{game_id}:{user_id}", game_id=game_id, user_id=user_id, name=name[:100],
                side=side, amount=amount, status="placed", win_amount=0, event_key=key, created_at=at), session=s)
            self.db.group_pvp_rounds.update_one({"_id": game_id}, {"$inc": {
                "total_bet": amount, f"{side}_count": 1, f"{side}_amount": amount}}, session=s)
        return self._tx(bet)

    def lock_group_round(self, game_id, now=None):
        # Both sides have equal probability; no tie, independent of stakes.
        percent = secrets.choice(tuple(range(1, 50)) + tuple(range(51, 100)))
        def lock(s):
            at = time.time() if now is None else now
            self.db.group_pvp_rounds.update_one(
                {"_id": game_id, "status": "open", "ends_at": {"$lte": at}},
                {"$set": {"status": "rolling", "h_percent": percent,
                          "settle_at": at + ANIMATION_SECONDS}}, session=s)
        self._tx(lock)
        return self.group_round(game_id)

    def settle_group_round(self, game_id, now=None):
        def settle(s):
            at = time.time() if now is None else now
            row = self.db.group_pvp_rounds.find_one({"_id": game_id}, session=s)
            if not row or row["status"] != "rolling" or at < row["settle_at"]:
                return
            side = "h" if row["h_percent"] > 50 else "l"
            percent = row["h_percent"] if side == "h" else 100 - row["h_percent"]
            total = 0
            for bet in self.db.group_pvp_bets.find({"game_id": game_id}, session=s):
                payout = winning_payout(bet["amount"], percent) if bet["side"] == side else 0
                if payout:
                    self._round_wallet(s, bet["user_id"], payout, "pvp_win",
                        f"group-pvp:{game_id}:{bet['user_id']}:win", f"PvP #{game_id} · {percent}%", at)
                total += payout
                self.db.group_pvp_bets.update_one({"_id": bet["_id"]}, {"$set": {
                    "status": "won" if payout else "lost", "win_amount": payout}}, session=s)
            self.db.group_pvp_rounds.update_one({"_id": game_id}, {"$set": {
                "status": "closed", "winner": side, "winner_percent": percent,
                "total_win": total, "owner_profit": row["total_bet"] - total, "closed_at": at}}, session=s)
        self._tx(settle)
        return self.group_round(game_id)

    def group_bets(self, game_id):
        return list(self.db.group_pvp_bets.find({"game_id": game_id}).sort([("created_at", 1), ("user_id", 1)]))

    def group_rendered(self, game_id, *, media=None, page=None, complete=False):
        changes = {}
        if media is not None:
            changes["media"] = media
        if page is not None:
            changes["result_page"] = page
        if complete:
            changes["announced_at"] = time.time()
        self.db.group_pvp_rounds.update_one({"_id": game_id}, {"$set": changes})

    def refund_moved_group_rounds(self, group_id):
        """Return open stakes when the configured group changes, including via env."""
        def refund(s):
            for row in self.db.group_pvp_rounds.find({"active": True, "group_id": {"$ne": group_id}}, session=s):
                if row["status"] != "closed":
                    for bet in self.db.group_pvp_bets.find({"game_id": row["id"], "status": "placed"}, session=s):
                        self._round_wallet(s, bet["user_id"], bet["amount"], "game_refund",
                            f"group-pvp:{row['id']}:{bet['user_id']}:refund", "Game group changed", time.time())
                        self.db.group_pvp_bets.update_one({"_id": bet["_id"]}, {"$set": {"status": "refunded"}}, session=s)
                self.db.group_pvp_rounds.update_one({"_id": row["id"]}, {"$set": {
                    "active": False, "status": "closed" if row["status"] == "closed" else "cancelled"}}, session=s)
        self._tx(refund)

    def save_group_permissions(self, game_id, permissions):
        # Persist before calling Telegram, so a crash during the call is recoverable.
        self.db.group_pvp_rounds.update_one(
            {"_id": game_id, "saved_permissions": {"$exists": False}},
            {"$set": {"saved_permissions": permissions, "permissions_restored": False}})

    def mark_group_permissions(self, game_id, restored=False):
        self.db.group_pvp_rounds.update_one({"_id": game_id}, {"$set": {
            "permissions_restored": restored, "muted": not restored}})

    def pending_permission_restores(self):
        return [self._clean(row) for row in self.db.group_pvp_rounds.find({
            "permissions_restored": False, "$or": [
                {"status": "cancelled"}, {"status": "closed", "announced_at": {"$ne": None}}]})]

    def replace_group_message(self, game_id, message_id, media):
        self.db.group_pvp_rounds.update_one({"_id": game_id}, {"$set": {
            "message_id": message_id, "media": media}})

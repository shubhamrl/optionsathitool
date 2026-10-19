"""
Telegram Signal & Outcome Dispatcher (Strategy Scanner signals only).

Design:
  - notify_new_signal() is SYNC and O(1): it copies a few fields and drops a job on an
    in-memory queue. The strategy loop never waits on Telegram.
  - ONE sender task drains the queue (>=1.1s apart, Telegram's per-chat limit, handles 429).
  - A separate watcher task polls the `signals` collection every few seconds; when a posted
    signal turns TARGET_HIT / SL_HIT / EXPIRED it enqueues an outcome reply. This works
    no matter which part of the engine closed the signal (tick path, safety-net, EOD).
  - Telegram message_id + signal state live in MongoDB (`telegram_posts`, 7-day TTL),
    so a server restart loses nothing.
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from bson import ObjectId

from app.core.config import settings
from app.core.market_hours import is_market_open
from app.models.paper_trading import LOT_SIZES

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

# ---------------- tunables ----------------
SEND_GAP_SECONDS = 1.1             # Telegram allows ~1 msg/sec per chat
WATCH_INTERVAL_SECONDS = 3         # how fast outcomes are noticed
DEDUP_MINUTES = 30                 # same contract is not re-posted inside this window
MAX_POSTS_PER_INDEX_PER_HOUR = 4   # flood guard per index
STALE_POST_HOURS = 18              # never post an outcome for a signal older than this
STOP_T2_WATCH_HM = (15, 20)        # IST: stop watching for Target 2 after this
ENTRY_ZONE_PCT = 0.0               # 0 = single entry price; 0.01 = show entry..entry+1%
POST_RETENTION_DAYS = 7
MAX_QUEUE_SIZE = 200
FOOTER = "Testing phase • Not investment advice"

_queue: Optional[asyncio.Queue] = None
_client: Optional[httpx.AsyncClient] = None
_peaks: Dict[str, float] = {}      # signal_id -> highest sampled option LTP while open


# ---------------- config helpers ----------------
def _cfg(name: str) -> str:
    val = getattr(settings, name, "") or os.environ.get(name, "")
    return str(val or "").strip()


def is_enabled() -> bool:
    return (
        _cfg("TELEGRAM_ENABLED").lower() in ("true", "1", "yes")
        and bool(_cfg("TELEGRAM_BOT_TOKEN"))
        and bool(_cfg("TELEGRAM_CHAT_ID"))
    )


def _redact(text: str) -> str:
    token = _cfg("TELEGRAM_BOT_TOKEN")
    return text.replace(token, "***") if token else text


# ---------------- formatting helpers ----------------
def _esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def _p(v: float) -> str:
    s = f"{v:,.2f}"
    return s[:-3] if s.endswith(".00") else s


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _fmt_time() -> str:
    return _now_ist().strftime("%I:%M %p IST")


def _contract_label(index_name: str, strike: str, expiry: Optional[str]) -> str:
    exp = ""
    if expiry:
        try:
            exp = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").strftime("%d %b").upper() + " "
        except Exception:
            exp = ""
    m = re.match(r"^(\d+)(CE|PE)$", strike or "")
    if m:
        return f"{index_name} {exp}{m.group(1)} {m.group(2)}"
    return f"{index_name} {exp}{strike}"


def _inr(v: float) -> str:
    return f"₹{abs(v):,.0f}"


def _signed_inr(v: float) -> str:
    return f"+₹{v:,.0f}" if v >= 0 else f"-₹{abs(v):,.0f}"


def _bar(kind: str) -> str:
    return {"green": "🟩", "red": "🟥", "amber": "🟨"}.get(kind, "⬜") * 10


def _signal_text(job: Dict[str, Any], expiry: Optional[str]) -> str:
    is_ce = job["selected_type"] == "CE"
    icon = "🟢" if is_ce else "🔴"
    view = "Bullish view 📈" if is_ce else "Bearish view 📉"
    bar = _bar("green" if is_ce else "red")
    qty = LOT_SIZES.get(job["index_name"], 0)
    entry, sl, t1, t2 = job["entry"], job["sl"], job["t1"], job["t2"]

    if ENTRY_ZONE_PCT > 0:
        entry_txt = f"₹{_p(entry)} - ₹{_p(round(entry * (1 + ENTRY_ZONE_PCT), 2))}"
    else:
        entry_txt = f"₹{_p(entry)}"

    reason = job.get("reason", "")
    if len(reason) > 110:
        reason = reason[:107] + "..."
    trigger = f"{job['strategy']} — {reason}" if reason else job["strategy"]
    spot_txt = f" (Spot: {job['spot']:,.0f})" if job.get("spot", 0) > 0 else ""

    lines = [
        f"⚡🔥 <b>OPTIONSAATHI RADAR | {_esc(job['index_name'])}</b> 🔥⚡",
        bar,
        f"🎯 <b>{_esc(_contract_label(job['index_name'], job['strike'], expiry))}</b>",
        f"{icon} <b>BUY {job['selected_type']}</b> — {view}",
        "",
        f"💰 Entry: <b>{entry_txt}</b>",
        f"🛑 Stop-Loss: ₹{_p(sl)}",
        f"🎯 Target 1: ₹{_p(t1)}",
    ]
    if t2 > t1:
        lines.append(f"🚀 Target 2: ₹{_p(t2)}")

    if qty and entry > sl > 0 and t1 > entry:
        risk_lot = (entry - sl) * qty
        reward_lot = (t1 - entry) * qty
        rr = (t1 - entry) / (entry - sl)
        lines += [
            "",
            f"💼 1 Lot ({qty} qty) = {_inr(entry * qty)}",
            f"📉 Risk/lot ≈ {_inr(risk_lot)}  |  📈 T1 reward/lot ≈ {_inr(reward_lot)}  (R:R 1:{rr:.1f})",
        ]

    lines += [
        "",
        f"🔍 Trigger: {_esc(trigger)}{spot_txt}",
        f"⏰ Time: {_fmt_time()}",
        bar,
        _esc(FOOTER),
    ]
    return "\n".join(lines)


def _outcome_text(post: Dict[str, Any], status: str, exit_price: float, high_made: Optional[float]) -> str:
    entry = float(post["entry"])
    pts = exit_price - entry
    pct = (pts / entry * 100) if entry else 0.0
    qty = LOT_SIZES.get(post["index_name"], 0)
    lot_pl = pts * qty
    res = f"{pts:+.1f} Pts ({pct:+.1f}%)"

    if status == "TARGET_HIT":
        head = "🚀🚀 <b>TARGET 1 HIT!</b> 🚀🚀 | OPTIONSAATHI"
        bar = _bar("green")
        result_line = f"💵 Profit: <b>{res}</b>"
        money_label = "🤑 Profit per lot"
        foot = "✅ Target 1 achieved. 🔥"
        time_label = "Hit Time"
    elif status == "SL_HIT":
        head = "🛑 <b>STOP-LOSS HIT</b> | OPTIONSAATHI"
        bar = _bar("red")
        result_line = f"💸 Loss: <b>{res}</b>"
        money_label = "📉 Loss per lot"
        foot = "❌ Stop-loss triggered. Position closed."
        time_label = "Hit Time"
    else:
        head = "⏰ <b>EOD CLOSE</b> | OPTIONSAATHI"
        bar = _bar("amber")
        result_line = f"📊 P/L at close: <b>{res}</b>"
        money_label = "💼 P/L per lot"
        foot = "Closed at market close (CMP)."
        time_label = "Close Time"

    lines = [
        head,
        bar,
        f"📌 Contract: {_esc(post['label'])}",
        f"💰 Entry ₹{entry:,.2f}  ➜  🏁 Exit ₹{exit_price:,.2f}",
    ]
    if status == "TARGET_HIT" and high_made:
        lines.append(f"🔥 High Made: ₹{high_made:,.2f}")
    lines.append(result_line)
    if qty:
        lines.append(f"{money_label} ({qty} qty): <b>{_signed_inr(lot_pl)}</b>  <i>(before charges)</i>")
    lines += [
        f"⏰ {time_label}: {_fmt_time()}",
        bar,
        foot,
    ]
    return "\n".join(lines)


def _t2_text(post: Dict[str, Any], ltp: float) -> str:
    entry = float(post["entry"])
    pts = ltp - entry
    pct = (pts / entry * 100) if entry else 0.0
    qty = LOT_SIZES.get(post["index_name"], 0)
    lines = [
        "🎯🚀 <b>TARGET 2 HIT!</b> 🚀🎯 | OPTIONSAATHI",
        _bar("green"),
        f"📌 Contract: {_esc(post['label'])}",
        f"💰 Entry ₹{entry:,.2f}  ➜  🚀 Reached ₹{ltp:,.2f}",
        f"💵 Move vs entry: <b>{pts:+.1f} Pts ({pct:+.1f}%)</b>",
    ]
    if qty:
        lines.append(f"🤑 Per lot ({qty} qty): <b>{_signed_inr(pts * qty)}</b>  <i>(before charges)</i>")
    lines += [
        f"⏰ Time: {_fmt_time()}",
        _bar("green"),
        "Follow-up: Target 2 reached after Target 1. 🔥",
    ]
    return "\n".join(lines)


# ---------------- producer side (called from the strategy engine) ----------------
def _enqueue(job: Dict[str, Any]):
    if _queue is None:
        return
    try:
        _queue.put_nowait(job)
    except asyncio.QueueFull:
        logger.warning("📨 Telegram queue full — job dropped.")


def notify_new_signal(signal_doc: Dict[str, Any], strategy_nickname: str, reason_lines: Optional[List[str]] = None):
    """Non-blocking and exception-safe. Does nothing if Telegram is disabled."""
    if _queue is None:
        return
    try:
        reasons = [r for r in (reason_lines or []) if r]
        _enqueue({
            "type": "signal",
            "signal_id": str(signal_doc["_id"]),
            "index_name": signal_doc["index_name"],
            "strike": signal_doc.get("strike", ""),
            "selected_type": signal_doc["selected_type"],
            "security_id": str(signal_doc["security_id"]),
            "entry": float(signal_doc["entry_price"]),
            "sl": float(signal_doc["stop_loss"]),
            "t1": float(signal_doc["shz_upper"]),
            "t2": float(signal_doc.get("target2") or 0.0),
            "spot": float(signal_doc.get("index_spot") or 0.0),
            "strategy": strategy_nickname,
            "reason": reasons[0] if reasons else "",
        })
    except Exception as e:
        logger.warning(f"📨 Telegram enqueue failed: {_redact(str(e))}")


# ---------------- Telegram API ----------------
async def _send(text: str, reply_to: Optional[int] = None) -> Optional[int]:
    url = f"https://api.telegram.org/bot{_cfg('TELEGRAM_BOT_TOKEN')}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": _cfg("TELEGRAM_CHAT_ID"),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}

    for attempt in range(3):
        try:
            resp = await _client.post(url, json=payload)
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get("ok"):
                return int(data["result"]["message_id"])
            if resp.status_code == 429:
                wait = int((data.get("parameters") or {}).get("retry_after", 5)) + 1
                logger.warning(f"📨 Telegram rate-limited, waiting {wait}s.")
                await asyncio.sleep(wait)
                continue
            # NOTE: never log the URL (it contains the bot token)
            logger.error(f"📨 Telegram sendMessage failed: HTTP {resp.status_code} — {data.get('description')}")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"📨 Telegram send error ({type(e).__name__}), attempt {attempt + 1}/3")
            await asyncio.sleep(2 * (attempt + 1))
    return None


# ---------------- live price helper (works even if token->index routing is off) ----------------
def _live_ltp(index_name: str, security_id: str) -> Optional[float]:
    from app.services.dhan_websocket import market_data_store
    node = (market_data_store.get(index_name) or {}).get(security_id)
    if not (node and node.get("ltp", 0) > 0):
        for bucket in market_data_store.values():
            node = bucket.get(security_id)
            if node and node.get("ltp", 0) > 0:
                break
        else:
            return None
    return float(node["ltp"])


# ---------------- sender-side job handlers ----------------
async def _get_db():
    from app.core.database import get_database
    return await get_database()


async def _handle_signal_job(db, job: Dict[str, Any]):
    now = datetime.utcnow()
    contract_key = f"{job['index_name']}:{job['strike']}"

    dup = await db.telegram_posts.find_one({
        "contract_key": contract_key,
        "posted_at": {"$gte": now - timedelta(minutes=DEDUP_MINUTES)}
    })
    if dup:
        logger.info(f"📨 [TG SKIP] duplicate contract within {DEDUP_MINUTES} min: {contract_key}")
        return

    hourly = await db.telegram_posts.count_documents({
        "index_name": job["index_name"],
        "posted_at": {"$gte": now - timedelta(hours=1)}
    })
    if hourly >= MAX_POSTS_PER_INDEX_PER_HOUR:
        logger.info(f"📨 [TG SKIP] hourly cap reached for {job['index_name']}.")
        return

    snap = await db.market_snapshots.find_one({"index_name": job["index_name"]}, {"expiry": 1})
    expiry = snap.get("expiry") if snap else None
    label = _contract_label(job["index_name"], job["strike"], expiry)

    message_id = await _send(_signal_text(job, expiry))
    if message_id is None:
        return

    await db.telegram_posts.insert_one({
        "signal_id": job["signal_id"],
        "contract_key": contract_key,
        "index_name": job["index_name"],
        "security_id": job["security_id"],
        "label": label,
        "entry": job["entry"], "sl": job["sl"], "t1": job["t1"], "t2": job["t2"],
        "chat_id": _cfg("TELEGRAM_CHAT_ID"),
        "message_id": message_id,
        "posted_at": now,
        "outcome_queued": False, "outcome_posted": False,
        "t2_watch": False, "t2_posted": False,
    })
    logger.info(f"📨 [TG SIGNAL] {label} posted (message_id {message_id}).")


async def _handle_outcome_job(db, job: Dict[str, Any]):
    post = job["post"]
    text = _outcome_text(post, job["status"], job["exit_price"], job.get("high_made"))
    message_id = await _send(text, reply_to=post["message_id"])

    if message_id is None:
        attempts = post.get("outcome_attempts", 0) + 1
        upd: Dict[str, Any] = {"outcome_attempts": attempts}
        if attempts < 3:
            upd["outcome_queued"] = False   # watcher will retry next cycle
        await db.telegram_posts.update_one({"_id": post["_id"]}, {"$set": upd})
        return

    upd = {"outcome_posted": True, "outcome_message_id": message_id, "outcome_status": job["status"]}
    if job["status"] == "TARGET_HIT" and post["t2"] > post["t1"]:
        upd["t2_watch"] = True
    await db.telegram_posts.update_one({"_id": post["_id"]}, {"$set": upd})
    logger.info(f"📨 [TG OUTCOME] {post['label']} → {job['status']}.")


async def _handle_t2_job(db, job: Dict[str, Any]):
    post = job["post"]
    message_id = await _send(_t2_text(post, job["ltp"]), reply_to=post["message_id"])
    if message_id:
        logger.info(f"📨 [TG T2] {post['label']} reached Target 2.")


async def _sender_loop():
    logger.info("📨 Telegram sender started.")
    try:
        try:
            db = await _get_db()
            await db.telegram_posts.create_index("signal_id", unique=True)
            await db.telegram_posts.create_index("posted_at", expireAfterSeconds=POST_RETENTION_DAYS * 86400)
        except Exception as e:
            logger.warning(f"📨 telegram_posts index setup skipped: {_redact(str(e))}")

        while True:
            job = await _queue.get()
            try:
                db = await _get_db()
                kind = job.get("type")
                if kind == "signal":
                    await _handle_signal_job(db, job)
                elif kind == "outcome":
                    await _handle_outcome_job(db, job)
                elif kind == "t2":
                    await _handle_t2_job(db, job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"📨 Telegram job '{job.get('type')}' failed: {type(e).__name__}: {_redact(str(e))}")
            finally:
                _queue.task_done()
            await asyncio.sleep(SEND_GAP_SECONDS)
    except asyncio.CancelledError:
        logger.info("📨 Telegram sender stopped.")
    finally:
        if _client:
            await _client.aclose()


# ---------------- outcome watcher ----------------
async def _check_open_posts(db):
    posts = await db.telegram_posts.find(
        {"outcome_queued": False},
        {"signal_id": 1, "index_name": 1, "security_id": 1, "posted_at": 1}
    ).to_list(length=300)
    if not posts:
        return

    now = datetime.utcnow()
    obj_ids = []
    for p in posts:
        try:
            obj_ids.append(ObjectId(p["signal_id"]))
        except Exception:
            pass

    status_map: Dict[str, Dict[str, Any]] = {}
    async for s in db.signals.find({"_id": {"$in": obj_ids}}, {"status": 1, "exit_ltp": 1}):
        status_map[str(s["_id"])] = s

    for p in posts:
        sid = p["signal_id"]

        ltp = _live_ltp(p["index_name"], p["security_id"])
        if ltp is not None:
            _peaks[sid] = max(_peaks.get(sid, ltp), ltp)

        if now - p["posted_at"] > timedelta(hours=STALE_POST_HOURS):
            await db.telegram_posts.update_one({"_id": p["_id"]}, {"$set": {"outcome_queued": True, "skipped_stale": True}})
            _peaks.pop(sid, None)
            continue

        s = status_map.get(sid)
        if not s or s.get("status") == "ACTIVE":
            continue

        claim = await db.telegram_posts.find_one_and_update(
            {"_id": p["_id"], "outcome_queued": False},
            {"$set": {"outcome_queued": True}}
        )
        if not claim:
            continue

        status = s["status"]
        exit_price = s.get("exit_ltp")
        if not exit_price or exit_price <= 0:
            exit_price = claim["t1"] if status == "TARGET_HIT" else (claim["sl"] if status == "SL_HIT" else claim["entry"])
        exit_price = float(exit_price)

        high_made = max(_peaks.get(sid, 0.0), exit_price) if status == "TARGET_HIT" else None
        _peaks.pop(sid, None)

        _enqueue({"type": "outcome", "post": claim, "status": status,
                  "exit_price": exit_price, "high_made": high_made})


async def _check_t2_watch(db):
    ist = _now_ist()
    if (ist.hour, ist.minute) >= STOP_T2_WATCH_HM:
        await db.telegram_posts.update_many({"t2_watch": True}, {"$set": {"t2_watch": False}})
        return

    posts = await db.telegram_posts.find({"t2_watch": True, "t2_posted": False}).to_list(length=100)
    for p in posts:
        ltp = _live_ltp(p["index_name"], p["security_id"])
        if ltp is None or ltp < p["t2"]:
            continue
        claim = await db.telegram_posts.find_one_and_update(
            {"_id": p["_id"], "t2_posted": False}, {"$set": {"t2_posted": True}}
        )
        if claim:
            _enqueue({"type": "t2", "post": claim, "ltp": ltp})


async def _outcome_watcher_loop():
    logger.info("📨 Telegram outcome watcher started.")
    while True:
        try:
            if not is_market_open():
                await asyncio.sleep(60)   # no polling (and no DB traffic) outside market hours
                continue
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            db = await _get_db()
            await _check_open_posts(db)
            await _check_t2_watch(db)
        except asyncio.CancelledError:
            logger.info("📨 Telegram outcome watcher stopped.")
            break
        except Exception as e:
            logger.warning(f"📨 Telegram watcher error: {type(e).__name__}: {_redact(str(e))}")


# ---------------- startup hook ----------------
def start_telegram_dispatcher() -> List[asyncio.Task]:
    global _queue, _client
    if not is_enabled():
        logger.info("📨 Telegram dispatcher disabled (needs TELEGRAM_ENABLED=true + bot token + chat id).")
        return []
    _queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    _client = httpx.AsyncClient(timeout=10.0)
    logger.info("📨 Telegram dispatcher enabled.")
    return [
        asyncio.create_task(_sender_loop()),
        asyncio.create_task(_outcome_watcher_loop()),
    ]
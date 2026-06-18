"""
Akavafa Design Telegram Bot — نسخه ۲.۰
──────────────────────────────────────────────────────────────
 ویژگی‌های جدید این نسخه:
 • انتخاب تعداد هنگام ثبت سفارش
 • دسته‌بندی محصولات (ادمین + مشتری)
 • سیستم رفرال با تخفیف ۵٪ خودکار
 • ورود کد تخفیف پیش از پرداخت
 • دکمه وبسایت در منوی اصلی
 • صفحه حساب کاربری با لینک دعوت
 • مشاهده تمام تعرفه‌ها یکجا
"""

import logging
import sqlite3
import os
import re
import io
import random
import string
import time
import urllib.request
import json
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                           MessageHandler, filters, ContextTypes)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ─── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "8673942002:AAEMRvP0CAquv_x9vxOrSy7G42NfdeMLsQs")
ADMIN_CHAT_ID     = 81941910
WEBSITE_URL       = "https://www.akavafa.com"
REFERRAL_DISCOUNT = 5           # درصد تخفیف رفرال

CARD_NUMBER   = "6219861903411345"
SHEBA_NUMBER  = "IR740560611828005102763501"
WALLET_BTC    = "bc1q0tqn52cm6k48c22hah4cwhazh0cs6hzf7pku8a"
WALLET_ETH    = "0xE5bBDE466646A3d6D9C1F36Ad7edc0064234fe10"
WALLET_USDT   = "TNXz7tDFgeVQwpkvsY2CsZnWH48cVhJQqW"

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.path.join(BASE_DIR, "persian_bot.db")

_BOT_USERNAME: str | None = None      # cached for referral links

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Tehran timezone helper ────────────────────────────────────────────────────
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

def now_tehran() -> str:
    """زمان دقیق تهران (UTC+3:30) برای ثبت در دیتابیس."""
    return (datetime.now(timezone.utc) + TEHRAN_OFFSET).strftime("%Y-%m-%d %H:%M:%S")

def fmt_tehran(utc_str: str) -> str:
    """تبدیل timestamp ذخیره‌شده به زمان تهران برای نمایش."""
    try:
        dt = datetime.strptime(str(utc_str)[:19], "%Y-%m-%d %H:%M:%S")
        return (dt + TEHRAN_OFFSET).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(utc_str)[:16]

# ─── Live price fetching ───────────────────────────────────────────────────────
# منابع: Tetherland (USDT/تومان) + CoinGecko/Binance (BTC & ETH/USD)
_price_cache: dict = {}
CACHE_TTL         = 90            # ثانیه — نرخ هر ۹۰ ثانیه به‌روز می‌شود
USDT_TOMAN_FB     = 95_000        # fallback در صورت در دسترس نبودن API

COINGECKO_IDS = {"btc": "bitcoin", "eth": "ethereum"}

def _fetch_url(url: str, timeout: int = 7) -> dict | list | None:
    """GET ساده بدون کتابخانه اضافی."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning(f"fetch {url[:60]}…  err: {e}")
        return None

# ── نرخ USDT/تومان ─────────────────────────────────────────────────────────────
def get_usdt_toman() -> int:
    """
    دریافت نرخ لحظه‌ای تتر به تومان.
    اولویت: ۱. Tetherland  ۲. Nobitex  ۳. مقدار ثابت
    """
    # ۱. Tetherland — endpoint رسمی
    data = _fetch_url("https://api.tetherland.com/currencies")
    if data:
        try:
            # پاسخ: {"USDT": {"price": "95000", ...}, ...}
            if isinstance(data, dict) and "USDT" in data:
                price = data["USDT"].get("price") or data["USDT"].get("sell_price")
                if price:
                    return int(float(str(price).replace(",", "")))
            # پاسخ آرایه‌ای: [{"symbol":"USDT","price":"95000"}, ...]
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("symbol", "").upper() == "USDT":
                        p = item.get("price") or item.get("sell_price") or item.get("buy_price")
                        if p:
                            return int(float(str(p).replace(",", "")))
        except Exception as e:
            logger.warning(f"Tetherland parse error: {e}")

    # ۲. Nobitex (ایران) — USDT-RLS تقسیم بر ۱۰
    data2 = _fetch_url("https://api.nobitex.ir/market/stats?srcCurrency=usdt&dstCurrency=rls")
    if data2 and data2.get("status") == "ok":
        try:
            last = data2["stats"]["usdt-rls"].get("last")
            if last:
                return int(float(last) / 10)          # ریال ← تومان
        except Exception as e:
            logger.warning(f"Nobitex parse error: {e}")

    return USDT_TOMAN_FB

# ── قیمت BTC / ETH به دلار ────────────────────────────────────────────────────
def get_crypto_usd(coin: str) -> float | None:
    """
    قیمت BTC یا ETH به دلار.
    اولویت: ۱. CoinGecko  ۲. Binance  ۳. None
    """
    now    = time.time()
    cached = _price_cache.get(coin)
    if cached and now - cached[1] < CACHE_TTL:
        return cached[0]

    # ۱. CoinGecko (بدون API key)
    cg_id = COINGECKO_IDS.get(coin)
    if cg_id:
        data = _fetch_url(
            f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        )
        if data and cg_id in data:
            price = float(data[cg_id]["usd"])
            _price_cache[coin] = (price, now)
            return price

    # ۲. Binance (بدون API key)
    sym_map = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
    sym     = sym_map.get(coin)
    if sym:
        data = _fetch_url(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}")
        if data and "price" in data:
            price = float(data["price"])
            _price_cache[coin] = (price, now)
            return price

    return None

# ── بلوک قیمت برای پیام کریپتو ───────────────────────────────────────────────
def build_crypto_price_line(coin: str, price_toman: int | None) -> str:
    """بلوک اطلاعات نرخ برای نمایش در پیام پرداخت کریپتو."""
    label_map  = {"btc": "Bitcoin (BTC)", "eth": "Ethereum (ETH)", "usdt": "Tether USDT"}
    coin_label = label_map.get(coin, coin.upper())
    lines      = []

    usdt_toman = get_usdt_toman()    # نرخ تتر به تومان (برای همه ارزها)

    if coin == "usdt":
        lines.append(f"💱 *نرخ USDT/تومان (تترلند):* `{usdt_toman:,} تومان`")
        if price_toman:
            amount_usdt = price_toman / usdt_toman
            lines.append(f"💰 *مبلغ سفارش:* `{price_toman:,} تومان`")
            lines.append(f"🔄 *معادل پرداخت:* `{amount_usdt:.2f} USDT`")
    else:
        usd_price = get_crypto_usd(coin)
        if usd_price:
            lines.append(f"📈 *نرخ {coin_label}:* `${usd_price:,.2f}`")
            lines.append(f"💱 *نرخ تتر/تومان:* `{usdt_toman:,} تومان`")
            if price_toman:
                price_usd   = price_toman / usdt_toman
                amount_coin = price_usd   / usd_price
                lines.append(f"💰 *مبلغ سفارش:* `{price_toman:,} تومان` ≈ `${price_usd:,.2f}`")
                if coin == "btc":
                    lines.append(f"🔄 *معادل پرداخت:* `{amount_coin:.6f} BTC`")
                elif coin == "eth":
                    lines.append(f"🔄 *معادل پرداخت:* `{amount_coin:.5f} ETH`")
        else:
            lines.append("⚠️ _قیمت لحظه‌ای در دسترس نیست — از CoinGecko یا Binance بررسی کنید._")

    lines.append(f"🕐 _به‌روزرسانی هر {CACHE_TTL} ثانیه_")
    return "\n".join(lines)


# ─── QR Code generator ─────────────────────────────────────────────────────────
def make_qr_bytes(text: str) -> bytes | None:
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#DA291C", back_color="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

def qr_image_for(coin: str) -> bytes | None:
    addr_map = {"btc": WALLET_BTC, "eth": WALLET_ETH, "usdt": WALLET_USDT}
    file_map = {
        "btc":  os.path.join(BASE_DIR, "btc_qr.png"),
        "eth":  os.path.join(BASE_DIR, "eth_qr.png"),
        "usdt": os.path.join(BASE_DIR, "usdt_trc20_qr.png"),
    }
    fp = file_map.get(coin, "")
    if os.path.exists(fp):
        with open(fp, "rb") as f:
            return f.read()
    return make_qr_bytes(addr_map.get(coin, ""))

# ─── Validation ────────────────────────────────────────────────────────────────
def valid_email(s: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", s.strip()))

def valid_phone(s: str) -> bool:
    c = re.sub(r"[\s\-]", "", s.strip())
    return bool(re.fullmatch(r"(\+98|0098|0)?9[0-9]{9}", c))

def valid_fullname(s: str) -> bool:
    s = s.strip()
    return 4 <= len(s) <= 60 and len(s.split()) >= 2

# ─── Database ──────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            telegram_username TEXT,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            is_verified INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            price_toman TEXT,
            description TEXT,
            category TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            quantity INTEGER DEFAULT 1,
            amount_toman TEXT,
            payment_method TEXT,
            tx_hash_or_fish TEXT,
            discount_code TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS discount_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            discount_percent INTEGER DEFAULT 5,
            is_used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referee_id INTEGER NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    c.commit()
    # ─── Migration: add new columns to existing DBs (safe, ignores duplicates) ─
    for sql in [
        "ALTER TABLE users   ADD COLUMN referred_by   INTEGER DEFAULT NULL",
        "ALTER TABLE orders  ADD COLUMN quantity      INTEGER DEFAULT 1",
        "ALTER TABLE orders  ADD COLUMN discount_code TEXT    DEFAULT ''",
    ]:
        try:
            c.execute(sql); c.commit()
        except Exception:
            pass
    c.close()

def get_user(tid: int):
    c = db()
    u = c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    c.close(); return u

def is_verified(tid: int) -> bool:
    u = get_user(tid)
    return bool(u and u["is_verified"] == 1)

def _gen_code() -> str:
    """تولید کد تخفیف یکتا مانند: AKV-AB1234"""
    chars = string.ascii_uppercase + string.digits
    c = db()
    while True:
        code = "AKV-" + "".join(random.choices(chars, k=6))
        if not c.execute("SELECT 1 FROM discount_codes WHERE code=?", (code,)).fetchone():
            break
    c.close()
    return code

async def _bot_username(bot) -> str:
    """دریافت و کش نام کاربری ربات برای لینک رفرال."""
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        me = await bot.get_me()
        _BOT_USERNAME = me.username
    return _BOT_USERNAME

# ─── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_kbd(uid: int):
    logged = is_verified(uid)
    is_adm = uid == ADMIN_CHAT_ID
    rows = [
        [InlineKeyboardButton("📂 خدمات بر اساس دسته‌بندی", callback_data="menu_categories")],
        [InlineKeyboardButton("🛍️ همه خدمات",    callback_data="menu_products"),
         InlineKeyboardButton("📋 تمام تعرفه‌ها", callback_data="menu_all_prices")],
    ]
    if logged:
        rows.append([
            InlineKeyboardButton("👤 حساب کاربری", callback_data="user_profile"),
            InlineKeyboardButton("📦 سفارشات من",  callback_data="menu_history"),
        ])
        rows.append([
            InlineKeyboardButton("🔗 لینک دعوت من", callback_data="my_referral"),
            InlineKeyboardButton("🚪 خروج",          callback_data="logout_ask"),
        ])
    else:
        rows.append([InlineKeyboardButton("🔐 ورود / ثبت‌نام", callback_data="menu_auth")])
    rows.append([
        InlineKeyboardButton("🌐 وبسایت ما",  url=WEBSITE_URL),
        InlineKeyboardButton("🆘 پشتیبانی",  url="https://t.me/akavafa"),
    ])
    if is_adm:
        rows.append([InlineKeyboardButton("⚙️ پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)

def back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")]])

def confirm_reg(step: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید",  callback_data=f"conf_{step}"),
         InlineKeyboardButton("✏️ اصلاح", callback_data=f"edit_{step}")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")],
    ])

def after_order_kbd(oid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 مشاهده سفارش",        callback_data=f"view_order_{oid}")],
        [InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="menu_main")],
    ])

def qty_kbd(pid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣  یک",    callback_data=f"qty_{pid}_1"),
         InlineKeyboardButton("2️⃣  دو",    callback_data=f"qty_{pid}_2"),
         InlineKeyboardButton("3️⃣  سه",    callback_data=f"qty_{pid}_3")],
        [InlineKeyboardButton("5️⃣  پنج",  callback_data=f"qty_{pid}_5"),
         InlineKeyboardButton("🔟  ده",    callback_data=f"qty_{pid}_10"),
         InlineKeyboardButton("🔢  سایر…", callback_data=f"qty_custom_{pid}")],
        [InlineKeyboardButton("◀️ بازگشت", callback_data=f"product_{pid}")],
    ])

def discount_kbd(pid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 دارم، وارد کنم",  callback_data=f"disc_have_{pid}"),
         InlineKeyboardButton("⏩ ندارم، ادامه",    callback_data=f"disc_none_{pid}")],
        [InlineKeyboardButton("◀️ بازگشت",          callback_data=f"order_{pid}")],
    ])

def payment_kbd(pid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 کارت / شبا", callback_data=f"pay_card_{pid}")],
        [InlineKeyboardButton("💎 کریپتو",     callback_data=f"pay_crypto_{pid}")],
        [InlineKeyboardButton("◀️ بازگشت",     callback_data=f"product_{pid}")],
    ])

# ─── Error handler ─────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    if isinstance(ctx.error, BadRequest) and "not modified" in str(ctx.error).lower():
        return
    logger.error("Update error:", exc_info=ctx.error)

# ─── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = db()
    if not c.execute("SELECT 1 FROM users WHERE telegram_id=?", (u.id,)).fetchone():
        c.execute("INSERT INTO users (telegram_id, telegram_username) VALUES (?,?)",
                  (u.id, u.username or ""))
        c.commit()
    # ── Handle referral deep-link: /start ref_USERID ──────────────────────────
    if ctx.args and ctx.args[0].startswith("ref_"):
        try:
            ref_id = int(ctx.args[0][4:])
        except ValueError:
            ref_id = None
        if ref_id and ref_id != u.id:
            row = c.execute(
                "SELECT referred_by, is_verified FROM users WHERE telegram_id=?", (u.id,)
            ).fetchone()
            if row and row["referred_by"] is None and not row["is_verified"]:
                c.execute("UPDATE users SET referred_by=? WHERE telegram_id=?", (ref_id, u.id))
                c.commit()
    c.close()
    await update.message.reply_text(
        "🌹 *به ربات Akavafa Design خوش آمدید* 🌹\n\nلطفاً از منوی زیر انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kbd(u.id),
    )

# ─── Button handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = update.effective_user.id

    # ══════════════════════════════════════════════════════
    #  MAIN MENU
    # ══════════════════════════════════════════════════════
    if data == "menu_main":
        ctx.user_data.clear()
        await q.edit_message_text("🏠 *منوی اصلی*", parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=main_menu_kbd(uid))

    # ══════════════════════════════════════════════════════
    #  WEBSITE  (URL button — no callback needed)
    # ══════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════
    #  MY REFERRAL LINK (دکمه سریع لینک دعوت)
    # ══════════════════════════════════════════════════════
    elif data == "my_referral":
        if not is_verified(uid):
            await q.edit_message_text("🔐 ابتدا ثبت‌نام کنید.", reply_markup=main_menu_kbd(uid)); return
        bot_user = await _bot_username(ctx.bot)
        ref_link = f"https://t.me/{bot_user}?start=ref_{uid}"
        c    = db()
        codes= c.execute(
            "SELECT code, discount_percent FROM discount_codes WHERE owner_id=? AND is_used=0", (uid,)
        ).fetchall()
        refs = c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        c.close()
        codes_txt = "\n".join(f"  🎫 `{dc['code']}` — {dc['discount_percent']}٪" for dc in codes) \
                    if codes else "  _ندارید_"
        await q.edit_message_text(
            f"🔗 *لینک دعوت شما:*\n`{ref_link}`\n\n"
            f"👥 دعوت‌شدگان موفق: *{refs} نفر*\n\n"
            f"🎫 *کدهای تخفیف فعال:*\n{codes_txt}\n\n"
            f"_این لینک را برای دوستانتان ارسال کنید — وقتی ثبت‌نام کردند، یک کد تخفیف {REFERRAL_DISCOUNT}٪ دریافت می‌کنید._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")]
            ])
        )
        if not is_verified(uid):
            await q.edit_message_text(
                "🔐 ابتدا ثبت‌نام کنید.", reply_markup=main_menu_kbd(uid)); return
        c = db()
        u_row   = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        o_count = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
        my_codes= c.execute(
            "SELECT code, discount_percent FROM discount_codes WHERE owner_id=? AND is_used=0",
            (uid,)).fetchall()
        ref_cnt = c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        c.close()
        bot_user = await _bot_username(ctx.bot)
        ref_link = f"https://t.me/{bot_user}?start=ref_{uid}"
        if my_codes:
            codes_txt = "\n".join(f"  🎫 `{dc['code']}` — {dc['discount_percent']}٪" for dc in my_codes)
        else:
            codes_txt = "  _ندارید_ — با دعوت از دوستان کد تخفیف بگیرید!"
        await q.edit_message_text(
            f"👤 *حساب کاربری*\n\n"
            f"📛 نام: *{u_row['full_name'] or '—'}*\n"
            f"📧 ایمیل: `{u_row['email'] or '—'}`\n"
            f"📱 شماره: `{u_row['phone'] or '—'}`\n"
            f"📅 عضویت: {fmt_tehran(str(u_row['created_at']))}\n"
            f"📦 سفارشات: {o_count} عدد\n"
            f"👥 دوستان دعوت‌شده: {ref_cnt} نفر\n\n"
            f"🔗 *لینک دعوت شما:*\n`{ref_link}`\n\n"
            f"🎫 *کدهای تخفیف فعال:*\n{codes_txt}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")]
            ])
        )

    # ══════════════════════════════════════════════════════
    #  LOGOUT
    # ══════════════════════════════════════════════════════
    elif data == "logout_ask":
        await q.edit_message_text(
            "🚪 *خروج از حساب کاربری*\n\nآیا مطمئن هستید؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، خروج", callback_data="logout_confirm"),
                 InlineKeyboardButton("❌ انصراف",    callback_data="menu_main")],
            ]))

    elif data == "logout_confirm":
        c = db()
        c.execute(
            "UPDATE users SET is_verified=0, email=NULL, phone=NULL, full_name=NULL WHERE telegram_id=?",
            (uid,))
        c.commit(); c.close()
        ctx.user_data.clear()
        await q.edit_message_text("✅ *از حساب خارج شدید.*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=main_menu_kbd(uid))

    # ══════════════════════════════════════════════════════
    #  REGISTRATION
    # ══════════════════════════════════════════════════════
    elif data == "menu_auth":
        await q.edit_message_text(
            "🔐 *مرحله ۱ از ۳ — ایمیل*\n\nلطفاً آدرس ایمیل خود را ارسال کنید:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())
        ctx.user_data["auth_step"] = "wait_email"

    elif data == "conf_email":
        await q.edit_message_text(
            "🔐 *مرحله ۲ از ۳ — شماره تماس*\n\nلطفاً شماره موبایل خود را ارسال کنید:\nمثال: `09123456789`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())
        ctx.user_data["auth_step"] = "wait_phone"

    elif data == "conf_phone":
        await q.edit_message_text(
            "🔐 *مرحله ۳ از ۳ — نام کامل*\n\nلطفاً نام و نام خانوادگی خود را ارسال کنید:\nمثال: `علی محمدی`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())
        ctx.user_data["auth_step"] = "wait_name"

    elif data == "conf_name":
        email = ctx.user_data.get("reg_email", "")
        phone = ctx.user_data.get("reg_phone", "")
        name  = ctx.user_data.get("reg_name",  "")
        c = db()
        c.execute(
            "UPDATE users SET email=?, phone=?, full_name=?, is_verified=1 WHERE telegram_id=?",
            (email, phone, name, uid))
        c.commit()
        # ── پردازش رفرال ────────────────────────────────────────────────────────
        row = c.execute("SELECT referred_by FROM users WHERE telegram_id=?", (uid,)).fetchone()
        referrer_id = row["referred_by"] if row else None
        if referrer_id:
            already = c.execute("SELECT 1 FROM referrals WHERE referee_id=?", (uid,)).fetchone()
            if not already:
                code = _gen_code()
                c.execute("INSERT INTO referrals (referrer_id, referee_id) VALUES (?,?)",
                          (referrer_id, uid))
                c.execute(
                    "INSERT INTO discount_codes (code, owner_id, discount_percent) VALUES (?,?,?)",
                    (code, referrer_id, REFERRAL_DISCOUNT))
                c.commit()
                ref_name = c.execute(
                    "SELECT full_name FROM users WHERE telegram_id=?", (uid,)
                ).fetchone()
                new_name = ref_name["full_name"] if ref_name else "یک کاربر جدید"
                try:
                    await ctx.bot.send_message(
                        referrer_id,
                        f"🎉 *دوستتان ثبت‌نام کرد!*\n\n"
                        f"👤 {new_name} از طریق لینک دعوت شما عضو شد.\n\n"
                        f"🎫 *کد تخفیف {REFERRAL_DISCOUNT}٪ برای سفارش بعدیتان:*\n"
                        f"`{code}`\n\n"
                        f"این کد یک‌بار مصرف است — قبل از پرداخت وارد کنید.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.warning(f"referral notify: {e}")
        c.close()
        pid = ctx.user_data.get("redirect_pid")
        ctx.user_data.clear()
        if pid:
            await q.edit_message_text(
                f"✅ *ثبت‌نام موفق، {name}!*\n\nاکنون تعداد سفارش را انتخاب کنید:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=qty_kbd(pid))
        else:
            await q.edit_message_text(
                f"✅ *ثبت‌نام موفق!*\n\nخوش آمدید {name}.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kbd(uid))

    elif data.startswith("edit_"):
        step   = data.split("_", 1)[1]
        labels = {"email": "ایمیل", "phone": "شماره تماس", "name": "نام کامل"}
        await q.edit_message_text(
            f"✏️ لطفاً *{labels.get(step, step)}* را مجدداً ارسال کنید:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())
        ctx.user_data["auth_step"] = f"wait_{step}"

    # ══════════════════════════════════════════════════════
    #  ALL PRICES (تمام تعرفه‌ها یکجا)
    # ══════════════════════════════════════════════════════
    elif data == "menu_all_prices":
        c     = db()
        prods = c.execute(
            "SELECT * FROM products WHERE is_active=1 ORDER BY category, name"
        ).fetchall()
        c.close()
        if not prods:
            await q.edit_message_text("📋 هیچ خدمتی ثبت نشده.", reply_markup=back_home()); return
        # گروه‌بندی بر اساس دسته‌بندی
        cats: dict = {}
        for p in prods:
            key = p["category"] or "سایر"
            cats.setdefault(key, []).append(p)
        text = "📋 *تمام تعرفه‌های خدمات Akavafa*\n\n"
        for cat, ps in sorted(cats.items()):
            text += f"🏷️ *{cat}*\n"
            for p in ps:
                if str(p["price_toman"]).isdigit():
                    price = f"{int(p['price_toman']):,} تومان"
                else:
                    price = p["price_toman"]
                text += f"  • {p['name']} — {price}\n"
            text += "\n"
        if len(text) > 4000:
            text = text[:3980] + "\n_... برای مشاهده کامل با پشتیبانی تماس بگیرید_"
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("📂 مشاهده بر اساس دسته‌بندی",
                                                            callback_data="menu_categories")],
                                      [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")],
                                  ]))

    # ══════════════════════════════════════════════════════
    #  CATEGORIES (دسته‌بندی مشتری)
    # ══════════════════════════════════════════════════════
    elif data == "menu_categories":
        c    = db()
        cats = c.execute(
            "SELECT DISTINCT category FROM products WHERE is_active=1 AND category!='' ORDER BY category"
        ).fetchall()
        has_unc = c.execute(
            "SELECT 1 FROM products WHERE is_active=1 AND (category IS NULL OR category='')"
        ).fetchone()
        c.close()
        rows = []
        for cat in cats:
            nm = cat["category"]
            rows.append([InlineKeyboardButton(f"📂 {nm}", callback_data=f"cat_{nm[:25]}")])
        if has_unc:
            rows.append([InlineKeyboardButton("📦 سایر",        callback_data="cat_")])
        rows.append([InlineKeyboardButton("🛍️ همه محصولات",     callback_data="menu_products")])
        rows.append([InlineKeyboardButton("🏠 منوی اصلی",       callback_data="menu_main")])
        await q.edit_message_text("📂 *دسته‌بندی خدمات*\n\nانتخاب کنید:",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("cat_") and not data.startswith("admin_cat"):
        cat_name = data[4:]        # هر چیزی بعد از "cat_"
        c = db()
        if cat_name:
            prods = c.execute(
                "SELECT * FROM products WHERE is_active=1 AND category=?", (cat_name,)
            ).fetchall()
        else:
            prods = c.execute(
                "SELECT * FROM products WHERE is_active=1 AND (category IS NULL OR category='')"
            ).fetchall()
        c.close()
        cat_display = cat_name or "سایر"
        if not prods:
            await q.edit_message_text(f"📂 *{cat_display}*\n\nهیچ محصولی یافت نشد.",
                                      parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup([
                                          [InlineKeyboardButton("◀️ بازگشت", callback_data="menu_categories")]
                                      ])); return
        rows = [[InlineKeyboardButton(
            f"🔹 {p['name']} — {p['price_toman']}",
            callback_data=f"product_{p['id']}")] for p in prods]
        rows.append([InlineKeyboardButton("◀️ بازگشت به دسته‌بندی‌ها", callback_data="menu_categories")])
        await q.edit_message_text(f"📂 *{cat_display}*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    # ══════════════════════════════════════════════════════
    #  PRODUCTS
    # ══════════════════════════════════════════════════════
    elif data == "menu_products":
        c     = db()
        prods = c.execute("SELECT * FROM products WHERE is_active=1 ORDER BY category, name").fetchall()
        c.close()
        rows = [[InlineKeyboardButton(f"🔹 {p['name']} — {p['price_toman']}",
                                      callback_data=f"product_{p['id']}")] for p in prods]
        rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")])
        await q.edit_message_text("🛍️ *همه خدمات و تعرفه‌ها*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("product_"):
        pid = int(data.split("_")[1])
        c   = db()
        p   = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        if not p:
            await q.edit_message_text("❌ محصول یافت نشد.", reply_markup=back_home()); return
        price = (f"{int(p['price_toman']):,} تومان"
                 if str(p["price_toman"]).isdigit() else p["price_toman"])
        cat_txt = f"\n📂 دسته‌بندی: {p['category']}" if p["category"] else ""
        msg = f"📦 *{p['name']}*\n💰 *قیمت واحد:* {price}{cat_txt}"
        if p["description"]:
            msg += f"\n\n📝 {p['description']}"
        rows = []
        if str(p["price_toman"]).isdigit():
            rows.append([InlineKeyboardButton("🛒 ثبت سفارش", callback_data=f"order_{pid}")])
        else:
            rows.append([InlineKeyboardButton("🆘 تماس با پشتیبانی", url="https://t.me/akavafa")])
        back_cb = f"cat_{p['category'][:25]}" if p["category"] else "menu_categories"
        rows.append([InlineKeyboardButton("◀️ بازگشت", callback_data=back_cb)])
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    # ══════════════════════════════════════════════════════
    #  ORDER → QUANTITY → DISCOUNT → PAYMENT
    # ══════════════════════════════════════════════════════
    elif data.startswith("order_"):
        pid = int(data.split("_")[1])
        if not is_verified(uid):
            ctx.user_data["redirect_pid"] = pid
            await q.edit_message_text(
                "⚠️ *برای ثبت سفارش باید ابتدا ثبت‌نام کنید.*\n\nلطفاً ایمیل خود را ارسال کنید:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())
            ctx.user_data["auth_step"] = "wait_email"
            return
        c = db()
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        price = (f"{int(p['price_toman']):,} تومان"
                 if str(p["price_toman"]).isdigit() else p["price_toman"])
        ctx.user_data["order_pid"] = pid
        await q.edit_message_text(
            f"🔢 *انتخاب تعداد*\n\n📦 محصول: *{p['name']}*\n💰 قیمت واحد: *{price}*\n\nچند عدد می‌خواهید؟",
            parse_mode=ParseMode.MARKDOWN, reply_markup=qty_kbd(pid))

    # ── Quantity buttons ───────────────────────────────────────────────────────
    elif data.startswith("qty_custom_"):
        pid = int(data.split("_")[2])
        ctx.user_data["order_pid"]  = pid
        ctx.user_data["auth_step"]  = "wait_qty"
        await q.edit_message_text(
            "🔢 *تعداد دلخواه را وارد کنید:*\n(عدد صحیح، حداکثر ۱۰۰)",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    elif data.startswith("qty_"):
        parts = data.split("_")
        pid   = int(parts[1])
        qty   = int(parts[2])
        ctx.user_data["order_pid"] = pid
        ctx.user_data["order_qty"] = qty
        c = db()
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        if str(p["price_toman"]).isdigit():
            total = int(p["price_toman"]) * qty
            total_str = f"{total:,} تومان"
        else:
            total_str = p["price_toman"]
        await q.edit_message_text(
            f"🎫 *کد تخفیف*\n\n📦 {p['name']} × {qty}\n💰 مبلغ کل: *{total_str}*\n\nآیا کد تخفیف دارید؟",
            parse_mode=ParseMode.MARKDOWN, reply_markup=discount_kbd(pid))

    # ── Discount code prompt ───────────────────────────────────────────────────
    elif data.startswith("disc_have_"):
        pid = int(data.split("_")[2])
        ctx.user_data["order_pid"] = pid
        ctx.user_data["auth_step"] = "wait_discount"
        await q.edit_message_text(
            "🎫 *کد تخفیف خود را وارد کنید:*\nمثال: `AKV-AB1234`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    elif data.startswith("disc_none_"):
        pid = int(data.split("_")[2])
        qty = ctx.user_data.get("order_qty", 1)
        c   = db()
        p   = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        if str(p["price_toman"]).isdigit():
            total = int(p["price_toman"]) * qty
            amount_str   = str(total)
            price_display= f"{total:,} تومان"
        else:
            amount_str   = p["price_toman"]
            price_display= p["price_toman"]
        ctx.user_data["order_draft"] = {
            "pid": pid, "qty": qty, "final_amount": amount_str, "discount_code": ""}
        await q.edit_message_text(
            f"💳 *انتخاب روش پرداخت*\n\n📦 {p['name']} × {qty}\n💰 مبلغ نهایی: *{price_display}*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=payment_kbd(pid))

    # ── Payment: card ──────────────────────────────────────────────────────────
    elif data.startswith("pay_card_"):
        pid   = int(data.split("_")[2])
        draft = ctx.user_data.get("order_draft", {})
        if draft:
            amount = draft["final_amount"]
            qty    = draft.get("qty", 1)
            disc   = draft.get("discount_code", "")
        else:
            c = db(); p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone(); c.close()
            amount = p["price_toman"]; qty = 1; disc = ""
        price_str = (f"{int(amount):,} تومان" if str(amount).isdigit() else str(amount))
        ctx.user_data["pending_order"] = {
            "pid": pid, "method": "card", "amount": amount, "qty": qty, "discount_code": disc}
        await q.edit_message_text(
            f"💳 *پرداخت بانکی*\n\n"
            f"💰 مبلغ نهایی: *{price_str}*\n\n"
            f"🔢 شماره کارت:\n`{CARD_NUMBER}`\n\n"
            f"🏦 شماره شبا:\n`{SHEBA_NUMBER}`\n\n"
            f"📸 *پس از واریز، تصویر فیش را ارسال کنید.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ انصراف", callback_data=f"order_{pid}")]]))

    # ── Payment: crypto coin selection ─────────────────────────────────────────
    elif data.startswith("pay_crypto_"):
        pid = int(data.split("_")[2])
        await q.edit_message_text("💎 *انتخاب ارز دیجیتال*", parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("₿  BTC",          callback_data=f"crypto_btc_{pid}")],
                                      [InlineKeyboardButton("Ξ  ETH",          callback_data=f"crypto_eth_{pid}")],
                                      [InlineKeyboardButton("₮  USDT (TRC20)", callback_data=f"crypto_usdt_{pid}")],
                                      [InlineKeyboardButton("◀️ بازگشت",        callback_data=f"order_{pid}")],
                                  ]))

    # ── Payment: crypto QR ─────────────────────────────────────────────────────
    elif data.startswith("crypto_"):
        parts = data.split("_")
        coin  = parts[1]
        pid   = int(parts[2])
        addr_map  = {"btc": WALLET_BTC, "eth": WALLET_ETH, "usdt": WALLET_USDT}
        label_map = {"btc": "Bitcoin (BTC)", "eth": "Ethereum (ETH)", "usdt": "Tether USDT (TRC20)"}
        addr      = addr_map[coin]
        draft     = ctx.user_data.get("order_draft", {})
        qty       = draft.get("qty", 1)
        disc      = draft.get("discount_code", "")
        c = db()
        p = c.execute("SELECT price_toman FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        price_toman_int = None
        if draft and str(draft.get("final_amount","")).isdigit():
            price_toman_int = int(draft["final_amount"])
        elif p and str(p["price_toman"]).isdigit():
            price_toman_int = int(p["price_toman"]) * qty
        ctx.user_data["pending_order"] = {
            "pid": pid, "method": coin,
            "amount": str(price_toman_int) if price_toman_int else "crypto",
            "qty": qty, "discount_code": disc}
        price_info = build_crypto_price_line(coin, price_toman_int)
        caption = (
            f"💎 *{label_map[coin]}*\n\n"
            f"{price_info}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *آدرس کیف پول:*\n`{addr}`\n\n"
            f"✅ پس از تراکنش، *TxHash* را ارسال کنید.\n"
            f"⚠️ فقط {coin.upper()} به این آدرس ارسال کنید.\n"
            f"🕐 _قیمت هر ۲ دقیقه به‌روز می‌شود_"
        )
        try:
            await q.message.delete()
        except Exception:
            pass
        qr_bytes = qr_image_for(coin)
        if qr_bytes:
            await ctx.bot.send_photo(chat_id=uid, photo=io.BytesIO(qr_bytes),
                                     caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await ctx.bot.send_message(chat_id=uid, text=caption, parse_mode=ParseMode.MARKDOWN)

    # ══════════════════════════════════════════════════════
    #  ORDER HISTORY (user)
    # ══════════════════════════════════════════════════════
    elif data.startswith("view_order_"):
        oid = int(data.split("_")[2])
        c   = db()
        o   = c.execute(
            "SELECT o.*, p.name AS pname FROM orders o "
            "JOIN products p ON o.product_id=p.id WHERE o.id=? AND o.user_id=?",
            (oid, uid)).fetchone()
        c.close()
        if not o:
            await q.edit_message_text("❌ سفارش یافت نشد.", reply_markup=back_home()); return
        smap = {"pending": "⏳ در انتظار بررسی", "approved": "✅ تایید شده", "rejected": "❌ رد شده"}
        disc_txt = f"\n🎫 کد تخفیف: `{o['discount_code']}`" if o["discount_code"] else ""
        await q.edit_message_text(
            f"📦 *سفارش #{o['id']}*\n\n"
            f"🛍️ محصول: *{o['pname']}*\n"
            f"🔢 تعداد: *{o['quantity']}*\n"
            f"💰 مبلغ کل: *{o['amount_toman']}*\n"
            f"💳 روش: *{o['payment_method'].upper()}*{disc_txt}\n"
            f"📅 تاریخ: *{fmt_tehran(str(o['created_at']))}*\n"
            f"📊 وضعیت: *{smap.get(o['status'], o['status'])}*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    elif data == "menu_history":
        if not is_verified(uid):
            await q.edit_message_text(
                "🔐 *برای مشاهده سفارشات باید ابتدا وارد شوید.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 ورود / ثبت‌نام", callback_data="menu_auth")],
                    [InlineKeyboardButton("🏠 منوی اصلی",       callback_data="menu_main")],
                ])); return
        c      = db()
        orders = c.execute(
            "SELECT o.*, p.name AS pname FROM orders o "
            "JOIN products p ON o.product_id=p.id WHERE o.user_id=? ORDER BY o.created_at DESC",
            (uid,)).fetchall()
        c.close()
        if not orders:
            await q.edit_message_text("📋 *تاریخچه سفارشات*\n\nهنوز سفارشی ثبت نکرده‌اید.",
                                      parse_mode=ParseMode.MARKDOWN, reply_markup=back_home()); return
        smap = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
        rows = [[InlineKeyboardButton(
            f"{smap.get(o['status'],'❓')} #{o['id']} — {o['pname']} ×{o['quantity']}",
            callback_data=f"view_order_{o['id']}")] for o in orders]
        rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_main")])
        await q.edit_message_text("📋 *تاریخچه سفارشات:*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    # ══════════════════════════════════════════════════════
    #  ADMIN PANEL
    # ══════════════════════════════════════════════════════
    elif data == "admin_panel":
        if uid != ADMIN_CHAT_ID: return
        await q.edit_message_text("⚙️ *پنل مدیریت*", parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("➕ افزودن محصول",      callback_data="admin_add_p")],
                                      [InlineKeyboardButton("✏️ ویرایش قیمت",       callback_data="admin_edit_p"),
                                       InlineKeyboardButton("📂 ویرایش دسته‌بندی",  callback_data="admin_edit_cat")],
                                      [InlineKeyboardButton("🗑️ حذف دسته‌بندی",     callback_data="admin_del_cat"),
                                       InlineKeyboardButton("🗑️ حذف محصول",         callback_data="admin_del_p")],
                                      [InlineKeyboardButton("👥 لیست مشتریان",      callback_data="admin_users")],
                                      [InlineKeyboardButton("📦 لیست سفارشات",      callback_data="admin_orders")],
                                      [InlineKeyboardButton("🎫 مدیریت کدهای تخفیف", callback_data="admin_codes")],
                                      [InlineKeyboardButton("🏠 منوی اصلی",         callback_data="menu_main")],
                                  ]))

    # ── Admin: کدهای تخفیف — مدیریت کامل ────────────────────────────────────────
    elif data == "admin_codes":
        if uid != ADMIN_CHAT_ID: return
        c    = db()
        codes= c.execute(
            "SELECT dc.*, COALESCE(u.full_name, u.telegram_username, '—') AS uname "
            "FROM discount_codes dc LEFT JOIN users u ON dc.owner_id=u.telegram_id "
            "ORDER BY dc.is_used ASC, dc.id DESC LIMIT 50"
        ).fetchall()
        total = c.execute("SELECT COUNT(*) FROM discount_codes").fetchone()[0]
        used  = c.execute("SELECT COUNT(*) FROM discount_codes WHERE is_used=1").fetchone()[0]
        c.close()
        rows = []
        for dc in codes:
            mark = "✅" if not dc["is_used"] else "🔴"
            rows.append([InlineKeyboardButton(
                f"{mark} {dc['code']} ({dc['discount_percent']}٪) | {dc['uname'][:10]}",
                callback_data=f"adm_code_{dc['id']}")])
        rows.append([InlineKeyboardButton("➕ افزودن کد تخفیف", callback_data="adm_code_new")])
        rows.append([InlineKeyboardButton("◀️ بازگشت",           callback_data="admin_panel")])
        await q.edit_message_text(
            f"🎫 *مدیریت کدهای تخفیف*\n\n"
            f"📊 کل: {total}  |  ✅ فعال: {total-used}  |  🔴 استفاده‌شده: {used}\n\n"
            f"روی هر کد کلیک کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("adm_code_") and not data.startswith("adm_code_new") \
            and not data.startswith("adm_code_del") and not data.startswith("adm_code_edit") \
            and not data.startswith("adm_code_reset"):
        if uid != ADMIN_CHAT_ID: return
        cid = int(data.split("_")[2])
        c   = db()
        dc  = c.execute(
            "SELECT dc.*, COALESCE(u.full_name, u.telegram_username, u.phone, '—') AS uname "
            "FROM discount_codes dc LEFT JOIN users u ON dc.owner_id=u.telegram_id WHERE dc.id=?",
            (cid,)).fetchone()
        c.close()
        if not dc:
            await q.edit_message_text("❌ کد یافت نشد.", reply_markup=back_home()); return
        status = "✅ فعال" if not dc["is_used"] else "🔴 استفاده‌شده"
        await q.edit_message_text(
            f"🎫 *جزئیات کد تخفیف*\n\n"
            f"🔑 کد: `{dc['code']}`\n"
            f"📊 تخفیف: *{dc['discount_percent']}٪*\n"
            f"👤 مالک: {dc['uname']}\n"
            f"📌 وضعیت: {status}\n"
            f"📅 ایجاد: {fmt_tehran(str(dc['created_at']))}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ ویرایش درصد",    callback_data=f"adm_code_edit_{cid}"),
                 InlineKeyboardButton("🔁 بازنشانی وضعیت", callback_data=f"adm_code_reset_{cid}")],
                [InlineKeyboardButton("🗑️ حذف کد",         callback_data=f"adm_code_del_{cid}")],
                [InlineKeyboardButton("◀️ بازگشت",         callback_data="admin_codes")],
            ]))

    elif data == "adm_code_new":
        if uid != ADMIN_CHAT_ID: return
        ctx.user_data["admin_action"] = "code_new_uid"
        await q.edit_message_text(
            "🎫 *افزودن کد تخفیف دستی*\n\n"
            "Telegram ID کاربر مقصد را وارد کنید\n(یا `0` برای کد عمومی بدون صاحب مشخص):",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    elif data.startswith("adm_code_edit_"):
        if uid != ADMIN_CHAT_ID: return
        cid = int(data.split("_")[3])
        ctx.user_data["admin_action"] = "code_edit_pct"
        ctx.user_data["edit_code_id"] = cid
        await q.edit_message_text(
            "✏️ *درصد تخفیف جدید را وارد کنید* (عدد ۱ تا ۱۰۰):",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    elif data.startswith("adm_code_reset_"):
        if uid != ADMIN_CHAT_ID: return
        cid = int(data.split("_")[3])
        c   = db()
        dc  = c.execute("SELECT is_used FROM discount_codes WHERE id=?", (cid,)).fetchone()
        new_status = 0 if dc and dc["is_used"] else 1
        c.execute("UPDATE discount_codes SET is_used=? WHERE id=?", (new_status, cid))
        c.commit(); c.close()
        label = "فعال" if new_status == 0 else "استفاده‌شده"
        await q.edit_message_text(f"✅ وضعیت کد به *{label}* تغییر یافت.",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("◀️ بازگشت", callback_data="admin_codes")]
                                  ]))

    elif data.startswith("adm_code_del_"):
        if uid != ADMIN_CHAT_ID: return
        cid = int(data.split("_")[3])
        c   = db()
        c.execute("DELETE FROM discount_codes WHERE id=?", (cid,))
        c.commit(); c.close()
        await q.edit_message_text("🗑️ کد تخفیف حذف شد.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("◀️ بازگشت", callback_data="admin_codes")]
                                  ]))

    # ── Admin: customers ───────────────────────────────────────────────────────
    elif data == "admin_users":
        if uid != ADMIN_CHAT_ID: return
        c     = db()
        users = c.execute(
            "SELECT telegram_id, COALESCE(full_name,'—') as full_name, "
            "COALESCE(email,'—') as email, COALESCE(phone,'—') as phone, "
            "COALESCE(telegram_username,'—') as telegram_username "
            "FROM users WHERE is_verified=1 ORDER BY id DESC"
        ).fetchall()
        c.close()
        if not users:
            await q.edit_message_text("👥 هیچ مشتری ثبت‌نامی وجود ندارد.",
                                      reply_markup=InlineKeyboardMarkup(
                                          [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]])); return
        rows = [[InlineKeyboardButton(
            f"👤 {u['full_name']} | @{u['telegram_username']}",
            callback_data=f"admin_user_{u['telegram_id']}")] for u in users]
        rows.append([InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")])
        await q.edit_message_text(f"👥 *لیست مشتریان ({len(users)} نفر):*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("admin_user_"):
        if uid != ADMIN_CHAT_ID: return
        tid = int(data.split("_")[2])
        c   = db()
        u_  = c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
        oc  = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (tid,)).fetchone()[0]
        c.close()
        if not u_:
            await q.edit_message_text("❌ کاربر یافت نشد.", reply_markup=back_home()); return
        await q.edit_message_text(
            f"👤 *{u_['full_name'] or '—'}*\n🆔 @{u_['telegram_username'] or '—'}\n"
            f"📧 {u_['email'] or '—'}\n📱 {u_['phone'] or '—'}\n"
            f"📦 سفارشات: {oc}\n📅 عضویت: {fmt_tehran(str(u_['created_at']))}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ حذف مشتری", callback_data=f"admin_del_user_ask_{tid}")],
                [InlineKeyboardButton("◀️ بازگشت",    callback_data="admin_users")],
            ]))

    elif data.startswith("admin_del_user_ask_"):
        if uid != ADMIN_CHAT_ID: return
        tid = int(data.split("_")[4])
        await q.edit_message_text(
            "⚠️ *آیا از حذف این مشتری مطمئن هستید؟*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، حذف", callback_data=f"admin_del_user_ok_{tid}"),
                 InlineKeyboardButton("❌ انصراف",   callback_data=f"admin_user_{tid}")],
            ]))

    elif data.startswith("admin_del_user_ok_"):
        if uid != ADMIN_CHAT_ID: return
        tid = int(data.split("_")[4])
        c   = db()
        c.execute("DELETE FROM orders   WHERE user_id=?",     (tid,))
        c.execute("DELETE FROM referrals WHERE referrer_id=? OR referee_id=?", (tid, tid))
        c.execute("DELETE FROM users    WHERE telegram_id=?", (tid,))
        c.commit(); c.close()
        await q.edit_message_text("✅ مشتری حذف شد.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_users")]]))

    # ── Admin: orders ──────────────────────────────────────────────────────────
    elif data == "admin_orders":
        if uid != ADMIN_CHAT_ID: return
        await _show_admin_orders(q, "all")

    elif data.startswith("admin_orders_filter_"):
        if uid != ADMIN_CHAT_ID: return
        await _show_admin_orders(q, data.replace("admin_orders_filter_", ""))

    elif data.startswith("admin_order_"):
        if uid != ADMIN_CHAT_ID: return
        oid = int(data.split("_")[2])
        c   = db()
        o   = c.execute(
            "SELECT o.*, u.full_name, u.telegram_username, u.email, u.phone, p.name AS pname "
            "FROM orders o JOIN users u ON o.user_id=u.telegram_id "
            "JOIN products p ON o.product_id=p.id WHERE o.id=?", (oid,)).fetchone()
        c.close()
        if not o:
            await q.edit_message_text("❌ سفارش یافت نشد.", reply_markup=back_home()); return
        smap     = {"pending": "⏳ در انتظار", "approved": "✅ تایید", "rejected": "❌ رد شده"}
        disc_txt = f"\n🎫 کد تخفیف: `{o['discount_code']}`" if o["discount_code"] else ""
        await q.edit_message_text(
            f"📦 *سفارش #{o['id']}*\n\n"
            f"👤 *{o['full_name']}* | @{o['telegram_username']}\n"
            f"📧 {o['email']} | 📱 {o['phone']}\n\n"
            f"🛍️ {o['pname']} × {o['quantity']}\n"
            f"💰 {o['amount_toman']} | 💳 {o['payment_method'].upper()}{disc_txt}\n"
            f"📝 TxHash/فیش:\n`{o['tx_hash_or_fish']}`\n"
            f"📅 {fmt_tehran(str(o['created_at']))}\n"
            f"📊 {smap.get(o['status'], o['status'])}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تایید",           callback_data=f"approve_order_{oid}"),
                 InlineKeyboardButton("❌ رد",              callback_data=f"reject_order_{oid}")],
                [InlineKeyboardButton("🗑️ حذف سفارش",      callback_data=f"del_order_ask_{oid}")],
                [InlineKeyboardButton("◀️ بازگشت به لیست", callback_data="admin_orders")],
            ]))

    elif data.startswith("approve_order_"):
        if uid != ADMIN_CHAT_ID: return
        oid = int(data.split("_")[2])
        c   = db()
        c.execute("UPDATE orders SET status='approved' WHERE id=?", (oid,))
        o = c.execute("SELECT user_id FROM orders WHERE id=?", (oid,)).fetchone()
        c.commit(); c.close()
        await q.edit_message_text(f"✅ سفارش #{oid} تایید شد.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_orders")]]))
        try:
            await ctx.bot.send_message(o["user_id"],
                f"🎉 *سفارش #{oid} تایید شد!*\nتیم ما در حال پردازش سفارش شماست.",
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"notify user: {e}")

    elif data.startswith("reject_order_"):
        if uid != ADMIN_CHAT_ID: return
        oid = int(data.split("_")[2])
        c   = db()
        c.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        o = c.execute("SELECT user_id FROM orders WHERE id=?", (oid,)).fetchone()
        c.commit(); c.close()
        await q.edit_message_text(f"❌ سفارش #{oid} رد شد.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_orders")]]))
        try:
            await ctx.bot.send_message(o["user_id"],
                f"⚠️ *سفارش #{oid} تایید نشد.*\nبرای اطلاعات بیشتر: @akavafa",
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"notify user: {e}")

    elif data.startswith("del_order_ask_"):
        if uid != ADMIN_CHAT_ID: return
        oid = int(data.split("_")[3])
        await q.edit_message_text(
            f"⚠️ *آیا سفارش #{oid} حذف شود؟*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله", callback_data=f"del_order_ok_{oid}"),
                 InlineKeyboardButton("❌ انصراف", callback_data=f"admin_order_{oid}")],
            ]))

    elif data.startswith("del_order_ok_"):
        if uid != ADMIN_CHAT_ID: return
        oid = int(data.split("_")[3])
        c   = db()
        c.execute("DELETE FROM orders WHERE id=?", (oid,)); c.commit(); c.close()
        await q.edit_message_text(f"🗑️ سفارش #{oid} حذف شد.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_orders")]]))

    elif data == "admin_clear_orders_ask":
        if uid != ADMIN_CHAT_ID: return
        await q.edit_message_text(
            "⚠️ *پاک کردن تمام سفارشات؟* غیرقابل بازگشت!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، همه حذف", callback_data="admin_clear_orders_ok"),
                 InlineKeyboardButton("❌ انصراف",       callback_data="admin_orders")],
            ]))

    elif data == "admin_clear_orders_ok":
        if uid != ADMIN_CHAT_ID: return
        c = db()
        cnt = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        c.execute("DELETE FROM orders"); c.commit(); c.close()
        await q.edit_message_text(f"🗑️ *{cnt} سفارش* حذف شدند.",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]]))

    # ── Admin: add product (with category step) ────────────────────────────────
    elif data == "admin_add_p":
        if uid != ADMIN_CHAT_ID: return
        await q.edit_message_text("📝 *نام محصول جدید را وارد کنید:*",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())
        ctx.user_data["admin_action"] = "add_name"

    # ── Admin: category buttons when adding product ────────────────────────────
    elif data.startswith("admin_cat_pick_"):
        if uid != ADMIN_CHAT_ID: return
        idx       = int(data.split("_")[3])
        cat_opts  = ctx.user_data.get("cat_options", [])
        cat_name  = cat_opts[idx] if idx < len(cat_opts) else ""
        c = db()
        c.execute("INSERT INTO products (name, price_toman, description, category) VALUES (?,?,?,?)",
                  (ctx.user_data["new_p_name"], ctx.user_data["new_p_price"], "", cat_name))
        c.commit(); c.close()
        ctx.user_data.pop("admin_action", None)
        ctx.user_data.pop("cat_options",  None)
        await q.edit_message_text(
            f"✅ *محصول اضافه شد!*\nنام: {ctx.user_data.get('new_p_name','')}\nدسته‌بندی: {cat_name or 'ندارد'}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ پنل مدیریت", callback_data="admin_panel")]
            ]))

    elif data == "admin_cat_new":
        if uid != ADMIN_CHAT_ID: return
        ctx.user_data["admin_action"] = "add_cat_new"
        await q.edit_message_text("📂 *نام دسته‌بندی جدید را وارد کنید:*\n(حداکثر ۲۰ کاراکتر)",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    elif data == "admin_cat_none":
        if uid != ADMIN_CHAT_ID: return
        c = db()
        c.execute("INSERT INTO products (name, price_toman, description, category) VALUES (?,?,?,?)",
                  (ctx.user_data["new_p_name"], ctx.user_data["new_p_price"], "", ""))
        c.commit(); c.close()
        ctx.user_data.pop("admin_action", None)
        await q.edit_message_text("✅ محصول بدون دسته‌بندی اضافه شد.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("◀️ پنل مدیریت", callback_data="admin_panel")]
                                  ]))

    # ── Admin: edit price ──────────────────────────────────────────────────────
    elif data == "admin_edit_p":
        if uid != ADMIN_CHAT_ID: return
        c     = db()
        prods = c.execute("SELECT * FROM products WHERE is_active=1").fetchall()
        c.close()
        rows = [[InlineKeyboardButton(f"{p['name']} ({p['price_toman']})",
                                      callback_data=f"aedit_{p['id']}")] for p in prods]
        rows.append([InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")])
        await q.edit_message_text("💰 *محصول مورد نظر:*",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("aedit_"):
        if uid != ADMIN_CHAT_ID: return
        ctx.user_data["edit_pid"]     = int(data.split("_")[1])
        ctx.user_data["admin_action"] = "edit_price"
        await q.edit_message_text("💰 *قیمت جدید (فقط عدد به تومان):*",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    # ── Admin: edit category ───────────────────────────────────────────────────
    elif data == "admin_edit_cat":
        if uid != ADMIN_CHAT_ID: return
        c     = db()
        prods = c.execute("SELECT id, name, category FROM products WHERE is_active=1").fetchall()
        c.close()
        rows = [[InlineKeyboardButton(
            f"{p['name']} [{p['category'] or 'بدون دسته'}]",
            callback_data=f"acatedit_{p['id']}")] for p in prods]
        rows.append([InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")])
        await q.edit_message_text("📂 *محصول مورد نظر برای تغییر دسته‌بندی:*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("acatedit_"):
        if uid != ADMIN_CHAT_ID: return
        ctx.user_data["edit_cat_pid"]  = int(data.split("_")[1])
        ctx.user_data["admin_action"] = "edit_cat"
        await q.edit_message_text("📂 *نام دسته‌بندی جدید را وارد کنید:*\n(برای حذف دسته‌بندی، یک فاصله ارسال کنید)",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=back_home())

    # ── Admin: delete category (حذف دسته‌بندی) ────────────────────────────────
    elif data == "admin_del_cat":
        if uid != ADMIN_CHAT_ID: return
        c    = db()
        cats = c.execute(
            "SELECT category, COUNT(*) AS cnt FROM products "
            "WHERE category!='' AND is_active=1 GROUP BY category ORDER BY category"
        ).fetchall()
        c.close()
        if not cats:
            await q.edit_message_text("📭 هیچ دسته‌بندی‌ای یافت نشد.",
                                      reply_markup=InlineKeyboardMarkup(
                                          [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]])); return
        rows = [[InlineKeyboardButton(
            f"🗑️ {cat['category']} ({cat['cnt']} محصول)",
            callback_data=f"del_cat_ask_{cat['category'][:20]}")] for cat in cats]
        rows.append([InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")])
        await q.edit_message_text("🗑️ *دسته‌بندی مورد نظر برای حذف:*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("del_cat_ask_"):
        if uid != ADMIN_CHAT_ID: return
        cat_name = data[12:]      # بعد از "del_cat_ask_"
        c = db()
        cnt = c.execute("SELECT COUNT(*) FROM products WHERE category=?", (cat_name,)).fetchone()[0]
        c.close()
        await q.edit_message_text(
            f"⚠️ *آیا دسته‌بندی «{cat_name}» حذف شود؟*\n\n"
            f"🔢 {cnt} محصول از این دسته‌بندی خارج می‌شوند (حذف نمی‌شوند، بدون دسته می‌مانند).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، حذف شود",  callback_data=f"del_cat_ok_{cat_name[:20]}"),
                 InlineKeyboardButton("❌ انصراف",         callback_data="admin_del_cat")],
            ]))

    elif data.startswith("del_cat_ok_"):
        if uid != ADMIN_CHAT_ID: return
        cat_name = data[11:]      # بعد از "del_cat_ok_"
        c = db()
        c.execute("UPDATE products SET category='' WHERE category=?", (cat_name,))
        c.commit(); c.close()
        await q.edit_message_text(f"✅ دسته‌بندی «{cat_name}» حذف شد و محصولات بدون دسته ماندند.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]]))

    # ── Admin: delete product ──────────────────────────────────────────────────
    elif data == "admin_del_p":
        if uid != ADMIN_CHAT_ID: return
        c     = db()
        prods = c.execute("SELECT * FROM products").fetchall()
        c.close()
        if not prods:
            await q.edit_message_text("📭 هیچ محصولی وجود ندارد.",
                                      reply_markup=InlineKeyboardMarkup(
                                          [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]])); return
        rows = [[InlineKeyboardButton(f"🗑️ {p['name']}", callback_data=f"del_p_ask_{p['id']}")] for p in prods]
        rows.append([InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")])
        await q.edit_message_text("🗑️ *محصول مورد نظر:*",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("del_p_ask_"):
        if uid != ADMIN_CHAT_ID: return
        pid = int(data.split("_")[3])
        c   = db()
        p   = c.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        await q.edit_message_text(f"⚠️ *آیا محصول «{p['name']}» حذف شود؟*",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("✅ بله", callback_data=f"del_p_ok_{pid}"),
                                       InlineKeyboardButton("❌ انصراف", callback_data="admin_del_p")],
                                  ]))

    elif data.startswith("del_p_ok_"):
        if uid != ADMIN_CHAT_ID: return
        pid = int(data.split("_")[3])
        c   = db()
        c.execute("DELETE FROM products WHERE id=?", (pid,)); c.commit(); c.close()
        await q.edit_message_text("✅ محصول حذف شد.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]]))


# ─── Helper: admin orders with filter ──────────────────────────────────────────
async def _show_admin_orders(q, flt: str):
    c = db()
    if flt == "all":
        orders = c.execute(
            "SELECT o.*, u.full_name, u.telegram_username, p.name AS pname "
            "FROM orders o JOIN users u ON o.user_id=u.telegram_id "
            "JOIN products p ON o.product_id=p.id ORDER BY o.created_at DESC LIMIT 40"
        ).fetchall()
    else:
        orders = c.execute(
            "SELECT o.*, u.full_name, u.telegram_username, p.name AS pname "
            "FROM orders o JOIN users u ON o.user_id=u.telegram_id "
            "JOIN products p ON o.product_id=p.id WHERE o.status=? ORDER BY o.created_at DESC LIMIT 40",
            (flt,)
        ).fetchall()
    cnt = {s: c.execute("SELECT COUNT(*) FROM orders WHERE status=?", (s,)).fetchone()[0]
           for s in ("pending", "approved", "rejected")}
    cnt["all"] = sum(cnt.values())
    c.close()
    tabs = [
        InlineKeyboardButton(f"{'🔵' if flt=='all'      else '⚪'} همه ({cnt['all']})",        callback_data="admin_orders_filter_all"),
        InlineKeyboardButton(f"{'🔵' if flt=='pending'  else '⚪'} ⏳ ({cnt['pending']})",     callback_data="admin_orders_filter_pending"),
        InlineKeyboardButton(f"{'🔵' if flt=='approved' else '⚪'} ✅ ({cnt['approved']})",    callback_data="admin_orders_filter_approved"),
        InlineKeyboardButton(f"{'🔵' if flt=='rejected' else '⚪'} ❌ ({cnt['rejected']})",    callback_data="admin_orders_filter_rejected"),
    ]
    if not orders:
        await q.edit_message_text("📦 هیچ سفارشی یافت نشد.",
                                  reply_markup=InlineKeyboardMarkup([tabs[:2], tabs[2:],
                                      [InlineKeyboardButton("◀️ بازگشت", callback_data="admin_panel")]])); return
    smap = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    rows = [[InlineKeyboardButton(
        f"{smap.get(o['status'],'❓')} #{o['id']} | {o['full_name']} | {o['pname']} ×{o['quantity']}",
        callback_data=f"admin_order_{o['id']}")] for o in orders]
    rows.append([InlineKeyboardButton("🗑️ پاک کردن همه", callback_data="admin_clear_orders_ask")])
    rows.append([InlineKeyboardButton("◀️ بازگشت",      callback_data="admin_panel")])
    await q.edit_message_text(
        f"📦 *سفارشات* — نمایش {len(orders)} مورد",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([tabs[:2], tabs[2:]] + rows))


# ─── Message handler ────────────────────────────────────────────────────────────
async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u    = update.effective_user
    text = (update.message.text or "").strip()
    step = ctx.user_data.get("auth_step")

    # ── Registration flow ──────────────────────────────────────────────────────
    if step == "wait_email":
        if not valid_email(text):
            await update.message.reply_text(
                "❌ *فرمت ایمیل نادرست.*\nمثال: `example@gmail.com`",
                parse_mode=ParseMode.MARKDOWN); return
        c = db()
        if c.execute("SELECT 1 FROM users WHERE email=? AND telegram_id!=?", (text, u.id)).fetchone():
            c.close()
            await update.message.reply_text("❌ این ایمیل قبلاً ثبت شده است."); return
        c.close()
        ctx.user_data["reg_email"] = text.lower()
        ctx.user_data["auth_step"] = None
        await update.message.reply_text(f"📧 ایمیل: `{text}`\n\nتایید می‌کنید؟",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=confirm_reg("email")); return

    if step == "wait_phone":
        if not valid_phone(text):
            await update.message.reply_text(
                "❌ *شماره تماس نادرست.*\nمثال: `09123456789`",
                parse_mode=ParseMode.MARKDOWN); return
        ctx.user_data["reg_phone"] = text
        ctx.user_data["auth_step"] = None
        await update.message.reply_text(f"📱 شماره: `{text}`\n\nتایید می‌کنید؟",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=confirm_reg("phone")); return

    if step == "wait_name":
        if not valid_fullname(text):
            await update.message.reply_text(
                "❌ *نام کامل لازم است.*\nمثال: `علی محمدی`",
                parse_mode=ParseMode.MARKDOWN); return
        ctx.user_data["reg_name"]  = text
        ctx.user_data["auth_step"] = None
        await update.message.reply_text(f"👤 نام: *{text}*\n\nتایید می‌کنید؟",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=confirm_reg("name")); return

    # ── Custom quantity input ──────────────────────────────────────────────────
    if step == "wait_qty":
        try:
            qty = int(text)
            if not (1 <= qty <= 100):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد صحیح بین ۱ و ۱۰۰ وارد کنید:"); return
        pid = ctx.user_data.get("order_pid")
        if not pid:
            await update.message.reply_text("❌ خطا. دوباره از منوی اصلی شروع کنید.",
                                            reply_markup=main_menu_kbd(u.id))
            ctx.user_data["auth_step"] = None; return
        ctx.user_data["order_qty"] = qty
        ctx.user_data["auth_step"] = None
        c = db()
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        if str(p["price_toman"]).isdigit():
            total_str = f"{int(p['price_toman']) * qty:,} تومان"
        else:
            total_str = p["price_toman"]
        await update.message.reply_text(
            f"🎫 *کد تخفیف*\n\n📦 {p['name']} × {qty}\n💰 مبلغ کل: *{total_str}*\n\nآیا کد تخفیف دارید؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎫 دارم", callback_data=f"disc_have_{pid}"),
                 InlineKeyboardButton("⏩ ندارم", callback_data=f"disc_none_{pid}")],
                [InlineKeyboardButton("◀️ بازگشت", callback_data=f"order_{pid}")],
            ])
        ); return

    # ── Discount code input ────────────────────────────────────────────────────
    if step == "wait_discount":
        code = text.upper().strip()
        pid  = ctx.user_data.get("order_pid")
        qty  = ctx.user_data.get("order_qty", 1)
        c    = db()
        dc   = c.execute(
            "SELECT * FROM discount_codes WHERE code=? AND is_used=0 AND owner_id=?",
            (code, u.id)).fetchone()
        if not dc:
            c.close()
            await update.message.reply_text(
                "❌ *کد تخفیف نامعتبر یا قبلاً استفاده شده.*\n\n"
                "کد خود را بررسی کنید یا از گزینه «ندارم» استفاده کنید.",
                parse_mode=ParseMode.MARKDOWN); return
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        c.close()
        ctx.user_data["auth_step"] = None
        if str(p["price_toman"]).isdigit():
            base     = int(p["price_toman"]) * qty
            disc_amt = int(base * dc["discount_percent"] / 100)
            final    = base - disc_amt
            ctx.user_data["order_draft"] = {
                "pid": pid, "qty": qty, "final_amount": str(final), "discount_code": code}
            await update.message.reply_text(
                f"✅ *کد تخفیف اعمال شد!*\n\n"
                f"💰 مبلغ پایه ({qty} × {int(p['price_toman']):,}): *{base:,} تومان*\n"
                f"🎫 تخفیف {dc['discount_percent']}٪: *−{disc_amt:,} تومان*\n"
                f"💳 *مبلغ نهایی: {final:,} تومان*\n\n"
                f"اکنون روش پرداخت را انتخاب کنید:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 کارت / شبا", callback_data=f"pay_card_{pid}")],
                    [InlineKeyboardButton("💎 کریپتو",     callback_data=f"pay_crypto_{pid}")],
                    [InlineKeyboardButton("❌ انصراف",     callback_data=f"product_{pid}")],
                ])
            )
        else:
            await update.message.reply_text(
                "⚠️ این محصول قیمت ثابت ندارد؛ کد تخفیف قابل اعمال نیست.",
                reply_markup=main_menu_kbd(u.id))
        return

    # ── Admin: add product — name step ─────────────────────────────────────────
    if ctx.user_data.get("admin_action") == "add_name":
        ctx.user_data["new_p_name"]   = text
        ctx.user_data["admin_action"] = "add_price"
        await update.message.reply_text("💰 قیمت محصول *(فقط عدد، به تومان):*",
                                        parse_mode=ParseMode.MARKDOWN); return

    # ── Admin: add product — price step → ask category ─────────────────────────
    if ctx.user_data.get("admin_action") == "add_price":
        ctx.user_data["new_p_price"]  = text
        ctx.user_data["admin_action"] = "await_cat"
        c    = db()
        cats = c.execute(
            "SELECT DISTINCT category FROM products WHERE category!='' ORDER BY category"
        ).fetchall()
        c.close()
        cat_names = [r["category"] for r in cats]
        ctx.user_data["cat_options"] = cat_names
        rows = []
        for i, nm in enumerate(cat_names):
            rows.append([InlineKeyboardButton(f"📂 {nm}", callback_data=f"admin_cat_pick_{i}")])
        rows.append([InlineKeyboardButton("➕ دسته‌بندی جدید", callback_data="admin_cat_new")])
        rows.append([InlineKeyboardButton("🚫 بدون دسته‌بندی",  callback_data="admin_cat_none")])
        await update.message.reply_text("📂 *دسته‌بندی محصول را انتخاب کنید:*",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=InlineKeyboardMarkup(rows)); return

    # ── Admin: add product — new category name ─────────────────────────────────
    if ctx.user_data.get("admin_action") == "add_cat_new":
        cat_name = text.strip()[:20]
        c = db()
        c.execute("INSERT INTO products (name, price_toman, description, category) VALUES (?,?,?,?)",
                  (ctx.user_data["new_p_name"], ctx.user_data["new_p_price"], "", cat_name))
        c.commit(); c.close()
        ctx.user_data["admin_action"] = None
        await update.message.reply_text(
            f"✅ محصول با دسته‌بندی «{cat_name}» اضافه شد.",
            reply_markup=main_menu_kbd(u.id)); return

    # ── Admin: edit price ───────────────────────────────────────────────────────
    if ctx.user_data.get("admin_action") == "edit_price":
        c = db()
        c.execute("UPDATE products SET price_toman=? WHERE id=?",
                  (text, ctx.user_data["edit_pid"]))
        c.commit(); c.close()
        ctx.user_data["admin_action"] = None
        await update.message.reply_text("✅ قیمت به‌روز شد.", reply_markup=main_menu_kbd(u.id)); return

    # ── Admin: new discount code — step 1: get target user ID ─────────────────
    if ctx.user_data.get("admin_action") == "code_new_uid":
        try:
            target_uid = int(text)
        except ValueError:
            await update.message.reply_text("❌ Telegram ID باید عدد صحیح باشد:"); return
        ctx.user_data["code_target_uid"]  = target_uid
        ctx.user_data["admin_action"]     = "code_new_pct"
        await update.message.reply_text("✏️ *درصد تخفیف را وارد کنید* (عدد ۱ تا ۱۰۰):",
                                        parse_mode=ParseMode.MARKDOWN); return

    # ── Admin: new discount code — step 2: get percent ────────────────────────
    if ctx.user_data.get("admin_action") == "code_new_pct":
        try:
            pct = int(text)
            if not (1 <= pct <= 100):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ عدد صحیح بین ۱ و ۱۰۰ وارد کنید:"); return
        target_uid = ctx.user_data.get("code_target_uid", 0)
        code = _gen_code()
        c = db()
        c.execute("INSERT INTO discount_codes (code, owner_id, discount_percent) VALUES (?,?,?)",
                  (code, target_uid, pct))
        c.commit(); c.close()
        ctx.user_data["admin_action"] = None
        msg = f"✅ *کد تخفیف ایجاد شد!*\n\n🎫 کد: `{code}`\n📊 تخفیف: {pct}٪\n👤 مالک ID: `{target_uid}`"
        if target_uid and target_uid != 0:
            try:
                await ctx.bot.send_message(
                    target_uid,
                    f"🎫 *ادمین یک کد تخفیف برای شما ایجاد کرد!*\n\n"
                    f"کد: `{code}`\n💰 تخفیف {pct}٪\n\n"
                    f"این کد را در سفارش بعدی قبل از پرداخت وارد کنید.",
                    parse_mode=ParseMode.MARKDOWN)
                msg += "\n\n✅ کد برای کاربر ارسال شد."
            except Exception as e:
                msg += f"\n\n⚠️ ارسال به کاربر ناموفق: {e}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("◀️ مدیریت کدها", callback_data="admin_codes")]
                                        ])); return

    # ── Admin: edit discount percent ───────────────────────────────────────────
    if ctx.user_data.get("admin_action") == "code_edit_pct":
        try:
            pct = int(text)
            if not (1 <= pct <= 100):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ عدد صحیح بین ۱ و ۱۰۰ وارد کنید:"); return
        cid = ctx.user_data.get("edit_code_id")
        c   = db()
        c.execute("UPDATE discount_codes SET discount_percent=? WHERE id=?", (pct, cid))
        c.commit(); c.close()
        ctx.user_data["admin_action"] = None
        await update.message.reply_text(f"✅ درصد تخفیف به *{pct}٪* تغییر یافت.",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("◀️ مدیریت کدها", callback_data="admin_codes")]
                                        ])); return

    # ── Admin: edit category ────────────────────────────────────────────────────
    if ctx.user_data.get("admin_action") == "edit_cat":
        cat_name = "" if text.strip() == " " else text.strip()[:20]
        c = db()
        c.execute("UPDATE products SET category=? WHERE id=?",
                  (cat_name, ctx.user_data["edit_cat_pid"]))
        c.commit(); c.close()
        ctx.user_data["admin_action"] = None
        label = f"«{cat_name}»" if cat_name else "بدون دسته‌بندی"
        await update.message.reply_text(f"✅ دسته‌بندی به {label} تغییر یافت.",
                                        reply_markup=main_menu_kbd(u.id)); return

    # ── Pending order: receive receipt / TxHash ────────────────────────────────
    if "pending_order" in ctx.user_data:
        order_info = ctx.user_data["pending_order"]
        fish       = (f"photo_{update.message.photo[-1].file_id}"
                      if update.message.photo else text)
        qty        = order_info.get("qty", 1)
        disc_code  = order_info.get("discount_code", "")
        c          = db(); cur = c.cursor()
        cur.execute(
            "INSERT INTO orders "
            "(user_id, product_id, quantity, amount_toman, payment_method, tx_hash_or_fish, discount_code, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (u.id, order_info["pid"], qty,
             order_info["amount"], order_info["method"], fish, disc_code, now_tehran()))
        oid = cur.lastrowid
        if disc_code:
            c.execute("UPDATE discount_codes SET is_used=1 WHERE code=?", (disc_code,))
        c.commit(); c.close()

        await update.message.reply_text(
            f"🙏 *با تشکر از خرید شما!*\n\n"
            f"✅ سفارش *#{oid}* ثبت شد.\n"
            f"🔢 تعداد: {qty} عدد\n"
            f"💰 مبلغ پرداختی: {order_info['amount']}\n"
            f"پس از بررسی به شما اطلاع داده می‌شود.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=after_order_kbd(oid))

        disc_txt = f"\n🎫 کد تخفیف: {disc_code}" if disc_code else ""
        admin_msg = (
            f"🔔 *سفارش جدید #{oid}*\n"
            f"👤 {u.full_name or '—'} | @{u.username or '—'}\n"
            f"💳 {order_info['method'].upper()} | 🔢 ×{qty} | 💰 {order_info['amount']}{disc_txt}\n"
            f"📝 TxHash/فیش: `{fish}`"
        )
        try:
            if update.message.photo:
                await ctx.bot.send_photo(ADMIN_CHAT_ID, update.message.photo[-1].file_id,
                                         caption=admin_msg, parse_mode=ParseMode.MARKDOWN)
            else:
                await ctx.bot.send_message(ADMIN_CHAT_ID, admin_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"admin notify: {e}")
        del ctx.user_data["pending_order"]

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = (Application.builder()
               .token(BOT_TOKEN)
               .read_timeout(30)
               .write_timeout(30)
               .connect_timeout(30)
               .pool_timeout(30)
               .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)
    logger.info("🚀 Bot v2.0 started — polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

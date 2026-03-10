"""
Telegram TON Faucet Bot — Tonkeeper Compatible
Sends 0.001 TON to provided addresses with memo "testing!!!"

Requirements:
    pip install python-telegram-bot tonsdk aiohttp PyNaCl

HOW TO GET YOUR FREE API KEY (fixes "Ratelimit exceeded"):
    1. Open Telegram → search @tonapibot
    2. Send /start → click "Create API Key"
    3. Paste the key into TONCENTER_API_KEY below

CONFIGURATION:
    BOT_TOKEN         = Telegram bot token from @BotFather
    SENDER_MNEMONIC   = 24-word seed phrase of the sending wallet
    TONCENTER_API_KEY = Free key from @tonapibot  ← REQUIRED to avoid rate limits
    USE_BIP39         = True  for Tonkeeper / MyTonWallet
                      = False for TonHub / tonsdk native wallet
    WALLET_VERSION    = "v4r2" for Tonkeeper (default)
"""

import logging
import hashlib
import hmac
import struct
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
BOT_TOKEN         = "8655509455:AAEpEODcdE4VoxD40P2Y7SfU52xEJ8kbua8"
SENDER_MNEMONIC   = "cash matrix behind engage hover shoulder include dove process bachelor body cousin lemon around kitten utility trend sunset arm swift host purity animal dose"
TONCENTER_API_KEY = "640b4486094ffd81a5e49a4bb7c599fb55e8bfa3d391f140fb02b12b10c032ca"        # ← FREE from @tonapibot on Telegram — PASTE HERE
AMOUNT_TON        = 0.001
MEMO              = "testing!!!"
NETWORK           = "mainnet" # "mainnet" or "testnet"
WALLET_VERSION    = "v4r2"    # "v3r2" or "v4r2"
USE_BIP39         = True      # True = Tonkeeper/MyTonWallet | False = TON native
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TONCENTER_BASE = {
    "mainnet": "https://toncenter.com/api/v2",
    "testnet": "https://testnet.toncenter.com/api/v2",
}

# One transaction at a time — prevents seqno conflicts when multiple users request simultaneously
_tx_lock = asyncio.Lock()


# ─── RATE-LIMIT-AWARE API CALLER ─────────────────────────────────────────────

async def toncenter_get(session, method, params, headers, retries=6):
    """GET with automatic retry on HTTP 429 rate limit."""
    base_url = TONCENTER_BASE[NETWORK]
    for attempt in range(retries):
        async with session.get(f"{base_url}/{method}", params=params, headers=headers) as resp:
            if resp.status == 429:
                wait = 2.0 * (attempt + 1)
                logger.warning(f"Rate limited [{method}], retry in {wait}s ({attempt+1}/{retries})")
                await asyncio.sleep(wait)
                continue
            return await resp.json()
    return {"ok": False, "error": "Rate limit: too many retries. Set TONCENTER_API_KEY from @tonapibot"}


async def toncenter_post(session, method, body, headers, retries=6):
    """POST with automatic retry on HTTP 429 rate limit."""
    base_url = TONCENTER_BASE[NETWORK]
    for attempt in range(retries):
        async with session.post(f"{base_url}/{method}", json=body, headers=headers) as resp:
            if resp.status == 429:
                wait = 2.0 * (attempt + 1)
                logger.warning(f"Rate limited [{method}], retry in {wait}s ({attempt+1}/{retries})")
                await asyncio.sleep(wait)
                continue
            return await resp.json()
    return {"ok": False, "error": "Rate limit: too many retries. Set TONCENTER_API_KEY from @tonapibot"}


# ─── KEY DERIVATION ───────────────────────────────────────────────────────────

def bip39_to_seed(mnemonic: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", mnemonic.strip().encode(), b"mnemonic", 2048)


def slip10_derive_ed25519(seed: bytes) -> tuple:
    """Tonkeeper key derivation: SLIP-0010 path m/44'/607'/0'"""
    def ckd(k, c, index):
        data = b"\x00" + k + struct.pack(">I", 0x80000000 | index)
        I = hmac.new(c, data, hashlib.sha512).digest()
        return I[:32], I[32:]

    I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    kL, kR = I[:32], I[32:]
    for i in [44, 607, 0]:
        kL, kR = ckd(kL, kR, i)

    import nacl.signing
    pub = bytes(nacl.signing.SigningKey(kL).verify_key)
    return kL, pub


def build_wallet_from_pubkey(pub_key: bytes, version: str, workchain: int = 0):
    from tonsdk.contract.wallet import WalletV3ContractR1, WalletV3ContractR2, WalletV4ContractR2
    cls = {"v3r1": WalletV3ContractR1, "v3r2": WalletV3ContractR2, "v4r2": WalletV4ContractR2}[version]
    return cls(public_key=pub_key, wc=workchain)


def build_keypair():
    words = SENDER_MNEMONIC.strip().split()
    if len(words) != 24:
        raise ValueError(f"Mnemonic must be 24 words, got {len(words)}")

    if USE_BIP39:
        seed = bip39_to_seed(SENDER_MNEMONIC)
        priv_key, pub_key = slip10_derive_ed25519(seed)
        wallet = build_wallet_from_pubkey(pub_key, WALLET_VERSION)
        return priv_key, pub_key, wallet
    else:
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        version_map = {
            "v3r1": WalletVersionEnum.v3r1,
            "v3r2": WalletVersionEnum.v3r2,
            "v4r2": WalletVersionEnum.v4r2,
        }
        _, pub_key, priv_key, wallet = Wallets.from_mnemonics(
            words, version_map[WALLET_VERSION], workchain=0
        )
        return priv_key, pub_key, wallet


def sign_bip39(priv_key: bytes, msg_bytes: bytes) -> bytes:
    import nacl.signing
    return bytes(nacl.signing.SigningKey(priv_key).sign(msg_bytes).signature)


# ─── TON TRANSFER ─────────────────────────────────────────────────────────────

async def get_account_state(session, address, headers):
    data = await toncenter_get(session, "getAddressInformation", {"address": address}, headers)
    if not data.get("ok"):
        raise RuntimeError(f"getAddressInformation failed: {data}")
    state   = data["result"].get("state", "uninitialized")
    balance = int(data["result"].get("balance", 0)) / 1e9
    return state, balance


async def get_seqno(session, address, headers) -> int:
    data = await toncenter_get(session, "runGetMethod",
        {"address": address, "method": "seqno", "stack": "[]"}, headers)
    try:
        if data.get("ok") and data["result"].get("exit_code") == 0:
            stack = data["result"].get("stack", [])
            if stack:
                return int(stack[0][1], 16)
    except Exception:
        pass
    return 0


async def send_ton(to_address: str, amount_ton: float, memo: str) -> dict:
    import aiohttp
    from tonsdk.utils import to_nano, bytes_to_b64str

    try:
        priv_key, pub_key, wallet = build_keypair()
    except ImportError as e:
        return {"ok": False, "error": f"Missing library: {e}. Run: pip install tonsdk PyNaCl aiohttp"}
    except Exception as e:
        return {"ok": False, "error": f"Key derivation failed: {e}"}

    headers = {"Content-Type": "application/json"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    else:
        logger.warning("No TONCENTER_API_KEY set — limited to 1 req/sec, rate limits likely!")

    # Serialize all transactions — prevents seqno reuse if two users send at once
    async with _tx_lock:
        try:
            async with aiohttp.ClientSession() as session:
                sender_addr = wallet.address.to_string(True, True, False)
                logger.info(f"Sender: {sender_addr}")

                # 1. Balance check
                state, balance = await get_account_state(session, sender_addr, headers)
                logger.info(f"State={state}  Balance={balance:.5f} TON")

                min_needed = amount_ton + 0.015
                if balance < min_needed:
                    return {
                        "ok": False,
                        "error": (
                            f"Insufficient balance: {balance:.5f} TON available, "
                            f"need {min_needed:.4f} TON (amount + fees).\n"
                            f"Deposit TON to: `{sender_addr}`"
                        )
                    }

                # 2. Seqno
                seqno = await get_seqno(session, sender_addr, headers)
                logger.info(f"Seqno={seqno}")

                # Small delay between API calls to respect rate limits
                await asyncio.sleep(0.5 if TONCENTER_API_KEY else 1.2)

                # 3. Build + sign message
                if USE_BIP39:
                    transfer = wallet.create_transfer_message(
                        to_addr=to_address,
                        amount=to_nano(amount_ton, "ton"),
                        seqno=seqno,
                        payload=memo,
                        sign_func=lambda msg: sign_bip39(priv_key, msg),
                    )
                else:
                    transfer = wallet.create_transfer_message(
                        to_addr=to_address,
                        amount=to_nano(amount_ton, "ton"),
                        seqno=seqno,
                        payload=memo,
                    )

                boc = bytes_to_b64str(transfer["message"].to_boc(False))

                # 4. Broadcast
                result = await toncenter_post(session, "sendBoc", {"boc": boc}, headers)
                logger.info(f"sendBoc -> {result}")
                return result

        except Exception as e:
            logger.exception("send_ton error")
            return {"ok": False, "error": str(e)}


# ─── ADDRESS VALIDATION ───────────────────────────────────────────────────────

def is_valid_ton_address(addr: str) -> bool:
    import re
    addr = addr.strip()
    if ":" in addr:
        parts = addr.split(":")
        return len(parts) == 2 and parts[0].lstrip("-").isdigit() and bool(re.fullmatch(r"[0-9a-fA-F]{64}", parts[1]))
    return bool(re.fullmatch(r"[A-Za-z0-9+/\-_]{48}", addr))


# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *TON Faucet Bot*\n\n"
        f"Send me a TON wallet address and I'll transfer *{AMOUNT_TON} TON* "
        f"with memo: `{MEMO}`\n\n"
        "Just paste your TON address below!",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *How to use:*\n\n"
        "1️⃣ Paste your TON wallet address\n"
        f"2️⃣ Bot sends *{AMOUNT_TON} TON* to it\n"
        f"3️⃣ Memo: `{MEMO}`\n\n"
        "Supported formats:\n"
        "• `EQD...` / `UQD...` (48 chars)\n"
        "• `0:abcdef...` (raw hex)",
        parse_mode="Markdown"
    )


async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()

    if not is_valid_ton_address(address):
        await update.message.reply_text(
            "❌ *Invalid TON address.*\n\nPlease send a valid address like:\n"
            "`EQD4FPq-PRDieyQKkizFTRtSDyucUIqrj0v_zXJmqaDp6_0t`",
            parse_mode="Markdown"
        )
        return

    msg = await update.message.reply_text(
        f"⏳ *Sending...*\n\n📬 To: `{address}`\n💎 *{AMOUNT_TON} TON*\n📝 `{MEMO}`",
        parse_mode="Markdown"
    )

    result = await send_ton(address, AMOUNT_TON, MEMO)

    if result.get("ok"):
        await msg.edit_text(
            f"✅ *Sent!*\n\n"
            f"📬 To: `{address}`\n"
            f"💎 *{AMOUNT_TON} TON*\n"
            f"📝 `{MEMO}`\n\n"
            f"_Check your wallet in a few seconds._",
            parse_mode="Markdown"
        )
        logger.info(f"SUCCESS: {AMOUNT_TON} TON -> {address}")
    else:
        error = result.get("error") or str(result.get("result", "Unknown error"))
        await msg.edit_text(
            f"❌ *Failed*\n\n`{error}`\n\n"
            f"_If you see 'Ratelimit', get a free API key from @tonapibot_",
            parse_mode="Markdown"
        )
        logger.error(f"FAILED: {error}")


# ─── STARTUP ──────────────────────────────────────────────────────────────────

async def post_init(application):
    try:
        _, _, wallet = build_keypair()
        addr_eq = wallet.address.to_string(True,  True,  False)
        addr_uq = wallet.address.to_string(True,  False, False)
        mode    = "BIP39/Tonkeeper" if USE_BIP39 else "TON Native"
        has_key = "✅ Set" if TONCENTER_API_KEY else "❌ MISSING — get free key from @tonapibot!"
        print(f"\n{'='*60}")
        print(f"  Mode          : {mode}")
        print(f"  Wallet ver    : {WALLET_VERSION}")
        print(f"  Network       : {NETWORK}")
        print(f"  API Key       : {has_key}")
        print(f"  Sender EQ...  : {addr_eq}")
        print(f"  Sender UQ...  : {addr_uq}")
        print(f"  Amount/tx     : {AMOUNT_TON} TON  |  Memo: {MEMO}")
        print(f"{'='*60}")
        net = "testnet." if NETWORK == "testnet" else ""
        print(f"\n  Deposit TON to UQ address above to fund the bot")
        print(f"  Check balance: https://{net}tonscan.org/address/{addr_uq}\n")
    except Exception as e:
        print(f"⚠️  Startup error: {e}")


def main():
    if "YOUR_TELEGRAM_BOT_TOKEN" in BOT_TOKEN:
        print("❌ Set BOT_TOKEN before running!")
        return
    if "word1" in SENDER_MNEMONIC:
        print("❌ Set SENDER_MNEMONIC before running!")
        return
    if not TONCENTER_API_KEY:
        print("⚠️  WARNING: No TONCENTER_API_KEY — you will hit rate limits!")
        print("   Get a free key from @tonapibot on Telegram\n")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))

    print("🤖 Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

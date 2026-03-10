"""
Telegram TON Faucet Bot — Tonkeeper Compatible
Sends 0.001 TON to provided addresses with memo "testing!!!"

Requirements:
    pip install python-telegram-bot tonsdk aiohttp pytoniq-core

HOW TO GET YOUR FREE API KEY (fixes "Ratelimit exceeded"):
    1. Open Telegram → search @tonapibot
    2. Send /start → click "Create API Key"
    3. Paste the key into TONCENTER_API_KEY below

CONFIGURATION:
    BOT_TOKEN         = Telegram bot token from @BotFather
    SENDER_MNEMONIC   = 24-word seed phrase of the sending wallet
    TONCENTER_API_KEY = Free key from @tonapibot on Telegram
    WALLET_VERSION    = "v4r2" for Tonkeeper (default), "v3r2" for older wallets
"""

import logging
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
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TONCENTER_BASE = {
    "mainnet": "https://toncenter.com/api/v2",
    "testnet": "https://testnet.toncenter.com/api/v2",
}

# One tx at a time — prevents seqno conflicts with concurrent users
_tx_lock = asyncio.Lock()


# ─── KEY DERIVATION (supports both Tonkeeper BIP39 and TON native) ─────────────

def derive_keypair_from_mnemonic():
    """
    Derives private key + public key from mnemonic.
    Automatically tries Tonkeeper BIP39 derivation first (pytoniq-core),
    then falls back to tonsdk TON-native derivation.
    Returns (priv_key_bytes, pub_key_bytes, wallet_address_string)
    """
    words = SENDER_MNEMONIC.strip().split()
    if len(words) != 24:
        raise ValueError(f"Expected 24 mnemonic words, got {len(words)}")

    # ── Try pytoniq-core (handles Tonkeeper BIP39 correctly) ─────────────────
    try:
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, mnemonic_is_legacy
        from pytoniq_core.crypto.signature import sign_message

        is_legacy = mnemonic_is_legacy(words)
        logger.info(f"Mnemonic type: {'TON native/legacy' if is_legacy else 'BIP39/Tonkeeper'}")

        pub_key, priv_key = mnemonic_to_private_key(words)
        return priv_key, pub_key, "pytoniq"

    except ImportError:
        logger.warning("pytoniq-core not installed, falling back to tonsdk native derivation")

    # ── Fallback: tonsdk native ───────────────────────────────────────────────
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    version_map = {
        "v3r1": WalletVersionEnum.v3r1,
        "v3r2": WalletVersionEnum.v3r2,
        "v4r2": WalletVersionEnum.v4r2,
    }
    _, pub_key, priv_key, wallet = Wallets.from_mnemonics(
        words, version_map[WALLET_VERSION], workchain=0
    )
    addr = wallet.address.to_string(True, True, False)
    return priv_key, pub_key, addr


def build_wallet_and_keys():
    """
    Returns (priv_key, pub_key, wallet_object, sender_address)
    Works with both pytoniq-core (Tonkeeper) and tonsdk native.
    """
    words = SENDER_MNEMONIC.strip().split()
    if len(words) != 24:
        raise ValueError(f"Expected 24 mnemonic words, got {len(words)}")

    # ── pytoniq-core path (correct for Tonkeeper BIP39) ──────────────────────
    try:
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, mnemonic_is_legacy
        import nacl.signing
        from tonsdk.contract.wallet import WalletV3ContractR2, WalletV4ContractR2

        pub_key, priv_key = mnemonic_to_private_key(words)

        # Build wallet address using tonsdk with the correct public key
        cls = WalletV4ContractR2 if WALLET_VERSION == "v4r2" else WalletV3ContractR2

        # tonsdk wallet classes take an options dict
        wallet = cls(options={"public_key": pub_key, "wc": 0})
        sender_addr = wallet.address.to_string(True, True, False)

        logger.info(f"pytoniq-core derivation OK. Address: {sender_addr}")
        return priv_key, pub_key, wallet, sender_addr, "pytoniq"

    except ImportError:
        logger.warning("pytoniq-core not found — using tonsdk native (may not match Tonkeeper)")
    except Exception as e:
        logger.warning(f"pytoniq-core path failed: {e} — trying tonsdk native")

    # ── tonsdk native fallback ────────────────────────────────────────────────
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    version_map = {
        "v3r1": WalletVersionEnum.v3r1,
        "v3r2": WalletVersionEnum.v3r2,
        "v4r2": WalletVersionEnum.v4r2,
    }
    _, pub_key, priv_key, wallet = Wallets.from_mnemonics(
        words, version_map[WALLET_VERSION], workchain=0
    )
    sender_addr = wallet.address.to_string(True, True, False)
    logger.info(f"tonsdk native derivation. Address: {sender_addr}")
    return priv_key, pub_key, wallet, sender_addr, "tonsdk"


def sign_with_priv_key(priv_key: bytes, msg: bytes) -> bytes:
    """Sign msg bytes with Ed25519 private key."""
    import nacl.signing
    return bytes(nacl.signing.SigningKey(priv_key).sign(msg).signature)


# ─── RATE-LIMIT-AWARE TON CENTER CALLS ────────────────────────────────────────

async def tc_get(session, method, params, headers, retries=6):
    base = TONCENTER_BASE[NETWORK]
    for attempt in range(retries):
        async with session.get(f"{base}/{method}", params=params, headers=headers) as r:
            if r.status == 429:
                wait = 2.0 * (attempt + 1)
                logger.warning(f"Rate limited on {method}, waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            return await r.json()
    return {"ok": False, "error": "Rate limit — max retries exceeded. Set TONCENTER_API_KEY from @tonapibot"}


async def tc_post(session, method, body, headers, retries=6):
    base = TONCENTER_BASE[NETWORK]
    for attempt in range(retries):
        async with session.post(f"{base}/{method}", json=body, headers=headers) as r:
            if r.status == 429:
                wait = 2.0 * (attempt + 1)
                logger.warning(f"Rate limited on {method}, waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            return await r.json()
    return {"ok": False, "error": "Rate limit — max retries exceeded. Set TONCENTER_API_KEY from @tonapibot"}


# ─── SEND TON ─────────────────────────────────────────────────────────────────

async def send_ton(to_address: str, amount_ton: float, memo: str) -> dict:
    import aiohttp
    from tonsdk.utils import to_nano, bytes_to_b64str

    try:
        priv_key, pub_key, wallet, sender_addr, derivation = build_wallet_and_keys()
    except Exception as e:
        return {"ok": False, "error": f"Wallet init failed: {e}"}

    headers = {"Content-Type": "application/json"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    else:
        logger.warning("No API key — rate limits likely! Get one free from @tonapibot")

    async with _tx_lock:
        try:
            async with aiohttp.ClientSession() as session:

                # 1. Check balance
                acc = await tc_get(session, "getAddressInformation", {"address": sender_addr}, headers)
                if not acc.get("ok"):
                    return {"ok": False, "error": f"Could not fetch balance: {acc}"}

                balance = int(acc["result"].get("balance", 0)) / 1e9
                state   = acc["result"].get("state", "uninitialized")
                logger.info(f"Sender={sender_addr} State={state} Balance={balance:.5f} TON")

                if balance < amount_ton + 0.015:
                    uq_addr = wallet.address.to_string(True, False, False)
                    return {
                        "ok": False,
                        "error": (
                            f"Insufficient balance: {balance:.5f} TON.\n"
                            f"Need at least {amount_ton + 0.015:.4f} TON.\n\n"
                            f"Deposit TON to sender wallet:\n`{uq_addr}`"
                        )
                    }

                await asyncio.sleep(0.4)  # avoid rate limit between calls

                # 2. Get seqno — use getWalletInformation (more reliable than runGetMethod)
                seqno = None
                wi = await tc_get(session, "getWalletInformation", {"address": sender_addr}, headers)
                logger.info(f"getWalletInformation response: {wi}")
                try:
                    if wi.get("ok") and wi["result"].get("seqno") is not None:
                        seqno = int(wi["result"]["seqno"])
                        logger.info(f"Seqno from getWalletInformation: {seqno}")
                except Exception as e:
                    logger.warning(f"getWalletInformation seqno parse failed: {e}")

                # Fallback: runGetMethod
                if seqno is None:
                    await asyncio.sleep(0.4)
                    sq = await tc_get(session, "runGetMethod",
                        {"address": sender_addr, "method": "seqno", "stack": "[]"}, headers)
                    logger.info(f"runGetMethod response: {sq}")
                    try:
                        if sq.get("ok") and sq["result"].get("exit_code") == 0:
                            raw = sq["result"]["stack"][0][1]
                            # stack value can be decimal string or hex string
                            seqno = int(raw, 16) if raw.startswith("0x") else int(raw)
                            logger.info(f"Seqno from runGetMethod: {seqno}")
                    except Exception as e:
                        logger.warning(f"runGetMethod seqno parse failed: {e}, raw response: {sq}")

                if seqno is None:
                    return {"ok": False, "error": "Could not fetch wallet seqno. Wallet may not be deployed yet or API is unreachable."}

                logger.info(f"Final seqno={seqno}")

                await asyncio.sleep(0.4)

                # 3. Build + sign transfer
                if derivation == "pytoniq":
                    transfer = wallet.create_transfer_message(
                        to_addr=to_address,
                        amount=to_nano(amount_ton, "ton"),
                        seqno=seqno,
                        payload=memo,
                        sign_func=lambda msg: sign_with_priv_key(priv_key, msg),
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
                result = await tc_post(session, "sendBoc", {"boc": boc}, headers)
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
            "❌ *Invalid TON address.*\n\nSend a valid address like:\n"
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
            f"❌ *Failed*\n\n`{error}`",
            parse_mode="Markdown"
        )
        logger.error(f"FAILED: {error}")


# ─── STARTUP ──────────────────────────────────────────────────────────────────

async def post_init(application):
    try:
        priv_key, pub_key, wallet, addr_eq, derivation = build_wallet_and_keys()
        addr_uq = wallet.address.to_string(True, False, False)
        has_key = "✅ Set" if TONCENTER_API_KEY else "❌ MISSING — get free key from @tonapibot!"
        net     = "testnet." if NETWORK == "testnet" else ""
        print(f"\n{'='*62}")
        print(f"  Derivation    : {derivation} ({'Tonkeeper BIP39' if derivation == 'pytoniq' else 'TON native'})")
        print(f"  Wallet ver    : {WALLET_VERSION}  |  Network: {NETWORK}")
        print(f"  API Key       : {has_key}")
        print(f"  Sender EQ...  : {addr_eq}")
        print(f"  Sender UQ...  : {addr_uq}")
        print(f"  Amount/tx     : {AMOUNT_TON} TON  |  Memo: {MEMO}")
        print(f"{'='*62}")
        print(f"\n  ⚠️  Deposit TON to the UQ... address above to fund the bot")
        print(f"  Check: https://{net}tonscan.org/address/{addr_uq}\n")
    except Exception as e:
        print(f"⚠️  Startup error: {e}")
        import traceback; traceback.print_exc()


def main():
    if "YOUR_TELEGRAM_BOT_TOKEN" in BOT_TOKEN:
        print("❌ Set BOT_TOKEN before running!")
        return
    if "word1" in SENDER_MNEMONIC:
        print("❌ Set SENDER_MNEMONIC before running!")
        return
    if not TONCENTER_API_KEY:
        print("⚠️  No TONCENTER_API_KEY — rate limits likely! Get free key from @tonapibot\n")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    print("🤖 Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

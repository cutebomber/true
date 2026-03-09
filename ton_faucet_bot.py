"""
Telegram TON Faucet Bot
Sends 0.001 TON to provided addresses with memo "testing!!!"

Requirements:
    pip install python-telegram-bot tonsdk aiohttp

Setup:
    1. Create a bot via @BotFather on Telegram -> get BOT_TOKEN
    2. Fill in BOT_TOKEN, SENDER_MNEMONIC (24 words), and optionally TONCENTER_API_KEY below
    3. Make sure your sender wallet has TON funded in it
    4. Run: python ton_faucet_bot.py

FIX for "cannot apply external message / Failed to unpack account state":
    This error means the sender wallet is NOT yet deployed on-chain.
    A TON wallet only exists on-chain after it sends its FIRST transaction.
    This bot now auto-detects undeployed wallets and includes state_init
    in the message to deploy + send in a single operation.

CHECKLIST before running:
    [ ] BOT_TOKEN set
    [ ] SENDER_MNEMONIC set (24 words)
    [ ] WALLET_VERSION matches what your wallet app uses (usually v4r2)
    [ ] Sender wallet is funded with TON (at tonscan.org or tonviewer.com)
    [ ] If first ever use: wallet just needs a balance - bot will deploy it automatically
"""

import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
BOT_TOKEN         = "8655509455:AAEpEODcdE4VoxD40P2Y7SfU52xEJ8kbua8"    # From @BotFather
SENDER_MNEMONIC   = "cash matrix behind engage hover shoulder include dove process bachelor body cousin lemon around kitten utility trend sunset arm swift host purity animal dose"     # 24-word seed phrase of sender wallet
TONCENTER_API_KEY = ""                            # Optional but recommended (toncenter.com/api)
AMOUNT_TON        = 0.001                         # Amount to send per request
MEMO              = "testing!!!"                 # Fixed memo/comment
NETWORK           = "mainnet"                     # "mainnet" or "testnet"
WALLET_VERSION    = "v4r2"                        # v3r2 or v4r2 — must match your wallet app
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TONCENTER_BASE = {
    "mainnet": "https://toncenter.com/api/v2",
    "testnet": "https://testnet.toncenter.com/api/v2",
}


# ─── TON HELPERS ──────────────────────────────────────────────────────────────

def build_wallet():
    """Derive wallet object from mnemonic."""
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    version_map = {
        "v3r1": WalletVersionEnum.v3r1,
        "v3r2": WalletVersionEnum.v3r2,
        "v4r2": WalletVersionEnum.v4r2,
    }
    mnemonics = SENDER_MNEMONIC.strip().split()
    if len(mnemonics) != 24:
        raise ValueError(f"Mnemonic must be 24 words, got {len(mnemonics)}")
    _, pub_key, priv_key, wallet = Wallets.from_mnemonics(
        mnemonics, version_map[WALLET_VERSION], workchain=0
    )
    return wallet


async def get_account_state(session, base_url, address, headers):
    """Return (state_str, balance_ton). state_str: 'active'|'uninitialized'|'frozen'"""
    async with session.get(
        f"{base_url}/getAddressInformation",
        params={"address": address},
        headers=headers
    ) as resp:
        data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getAddressInformation failed: {data}")
    result  = data["result"]
    state   = result.get("state", "uninitialized")
    balance = int(result.get("balance", 0)) / 1e9
    return state, balance


async def get_seqno(session, base_url, address, headers):
    """Get seqno; returns 0 if wallet not yet deployed."""
    try:
        async with session.get(
            f"{base_url}/runGetMethod",
            params={"address": address, "method": "seqno", "stack": "[]"},
            headers=headers
        ) as resp:
            data = await resp.json()
        if data.get("ok") and data["result"].get("exit_code") == 0:
            stack = data["result"].get("stack", [])
            if stack:
                return int(stack[0][1], 16)
    except Exception as e:
        logger.warning(f"get_seqno error (defaulting to 0): {e}")
    return 0


async def send_ton(to_address: str, amount_ton: float, memo: str) -> dict:
    """
    Build and broadcast a TON transfer.
    Handles both:
      - Undeployed wallets (state_init included, seqno=0)
      - Active wallets (normal transfer)
    """
    import aiohttp
    from tonsdk.utils import to_nano, bytes_to_b64str

    try:
        wallet   = build_wallet()
        base_url = TONCENTER_BASE[NETWORK]
        headers  = {"Content-Type": "application/json"}
        if TONCENTER_API_KEY:
            headers["X-API-Key"] = TONCENTER_API_KEY

        async with aiohttp.ClientSession() as session:
            wallet_addr = wallet.address.to_string(True, True, False)
            logger.info(f"Sender: {wallet_addr}")

            # 1. Check account state & balance
            state, balance = await get_account_state(session, base_url, wallet_addr, headers)
            logger.info(f"State={state}, Balance={balance:.4f} TON")

            min_required = amount_ton + 0.015  # amount + gas estimate
            if balance < min_required:
                return {
                    "ok": False,
                    "error": (
                        f"Insufficient funds. Sender balance: {balance:.4f} TON. "
                        f"Need at least {min_required:.4f} TON (amount + fees). "
                        f"Top up: {wallet_addr}"
                    )
                }

            # 2. Get seqno (0 for undeployed wallets)
            seqno = await get_seqno(session, base_url, wallet_addr, headers)
            logger.info(f"Seqno={seqno}, Deployed={state == 'active'}")

            # 3. Build transfer message
            #    For undeployed wallets (seqno=0 / state != active),
            #    tonsdk automatically includes state_init when seqno=0,
            #    which deploys the wallet contract in the same transaction.
            transfer = wallet.create_transfer_message(
                to_addr=to_address,
                amount=to_nano(amount_ton, "ton"),
                seqno=seqno,
                payload=memo,
            )

            boc = bytes_to_b64str(transfer["message"].to_boc(False))

            # 4. Broadcast
            async with session.post(
                f"{base_url}/sendBoc",
                json={"boc": boc},
                headers=headers
            ) as resp:
                result = await resp.json()

            logger.info(f"sendBoc -> {result}")
            return result

    except ImportError as e:
        return {"ok": False, "error": f"Missing library: {e}. Run: pip install tonsdk aiohttp"}
    except Exception as e:
        logger.exception("send_ton error")
        return {"ok": False, "error": str(e)}


def is_valid_ton_address(addr: str) -> bool:
    import re
    addr = addr.strip()
    if ":" in addr:
        parts = addr.split(":")
        return (
            len(parts) == 2
            and parts[0].lstrip("-").isdigit()
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", parts[1]))
        )
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
        f"3️⃣ Memo is always: `{MEMO}`\n\n"
        "*Supported address formats:*\n"
        "• `EQD...` or `UQD...` (user-friendly, 48 chars)\n"
        "• `0:abcdef...` (raw hex)\n\n"
        "Use /start for the welcome message.",
        parse_mode="Markdown"
    )


async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()

    if not is_valid_ton_address(address):
        await update.message.reply_text(
            "❌ *Invalid TON address.*\n\n"
            "Please send a valid address, for example:\n"
            "`EQD4FPq-PRDieyQKkizFTRtSDyucUIqrj0v_zXJmqaDp6_0t`",
            parse_mode="Markdown"
        )
        return

    msg = await update.message.reply_text(
        f"⏳ *Sending...*\n\n"
        f"📬 To: `{address}`\n"
        f"💎 Amount: *{AMOUNT_TON} TON*\n"
        f"📝 Memo: `{MEMO}`",
        parse_mode="Markdown"
    )

    result = await send_ton(address, AMOUNT_TON, MEMO)

    if result.get("ok"):
        await msg.edit_text(
            f"✅ *Sent successfully!*\n\n"
            f"📬 To: `{address}`\n"
            f"💎 Amount: *{AMOUNT_TON} TON*\n"
            f"📝 Memo: `{MEMO}`\n\n"
            f"_Check your wallet in a few seconds._",
            parse_mode="Markdown"
        )
        logger.info(f"SUCCESS: {AMOUNT_TON} TON -> {address}")
    else:
        error = result.get("error") or str(result.get("result", "Unknown error"))
        await msg.edit_text(
            f"❌ *Transaction failed*\n\n`{error}`",
            parse_mode="Markdown"
        )
        logger.error(f"FAILED: {error}")


# ─── STARTUP ──────────────────────────────────────────────────────────────────

async def post_init(application):
    try:
        wallet = build_wallet()
        addr   = wallet.address.to_string(True, True, False)
        print(f"\n{'='*55}")
        print(f"  Sender wallet : {addr}")
        print(f"  Network       : {NETWORK}")
        print(f"  Amount/tx     : {AMOUNT_TON} TON")
        print(f"  Memo          : {MEMO}")
        print(f"  Wallet ver    : {WALLET_VERSION}")
        print(f"{'='*55}")
        print(f"\n  ⚠️  Fund the sender wallet before sending requests!")
        print(f"  Check balance: https://{'testnet.' if NETWORK=='testnet' else ''}tonscan.org/address/{addr}\n")
    except Exception as e:
        print(f"⚠️  Startup check failed: {e}")


def main():
    if "YOUR_TELEGRAM_BOT_TOKEN" in BOT_TOKEN:
        print("❌ Please set BOT_TOKEN before running!")
        return
    if "word1" in SENDER_MNEMONIC:
        print("❌ Please set SENDER_MNEMONIC (24-word seed phrase) before running!")
        return

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))

    print(f"🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

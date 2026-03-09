"""
Telegram TON Faucet Bot
Sends 0.001 TON to provided addresses with memo "testing!!!"

Requirements:
    pip install python-telegram-bot tonsdk aiohttp

Setup:
    1. Create a bot via @BotFather on Telegram → get BOT_TOKEN
    2. Fill in BOT_TOKEN, SENDER_MNEMONIC (24 words), and optionally TONCENTER_API_KEY below
    3. Run: python ton_faucet_bot.py
"""

import asyncio
import logging
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"          # From @BotFather
SENDER_MNEMONIC = "word1 word2 ... word24"     # 24-word seed phrase of sender wallet
TONCENTER_API_KEY = ""                          # Optional: get free key at toncenter.com
AMOUNT_TON = 0.001                              # Amount to send
MEMO = "testing!!!"                            # Fixed memo/comment
NETWORK = "mainnet"                             # "mainnet" or "testnet"
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# TON Center API endpoints
TONCENTER_BASE = {
    "mainnet": "https://toncenter.com/api/v2",
    "testnet": "https://testnet.toncenter.com/api/v2",
}

# ─── TON TRANSACTION LOGIC ────────────────────────────────────────────────────

async def send_ton(to_address: str, amount_ton: float, memo: str) -> dict:
    """Send TON using TON Center API + tonsdk for signing."""
    try:
        import aiohttp
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.utils import to_nano, bytes_to_b64str
        from tonsdk.crypto import mnemonic_to_wallet_key

        # Derive keys from mnemonic
        mnemonics = SENDER_MNEMONIC.strip().split()
        _mnemonics, pub_key, priv_key, wallet = Wallets.from_mnemonics(
            mnemonics, WalletVersionEnum.v4r2, workchain=0
        )

        base_url = TONCENTER_BASE[NETWORK]
        headers = {"Content-Type": "application/json"}
        if TONCENTER_API_KEY:
            headers["X-API-Key"] = TONCENTER_API_KEY

        async with aiohttp.ClientSession() as session:
            # Get current wallet seqno
            wallet_address = wallet.address.to_string(True, True, False)
            async with session.get(
                f"{base_url}/runGetMethod",
                params={"address": wallet_address, "method": "seqno", "stack": "[]"},
                headers=headers
            ) as resp:
                data = await resp.json()
                if data.get("ok") and data["result"]["exit_code"] == 0:
                    seqno = int(data["result"]["stack"][0][1], 16)
                else:
                    seqno = 0

            # Build transfer
            transfer = wallet.create_transfer_message(
                to_addr=to_address,
                amount=to_nano(amount_ton, "ton"),
                seqno=seqno,
                payload=memo,
            )

            boc = bytes_to_b64str(transfer["message"].to_boc(False))

            # Broadcast
            async with session.post(
                f"{base_url}/sendBoc",
                json={"boc": boc},
                headers=headers
            ) as resp:
                result = await resp.json()
                return result

    except ImportError as e:
        return {"ok": False, "error": f"Missing dependency: {e}. Run: pip install tonsdk aiohttp"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def is_valid_ton_address(address: str) -> bool:
    """Basic TON address validation."""
    address = address.strip()
    # Raw format: workchain:hex (e.g. 0:abc123...)
    if ":" in address:
        parts = address.split(":")
        return len(parts) == 2 and parts[0].lstrip("-").isdigit() and len(parts[1]) == 64
    # Friendly format: base64url, 48 chars
    import re
    return bool(re.match(r'^[A-Za-z0-9+/\-_]{48}$', address))


# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *TON Faucet Bot*\n\n"
        f"Send me a TON wallet address and I'll transfer *{AMOUNT_TON} TON* to it "
        f"with memo: `{MEMO}`\n\n"
        "Just paste your TON address below!",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *How to use:*\n\n"
        "1. Send your TON wallet address (EQ..., UQ..., or raw format)\n"
        f"2. The bot will send *{AMOUNT_TON} TON* to that address\n"
        f"3. Memo will be: `{MEMO}`\n\n"
        "Example address formats:\n"
        "• `EQD...` (user-friendly)\n"
        "• `0:abcd1234...` (raw)\n\n"
        "Use /start to see the welcome message.",
        parse_mode="Markdown"
    )

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()

    if not is_valid_ton_address(address):
        await update.message.reply_text(
            "❌ That doesn't look like a valid TON address.\n\n"
            "Please send a valid TON wallet address (e.g., `EQD...` or `0:abc...`)",
            parse_mode="Markdown"
        )
        return

    # Send processing message
    processing_msg = await update.message.reply_text(
        f"⏳ Processing...\n"
        f"Sending *{AMOUNT_TON} TON* to `{address}`\n"
        f"Memo: `{MEMO}`",
        parse_mode="Markdown"
    )

    logger.info(f"Sending {AMOUNT_TON} TON to {address}")
    result = await send_ton(address, AMOUNT_TON, MEMO)

    if result.get("ok"):
        await processing_msg.edit_text(
            f"✅ *Transaction sent!*\n\n"
            f"📬 To: `{address}`\n"
            f"💎 Amount: *{AMOUNT_TON} TON*\n"
            f"📝 Memo: `{MEMO}`\n\n"
            f"Check your wallet in a few seconds.",
            parse_mode="Markdown"
        )
        logger.info(f"Success: sent {AMOUNT_TON} TON to {address}")
    else:
        error = result.get("error", result.get("result", "Unknown error"))
        await processing_msg.edit_text(
            f"❌ *Transaction failed*\n\n"
            f"Error: `{error}`\n\n"
            f"Please try again later or contact support.",
            parse_mode="Markdown"
        )
        logger.error(f"Failed to send TON: {error}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("❌ Please set your BOT_TOKEN in the script before running!")
        return
    if "word1" in SENDER_MNEMONIC:
        print("❌ Please set your SENDER_MNEMONIC (24-word seed phrase) before running!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))

    print(f"🤖 TON Faucet Bot is running ({NETWORK})")
    print(f"   Sends: {AMOUNT_TON} TON per request")
    print(f"   Memo:  {MEMO}")
    app.run_polling()

if __name__ == "__main__":
    main()
import os
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient

from pytonconnect import TonConnect
from pytonconnect.storage import IStorage
from pytonconnect.exceptions import TonConnectError

# ---------- Load .env ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

if not BOT_TOKEN or not MONGO_URL:
    raise ValueError("Missing BOT_TOKEN or MONGO_URL")

# ---------- MongoDB ----------
client = AsyncIOMotorClient(MONGO_URL)
db = client["pc_slicer_bot"]
users_collection = db["users"]

# ---------- Constants ----------
MINI_APP_URL = "https://caesarwbas.github.io/hardware-ninja-game/"
MANIFEST_URL = "https://caesarwbas.github.io/hardware-ninja-game/tonconnect-manifest.json"
COOLDOWN_HOURS = 6

# ---------- PyTonConnect storage adapter using MongoDB ----------
class MongoTonStorage(IStorage):
    """Persist TON Connect sessions in MongoDB."""
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.collection = db["tonconnect_sessions"]

    async def set(self, key: str, value: str):
        await self.collection.update_one(
            {"user_id": self.user_id, "key": key},
            {"$set": {"value": value}},
            upsert=True
        )

    async def get(self, key: str) -> Optional[str]:
        doc = await self.collection.find_one({"user_id": self.user_id, "key": key})
        return doc["value"] if doc else None

    async def remove(self, key: str):
        await self.collection.delete_one({"user_id": self.user_id, "key": key})

    async def get_all(self) -> Dict[str, str]:
        cursor = self.collection.find({"user_id": self.user_id})
        return {doc["key"]: doc["value"] async for doc in cursor}

# ---------- Bot setup ----------
bot = Bot(token=BOT_TOKEN)
router = Router()

# ---------- Helper: get or create user ----------
async def get_or_create_user(user_id: int) -> dict:
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "points": 0,
            "last_play": None,
            "wallet_address": None
        }
        await users_collection.insert_one(user)
    return user

# ---------- /start command ----------
@router.message(Command("start"))
async def start_handler(message: types.Message):
    user = await get_or_create_user(message.from_user.id)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🎮 PLAY NOW",
            web_app=WebAppInfo(url=MINI_APP_URL)
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔗 Link TON Wallet",
            callback_data="link_wallet"
        )
    )

    wallet_line = ""
    if user.get("wallet_address"):
        short_addr = user["wallet_address"][:6] + "..." + user["wallet_address"][-4:]
        wallet_line = f"\n💼 Wallet: `{short_addr}`"

    await message.answer(
        "⚡ *PC Component Slicer* ⚡\n\n"
        "Slice PC parts, earn points!\n"
        f"Points: *{user['points']}*{wallet_line}",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )

# ---------- Link wallet callback ----------
@router.callback_query(lambda c: c.data == "link_wallet")
async def link_wallet_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user = await get_or_create_user(user_id)

    # Create TON Connect instance with MongoDB storage
    storage = MongoTonStorage(user_id)
    connector = TonConnect(manifest_url=MANIFEST_URL, storage=storage)

    # Generate universal link
    wallets = await connector.get_wallets()
    # Use Tonkeeper as default, or pick the first available
    tonkeeper = next((w for w in wallets if w["app_name"] == "Tonkeeper"), wallets[0])
    generated_url = await connector.connect(tonkeeper)

    # Save the connector instance temporarily? Not needed; state is in storage.
    # The user will be redirected; we'll handle the return via polling or a separate callback.
    # For simplicity, we store the connector in a dict (or rely on storage).
    # Here we'll store the user's intent to connect.
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"awaiting_wallet_connection": True}}
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Open Tonkeeper", url=generated_url)],
        [InlineKeyboardButton(text="✅ I've Connected", callback_data="check_wallet")]
    ])

    await callback.message.edit_text(
        "🔗 *Connect your TON Wallet*\n\n"
        "1. Click the button below to open Tonkeeper.\n"
        "2. Approve the connection in the wallet.\n"
        "3. Return here and press *I've Connected*.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()

# ---------- Check wallet connection ----------
@router.callback_query(lambda c: c.data == "check_wallet")
async def check_wallet_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    storage = MongoTonStorage(user_id)
    connector = TonConnect(manifest_url=MANIFEST_URL, storage=storage)

    try:
        # Restore session
        await connector.restore_connection()
        if connector.connected and connector.wallet:
            wallet_address = connector.wallet.account.address

            # Save to user profile
            await users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"wallet_address": wallet_address}}
            )

            short_addr = wallet_address[:6] + "..." + wallet_address[-4:]
            await callback.message.edit_text(
                f"✅ Wallet connected!\n`{short_addr}`",
                parse_mode="Markdown"
            )
        else:
            await callback.message.edit_text(
                "❌ Not connected yet. Please open the link and approve in Tonkeeper, then try again.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Try Again", callback_data="link_wallet")]
                ])
            )
    except TonConnectError as e:
        await callback.message.edit_text(
            f"⚠️ Error: {str(e)}\nPlease try linking again.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Link Wallet", callback_data="link_wallet")]
            ])
        )
    await callback.answer()

# ---------- Optional: command to disconnect ----------
@router.message(Command("disconnect"))
async def disconnect_wallet(message: types.Message):
    user_id = message.from_user.id
    storage = MongoTonStorage(user_id)
    connector = TonConnect(manifest_url=MANIFEST_URL, storage=storage)

    try:
        await connector.restore_connection()
        if connector.connected:
            await connector.disconnect()
        await users_collection.update_one(
            {"user_id": user_id},
            {"$set": {"wallet_address": None}}
        )
        await message.answer("🔓 Wallet disconnected.")
    except Exception as e:
        await message.answer(f"Error: {e}")

# ---------- WebAppData handler (score submission) – unchanged from previous implementation ----------
@router.message(lambda msg: msg.web_app_data is not None)
async def web_app_data_handler(message: types.Message):
    import json
    user_id = message.from_user.id
    user = await get_or_create_user(user_id)

    try:
        data = json.loads(message.web_app_data.data)
        score = int(data.get("score", 0))
    except:
        await message.answer("❌ Invalid data received from game.")
        return

    # Cooldown check (6 hours)
    if user.get("last_play"):
        elapsed = datetime.utcnow() - user["last_play"]
        if elapsed < timedelta(hours=COOLDOWN_HOURS):
            remaining = timedelta(hours=COOLDOWN_HOURS) - elapsed
            h = remaining.seconds // 3600
            m = (remaining.seconds % 3600) // 60
            s = remaining.seconds % 60
            await message.answer(f"⏳ Cooldown: {h}h {m}m {s}s remaining.")
            return

    new_points = user["points"] + score
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"points": new_points, "last_play": datetime.utcnow()}}
    )

    await message.answer(
        f"✅ Score +{score}!\nTotal: *{new_points}* points.",
        parse_mode="Markdown"
    )

# ---------- Main ----------
async def main():
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
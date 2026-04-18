import os
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import uvicorn

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from motor.motor_asyncio import AsyncIOMotorClient


BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

if not BOT_TOKEN or not MONGO_URL:
    raise ValueError("请在 .env 文件中设置 BOT_TOKEN 和 MONGO_URL")

client = AsyncIOMotorClient(MONGO_URL)
db = client["ninjakumbi"]
users_collection = db["users"]

MINI_APP_URL = "https://caesarwbas.github.io/hardware-ninja-game/"
COOLDOWN_HOURS = 6

@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    router = Router()

    async def get_user(user_id: int):
        return await users_collection.find_one({"user_id": user_id})

    async def create_user(user_id: int):
        user = {
            "user_id": user_id,
            "balance": 0,
            "last_play": None,
            "last_claim": datetime.utcnow(),
            "upgrades": {"cpu": 1, "gpu": 1, "rig": 1}
        }
        await users_collection.insert_one(user)
        return user

    @router.message(Command("start"))
    async def start_handler(message: types.Message):
        user_id = message.from_user.id
        user = await get_user(user_id)
        if not user:
            user = await create_user(user_id)

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🎮 开始游戏", web_app=WebAppInfo(url=MINI_APP_URL)))
        await message.answer(
            f"⚡ <b>Ninja KumBI</b> ⚡\n\n"
            f"欢迎，{message.from_user.first_name}！\n"
            f"当前余额: <b>{user['balance']}</b> NNJA\n\n"
            f"每 6 小时可玩一次切片游戏，采矿可离线累积收益。",
            reply_markup=builder.as_markup()
        )

    dp.include_router(router)
    app.state.bot = bot
    import asyncio
    asyncio.create_task(dp.start_polling(bot))
    yield
    await dp.stop_polling()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# ---------- 辅助函数 ----------
base_profits = {"cpu": 10, "gpu": 25, "rig": 50}
base_costs = {"cpu": 50, "gpu": 120, "rig": 300}

def calc_profit_per_hour(upgrades: dict) -> int:
    return upgrades["cpu"] * base_profits["cpu"] + upgrades["gpu"] * base_profits["gpu"] + upgrades["rig"] * base_profits["rig"]

def calc_upgrade_cost(card: str, current_level: int) -> int:
    return int(base_costs[card] * (1.5 ** (current_level - 1)))

# ---------- API 端点 ----------
@app.get("/api/user/{user_id}")
async def get_user_data(user_id: int):
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        user = {"user_id": user_id, "balance": 0, "last_claim": datetime.utcnow(), "upgrades": {"cpu":1,"gpu":1,"rig":1}}
        await users_collection.insert_one(user)
    user["_id"] = str(user["_id"])
    return user

@app.post("/api/upgrade")
async def upgrade_card(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    card = data.get("card")
    cost = data.get("cost")
    new_level = data.get("new_level")
    if not all([user_id, card, cost, new_level]) or card not in base_costs:
        raise HTTPException(400, "参数错误")
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        raise HTTPException(404, "用户不存在")
    if user["balance"] < cost:
        return JSONResponse({"success": False, "message": "余额不足"}, status_code=400)
    current_level = user["upgrades"].get(card, 1)
    expected_cost = calc_upgrade_cost(card, current_level)
    if cost != expected_cost or new_level != current_level + 1:
        return JSONResponse({"success": False, "message": "升级数据不一致"}, status_code=400)
    new_balance = user["balance"] - cost
    user["upgrades"][card] = new_level
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"balance": new_balance, f"upgrades.{card}": new_level}}
    )
    return {"success": True, "new_balance": new_balance, "new_level": new_level}

@app.post("/api/claim")
async def claim_rewards(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        raise HTTPException(404, "用户不存在")
    now = datetime.utcnow()
    last_claim = user.get("last_claim", now)
    elapsed_hours = (now - last_claim).total_seconds() / 3600
    profit_per_hour = calc_profit_per_hour(user["upgrades"])
    earned = int(profit_per_hour * elapsed_hours)
    new_balance = user["balance"] + earned
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"balance": new_balance, "last_claim": now}}
    )
    return {"success": True, "earned": earned, "new_balance": new_balance}

@app.post("/api/update-balance")
async def update_balance(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    score = data.get("score")
    if not user_id or score is None:
        raise HTTPException(400, "缺少参数")
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        user = {"user_id": user_id, "balance": 0, "last_play": None, "upgrades": {"cpu":1,"gpu":1,"rig":1}}
        await users_collection.insert_one(user)
    if user.get("last_play"):
        elapsed = datetime.utcnow() - user["last_play"]
        if elapsed < timedelta(hours=COOLDOWN_HOURS):
            remaining = int((timedelta(hours=COOLDOWN_HOURS) - elapsed).total_seconds())
            return JSONResponse({"success": False, "message": f"冷却中，还需 {remaining} 秒", "remaining_seconds": remaining}, status_code=403)
    new_balance = user["balance"] + score
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"balance": new_balance, "last_play": datetime.utcnow()}}
    )
    return {"success": True, "new_balance": new_balance, "score_added": score}

@app.get("/")
async def root():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=8000, reload=True)

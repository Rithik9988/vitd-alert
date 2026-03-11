"""
VitD Alert — Backend Server
Handles user registration and sends automated UV push notifications
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
import os
from supabase import create_client
from datetime import datetime
import pytz

app = FastAPI()

# Allow frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Supabase ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Models ---
class UserProfile(BaseModel):
    name: str
    lat: float
    lon: float
    skin_tone: str        # light / medium / dark
    ntfy_topic: str       # unique topic they subscribe to
    city: str = "Unknown"


# --- Helpers ---
def get_duration(uv: float, skin: str) -> int:
    mult = {"light": 1.0, "medium": 1.5, "dark": 2.0}.get(skin, 1.5)
    d = int(15 * mult * (5 / max(uv, 1)))
    return max(10, min(d, 40))


async def get_uv(lat: float, lon: float) -> float:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "uv_index",
        "timezone": "Asia/Kolkata",
        "forecast_days": 1,
    }
    async with httpx.AsyncClient() as client:
        res = await client.get(url, params=params)
        data = res.json()
        ist = pytz.timezone("Asia/Kolkata")
        hour = datetime.now(ist).hour
        return data["hourly"]["uv_index"][hour]


async def send_ntfy(topic: str, title: str, message: str, tags: str, priority: str = "default"):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://ntfy.sh/{topic}",
            content=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Tags": tags,
                "Priority": priority,
                "Content-Type": "text/plain; charset=utf-8",
            }
        )


# --- Routes ---
@app.get("/")
def root():
    return {"status": "VitD Alert is running!"}


@app.post("/register")
async def register_user(profile: UserProfile):
    """Register a new user or update existing"""
    # Upsert by ntfy_topic
    data = {
        "name": profile.name,
        "lat": profile.lat,
        "lon": profile.lon,
        "skin_tone": profile.skin_tone,
        "ntfy_topic": profile.ntfy_topic,
        "city": profile.city,
        "active": True,
    }
    result = supabase.table("users").upsert(data, on_conflict="ntfy_topic").execute()

    # Send welcome notification
    await send_ntfy(
        topic=profile.ntfy_topic,
        title=f"Welcome to VitD Alert, {profile.name}! ☀️",
        message=(
            f"You're all set! 🎉\n\n"
            f"📍 Location: {profile.city}\n"
            f"🎨 Skin tone: {profile.skin_tone.title()}\n\n"
            f"You'll receive alerts every time UV is ideal for Vitamin D synthesis "
            f"between 9 AM – 5 PM daily. No action needed — just go outside when we ping you! 💪"
        ),
        tags="sun,white_check_mark",
        priority="default"
    )
    return {"success": True, "message": f"Welcome {profile.name}!"}


@app.get("/users")
def get_users():
    """Get all active users (admin)"""
    result = supabase.table("users").select("*").eq("active", True).execute()
    return result.data


# --- Scheduler: runs every 30 mins ---
async def check_all_users():
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    hour = now.hour
    time_str = now.strftime("%I:%M %p")

    print(f"\n[{time_str}] Running UV check for all users...")

    # Only run between 9 AM and 5 PM IST
    if hour < 9 or hour >= 17:
        print("Outside active hours. Skipping.")
        return

    # Fetch all active users
    result = supabase.table("users").select("*").eq("active", True).execute()
    users = result.data

    print(f"Checking {len(users)} users...")

    for user in users:
        try:
            uv = await get_uv(user["lat"], user["lon"])
            name = user["name"]
            skin = user["skin_tone"]
            topic = user["ntfy_topic"]
            city = user.get("city", "your area")

            print(f"  {name} ({city}): UV={uv:.1f}")

            if uv < 3:
                # Only notify at 9 AM if UV is low
                if hour == 9:
                    await send_ntfy(
                        topic=topic,
                        title=f"☁️ Low UV Today, {name}",
                        message=f"UV is only {uv:.1f} in {city} right now. We'll keep checking and alert you when it's ideal! 💪",
                        tags="cloud",
                        priority="low"
                    )

            elif 3 <= uv <= 7:
                duration = get_duration(uv, skin)
                await send_ntfy(
                    topic=topic,
                    title=f"☀️ Go Get Your Vit D, {name}!",
                    message=(
                        f"Perfect UV window right now in {city}!\n\n"
                        f"UV Index: {uv:.1f} (Ideal: 3–7)\n"
                        f"Go outside for {duration} minutes\n"
                        f"Expose arms & legs to sunlight\n"
                        f"Checked at {time_str}\n\n"
                        f"Your body will synthesize Vitamin D naturally. Don't miss this window!"
                    ),
                    tags="sun,muscle,white_check_mark",
                    priority="high"
                )

            else:
                await send_ntfy(
                    topic=topic,
                    title=f"🔥 High UV Alert, {name}!",
                    message=(
                        f"UV is very intense in {city} right now.\n\n"
                        f"UV Index: {uv:.1f} (High — risk of sunburn)\n"
                        f"Max 10 minutes outside\n"
                        f"Apply SPF 30+ sunscreen\n"
                        f"Checked at {time_str}\n\n"
                        f"Tip: Try before 10 AM or after 4 PM for safer Vit D."
                    ),
                    tags="fire,warning",
                    priority="urgent"
                )

        except Exception as e:
            print(f"  Error for {user.get('name')}: {e}")


scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

@app.on_event("startup")
async def startup():
    scheduler.add_job(check_all_users, "interval", minutes=30)
    scheduler.start()
    print("Scheduler started — checking UV every 30 minutes!")

import discord, requests, os, json, asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discord_bot")

TOKEN = os.getenv("DISCORD_TOKEN")
ODYSSEUS_ADMIN_USER = os.getenv("ODYSSEUS_ADMIN_USER", "admin")
ODYSSEUS_ADMIN_PASSWORD = os.getenv("ODYSSEUS_ADMIN_PASSWORD")
BASE_URL = "http://odysseus:7000"
ENDPOINT_ID = "f9f3acbe"
MODEL = "qwen-agent:latest"
CHANNEL_ID = 1515647781786877952

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not set")
if not ODYSSEUS_ADMIN_PASSWORD:
    raise SystemExit("ODYSSEUS_ADMIN_PASSWORD not set")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
session = requests.Session()
user_sessions = {}

def login():
    try:
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": ODYSSEUS_ADMIN_USER, "password": ODYSSEUS_ADMIN_PASSWORD, "remember": True},
        )
        if r.status_code != 200:
            logger.error(f"Login failed with status {r.status_code}")
        else:
            logger.info("Login succeeded")
    except Exception:
        logger.exception("Login request failed")

def new_chat():
    r = session.post(f"{BASE_URL}/api/session", data={"name":"Discord","endpoint_id":ENDPOINT_ID,"model":MODEL})
    r.raise_for_status()
    return r.json()["id"]

def ask(sid, prompt):
    r = session.post(f"{BASE_URL}/api/chat_stream", data={"message":prompt,"session":sid,"mode":"agent","allow_web_search":"true","allow_bash":"true","use_rag":"false","preset_id":"custom"}, stream=True, timeout=120)
    r.raise_for_status()
    out = ""
    for line in r.iter_lines():
        if line:
            l = line.decode("utf-8")
            if l.startswith("data:"):
                c = l[5:].strip()
                if c and c != "[DONE]":
                    try:
                        obj = json.loads(c)
                        if "delta" in obj:
                            out += obj["delta"]
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse stream chunk: {c[:100]}")
    return out.strip()

@client.event
async def on_ready():
    logger.info(f"Ready: {client.user}")
    await asyncio.get_event_loop().run_in_executor(None, login)

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.channel.id != CHANNEL_ID:
        return
    if message.content.startswith("!new"):
        user_sessions.pop(message.author.id, None)
        await message.channel.send("New conversation started!")
        return
    if message.content.startswith("!"):
        return

    prompt = message.content
    await message.channel.typing()
    try:
        uid = message.author.id
        if uid not in user_sessions:
            user_sessions[uid] = await asyncio.get_event_loop().run_in_executor(None, new_chat)
        sid = user_sessions[uid]
        reply = await asyncio.get_event_loop().run_in_executor(None, lambda: ask(sid, prompt))
        if not reply:
            reply = "Empty response"
        if len(reply) > 2000:
            for i in range(0, len(reply), 2000):
                await message.channel.send(reply[i:i+2000])
        else:
            await message.channel.send(reply)
    except requests.exceptions.RequestException:
        logger.exception(f"API request failed for user {message.author.id}")
        await message.channel.send("Sorry, something went wrong talking to Odysseus. Try again in a moment.")
    except Exception:
        logger.exception(f"Unexpected error handling message from {message.author.id}")
        await message.channel.send("Something went wrong on my end.")

client.run(TOKEN, log_handler=None)

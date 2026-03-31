import discord
from discord import app_commands, ui
from discord.ext import commands
import os
import json
import random
import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import aiohttp as _aiohttp
from dotenv import load_dotenv

load_dotenv()

# AI personality
SNEEZE_PERSONALITY = (
    "You are Sneeze, a Discord bot with a witty, sarcastic, and slightly unhinged personality. "
    "Keep responses short (1-3 sentences max). Be funny, roast people lightly, "
    "but never be actually mean or offensive. You're like a chaotic friend who always has a comeback. "
    "Never say you're an AI or language model. You're just Sneeze, the bot."
)


async def ask_ai(prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_KEY')}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "google/gemini-2.0-flash-001",
        "messages": [
            {"role": "system", "content": SNEEZE_PERSONALITY},
            {"role": "user", "content": prompt}
        ]
    }
    async with _aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers) as resp:
            data = await resp.json()
            print(f"AI response: {data}")
            return data["choices"][0]["message"]["content"]

intents = discord.Intents.all()

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None, member_cache_flags=discord.MemberCacheFlags.all())

# ---------- GLOBALS ----------
COOLDOWN_SECONDS = 60
XP_COOLDOWN = {}
VC_TRACKING = {}
ACTIVE_VCS = {}  # channel_id: {"owner": user_id, "locked": False, "banned": []}
INVITE_CACHE = {}  # guild_id: {invite_code: uses}
INVITE_TRACKER_CHANNEL = 1478778935746756739


# ============================================================
#  DATA HELPERS
# ============================================================

# In-memory caches
LEVELS_CACHE = None
CONFIG_CACHE = None
SAVE_COUNTER = 0


def load_levels():
    global LEVELS_CACHE
    if LEVELS_CACHE is not None:
        return LEVELS_CACHE
    try:
        with open("levels.json", "r") as f:
            LEVELS_CACHE = json.load(f)
    except FileNotFoundError:
        LEVELS_CACHE = {}
    return LEVELS_CACHE


def save_levels(data):
    global LEVELS_CACHE, SAVE_COUNTER
    LEVELS_CACHE = data
    SAVE_COUNTER += 1
    # Only write to disk every 10 updates to reduce I/O
    if SAVE_COUNTER >= 10:
        SAVE_COUNTER = 0
        with open("levels.json", "w") as f:
            json.dump(data, f, indent=4)


def force_save_levels():
    global SAVE_COUNTER
    if LEVELS_CACHE is not None:
        SAVE_COUNTER = 0
        with open("levels.json", "w") as f:
            json.dump(LEVELS_CACHE, f, indent=4)


def load_config():
    global CONFIG_CACHE
    if CONFIG_CACHE is not None:
        return CONFIG_CACHE
    try:
        with open("config.json", "r") as f:
            CONFIG_CACHE = json.load(f)
    except FileNotFoundError:
        CONFIG_CACHE = {}
    return CONFIG_CACHE


def save_config(data):
    global CONFIG_CACHE
    CONFIG_CACHE = data
    with open("config.json", "w") as f:
        json.dump(data, f, indent=4)


def get_guild_config(guild_id):
    config = load_config()
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {
            "jail_role": None,
            "ir_role": None,
            "booster_role": None,
            "j2c_channel": None,
            "image_level": 5,
            "log_channel": None,
            "default_timeout": 10
        }
        save_config(config)
    return config[gid]


def update_guild_config(guild_id, key, value):
    config = load_config()
    gid = str(guild_id)
    if gid not in config:
        get_guild_config(guild_id)
        config = load_config()
    config[gid][key] = value
    save_config(config)


def xp_needed(level):
    return 100 * (level ** 2)


def get_rank(user_id, levels):
    sorted_users = sorted(levels.items(), key=lambda x: x[1]["xp"], reverse=True)
    for i, (uid, data) in enumerate(sorted_users, 1):
        if uid == user_id:
            return i
    return 0


def ensure_user(levels, user_id):
    if user_id not in levels:
        levels[user_id] = {"xp": 0, "level": 0, "vc_minutes": 0}
    if "vc_minutes" not in levels[user_id]:
        levels[user_id]["vc_minutes"] = 0
    return levels


# ============================================================
#  IMAGE GENERATION
# ============================================================

def get_font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def make_progress_bar(progress, length=10):
    filled = int(progress * length)
    empty = length - filled
    return "**" + "\u2588" * filled + "**" + "\u2591" * empty


def build_levelup_embed(member, new_level, old_level, total_xp):
    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(
        description=(
            f"@{member.display_name} is now **Level {new_level}**\n"
            f"\u2022 Previous: **Level {old_level}**\n"
            f"\u2022 Total XP: **{total_xp}**"
        ),
        color=theme
    )
    embed.set_author(name=f"{member.display_name}  \u2022  Level {new_level}", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.with_size(256).url)
    return embed


# ============================================================
#  HELP MENU WITH DROPDOWN
# ============================================================

class HelpDropdown(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="home", description="back to main page"),
            discord.SelectOption(label="general", description="utility and info"),
            discord.SelectOption(label="setup", description="server setup"),
            discord.SelectOption(label="staff", description="moderation and vc"),
        ]
        super().__init__(placeholder="Choose a category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]

        theme = discord.Color.from_str("#1a501a")

        if category == "home":
            embed = discord.Embed(
                title="information",
                description=(
                    "> `[ ]` = optional, `< >` = required\n\n"
                    "This bot handles moderation, utility, leveling, "
                    "voice controls, and more.\n\n"
                    "Choose a category from the dropdown below."
                ),
                color=theme
            )
            embed.set_author(name=interaction.client.user.display_name, icon_url=interaction.client.user.display_avatar.url)
            embed.set_thumbnail(url=interaction.client.user.display_avatar.url)
            await interaction.response.edit_message(embed=embed)
            return

        if category == "general":
            description = (
                "**.help**\n> Open the help menu.\n\n"
                "**.ping**\n> Show bot latency.\n\n"
                "**.xp `[@user]`**\n> Show your XP.\n\n"
                "**.lb**\n> Show the leaderboard.\n\n"
                "**.av `[@user]`**\n> Show a user avatar.\n\n"
                "**.userinfo `[@user]`**\n> Show user information.\n\n"
                "**.serverinfo**\n> Show server information.\n\n"
                "**.membercount**\n> Show member counts.\n\n"
                "**.roleinfo `<@role>`**\n> Show role information."
            )
        elif category == "setup":
            description = (
                "**.setjail `<@role/id>`**\n> Set the jail role.\n\n"
                "**.setir `<@role/id>`**\n> Set the image restriction role.\n\n"
                "**.setbooster `<@role/id>`**\n> Set the booster role.\n\n"
                "**.setj2c `<#vc/id>`**\n> Set the join-to-create VC.\n\n"
                "**.setimglevel `<level>`**\n> Set minimum level to send images.\n\n"
                "**.setlogs `<#channel>`**\n> Set the command log channel."
            )
        elif category == "staff":
            description = (
                "**.jm `<@user>`**\n> Admin only. Toggle jail role.\n\n"
                "**.ir `<@user>`**\n> Admin only. Toggle image restriction role.\n\n"
                "**.to `<@user>` `[mins]`**\n> Admin only. Toggle timeout.\n\n"
                "**.purge `<amount>`**\n> Admin only. Delete up to 100 messages.\n\n"
                "**.lq**\n> Admin only. Reply to a message to delete it quickly.\n\n"
                "**.banvc `<@user>`**\n> Admin only. Ban user from current VC.\n\n"
                "**.unbanvc `<@user>`**\n> Admin only. Remove VC ban.\n\n"
                "**.lock**\n> Admin only. Lock the current text channel.\n\n"
                "**.unlock**\n> Admin only. Unlock the current text channel.\n\n"
                "**.vckick `<@user>`**\n> Kick a user from your owned VC.\n\n"
                "**.vclock**\n> Lock your owned VC.\n\n"
                "**.vcunlock**\n> Unlock your owned VC.\n\n"
                "**.vcpermit `<@user>`**\n> Permit a user into your locked VC.\n\n"
                "**.vcname `<name>`**\n> Rename your owned VC.\n\n"
                "**.vclimit `<0-99>`**\n> Set user limit on your VC.\n\n"
                "**.vcclaim**\n> Claim an ownerless VC."
            )

        embed = discord.Embed(
            title=category,
            description=f"> `[ ]` = optional, `< >` = required\n\n{description}",
            color=theme
        )
        embed.set_author(name=interaction.client.user.display_name, icon_url=interaction.client.user.display_avatar.url)
        embed.set_thumbnail(url=interaction.client.user.display_avatar.url)

        await interaction.response.edit_message(embed=embed)


class HelpView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpDropdown())


# ============================================================
#  VOICE CONTROL VIEWS (J2C)
# ============================================================

class VCNameModal(ui.Modal, title="Rename Voice Channel"):
    new_name = ui.TextInput(label="New Name", placeholder="Enter new VC name...", max_length=100)

    def __init__(self, vc_channel):
        super().__init__()
        self.vc_channel = vc_channel

    async def on_submit(self, interaction: discord.Interaction):
        await self.vc_channel.edit(name=str(self.new_name))
        await interaction.response.send_message(f"VC renamed to **{self.new_name}**.", ephemeral=True)


class VCLimitModal(ui.Modal, title="Set Custom User Limit"):
    limit = ui.TextInput(label="User Limit (0-99, 0 = unlimited)", placeholder="0", max_length=2)

    def __init__(self, vc_channel):
        super().__init__()
        self.vc_channel = vc_channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(str(self.limit))
            if val < 0 or val > 99:
                await interaction.response.send_message("Must be between 0 and 99.", ephemeral=True)
                return
            await self.vc_channel.edit(user_limit=val)
            label = "Unlimited" if val == 0 else str(val)
            await interaction.response.send_message(f"User limit set to **{label}**.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


class VCControlView(ui.View):
    def __init__(self, vc_channel, owner):
        super().__init__(timeout=None)
        self.vc_channel = vc_channel
        self.owner_id = owner.id

    async def owner_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the VC owner can use these controls.", ephemeral=True)
            return False
        return True

    @ui.button(label="Name", style=discord.ButtonStyle.primary, row=0)
    async def name_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(VCNameModal(self.vc_channel))

    @ui.button(label="Status", style=discord.ButtonStyle.primary, row=0)
    async def status_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        vc_data = ACTIVE_VCS.get(self.vc_channel.id, {})
        locked = vc_data.get("locked", False)
        status = "Locked" if locked else "none"
        limit = self.vc_channel.user_limit
        limit_text = "Unlimited" if limit == 0 else str(limit)
        embed = discord.Embed(title="VC Status", color=discord.Color.blurple())
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="User Limit", value=limit_text, inline=True)
        embed.add_field(name="Members", value=str(len(self.vc_channel.members)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="Speak", style=discord.ButtonStyle.danger, row=0)
    async def speak_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        overwrite = self.vc_channel.overwrites_for(interaction.guild.default_role)
        current = overwrite.speak
        if current is False:
            overwrite.speak = None
            await self.vc_channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            await interaction.response.send_message("Speaking **unlocked** for everyone.", ephemeral=True)
        else:
            overwrite.speak = False
            await self.vc_channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            await interaction.response.send_message("Speaking **locked**. Only permitted users can speak.", ephemeral=True)

    @ui.button(label="Bump", style=discord.ButtonStyle.secondary, row=0)
    async def bump_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        try:
            await self.vc_channel.edit(position=0)
            await interaction.response.send_message("VC bumped to top.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Could not bump VC.", ephemeral=True)

    @ui.button(label="+1", style=discord.ButtonStyle.secondary, row=1)
    async def plus1(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = min(self.vc_channel.user_limit + 1, 99)
        await self.vc_channel.edit(user_limit=new)
        await interaction.response.send_message(f"Limit: **{new}**", ephemeral=True)

    @ui.button(label="+2", style=discord.ButtonStyle.secondary, row=1)
    async def plus2(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = min(self.vc_channel.user_limit + 2, 99)
        await self.vc_channel.edit(user_limit=new)
        await interaction.response.send_message(f"Limit: **{new}**", ephemeral=True)

    @ui.button(label="+5", style=discord.ButtonStyle.secondary, row=1)
    async def plus5(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = min(self.vc_channel.user_limit + 5, 99)
        await self.vc_channel.edit(user_limit=new)
        await interaction.response.send_message(f"Limit: **{new}**", ephemeral=True)

    @ui.button(label="+10", style=discord.ButtonStyle.secondary, row=1)
    async def plus10(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = min(self.vc_channel.user_limit + 10, 99)
        await self.vc_channel.edit(user_limit=new)
        await interaction.response.send_message(f"Limit: **{new}**", ephemeral=True)

    @ui.button(label="-1", style=discord.ButtonStyle.secondary, row=2)
    async def minus1(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = max(self.vc_channel.user_limit - 1, 0)
        await self.vc_channel.edit(user_limit=new)
        label = "Unlimited" if new == 0 else str(new)
        await interaction.response.send_message(f"Limit: **{label}**", ephemeral=True)

    @ui.button(label="-2", style=discord.ButtonStyle.secondary, row=2)
    async def minus2(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = max(self.vc_channel.user_limit - 2, 0)
        await self.vc_channel.edit(user_limit=new)
        label = "Unlimited" if new == 0 else str(new)
        await interaction.response.send_message(f"Limit: **{label}**", ephemeral=True)

    @ui.button(label="-5", style=discord.ButtonStyle.secondary, row=2)
    async def minus5(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = max(self.vc_channel.user_limit - 5, 0)
        await self.vc_channel.edit(user_limit=new)
        label = "Unlimited" if new == 0 else str(new)
        await interaction.response.send_message(f"Limit: **{label}**", ephemeral=True)

    @ui.button(label="-10", style=discord.ButtonStyle.secondary, row=2)
    async def minus10(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        new = max(self.vc_channel.user_limit - 10, 0)
        await self.vc_channel.edit(user_limit=new)
        label = "Unlimited" if new == 0 else str(new)
        await interaction.response.send_message(f"Limit: **{label}**", ephemeral=True)

    @ui.button(label="Custom Limit", style=discord.ButtonStyle.danger, row=3)
    async def custom_limit(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(VCLimitModal(self.vc_channel))


# ============================================================
#  LOGGING HELPER
# ============================================================

async def log_action(guild, action):
    cfg = get_guild_config(guild.id)
    if cfg.get("log_channel"):
        channel = guild.get_channel(int(cfg["log_channel"]))
        if channel:
            embed = discord.Embed(description=action, color=discord.Color.from_str("#1a501a"), timestamp=datetime.datetime.now())
            embed.set_footer(text="Action Log")
            await channel.send(embed=embed)


# ============================================================
#  EVENTS
# ============================================================

@bot.event
async def on_ready():
    # Cache invites for tracking
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invites}
        except discord.HTTPException:
            INVITE_CACHE[guild.id] = {}

    print(f"Logged on as {bot.user}!")



@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # AI responses when bot is pinged
    if bot.user.mentioned_in(message) and not message.mention_everyone:
        # Remove the bot mention from the message to get the actual text
        clean_msg = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if clean_msg:
            try:
                response = await ask_ai(f"{message.author.display_name} says: {clean_msg}")
                await message.reply(response[:2000])
            except Exception as e:
                print(f"AI error: {e}")
                await message.reply("My brain just short-circuited. Try again.")
        else:
            await message.reply("You rang?")
        return

    user_id = str(message.author.id)
    now = datetime.datetime.now()
    levels = load_levels()
    levels = ensure_user(levels, user_id)

    cfg = get_guild_config(message.guild.id) if message.guild else None
    img_level = cfg.get("image_level", 5) if cfg else 5

    # Image and link check
    has_image_or_link = (
        len(message.attachments) > 0 or
        any(ext in message.content.lower() for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]) or
        "http://" in message.content.lower() or
        "https://" in message.content.lower() or
        "www." in message.content.lower()
    )

    if has_image_or_link and levels[user_id]["level"] < img_level:
        await message.delete()
        embed = discord.Embed(
            title="Images & Links Locked",
            description=(
                f"{message.author.mention}, you need to be **Level {img_level}** "
                f"to send images and links.\n\n"
                f"> Current Level: `{levels[user_id]['level']}`\n"
                f"> Required Level: `{img_level}`\n\n"
                f"Keep chatting to earn XP!"
            ),
            color=discord.Color.from_str("#8B0000")
        )
        await message.channel.send(embed=embed, delete_after=10)
        save_levels(levels)
        return

    # XP gain with cooldown
    if user_id in XP_COOLDOWN:
        diff = (now - XP_COOLDOWN[user_id]).total_seconds()
        if diff < COOLDOWN_SECONDS:
            save_levels(levels)
            await bot.process_commands(message)
            return

    XP_COOLDOWN[user_id] = now
    levels[user_id]["xp"] += random.randint(15, 25)

    current_xp = levels[user_id]["xp"]
    current_level = levels[user_id]["level"]
    needed = xp_needed(current_level + 1)

    if current_xp >= needed:
        old_level = current_level
        levels[user_id]["level"] += 1
        new_level = levels[user_id]["level"]

        embed = build_levelup_embed(message.author, new_level, old_level, levels[user_id]["xp"])
        await message.channel.send(embed=embed)

    save_levels(levels)
    await bot.process_commands(message)


# ---------- VOICE STATE (VC TIME + J2C) ----------

@bot.event
async def on_voice_state_update(member, before, after):
    user_id = str(member.id)
    cfg = get_guild_config(member.guild.id)

    # VC Time Tracking
    if before.channel is None and after.channel is not None:
        VC_TRACKING[user_id] = datetime.datetime.now()
    elif before.channel is not None and after.channel is None:
        if user_id in VC_TRACKING:
            joined_at = VC_TRACKING.pop(user_id)
            minutes = (datetime.datetime.now() - joined_at).total_seconds() / 60
            levels = load_levels()
            levels = ensure_user(levels, user_id)
            levels[user_id]["vc_minutes"] += minutes
            save_levels(levels)
            force_save_levels()

    # Join to Create
    j2c_id = cfg.get("j2c_channel")
    if j2c_id and after.channel and str(after.channel.id) == str(j2c_id):
        category = after.channel.category
        new_vc = await member.guild.create_voice_channel(
            name=f"{member.display_name}'s VC",
            category=category,
            reason="Join to Create"
        )
        await member.move_to(new_vc)

        ACTIVE_VCS[new_vc.id] = {
            "owner": member.id,
            "locked": False,
            "banned": []
        }

        vc_theme = discord.Color.from_str("#1a501a")
        embed = discord.Embed(title="Voice Controls", color=vc_theme)
        embed.add_field(name="Owner", value=member.mention, inline=False)
        embed.add_field(name="Channel", value=f"`{new_vc.name}`", inline=False)
        embed.add_field(name="Status", value="`none`", inline=True)
        embed.add_field(name="User Limit", value="`Unlimited`", inline=True)
        embed.add_field(name="Speak", value="`Unlocked`", inline=True)
        embed.add_field(name="Commands", value=(
            "`.vckick @user`\n`.vclock`\n`.vcunlock`\n`.vcpermit @user`\n"
            "`.vcname <name>`\n`.vclimit <0-99>`\n`.vcclaim`"
        ), inline=False)
        embed.set_footer(text="Only the VC owner can use these controls")

        view = VCControlView(new_vc, member)

        # Send in the VC's built-in text chat (small delay to let it initialize)
        await discord.utils.sleep_until(datetime.datetime.now() + datetime.timedelta(seconds=1))
        try:
            await new_vc.send(embed=embed, view=view)
        except discord.HTTPException:
            pass

    # Delete empty J2C VCs
    if before.channel and before.channel.id in ACTIVE_VCS:
        if len(before.channel.members) == 0:
            del ACTIVE_VCS[before.channel.id]
            try:
                await before.channel.delete(reason="Empty J2C VC")
            except discord.HTTPException:
                pass

    # Also clean up any VC in the J2C category that's empty and not the J2C channel itself
    j2c_id = cfg.get("j2c_channel")
    if before.channel and j2c_id and str(before.channel.id) != str(j2c_id):
        if before.channel.category and len(before.channel.members) == 0:
            j2c_channel = member.guild.get_channel(int(j2c_id))
            if j2c_channel and before.channel.category == j2c_channel.category:
                if before.channel.id not in ACTIVE_VCS:
                    try:
                        await before.channel.delete(reason="Empty J2C VC (cleanup)")
                    except discord.HTTPException:
                        pass

    # --- VC Logging ---
    log_ch = member.guild.get_channel(1478778935746756738)
    if log_ch:
        theme = discord.Color.from_str("#1a501a")
        if before.channel is None and after.channel is not None:
            embed = discord.Embed(description=f"{member.mention} joined voice channel **{after.channel.name}**", color=theme, timestamp=datetime.datetime.now())
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_footer(text="VC Join")
            await log_ch.send(embed=embed)
        elif before.channel is not None and after.channel is None:
            embed = discord.Embed(description=f"{member.mention} left voice channel **{before.channel.name}**", color=discord.Color.from_str("#8B0000"), timestamp=datetime.datetime.now())
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_footer(text="VC Leave")
            await log_ch.send(embed=embed)
        elif before.channel is not None and after.channel is not None and before.channel != after.channel:
            embed = discord.Embed(description=f"{member.mention} moved from **{before.channel.name}** to **{after.channel.name}**", color=theme, timestamp=datetime.datetime.now())
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_footer(text="VC Move")
            await log_ch.send(embed=embed)


# ============================================================
#  DETAILED LOGGING
# ============================================================

LOG_CHANNEL_ID = 1478778935746756738


@bot.event
async def on_message_delete(message):
    if message.author.bot or not message.guild:
        return
    log_ch = message.guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return
    embed = discord.Embed(
        description=f"**Message deleted in** {message.channel.mention}\n\n{message.content}" if message.content else f"**Message deleted in** {message.channel.mention}\n\n*[no text content]*",
        color=discord.Color.from_str("#8B0000"),
        timestamp=datetime.datetime.now()
    )
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    if message.attachments:
        filenames = ", ".join(a.filename for a in message.attachments)
        embed.add_field(name="Attachments", value=filenames, inline=False)
    embed.set_footer(text=f"Author ID: {message.author.id}")
    await log_ch.send(embed=embed)


@bot.event
async def on_message_edit(before, after):
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return
    log_ch = before.guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return
    embed = discord.Embed(
        description=f"**Message edited in** {before.channel.mention} [Jump]({after.jump_url})",
        color=discord.Color.from_str("#b58900"),
        timestamp=datetime.datetime.now()
    )
    embed.set_author(name=before.author.display_name, icon_url=before.author.display_avatar.url)
    before_text = before.content[:1000] if before.content else "*[empty]*"
    after_text = after.content[:1000] if after.content else "*[empty]*"
    embed.add_field(name="Before", value=before_text, inline=False)
    embed.add_field(name="After", value=after_text, inline=False)
    embed.set_footer(text=f"Author ID: {before.author.id}")
    await log_ch.send(embed=embed)


@bot.event
async def on_member_join(member):
    # --- Invite Tracking ---
    inviter = None
    invite_code = None
    invite_ch = member.guild.get_channel(INVITE_TRACKER_CHANNEL)
    try:
        current_invites = await member.guild.invites()
        old_invites = INVITE_CACHE.get(member.guild.id, {})

        for inv in current_invites:
            old_uses = old_invites.get(inv.code, 0)
            if inv.uses > old_uses:
                inviter = inv.inviter
                invite_code = inv.code
                break

        INVITE_CACHE[member.guild.id] = {inv.code: inv.uses for inv in current_invites}
    except discord.HTTPException:
        pass

    if invite_ch:
        theme = discord.Color.from_str("#1a501a")
        if inviter:
            inv_embed = discord.Embed(
                description=(
                    f"{member.mention} joined the server\n\n"
                    f"**Invited by:** {inviter.mention}\n"
                    f"**Invite Code:** `{invite_code}`\n"
                    f"**Account Created:** <t:{int(member.created_at.timestamp())}:R>"
                ),
                color=theme,
                timestamp=datetime.datetime.now()
            )
        else:
            inv_embed = discord.Embed(
                description=(
                    f"{member.mention} joined the server\n\n"
                    f"**Invited by:** Unknown (vanity or expired invite)\n"
                    f"**Account Created:** <t:{int(member.created_at.timestamp())}:R>"
                ),
                color=theme,
                timestamp=datetime.datetime.now()
            )
        inv_embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        inv_embed.set_footer(text=f"Member #{member.guild.member_count} | ID: {member.id}")
        await invite_ch.send(embed=inv_embed)

    # --- General Log ---
    log_ch = member.guild.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(
            description=f"{member.mention} joined the server\n\nAccount created: <t:{int(member.created_at.timestamp())}:R>",
            color=discord.Color.from_str("#1a501a"),
            timestamp=datetime.datetime.now()
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count} | ID: {member.id}")
        await log_ch.send(embed=embed)


@bot.event
async def on_member_remove(member):
    log_ch = member.guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return
    roles = [r.mention for r in member.roles if r != member.guild.default_role]
    embed = discord.Embed(
        description=f"{member.mention} left the server",
        color=discord.Color.from_str("#8B0000"),
        timestamp=datetime.datetime.now()
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    if roles:
        embed.add_field(name="Roles", value=", ".join(roles[:15]), inline=False)
    embed.set_footer(text=f"ID: {member.id}")
    await log_ch.send(embed=embed)


@bot.event
async def on_invite_create(invite):
    try:
        invites = await invite.guild.invites()
        INVITE_CACHE[invite.guild.id] = {inv.code: inv.uses for inv in invites}
    except discord.HTTPException:
        pass


@bot.event
async def on_invite_delete(invite):
    try:
        invites = await invite.guild.invites()
        INVITE_CACHE[invite.guild.id] = {inv.code: inv.uses for inv in invites}
    except discord.HTTPException:
        pass


@bot.event
async def on_member_update(before, after):
    log_ch = before.guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return

    # Nickname change
    if before.nick != after.nick:
        embed = discord.Embed(
            description=f"{after.mention} nickname changed",
            color=discord.Color.from_str("#b58900"),
            timestamp=datetime.datetime.now()
        )
        embed.set_author(name=after.display_name, icon_url=after.display_avatar.url)
        embed.add_field(name="Before", value=before.nick or "*None*", inline=True)
        embed.add_field(name="After", value=after.nick or "*None*", inline=True)
        embed.set_footer(text=f"ID: {after.id}")
        await log_ch.send(embed=embed)

    # Role changes
    if before.roles != after.roles:
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        embed = discord.Embed(
            description=f"{after.mention} roles updated",
            color=discord.Color.from_str("#1a501a"),
            timestamp=datetime.datetime.now()
        )
        embed.set_author(name=after.display_name, icon_url=after.display_avatar.url)
        if added:
            embed.add_field(name="Added", value=", ".join(added), inline=False)
        if removed:
            embed.add_field(name="Removed", value=", ".join(removed), inline=False)
        embed.set_footer(text=f"ID: {after.id}")
        await log_ch.send(embed=embed)


@bot.event
async def on_member_ban(guild, user):
    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return
    embed = discord.Embed(
        description=f"**{user}** was banned",
        color=discord.Color.from_str("#8B0000"),
        timestamp=datetime.datetime.now()
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_footer(text=f"ID: {user.id}")
    await log_ch.send(embed=embed)


@bot.event
async def on_member_unban(guild, user):
    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return
    embed = discord.Embed(
        description=f"**{user}** was unbanned",
        color=discord.Color.from_str("#1a501a"),
        timestamp=datetime.datetime.now()
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_footer(text=f"ID: {user.id}")
    await log_ch.send(embed=embed)


@bot.event
async def on_bulk_message_delete(messages):
    if not messages:
        return
    guild = messages[0].guild
    if not guild:
        return
    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return
    channel = messages[0].channel
    embed = discord.Embed(
        description=f"**{len(messages)} messages** bulk deleted in {channel.mention}",
        color=discord.Color.from_str("#8B0000"),
        timestamp=datetime.datetime.now()
    )
    embed.set_footer(text="Bulk Delete")
    await log_ch.send(embed=embed)


# ============================================================
#  GENERAL COMMANDS
# ============================================================

@bot.command(name="help")
async def help_cmd(ctx):
    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(
        title="information",
        description=(
            "> `[ ]` = optional, `< >` = required\n\n"
            "This bot handles moderation, utility, leveling, "
            "voice controls, and more.\n\n"
            "Choose a category from the dropdown below."
        ),
        color=theme
    )
    embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    await ctx.reply(embed=embed, view=HelpView(), mention_author=True)


@bot.command(name="ping")
async def ping(ctx):
    theme = discord.Color.from_str("#1a501a")
    latency = round(bot.latency * 1000)
    embed = discord.Embed(description=f"Pong! **{latency}ms**", color=theme)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="xp")
async def xp(ctx, member: discord.Member = None):
    member = member or ctx.author
    levels = load_levels()
    user_id = str(member.id)

    if user_id not in levels:
        await ctx.reply(f"**{member.display_name}** has no XP yet.", mention_author=False)
        return

    data = levels[user_id]
    needed = xp_needed(data["level"] + 1)
    rank = get_rank(user_id, levels)
    # Include live VC time if currently in a voice channel
    total_vc_minutes = data.get("vc_minutes", 0)
    if user_id in VC_TRACKING:
        live_minutes = (datetime.datetime.now() - VC_TRACKING[user_id]).total_seconds() / 60
        total_vc_minutes += live_minutes
    vc_hours = round(total_vc_minutes / 60, 1)
    progress = min(data["xp"] / needed, 1.0) if needed > 0 else 1
    percentage = int(progress * 100)
    xp_remaining = needed - data["xp"]
    bar = make_progress_bar(progress)

    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(color=theme)
    embed.set_author(name=f"{member.display_name}  \u2022  Level {data['level']}", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.with_size(256).url)

    embed.add_field(name="Current XP", value=f"**{data['xp']}**", inline=True)
    embed.add_field(name="Rank", value=f"**#{rank}**", inline=True)
    embed.add_field(name="VC Time", value=f"**{vc_hours}h**", inline=True)

    embed.add_field(name="Progress", value=f"{bar}\n**{percentage}%**\nXP to Next: **{xp_remaining}**", inline=False)

    await ctx.reply(embed=embed, mention_author=True)


@bot.command(name="lb")
async def lb(ctx):
    levels = load_levels()
    if not levels:
        await ctx.reply("No one has any XP yet!", mention_author=False)
        return

    sorted_users = sorted(levels.items(), key=lambda x: x[1]["xp"], reverse=True)[:10]

    description = ""
    for i, (user_id, data) in enumerate(sorted_users, 1):
        try:
            user = await bot.fetch_user(int(user_id))
            description += f"**{i}.** {user.mention} -- Level {data['level']} ({data['xp']} XP)\n"
        except discord.NotFound:
            description += f"**{i}.** Unknown User -- Level {data['level']} ({data['xp']} XP)\n"

    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(title="Leaderboard", description=description, color=theme)
    embed.set_footer(text=f"{ctx.guild.name} Rankings")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="av")
async def avatar(ctx, member: discord.Member = None):
    theme = discord.Color.from_str("#1a501a")
    member = member or ctx.author
    embed = discord.Embed(title=f"{member.display_name}'s Avatar", color=theme)
    embed.set_image(url=member.display_avatar.with_size(512).url)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    theme = discord.Color.from_str("#1a501a")
    member = member or ctx.author
    roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
    embed = discord.Embed(color=theme)
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.with_size(256).url)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
    embed.add_field(name=f"Roles [{len(roles)}]", value=", ".join(roles[:10]) + ("..." if len(roles) > 10 else "") if roles else "None", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="serverinfo")
async def serverinfo(ctx):
    theme = discord.Color.from_str("#1a501a")
    guild = ctx.guild
    owner_id = guild.owner_id if guild.owner_id else "Unknown"
    total_channels = len(guild.text_channels) + len(guild.voice_channels)
    created = guild.created_at.strftime("%A, %B %d, %Y %I:%M %p")

    embed = discord.Embed(title="server information", color=theme)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="name", value=guild.name, inline=True)
    embed.add_field(name="id", value=str(guild.id), inline=True)
    embed.add_field(name="owner id", value=str(owner_id), inline=True)

    embed.add_field(name="members", value=str(guild.member_count), inline=True)
    embed.add_field(name="channels", value=str(total_channels), inline=True)
    embed.add_field(name="roles", value=str(len(guild.roles)), inline=True)

    embed.add_field(name="created", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)

    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="membercount")
async def membercount(ctx):
    guild = ctx.guild
    total = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    humans = total - bots

    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(title="Member Count", color=theme)
    embed.add_field(name="Total", value=f"`{total}`", inline=True)
    embed.add_field(name="Humans", value=f"`{humans}`", inline=True)
    embed.add_field(name="Bots", value=f"`{bots}`", inline=True)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="roleinfo")
async def roleinfo(ctx, role: discord.Role = None):
    if not role:
        await ctx.reply("Please provide a role. Usage: `.roleinfo @role`", mention_author=False)
        return
    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(title=role.name, color=role.color if role.color.value != 0 else theme)
    embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
    embed.add_field(name="Color", value=f"`{role.color}`", inline=True)
    embed.add_field(name="Members", value=f"`{len(role.members)}`", inline=True)
    embed.add_field(name="Mentionable", value=f"`{role.mentionable}`", inline=True)
    embed.add_field(name="Hoisted", value=f"`{role.hoist}`", inline=True)
    embed.add_field(name="Position", value=f"`{role.position}`", inline=True)
    embed.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
    await ctx.reply(embed=embed, mention_author=False)


# ============================================================
#  SETUP COMMANDS (Admin only)
# ============================================================

async def send_confirm(ctx, message, delete_after=None):
    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(description=message, color=theme)
    msg = await ctx.reply(embed=embed, mention_author=False)
    if delete_after:
        try:
            await ctx.message.delete(delay=1)
            await msg.delete(delay=delete_after)
        except discord.HTTPException:
            pass


@bot.command(name="setjail")
@commands.has_permissions(administrator=True)
async def setjail(ctx, role: discord.Role):
    update_guild_config(ctx.guild.id, "jail_role", str(role.id))
    await send_confirm(ctx, f"Jail role set to {role.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} set jail role to {role.mention}")


@bot.command(name="setir")
@commands.has_permissions(administrator=True)
async def setir(ctx, role: discord.Role):
    update_guild_config(ctx.guild.id, "ir_role", str(role.id))
    await send_confirm(ctx, f"Image restriction role set to {role.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} set IR role to {role.mention}")


@bot.command(name="setbooster")
@commands.has_permissions(administrator=True)
async def setbooster(ctx, role: discord.Role):
    update_guild_config(ctx.guild.id, "booster_role", str(role.id))
    await send_confirm(ctx, f"Booster role set to {role.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} set booster role to {role.mention}")


@bot.command(name="setj2c")
@commands.has_permissions(administrator=True)
async def setj2c(ctx, channel: discord.VoiceChannel):
    update_guild_config(ctx.guild.id, "j2c_channel", str(channel.id))
    await send_confirm(ctx, f"Join-to-create channel set to {channel.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} set J2C channel to {channel.mention}")


@bot.command(name="setimglevel")
@commands.has_permissions(administrator=True)
async def setimglevel(ctx, level: int):
    update_guild_config(ctx.guild.id, "image_level", level)
    await send_confirm(ctx, f"Image permission level set to `{level}`.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} set image level to {level}")


@bot.command(name="setlogs")
@commands.has_permissions(administrator=True)
async def setlogs(ctx, channel: discord.TextChannel):
    update_guild_config(ctx.guild.id, "log_channel", str(channel.id))
    await send_confirm(ctx, f"Log channel set to {channel.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} set log channel to {channel.mention}")


# ============================================================
#  STAFF COMMANDS
# ============================================================

@bot.command(name="jm")
@commands.has_permissions(administrator=True)
async def jm(ctx, member: discord.Member):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("jail_role"):
        await send_confirm(ctx, "Jail role not set. Use `.setjail @role` first.", delete_after=5)
        return
    role = ctx.guild.get_role(int(cfg["jail_role"]))
    if not role:
        await send_confirm(ctx, "Jail role not found.", delete_after=5)
        return
    if role in member.roles:
        await member.remove_roles(role)
        await send_confirm(ctx, f"Removed jail role from {member.mention}.", delete_after=5)
    else:
        await member.add_roles(role)
        await send_confirm(ctx, f"Added jail role to {member.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} toggled jail on {member.mention}")


@bot.command(name="ir")
@commands.has_permissions(administrator=True)
async def ir(ctx, member: discord.Member):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("ir_role"):
        await send_confirm(ctx, "IR role not set. Use `.setir @role` first.", delete_after=5)
        return
    role = ctx.guild.get_role(int(cfg["ir_role"]))
    if not role:
        await send_confirm(ctx, "IR role not found.", delete_after=5)
        return
    if role in member.roles:
        await member.remove_roles(role)
        await send_confirm(ctx, f"Removed image restriction from {member.mention}.", delete_after=5)
    else:
        await member.add_roles(role)
        await send_confirm(ctx, f"Added image restriction to {member.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} toggled IR on {member.mention}")


@bot.command(name="to")
@commands.has_permissions(administrator=True)
async def to_cmd(ctx, member: discord.Member, minutes: int = None):
    cfg = get_guild_config(ctx.guild.id)
    if member.is_timed_out():
        await member.timeout(None)
        await send_confirm(ctx, f"Removed timeout from {member.mention}.", delete_after=5)
    else:
        mins = minutes or cfg.get("default_timeout", 10)
        duration = datetime.timedelta(minutes=mins)
        await member.timeout(duration)
        await send_confirm(ctx, f"Timed out {member.mention} for `{mins}` minutes.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} toggled timeout on {member.mention}")


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        await send_confirm(ctx, "Please choose a number between 1 and 100.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    theme = discord.Color.from_str("#1a501a")
    embed = discord.Embed(description=f"Deleted `{len(deleted) - 1}` messages.", color=theme)
    msg = await ctx.send(embed=embed)
    await msg.delete(delay=5)
    await log_action(ctx.guild, f"{ctx.author.mention} purged {len(deleted) - 1} messages in {ctx.channel.mention}")


@bot.command(name="lq")
@commands.has_permissions(manage_messages=True)
async def lq(ctx):
    if ctx.message.reference and ctx.message.reference.message_id:
        try:
            msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            await msg.delete()
            await ctx.message.delete()
        except discord.NotFound:
            await send_confirm(ctx, "Message not found.", delete_after=5)
    else:
        await send_confirm(ctx, "Reply to a message to use `.lq`.", delete_after=5)


@bot.command(name="banvc")
@commands.has_permissions(administrator=True)
async def banvc(ctx, member: discord.Member):
    if member.voice and member.voice.channel:
        vc_id = member.voice.channel.id
        if vc_id in ACTIVE_VCS:
            ACTIVE_VCS[vc_id]["banned"].append(member.id)
        await member.voice.channel.set_permissions(member, connect=False)
        await member.move_to(None)
        await send_confirm(ctx, f"{member.mention} has been banned from the VC.", delete_after=5)
        await log_action(ctx.guild, f"{ctx.author.mention} VC banned {member.mention}")
    else:
        await send_confirm(ctx, "That user is not in a voice channel.", delete_after=5)


@bot.command(name="unbanvc")
@commands.has_permissions(administrator=True)
async def unbanvc(ctx, member: discord.Member):
    for vc_id, data in ACTIVE_VCS.items():
        if member.id in data["banned"]:
            data["banned"].remove(member.id)
            channel = ctx.guild.get_channel(vc_id)
            if channel:
                await channel.set_permissions(member, overwrite=None)
    await send_confirm(ctx, f"Removed VC ban from {member.mention}.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} removed VC ban from {member.mention}")


@bot.command(name="lock")
@commands.has_permissions(administrator=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await send_confirm(ctx, "This channel has been locked.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} locked {ctx.channel.mention}")


@bot.command(name="unlock")
@commands.has_permissions(administrator=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await send_confirm(ctx, "This channel has been unlocked.", delete_after=5)
    await log_action(ctx.guild, f"{ctx.author.mention} unlocked {ctx.channel.mention}")


# ============================================================
#  VC OWNER COMMANDS
# ============================================================

def get_user_vc(member):
    if member.voice and member.voice.channel:
        vc_id = member.voice.channel.id
        if vc_id in ACTIVE_VCS and ACTIVE_VCS[vc_id]["owner"] == member.id:
            return member.voice.channel
    return None


@bot.command(name="vckick")
async def vckick(ctx, member: discord.Member):
    vc = get_user_vc(ctx.author)
    if not vc:
        await send_confirm(ctx, "You don't own a VC or you're not in it.")
        return
    if member.voice and member.voice.channel == vc:
        await member.move_to(None)
        await send_confirm(ctx, f"Kicked {member.mention} from your VC.")
    else:
        await send_confirm(ctx, "That user is not in your VC.")


@bot.command(name="vclock")
async def vclock(ctx):
    vc = get_user_vc(ctx.author)
    if not vc:
        await send_confirm(ctx, "You don't own a VC or you're not in it.")
        return
    await vc.set_permissions(ctx.guild.default_role, connect=False)
    ACTIVE_VCS[vc.id]["locked"] = True
    await send_confirm(ctx, "Your VC has been locked.")


@bot.command(name="vcunlock")
async def vcunlock(ctx):
    vc = get_user_vc(ctx.author)
    if not vc:
        await send_confirm(ctx, "You don't own a VC or you're not in it.")
        return
    await vc.set_permissions(ctx.guild.default_role, connect=None)
    ACTIVE_VCS[vc.id]["locked"] = False
    await send_confirm(ctx, "Your VC has been unlocked.")


@bot.command(name="vcpermit")
async def vcpermit(ctx, member: discord.Member):
    vc = get_user_vc(ctx.author)
    if not vc:
        await send_confirm(ctx, "You don't own a VC or you're not in it.")
        return
    await vc.set_permissions(member, connect=True)
    await send_confirm(ctx, f"Permitted {member.mention} into your VC.")


@bot.command(name="vcname")
async def vcname(ctx, *, name: str):
    vc = get_user_vc(ctx.author)
    if not vc:
        await send_confirm(ctx, "You don't own a VC or you're not in it.")
        return
    await vc.edit(name=name)
    await send_confirm(ctx, f"VC renamed to `{name}`.")


@bot.command(name="vclimit")
async def vclimit(ctx, limit: int):
    vc = get_user_vc(ctx.author)
    if not vc:
        await send_confirm(ctx, "You don't own a VC or you're not in it.")
        return
    if limit < 0 or limit > 99:
        await send_confirm(ctx, "Limit must be between 0 and 99 (0 = unlimited).")
        return
    await vc.edit(user_limit=limit)
    label = "Unlimited" if limit == 0 else str(limit)
    await send_confirm(ctx, f"VC user limit set to `{label}`.")


@bot.command(name="vcclaim")
async def vcclaim(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await send_confirm(ctx, "You need to be in a VC to claim it.")
        return
    vc = ctx.author.voice.channel
    if vc.id not in ACTIVE_VCS:
        await send_confirm(ctx, "This is not a J2C voice channel.")
        return
    owner_id = ACTIVE_VCS[vc.id]["owner"]
    owner_in_vc = any(m.id == owner_id for m in vc.members)
    if owner_in_vc:
        await send_confirm(ctx, "The owner is still in the VC.")
        return
    ACTIVE_VCS[vc.id]["owner"] = ctx.author.id
    await send_confirm(ctx, f"{ctx.author.mention} now owns this VC.")


# ============================================================
#  ERROR HANDLER
# ============================================================

@bot.event
async def on_command_error(ctx, error):
    err_color = discord.Color.from_str("#8B0000")
    if isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(description="You don't have permission to use this command.", color=err_color)
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MemberNotFound):
        embed = discord.Embed(description="Member not found.", color=err_color)
        await ctx.send(embed=embed)
    elif isinstance(error, commands.RoleNotFound):
        embed = discord.Embed(description="Role not found.", color=err_color)
        await ctx.send(embed=embed)
    elif isinstance(error, commands.ChannelNotFound):
        embed = discord.Embed(description="Channel not found.", color=err_color)
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(description=f"Missing argument: `{error.param.name}`. Use `.help` for command info.", color=err_color)
        await ctx.send(embed=embed)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"Error: {error}")


import atexit
atexit.register(force_save_levels)

bot.run(os.getenv("DISCORD_TOKEN"))

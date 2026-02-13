import os
import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
from hcloud import Client

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
HCLOUD_TOKEN = os.getenv("HCLOUD_TOKEN")
if not DISCORD_TOKEN or not HCLOUD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN or HCLOUD_TOKEN in env")

hc = Client(token=HCLOUD_TOKEN)
DEFAULT_SERVER_TYPE = "cx23"
DB_PATH = "servers.db"

def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS server_map (
                discord_user_id TEXT NOT NULL,
                server_id INTEGER NOT NULL,
                server_name TEXT NOT NULL,
                PRIMARY KEY(discord_user_id, server_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_defaults (
                discord_user_id TEXT PRIMARY KEY,
                network_id INTEGER,
                ssh_key_id INTEGER,
                firewall_id INTEGER
            )
            """
        )
        con.commit()

def db_add_server(discord_user_id: int, server_id: int, server_name: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO server_map(discord_user_id, server_id, server_name) VALUES (?, ?, ?)",
            (str(discord_user_id), int(server_id), server_name),
        )
        con.commit()

def db_find_server(discord_user_id: int, query: str) -> Optional[int]:
    q = (query or "").strip()
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        if q.isdigit():
            cur.execute(
                "SELECT server_id FROM server_map WHERE discord_user_id=? AND server_id=?",
                (str(discord_user_id), int(q)),
            )
            row = cur.fetchone()
            return row[0] if row else None

        cur.execute(
            "SELECT server_id FROM server_map WHERE discord_user_id=? AND server_name=?",
            (str(discord_user_id), q),
        )
        row = cur.fetchone()
        return row[0] if row else None

def db_get_defaults(discord_user_id: int) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT network_id, ssh_key_id, firewall_id FROM user_defaults WHERE discord_user_id=?",
            (str(discord_user_id),),
        )
        row = cur.fetchone()
        if not row:
            return None, None, None
        return row[0], row[1], row[2]

def db_set_defaults(
    discord_user_id: int,
    network_id: Optional[int],
    ssh_key_id: Optional[int],
    firewall_id: Optional[int],
) -> None:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO user_defaults(discord_user_id, network_id, ssh_key_id, firewall_id) VALUES (?, ?, ?, ?)",
            (str(discord_user_id), network_id, ssh_key_id, firewall_id),
        )
        con.commit()

@dataclass
class HetznerCache:
    locations: Dict[str, object]
    server_types: Dict[str, object]
    images_all: Dict[str, object]
    images_x86: Dict[str, object]
    networks: Dict[int, object]
    ssh_keys: Dict[int, object]
    firewalls: Dict[int, object]

HCACHE: Optional[HetznerCache] = None
AUTO_IMAGES_X86: List[str] = []
AUTO_LOCATIONS: List[str] = []

def _img_arch(img: object) -> str:
    arch = getattr(img, "architecture", None)
    return str(arch).lower() if arch is not None else "unknown"

def _is_x86_arch(arch: str) -> bool:
    a = (arch or "").lower()
    return ("x86" in a) or ("amd64" in a) or ("x86_64" in a)

def build_cache() -> HetznerCache:
    locations = {l.name: l for l in hc.locations.get_all()}
    server_types = {st.name: st for st in hc.server_types.get_all()}

    images_all_list = hc.images.get_all()
    images_all = {im.name: im for im in images_all_list}
    images_x86 = {im.name: im for im in images_all_list if _is_x86_arch(_img_arch(im))}

    networks = {n.id: n for n in hc.networks.get_all()}
    ssh_keys = {k.id: k for k in hc.ssh_keys.get_all()}
    firewalls = {f.id: f for f in hc.firewalls.get_all()}
    return HetznerCache(
        locations=locations,
        server_types=server_types,
        images_all=images_all,
        images_x86=images_x86,
        networks=networks,
        ssh_keys=ssh_keys,
        firewalls=firewalls,
    )

def cache_required() -> HetznerCache:
    global HCACHE
    if HCACHE is None:
        HCACHE = build_cache()
    return HCACHE

def _top(items: List[str], n: int = 50) -> str:
    head = "\n".join(items[:n])
    if len(items) <= n:
        return head
    return head + f"\n… (+{len(items) - n} more)"

def pick_single_or_user_default(kind: str, items_by_id: Dict[int, object], user_default_id: Optional[int]) -> object:
    if not items_by_id:
        raise RuntimeError(f"No {kind} exists in Hetzner.")

    if len(items_by_id) == 1:
        return next(iter(items_by_id.values()))

    if user_default_id and user_default_id in items_by_id:
        return items_by_id[user_default_id]

    lines: List[str] = []
    for obj in items_by_id.values():
        nm = getattr(obj, "name", str(getattr(obj, "id", "?")))
        oid = getattr(obj, "id", "?")
        lines.append(f"- {nm} (id {oid})")
    lines.sort()

    raise RuntimeError(
        f"Multiple {kind}s exist, and no default is set.\n"
        f"Use /setdefaults to choose IDs.\n\n"
        f"Available {kind}s:\n" + "\n".join(lines)
    )

def cloud_init_for_app(app: str) -> str:
    app = (app or "").strip().lower()

    if app in ("", "none"):
        return ""

    if app == "coolify":
        return (
            "#cloud-config\n"
            "package_update: true\n"
            "packages:\n"
            "  - curl\n"
            "runcmd:\n"
            "  - curl -fsSL https://get.docker.com | sh\n"
            "  - curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash\n"
        )

    if app == "wireguard":
        return (
            "#cloud-config\n"
            "package_update: true\n"
            "packages:\n"
            "  - wireguard\n"
            "  - qrencode\n"
            "runcmd:\n"
            "  - sysctl -w net.ipv4.ip_forward=1\n"
            "  - sysctl -w net.ipv6.conf.all.forwarding=1\n"
        )

    raise RuntimeError(f'Unknown app "{app}". Supported: none, coolify, wireguard')

def server_embed(server) -> discord.Embed:
    public_net = getattr(server, "public_net", None)
    ipv4 = public_net.ipv4.ip if public_net and getattr(public_net, "ipv4", None) else "N/A"
    ipv6 = public_net.ipv6.ip if public_net and getattr(public_net, "ipv6", None) else "N/A"

    dc = getattr(server, "datacenter", None)
    dc_name = dc.name if dc else "N/A"
    loc = getattr(dc, "location", None) if dc else None
    loc_name = loc.name if loc else "N/A"

    embed = discord.Embed(title=f"✅ VM Ready: {server.name}")
    embed.add_field(name="Server ID", value=str(server.id), inline=True)
    embed.add_field(name="Status", value=str(server.status), inline=True)
    embed.add_field(name="Type", value=server.server_type.name, inline=True)
    embed.add_field(name="Location", value=f"{dc_name} ({loc_name})", inline=False)
    embed.add_field(name="IPv4", value=ipv4, inline=True)
    embed.add_field(name="IPv6", value=ipv6, inline=True)
    embed.add_field(name="Image", value=(server.image.name if getattr(server, "image", None) else "N/A"), inline=False)
    embed.set_footer(text="Hetzner Provisioner")
    return embed

def suggest_locations_text() -> str:
    c = cache_required()
    names = sorted(c.locations.keys())
    return "Try a different location. Available (first 25): " + ", ".join(names[:25]) + (" …" if len(names) > 25 else "")

def suggest_x86_images_text() -> str:
    c = cache_required()
    names = sorted(c.images_x86.keys())
    return "Valid x86 images (first 25): " + ", ".join(names[:25]) + (" …" if len(names) > 25 else "")

def create_server(discord_user_id: int, name: str, location_name: str, image_name: str, app: str) -> object:
    c = cache_required()

    st = c.server_types.get(DEFAULT_SERVER_TYPE)
    if not st:
        raise RuntimeError(f"Server type {DEFAULT_SERVER_TYPE} not found in Hetzner.")

    loc_key = (location_name or "").strip()
    loc = c.locations.get(loc_key)
    if not loc:
        raise RuntimeError(f'Unknown location "{location_name}". {suggest_locations_text()}')

    img_key = (image_name or "").strip()
    img = c.images_x86.get(img_key)
    if not img:
        if img_key in c.images_all:
            actual = _img_arch(c.images_all[img_key])
            raise RuntimeError(
                f"Image \"{img_key}\" is not x86-compatible (architecture: {actual})."
                "Pick an x86 image. Use /images or autocomplete."
                f"{suggest_x86_images_text()}"
            )
        raise RuntimeError(
            f'Unknown image "{img_key}". Use /images to list valid x86 options.\n'
            f"{suggest_x86_images_text()}"
        )

    user_data = cloud_init_for_app(app)
    net_id, ssh_id, fw_id = db_get_defaults(discord_user_id)
    net = pick_single_or_user_default("network", c.networks, net_id)
    ssh_key = pick_single_or_user_default("SSH key", c.ssh_keys, ssh_id)
    firewall = pick_single_or_user_default("firewall", c.firewalls, fw_id)
    resp = hc.servers.create(
        name=name,
        server_type=st,
        image=img,
        location=loc,
        networks=[net],
        ssh_keys=[ssh_key],
        firewalls=[firewall],
        user_data=(user_data if user_data else None),
        labels={"managed_by": "discord-bot", "discord_user_id": str(discord_user_id)},
    )
    return resp.server


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def is_dm(interaction: discord.Interaction) -> bool:
    return interaction.guild is None

def _is_resource_limit_error(e: Exception) -> bool:
    s = str(e).lower()
    return ("resource_limit_exceeded" in s) or ("server limit reached" in s) or ("limit reached" in s)

def _server_quota_remaining() -> Optional[int]:
    try:
        limits = hc.limits.get()
        resources = getattr(limits, "resources", None)
        if not resources:
            return None
        servers = getattr(resources, "servers", None)
        if not servers:
            return None
        maxv = getattr(servers, "max", None)
        used = getattr(servers, "used", None)
        if maxv is None or used is None:
            return None
        return int(maxv) - int(used)
    except Exception:
        return None

async def safe_reply(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
):
    if is_dm(interaction):
        ephemeral = False

    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

async def send_long(interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
    if text is None:
        return
    ep = False if is_dm(interaction) else ephemeral
    chunks: List[str] = []
    s = text
    while len(s) > 1900:
        cut = s.rfind("\n", 0, 1900)
        if cut == -1:
            cut = 1900
        chunks.append(s[:cut])
        s = s[cut:].lstrip("\n")
    if s:
        chunks.append(s)

    first = True
    for chunk in chunks:
        if first and not interaction.response.is_done():
            await interaction.response.send_message(chunk, ephemeral=ep)
            first = False
        else:
            await interaction.followup.send(chunk, ephemeral=ep)

async def image_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    try:
        cur = (current or "").lower().strip()
        if not AUTO_IMAGES_X86:
            return []
        names = AUTO_IMAGES_X86
        if cur:
            names = [n for n in names if cur in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]
    except Exception as e:
        print("image_autocomplete error:", repr(e))
        return []

async def location_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    try:
        cur = (current or "").lower().strip()
        if not AUTO_LOCATIONS:
            return []
        names = AUTO_LOCATIONS
        if cur:
            names = [n for n in names if cur in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]
    except Exception as e:
        print("location_autocomplete error:", repr(e))
        return []

@bot.event
async def on_ready():
    global HCACHE, AUTO_IMAGES_X86, AUTO_LOCATIONS

    db_init()

    HCACHE = await asyncio.to_thread(build_cache)
    AUTO_IMAGES_X86 = sorted(HCACHE.images_x86.keys())
    AUTO_LOCATIONS = sorted(HCACHE.locations.keys())

    try:
        await bot.tree.sync()
    except Exception as e:
        print("Sync failed:", e)

    print(f"Logged in as {bot.user}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        await safe_reply(
            interaction,
            content=f"❌ Command error:\n`{type(error).__name__}: {error}`",
            ephemeral=True,
        )
    except Exception:
        pass
    raise error

@bot.tree.command(name="create", description="Create a Hetzner VM (required: name, location, image)")
@app_commands.describe(
    name="VM name",
    location="Hetzner location (e.g. hel1, nbg1)",
    image="x86 image name (use /images to list all, autocomplete helps)",
    app="Optional app install (none/coolify/wireguard) applied via cloud-init",
    count="How many VMs to create (1-10)",
)
@app_commands.autocomplete(location=location_autocomplete, image=image_autocomplete)
async def create_cmd(
    interaction: discord.Interaction,
    name: str,
    location: str,
    image: str,
    app: Optional[str] = "none",
    count: Optional[int] = 1,
):
    await interaction.response.defer(thinking=True)

    base = (name or "").strip().upper()
    if not base:
        await safe_reply(interaction, content="❌ Name can’t be empty.", ephemeral=True)
        return

    try:
        n = int(count or 1)
    except Exception:
        await safe_reply(interaction, content="❌ count must be a number.", ephemeral=True)
        return

    if n < 1 or n > 10:
        await safe_reply(interaction, content="❌ count must be between 1 and 10.", ephemeral=True)
        return

    remaining = _server_quota_remaining()
    if remaining is not None and remaining <= 0:
        await safe_reply(interaction, content="❌ Hetzner server limit reached on this account.", ephemeral=True)
        return
    if remaining is not None and n > remaining:
        await safe_reply(
            interaction,
            content=f"❌ You requested {n} VM(s), but your Hetzner account only has quota for {remaining} more.",
            ephemeral=True,
        )
        return

    loc = (location or "").strip()
    img = (image or "").strip()
    app_val = (app or "none").strip().lower()

    planned_names: List[str] = [base] + [f"{base}{i}" for i in range(1, n)]

    created_ids: List[int] = []
    created_names: List[str] = []

    for vm_name in planned_names:
        try:
            server = await asyncio.to_thread(create_server, interaction.user.id, vm_name, loc, img, app_val)
        except Exception as e:
            if _is_resource_limit_error(e):
                msg = "❌ Hetzner server limit reached on this account."
            else:
                msg = f"❌ Failed creating VM:\n`{e}`"

            if created_names:
                await safe_reply(
                    interaction,
                    content=(
                        "❌ Failed creating one of the VMs.\n"
                        f"Created so far: {', '.join(created_names)}\n\n"
                        + (msg if _is_resource_limit_error(e) else f"Error: `{e}`")
                    ),
                    ephemeral=True,
                )
            else:
                await safe_reply(interaction, content=msg, ephemeral=True)
            return

        db_add_server(interaction.user.id, server.id, server.name)
        created_ids.append(int(server.id))
        created_names.append(server.name)

    await asyncio.sleep(20)

    embeds: List[discord.Embed] = []
    for sid in created_ids:
        try:
            srv = hc.servers.get_by_id(sid)
            embeds.append(server_embed(srv))
        except Exception as e:
            await safe_reply(interaction, content=f"⚠️ Created server {sid}, but fetch failed: `{e}`", ephemeral=True)

    if not embeds:
        await safe_reply(interaction, content="⚠️ VMs created, but I couldn’t fetch details.", ephemeral=True)
        return

    for i in range(0, len(embeds), 10):
        await safe_reply(interaction, embed=embeds[i], ephemeral=True)
        for extra in embeds[i + 1 : i + 10]:
            await interaction.followup.send(embed=extra, ephemeral=(not is_dm(interaction)))

@bot.tree.command(name="s", description="Get info about one of your servers (by name or ID)")
@app_commands.describe(server="Server name or ID")
async def s_cmd(interaction: discord.Interaction, server: str):
    sid = db_find_server(interaction.user.id, server)
    if not sid:
        await safe_reply(interaction, content="I can’t find that server under your user.", ephemeral=True)
        return

    try:
        srv = hc.servers.get_by_id(sid)
    except Exception as e:
        await safe_reply(interaction, content=f"Fetch failed:\n`{e}`", ephemeral=True)
        return

    await safe_reply(interaction, embed=server_embed(srv), ephemeral=True)

@bot.tree.command(name="refresh", description="Refresh Hetzner options cache")
async def refresh_cmd(interaction: discord.Interaction):
    global HCACHE, AUTO_IMAGES_X86, AUTO_LOCATIONS
    await safe_reply(interaction, content="Refreshing Hetzner cache…", ephemeral=True)

    try:
        HCACHE = await asyncio.to_thread(build_cache)
        AUTO_IMAGES_X86 = sorted(HCACHE.images_x86.keys())
        AUTO_LOCATIONS = sorted(HCACHE.locations.keys())
    except Exception as e:
        await safe_reply(interaction, content=f"❌ Refresh failed:\n`{e}`", ephemeral=True)
        return

    await safe_reply(
        interaction,
        content=(
            "✅ Refreshed.\n"
            f"Locations: {len(HCACHE.locations)}\n"
            f"Server types: {len(HCACHE.server_types)}\n"
            f"Images(all): {len(HCACHE.images_all)}\n"
            f"Images(x86): {len(HCACHE.images_x86)}\n"
            f"Networks: {len(HCACHE.networks)}\n"
            f"SSH keys: {len(HCACHE.ssh_keys)}\n"
            f"Firewalls: {len(HCACHE.firewalls)}"
        ),
        ephemeral=True,
    )

@bot.tree.command(name="images", description="List ALL valid x86 image names (compatible with cx23)")
async def images_cmd(interaction: discord.Interaction):
    c = cache_required()
    names = sorted(c.images_x86.keys())
    text = "x86 Images:\n" + _top(names, n=len(names))
    await send_long(interaction, text, ephemeral=True)

@bot.tree.command(name="locations", description="List available Hetzner locations")
async def locations_cmd(interaction: discord.Interaction):
    c = cache_required()
    names = sorted(c.locations.keys())
    text = "Locations:\n" + _top(names, n=min(len(names), 200))
    await send_long(interaction, text, ephemeral=True)

@bot.tree.command(name="networks", description="List networks discovered from Hetzner")
async def networks_cmd(interaction: discord.Interaction):
    c = cache_required()
    lines = sorted([f"- {n.name} (id {n.id})" for n in c.networks.values()])
    text = "Networks:\n" + ("\n".join(lines) if lines else "None")
    await send_long(interaction, text, ephemeral=True)

@bot.tree.command(name="sshkeys", description="List SSH keys discovered from Hetzner")
async def sshkeys_cmd(interaction: discord.Interaction):
    c = cache_required()
    lines = sorted([f"- {k.name} (id {k.id})" for k in c.ssh_keys.values()])
    text = "SSH keys:\n" + ("\n".join(lines) if lines else "None")
    await send_long(interaction, text, ephemeral=True)

@bot.tree.command(name="firewalls", description="List firewalls discovered from Hetzner")
async def firewalls_cmd(interaction: discord.Interaction):
    c = cache_required()
    lines = sorted([f"- {f.name} (id {f.id})" for f in c.firewalls.values()])
    text = "Firewalls:\n" + ("\n".join(lines) if lines else "None")
    await send_long(interaction, text, ephemeral=True)

@bot.tree.command(
    name="setdefaults",
    description="Set defaults (only needed if you have multiple networks/ssh keys/firewalls)",
)
@app_commands.describe(
    network_id="Default network ID",
    ssh_key_id="Default SSH key ID",
    firewall_id="Default firewall ID",
)
async def setdefaults_cmd(
    interaction: discord.Interaction,
    network_id: Optional[int] = None,
    ssh_key_id: Optional[int] = None,
    firewall_id: Optional[int] = None,
):
    c = cache_required()

    if network_id is not None and network_id not in c.networks:
        await safe_reply(interaction, content=f"Unknown network_id {network_id}. Use /networks to list.", ephemeral=True)
        return
    if ssh_key_id is not None and ssh_key_id not in c.ssh_keys:
        await safe_reply(interaction, content=f"Unknown ssh_key_id {ssh_key_id}. Use /sshkeys to list.", ephemeral=True)
        return
    if firewall_id is not None and firewall_id not in c.firewalls:
        await safe_reply(interaction, content=f"Unknown firewall_id {firewall_id}. Use /firewalls to list.", ephemeral=True)
        return

    db_set_defaults(interaction.user.id, network_id, ssh_key_id, firewall_id)
    await safe_reply(
        interaction,
        content=(
            "✅ Defaults saved:\n"
            f"- network_id: {network_id}\n"
            f"- ssh_key_id: {ssh_key_id}\n"
            f"- firewall_id: {firewall_id}"
        ),
        ephemeral=True,
    )
bot.run(DISCORD_TOKEN)

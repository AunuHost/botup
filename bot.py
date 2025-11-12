#!/usr/bin/env python3
"""
Console Hacker Edition - Discord bot to manage Docker containers with tmate
Final version with Role-based Plan Access (Basic / Premium)
- Uses .env for DISCORD_TOKEN and LOG_CHANNEL_ID
- database.txt runtime with format: user|container_id|ssh_command|created_ts
- Console-style messages using | and ```text
"""

import os
import time
import random
import asyncio
import subprocess
from typing import Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# --- pastikan database file ada (auto-create) ---
def ensure_database_file_exists():
    try:
        # 'a' akan membuat file kalau belum ada, tanpa mengubah isinya jika sudah ada
        with open(DATABASE_FILE, "a"):
            pass
    except Exception as e:
        print(f"Warning: failed to ensure {DATABASE_FILE} exists: {e}")

# panggil segera saat modul dimuat
ensure_database_file_exists()

# ---------------- CONFIG ----------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
LOG_CHANNEL_ID = None
_raw_log_id = os.getenv("LOG_CHANNEL_ID", "").strip()
if _raw_log_id:
    try:
        LOG_CHANNEL_ID = int(_raw_log_id)
    except Exception:
        LOG_CHANNEL_ID = None

DATABASE_FILE = "database.txt"
SERVER_LIMIT_PER_USER = 12

IMAGE_UBUNTU = "ubuntu-tmate:22.04"
IMAGE_DEBIAN = "debian-tmate:12"

AUTO_CLEAN_INTERVAL_SECONDS = 6 * 60 * 60   # 6 hours
INSTANCE_TTL_SECONDS = 30 * 24 * 60 * 60    # 30 days
# ----------------------------------------

# --------- PLANS (name -> memory in gigabytes) ----------
PLANS = {
    "basic": 1,
    "second": 2,
    "tritium": 3,
    "loudclass": 4,
    "growsof": 5,
    "akama": 6,
    "curious": 7,
    "prembasic": 8,
    "premsecond": 9,
    "premtritium": 10,
    "kaze": 11,
    "null": 12
}

# Role requirements per plan (None = anyone)
# Role names must match exactly as in your Discord server: "Basic", "Premium"
PLAN_ROLE_REQUIREMENTS = {
    # 1-3GB: available to all
    "basic": None,
    "second": "Basic",
    "tritium": "Basic",
    # 4-7GB: Basic role required
    "loudclass": "Basic",
    "growsof": "Basic",
    "akama": "Basic",
    "curious": "Basic",
    # 8-12GB: Premium role required
    "prembasic": "Premium",
    "premsecond": "Premium",
    "premtritium": "Premium",
    "kaze": "Premium",
    "null": "Premium"
}


def plan_to_memory_str(plan_name: str) -> Optional[str]:
    if not plan_name:
        return None
    key = plan_name.lower().replace(" ", "")
    if key in PLANS:
        return f"{PLANS[key]}g"
    return None


# --------------------------------

intents = discord.Intents.default()
intents.members = True  # needed to resolve member roles
bot = commands.Bot(command_prefix='/', intents=intents)


# ---------------- DB helpers ----------------
# Format: user|container_id|ssh_command|created_ts
def add_to_database(user: str, container_id: str, ssh_command: str):
    ts = int(time.time())
    line = f"{user}|{container_id}|{ssh_command}|{ts}\n"
    with open(DATABASE_FILE, "a") as f:
        f.write(line)


def read_database_lines() -> List[str]:
    if not os.path.exists(DATABASE_FILE):
        return []
    with open(DATABASE_FILE, "r") as f:
        return [l.strip() for l in f.readlines() if l.strip()]


def write_database_lines(lines: List[str]):
    with open(DATABASE_FILE, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def remove_from_database_by_container(container_id: str):
    lines = read_database_lines()
    new_lines = [l for l in lines if not (l.split("|")[1] == container_id)]
    write_database_lines(new_lines)


def get_user_servers(user: str) -> List[Tuple[str, str, int]]:
    servers = []
    for line in read_database_lines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        u, cid, ssh_cmd, ts = parts
        if u == user:
            try:
                servers.append((cid, ssh_cmd, int(ts)))
            except Exception:
                servers.append((cid, ssh_cmd, 0))
    return servers


def count_user_servers(user: str) -> int:
    return len(get_user_servers(user))


def find_container_for_user_by_name(user: str, container_name_or_ssh: str) -> Optional[str]:
    for cid, ssh_cmd, ts in get_user_servers(user):
        if container_name_or_ssh == cid or container_name_or_ssh in ssh_cmd:
            return cid
    return None


# ---------------- Utilities ----------------
def generate_random_port() -> int:
    return random.randint(1025, 65535)


def get_public_ip() -> str:
    probes = [
        "curl -s ifconfig.me",
        "curl -s icanhazip.com",
        "dig +short myip.opendns.com @resolver1.opendns.com"
    ]
    for cmd in probes:
        try:
            out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=8)
            ip = out.decode().strip()
            if ip:
                return ip
        except Exception:
            continue
    return "0.0.0.0"


async def capture_ssh_session_line_from_process(process: asyncio.subprocess.Process, timeout: int = 25) -> Optional[str]:
    """Read lines until a tmate ssh line is found or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if not line:
            await asyncio.sleep(0.1)
            continue
        s = line.decode("utf-8", errors="ignore").strip()
        if not s:
            continue
        low = s.lower()
        if "ssh session" in low or ("ssh" in low and ("@" in s or "-p" in s)):
            return s
        if "forwarding" in low or "http" in low:
            return s
    return None


# ---------------- Console Block ----------------
def console_block(lines: List[str]) -> str:
    """Return a ```text block containing lines prefixed with |"""
    body = "\n".join([f"| {l}" for l in lines])
    return f"```text\n{body}\n```"


# ---------------- Log channel finder ----------------
async def find_log_channels() -> List[discord.TextChannel]:
    channels = []
    if LOG_CHANNEL_ID:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            channels.append(ch)
    if not channels:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name == "instance-logs":
                    channels.append(ch)
    return channels


# ---------------- Notifications ----------------
async def notify_deletion(user_str: str, container_id: str, ssh_cmd: str, reason: str = "Expired (>= 30 days)"):
    dm_lines = [
        "🗑️ | ⚠️ Instance Auto-Deleted",
        f"🧾 | Container: {container_id}",
        f"🔐 | SSH: {ssh_cmd}",
        f"⏱️ | Reason: {reason}",
    ]
    dm_sent = False
    for guild in bot.guilds:
        member = guild.get_member_named(user_str)
        if member:
            try:
                await member.send(console_block(dm_lines))
                dm_sent = True
            except Exception:
                dm_sent = False
            break

    log_lines = [
        "🗑️ | ⚠️ Instance Auto-Deleted",
        f"👤 | Owner: {user_str}",
        f"🧾 | Container: {container_id}",
        f"🔐 | SSH: {ssh_cmd}",
        f"⏱️ | Action: Stopped & Removed (TTL reached)"
    ]
    channels = await find_log_channels()
    for ch in channels:
        try:
            await ch.send(console_block(log_lines))
        except Exception:
            pass

    if not dm_sent and not channels:
        print("AUTO-DELETE:", "\n".join(log_lines))


# ---------------- Cleanup Task ----------------
@tasks.loop(seconds=AUTO_CLEAN_INTERVAL_SECONDS)
async def cleanup_old_instances():
    try:
        lines = read_database_lines()
        if not lines:
            return
        now = int(time.time())
        removed_items = []
        for line in lines:
            parts = line.split("|")
            if len(parts) != 4:
                continue
            user, cid, ssh_cmd, ts = parts
            try:
                created = int(ts)
            except Exception:
                created = 0
            age = now - created
            if age >= INSTANCE_TTL_SECONDS:
                try:
                    subprocess.run(["docker", "stop", cid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["docker", "rm", "-f", cid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                removed_items.append((user, cid, ssh_cmd))
        if removed_items:
            remain_lines = [l for l in lines if not any((l.split("|")[1] == cid) for (_, cid, _) in removed_items)]
            write_database_lines(remain_lines)
            for user, cid, ssh_cmd in removed_items:
                await notify_deletion(user, cid, ssh_cmd)
    except Exception as e:
        print("Cleanup error:", e)


# ---------------- Bot lifecycle ----------------
@bot.event
async def on_ready():
    print(f"Bot ready as {bot.user} (id: {bot.user.id})")
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Sync error:", e)
    if not cleanup_old_instances.is_running():
        cleanup_old_instances.start()


# ---------------- Role check helper ----------------
def user_has_required_role_for_plan(interaction: discord.Interaction, plan_name: str) -> Tuple[bool, Optional[str]]:
    """Return (allowed, required_role_name_or_None)."""
    key = plan_name.lower().replace(" ", "")
    required = PLAN_ROLE_REQUIREMENTS.get(key)
    if required is None:
        return True, None  # open to all
    # Try to resolve member for the guild where command was invoked
    member = None
    try:
        if isinstance(interaction.user, discord.Member):
            member = interaction.user
        elif interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
    except Exception:
        member = None
    if member:
        # check role names case-insensitive
        for r in member.roles:
            if r.name.lower() == required.lower():
                return True, required
    return False, required


# ---------------- Commands ----------------
@app_commands.command(name="deploy-ubuntu", description="Creates a new Instance with Ubuntu (tmate) and selected plan")
@app_commands.describe(plan="Name of plan (e.g. basic, prembasic, kaze, null)")
async def deploy_ubuntu(interaction: discord.Interaction, plan: str):
    await create_instance_task(interaction, IMAGE_UBUNTU, "Ubuntu 22.04", plan)


@app_commands.command(name="deploy-debian", description="Creates a new Instance with Debian (tmate) and selected plan")
@app_commands.describe(plan="Name of plan (e.g. basic, prembasic, kaze, null)")
async def deploy_debian(interaction: discord.Interaction, plan: str):
    await create_instance_task(interaction, IMAGE_DEBIAN, "Debian", plan)


bot.tree.add_command(deploy_ubuntu)
bot.tree.add_command(deploy_debian)


async def create_instance_task(interaction: discord.Interaction, image: str, os_label: str, plan_name: str):
    # Validate plan -> memory
    mem_str = plan_to_memory_str(plan_name)
    if mem_str is None:
        plan_lines = ["❌ | Unknown plan: " + (plan_name or "<empty>"), "", "Available Plans:"]
        for k, v in PLANS.items():
            req = PLAN_ROLE_REQUIREMENTS.get(k)
            req_text = f"(requires role: {req})" if req else "(open)"
            plan_lines.append(f"{k} | {v}GB {req_text}")
        await interaction.response.send_message(console_block(plan_lines), ephemeral=True)
        return

    # Role check
    allowed, required_role = user_has_required_role_for_plan(interaction, plan_name)
    if not allowed:
        await interaction.response.send_message(console_block([
            "❌ | Permission denied",
            f"🧠 | Plan `{plan_name}` requires role: {required_role}",
            "🔒 | Contact an admin to get the role."
        ]), ephemeral=True)
        return

    await interaction.response.send_message(console_block([f"⚙️ | Deploy: {os_label} | Plan: {plan_name}", f"⏳ | Status: Creating instance with {mem_str} RAM..."]), ephemeral=True)
    user_str = str(interaction.user)
    if count_user_servers(user_str) >= SERVER_LIMIT_PER_USER:
        await interaction.followup.send(console_block(["❌ | Error: Instance limit reached"]), ephemeral=True)
        return

    # Docker run with memory limit
    try:
        out = subprocess.check_output([
            "docker", "run", "-d", "--memory", mem_str, "--privileged", "--cap-add=ALL", image, "/sbin/init"
        ], stderr=subprocess.STDOUT, timeout=90)
        container_id = out.decode().strip()
    except subprocess.CalledProcessError as e:
        err = e.output.decode(errors="ignore") if hasattr(e, "output") else str(e)
        await interaction.followup.send(console_block([f"❌ | Failed to create container", f"🧾 | Error: {err}"]), ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(console_block([f"❌ | Unexpected error: {e}"]), ephemeral=True)
        return

    # exec tmate inside container
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except Exception as e:
        subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await interaction.followup.send(console_block([f"❌ | Failed to run tmate: {e}"]), ephemeral=True)
        return

    ssh_line = await capture_ssh_session_line_from_process(proc, timeout=25)
    if not ssh_line:
        ssh_line = await capture_ssh_session_line_from_process(proc, timeout=10)

    if ssh_line:
        db_ssh = f"{ssh_line} | plan={plan_name}"
        add_to_database(user_str, container_id, db_ssh)
        dm_lines = [
            "✅ | Instance Created",
            f"⚙️ | OS: {os_label}",
            f"🧾 | Container: {container_id}",
            f"🔐 | SSH: {ssh_line}",
            f"💾 | Plan: {plan_name} ({mem_str})",
            f"🌐 | Host IP: {get_public_ip()}",
            f"⏱️ | Created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
            "",
            "Note: Instance will be auto-removed after 30 days."
        ]
        try:
            await interaction.user.send(console_block(dm_lines))
            await interaction.followup.send(console_block([f"✅ | Instance created. Check your DMs for SSH info."]), ephemeral=True)
        except Exception:
            await interaction.followup.send(console_block([f"✅ | Instance created", f"🔐 | SSH: {ssh_line}", f"💾 | Plan: {plan_name} ({mem_str})", f"🌐 | Host IP: {get_public_ip()}"]), ephemeral=False)
    else:
        subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await interaction.followup.send(console_block(["❌ | Failed to capture tmate SSH session. Instance removed."]), ephemeral=True)


# ---------- start / stop / restart / regen-ssh (unchanged) ----------
@app_commands.command(name="start", description="Start an instance")
@app_commands.describe(container_name="container id or part of ssh command")
async def cmd_start(interaction: discord.Interaction, container_name: str):
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.response.send_message(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    try:
        subprocess.run(["docker", "start", cid], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = await asyncio.create_subprocess_exec("docker", "exec", cid, "tmate", "-F", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ssh_line = await capture_ssh_session_line_from_process(proc, timeout=15)
        if ssh_line:
            lines = read_database_lines()
            new = []
            for l in lines:
                if l.split("|")[1] == cid:
                    parts = l.split("|")
                    if len(parts) == 4:
                        parts[2] = ssh_line
                        new.append("|".join(parts))
                        continue
                new.append(l)
            write_database_lines(new)
            try:
                await interaction.user.send(console_block([f"✅ | Instance Started", f"🧾 | Container: {cid}", f"🔐 | SSH: {ssh_line}"]))
                await interaction.response.send_message(console_block(["✅ | Instance started. Check your DMs."]), ephemeral=True)
            except Exception:
                await interaction.response.send_message(console_block([f"✅ | Instance started", f"🔐 | SSH: {ssh_line}"]), ephemeral=False)
        else:
            await interaction.response.send_message(console_block(["⚠️ | Instance started but no SSH session captured. Use /regen-ssh"]), ephemeral=True)
    except subprocess.CalledProcessError as e:
        await interaction.response.send_message(console_block([f"❌ | Error starting instance: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_start)


@app_commands.command(name="stop", description="Stop an instance")
@app_commands.describe(container_name="container id or part of ssh command")
async def cmd_stop(interaction: discord.Interaction, container_name: str):
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.response.send_message(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    try:
        subprocess.run(["docker", "stop", cid], check=True)
        await interaction.response.send_message(console_block([f"🧾 | Instance stopped", f"🔒 | Container: {cid}"]), ephemeral=True)
    except subprocess.CalledProcessError as e:
        await interaction.response.send_message(console_block([f"❌ | Error stopping instance: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_stop)


@app_commands.command(name="restart", description="Restart an instance")
@app_commands.describe(container_name="container id or part of ssh command")
async def cmd_restart(interaction: discord.Interaction, container_name: str):
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.response.send_message(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    try:
        subprocess.run(["docker", "restart", cid], check=True)
        proc = await asyncio.create_subprocess_exec("docker", "exec", cid, "tmate", "-F", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ssh_line = await capture_ssh_session_line_from_process(proc, timeout=15)
        if ssh_line:
            lines = read_database_lines()
            new = []
            for l in lines:
                if l.split("|")[1] == cid:
                    parts = l.split("|")
                    if len(parts) == 4:
                        parts[2] = ssh_line
                        new.append("|".join(parts))
                        continue
                new.append(l)
            write_database_lines(new)
            try:
                await interaction.user.send(console_block([f"🔁 | Instance restarted", f"🧾 | Container: {cid}", f"🔐 | SSH: {ssh_line}"]))
                await interaction.response.send_message(console_block(["✅ | Instance restarted. Check your DMs."]), ephemeral=True)
            except Exception:
                await interaction.response.send_message(console_block([f"✅ | Instance restarted", f"🔐 | SSH: {ssh_line}"]), ephemeral=False)
        else:
            await interaction.response.send_message(console_block(["⚠️ | Restarted but no SSH session captured. Use /regen-ssh"]), ephemeral=True)
    except subprocess.CalledProcessError as e:
        await interaction.response.send_message(console_block([f"❌ | Error restarting instance: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_restart)


@app_commands.command(name="regen-ssh", description="Generate a new SSH session for your instance")
@app_commands.describe(container_name="container id or part of ssh command")
async def cmd_regen_ssh(interaction: discord.Interaction, container_name: str):
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.response.send_message(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    try:
        proc = await asyncio.create_subprocess_exec("docker", "exec", cid, "tmate", "-F", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ssh_line = await capture_ssh_session_line_from_process(proc, timeout=15)
        if ssh_line:
            lines = read_database_lines()
            new = []
            for l in lines:
                if l.split("|")[1] == cid:
                    parts = l.split("|")
                    if len(parts) == 4:
                        parts[2] = ssh_line
                        new.append("|".join(parts))
                        continue
                new.append(l)
            write_database_lines(new)
            try:
                await interaction.user.send(console_block([f"🔐 | New SSH session generated", f"🧾 | Container: {cid}", f"SSH: {ssh_line}"]))
                await interaction.response.send_message(console_block(["✅ | New SSH session generated. Check your DMs."]), ephemeral=True)
            except Exception:
                await interaction.response.send_message(console_block([f"✅ | New SSH session", f"SSH: {ssh_line}"]), ephemeral=False)
        else:
            await interaction.response.send_message(console_block(["❌ | Failed to capture SSH session."]), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(console_block([f"❌ | Error: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_regen_ssh)


# list
@app_commands.command(name="list", description="List your instances")
async def cmd_list(interaction: discord.Interaction):
    user_str = str(interaction.user)
    servers = get_user_servers(user_str)
    if not servers:
        await interaction.response.send_message(console_block(["ℹ️ | You have no servers."]), ephemeral=True)
        return
    lines = [f"📦 | Your Instances ({len(servers)})", "------------------------------------"]
    for cid, ssh_cmd, ts in servers:
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        lines.append(f"{cid} | SSH: {ssh_cmd} | Created: {created}")
    await interaction.response.send_message(console_block(lines), ephemeral=True)


bot.tree.add_command(cmd_list)


# plan
@app_commands.command(name="plan", description="Show available plans and their RAM limits")
async def cmd_plan(interaction: discord.Interaction):
    lines = ["⚙️ | Available Plans", "------------------------------------"]
    for name, gb in PLANS.items():
        req = PLAN_ROLE_REQUIREMENTS.get(name)
        req_text = f"(requires role: {req})" if req else "(open)"
        lines.append(f"{name} | {gb}GB {req_text}")
    lines.append("")
    lines.append("Usage: `/deploy-ubuntu <plan>` or `/deploy-debian <plan>`")
    await interaction.response.send_message(console_block(lines), ephemeral=True)


bot.tree.add_command(cmd_plan)


# remove
@app_commands.command(name="remove", description="Remove an instance")
@app_commands.describe(container_name="container id or part of ssh command")
async def cmd_remove(interaction: discord.Interaction, container_name: str):
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.response.send_message(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    try:
        subprocess.run(["docker", "stop", cid], check=False)
        subprocess.run(["docker", "rm", "-f", cid], check=False)
        remove_from_database_by_container(cid)
        try:
            await interaction.user.send(console_block([f"🗑️ | Instance Removed", f"🧾 | Container: {cid}", "Action: Manual removal by user."]))
        except Exception:
            pass
        channels = await find_log_channels()
        for ch in channels:
            try:
                await ch.send(console_block([f"🗑️ | Instance Removed", f"👤 | Owner: {user_str}", f"🧾 | Container: {cid}", "Action: Manual removal by user."]))
            except Exception:
                pass
        await interaction.response.send_message(console_block([f"✅ | Instance {cid} removed."]), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(console_block([f"❌ | Error removing instance: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_remove)

# ============================================================
# ⚙️ Command: /add-role (otomatis buat role jika belum ada)
# ============================================================
@app_commands.command(name="add-role", description="Tambahkan role ke user dengan ID Discord (Admin only, auto-create role)")
@app_commands.describe(role_name="Nama role yang ingin ditambahkan", user_id="ID Discord user target")
async def cmd_add_role(interaction: discord.Interaction, role_name: str, user_id: str):
    # pastikan hanya admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(console_block([
            "❌ | Kamu tidak punya izin untuk menjalankan perintah ini.",
            "🔒 | Dibutuhkan izin Administrator."
        ]), ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message(console_block([
            "❌ | Perintah ini hanya bisa digunakan di dalam server, bukan di DM."
        ]), ephemeral=True)
        return

    # cari role berdasarkan nama
    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)

    # jika tidak ada, buat role baru
    if not role:
        try:
            role = await guild.create_role(
                name=role_name,
                reason=f"Auto-created by bot via /add-role ({interaction.user})"
            )
            await interaction.response.send_message(console_block([
                f"⚙️ | Role `{role_name}` tidak ditemukan, membuat otomatis...",
                f"✅ | Role `{role.name}` berhasil dibuat."
            ]), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(console_block([
                "❌ | Bot tidak punya izin untuk membuat role baru.",
                "💡 | Pastikan bot memiliki izin **Manage Roles**."
            ]), ephemeral=True)
            return

    # cari user berdasarkan ID
    try:
        member = await guild.fetch_member(int(user_id))
    except Exception:
        member = None

    if not member:
        await interaction.followup.send(console_block([
            f"❌ | Tidak dapat menemukan user dengan ID `{user_id}`."
        ]), ephemeral=True)
        return

    # tambahkan role
    try:
        await member.add_roles(role, reason=f"Added by {interaction.user} via /add-role")
        await interaction.followup.send(console_block([
            f"✅ | Berhasil menambahkan role `{role.name}` ke {member.display_name} ({member.id})"
        ]), ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(console_block([
            "❌ | Bot tidak memiliki izin untuk menambahkan role ini.",
            "💡 | Pastikan role bot lebih tinggi dari role target."
        ]), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(console_block([
            f"❌ | Terjadi error: {e}"
        ]), ephemeral=True)

bot.tree.add_command(cmd_add_role)

# ============================================================
# ⚙️ Command: /remove-role (otomatis hapus role kosong)
# ============================================================
@app_commands.command(name="remove-role", description="Hapus role dari user dan auto-delete jika role tidak terpakai (Admin only)")
@app_commands.describe(role_name="Nama role yang ingin dihapus", user_id="ID Discord user target")
async def cmd_remove_role(interaction: discord.Interaction, role_name: str, user_id: str):
    # hanya admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(console_block([
            "❌ | Kamu tidak punya izin untuk menjalankan perintah ini.",
            "🔒 | Dibutuhkan izin Administrator."
        ]), ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message(console_block([
            "❌ | Perintah ini hanya bisa digunakan di dalam server, bukan di DM."
        ]), ephemeral=True)
        return

    # cari role
    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
    if not role:
        await interaction.response.send_message(console_block([
            f"❌ | Role `{role_name}` tidak ditemukan di server ini."
        ]), ephemeral=True)
        return

    # cari member
    try:
        member = await guild.fetch_member(int(user_id))
    except Exception:
        member = None

    if not member:
        await interaction.response.send_message(console_block([
            f"❌ | Tidak dapat menemukan user dengan ID `{user_id}`."
        ]), ephemeral=True)
        return

    # hapus role dari user
    try:
        await member.remove_roles(role, reason=f"Removed by {interaction.user} via /remove-role")
        msg_lines = [
            f"✅ | Role `{role.name}` berhasil dihapus dari {member.display_name} ({member.id})"
        ]

        # cek apakah role masih digunakan oleh siapa pun
        member_count_with_role = sum(1 for m in guild.members if role in m.roles)
        if member_count_with_role == 0:
            try:
                await role.delete(reason="Auto-clean: role tidak lagi digunakan.")
                msg_lines.append(f"🧹 | Role `{role.name}` tidak digunakan siapa pun, telah dihapus otomatis.")
            except discord.Forbidden:
                msg_lines.append("⚠️ | Tidak dapat menghapus role (izin tidak cukup).")
        
        await interaction.response.send_message(console_block(msg_lines), ephemeral=True)

    except discord.Forbidden:
        await interaction.response.send_message(console_block([
            "❌ | Bot tidak memiliki izin untuk menghapus role ini.",
            "💡 | Pastikan role bot lebih tinggi dari role target."
        ]), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(console_block([
            f"❌ | Terjadi error: {e}"
        ]), ephemeral=True)

bot.tree.add_command(cmd_remove_role)

# ============================================================
# ⚙️ Command: /list-roles (lihat semua role dan jumlah member)
# ============================================================
@app_commands.command(name="list-roles", description="Lihat semua role di server beserta jumlah penggunanya")
async def cmd_list_roles(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message(console_block([
            "❌ | Perintah ini hanya bisa digunakan di dalam server, bukan di DM."
        ]), ephemeral=True)
        return

    roles = [r for r in guild.roles if r.name != "@everyone"]
    if not roles:
        await interaction.response.send_message(console_block([
            "⚠️ | Tidak ada role di server ini selain `@everyone`."
        ]), ephemeral=True)
        return

    # urutkan role dari atas ke bawah sesuai hirarki
    roles = sorted(roles, key=lambda r: r.position, reverse=True)

    # buat daftar role dengan jumlah member
    lines = []
    for role in roles:
        count = sum(1 for m in guild.members if role in m.roles)
        lines.append(f"💠 | {role.name}  →  {count} member{'s' if count != 1 else ''}")

    # kirim sebagai block teks gaya console
    output = ["🧾 | **Daftar Role Server**", *lines]
    await interaction.response.send_message(console_block(output), ephemeral=True)

bot.tree.add_command(cmd_list_roles)

# ping
@app_commands.command(name="ping", description="Check bot latency")
async def cmd_ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(console_block([f"🏓 | Pong! Latency: {latency}ms"]), ephemeral=True)


bot.tree.add_command(cmd_ping)


# port-add (reverse tunnel via serveo.net)
@app_commands.command(name="port-add", description="Adds port forwarding (via serveo.net)")
@app_commands.describe(container_name="container id or part of ssh command", container_port="Port inside container")
async def cmd_port_add(interaction: discord.Interaction, container_name: str, container_port: int):
    await interaction.response.send_message(console_block([f"🌐 | Port forwarding: setting up {container_port} ..."]), ephemeral=True)
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.followup.send(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    public_port = generate_random_port()
    inner_cmd = f"ssh -o StrictHostKeyChecking=no -R {public_port}:localhost:{container_port} serveo.net -N -f"
    try:
        await asyncio.create_subprocess_exec("docker", "exec", cid, "bash", "-lc", inner_cmd,
                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        public_ip = get_public_ip()
        await interaction.followup.send(console_block([f"✅ | Port forwarded", f"🌐 | Access: {public_ip}:{public_port}"]), ephemeral=False)
    except Exception as e:
        await interaction.followup.send(console_block([f"❌ | Error setting port forwarding: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_port_add)


# port-http (serveo HTTP forwarding)
@app_commands.command(name="port-http", description="Forward HTTP traffic to your container (via serveo.net)")
@app_commands.describe(container_name="container id or part of ssh command", container_port="Port in container")
async def cmd_port_http(interaction: discord.Interaction, container_name: str, container_port: int):
    user_str = str(interaction.user)
    cid = find_container_for_user_by_name(user_str, container_name)
    if not cid:
        await interaction.response.send_message(console_block(["❌ | No instance found for your user."]), ephemeral=True)
        return
    try:
        proc = await asyncio.create_subprocess_exec("docker", "exec", cid, "ssh", "-o", "StrictHostKeyChecking=no", "-R", f"80:localhost:{container_port}", "serveo.net",
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out_line = await proc.stdout.readline()
        url_line = out_line.decode("utf-8", errors="ignore").strip() if out_line else ""
        if url_line:
            await interaction.response.send_message(console_block([f"✅ | Website forwarded", f"🔗 | {url_line}"]), ephemeral=False)
        else:
            await interaction.response.send_message(console_block(["⚠️ | Failed to capture forwarding URL."]), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(console_block([f"❌ | Error executing website forwarding: {e}"]), ephemeral=True)


bot.tree.add_command(cmd_port_http)


# help
@app_commands.command(name="help", description="Show help")
async def cmd_help(interaction: discord.Interaction):
    lines = [
        "⚙️ | Commands",
        "------------------------------------",
        "/deploy-ubuntu <plan> | Deploy Ubuntu with chosen plan (e.g. basic, prembasic, kaze, null)",
        "/deploy-debian <plan> | Deploy Debian with chosen plan",
        "/plan | Show available plans",
        "/list | List your instances",
        "/remove <id/name> | Remove instance",
        "/start <id/name> | Start instance",
        "/stop <id/name> | Stop instance",
        "/restart <id/name> | Restart instance",
        "/regen-ssh <id/name> | Regenerate SSH session",
        "/port-add <id/name> <port> | Forward port",
        "/port-http <id/name> <port> | Forward HTTP",
        "/ping | Bot latency"
    ]
    await interaction.response.send_message(console_block(lines), ephemeral=True)


bot.tree.add_command(cmd_help)


# ---------------- Run ----------------
if __name__ == "__main__":
    if not TOKEN:
        print("Please set DISCORD_TOKEN in .env before running.")
        exit(1)
    bot.run(TOKEN)
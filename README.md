# Hetzner Provisioner Discord Bot
A small Discord slash-command bot that lets users provision Hetzner Cloud VMs from Discord DMs or a server, then stores a per-user mapping so they can fetch VM info later. It also caches Hetzner “options” (locations, images, networks, SSH keys, firewalls) to power autocompletion and validation. :contentReference[oaicite:0]{index=0}

## What it does

### Core flow
- Loads `DISCORD_TOKEN` and `HCLOUD_TOKEN` from environment variables (via `.env` support). If either is missing, it crashes on startup. :contentReference[oaicite:1]{index=1}
- Connects to Hetzner Cloud using the official Python client. :contentReference[oaicite:2]{index=2}
- Uses a local SQLite database (`servers.db`) to:
  - remember which Hetzner server IDs belong to which Discord user (by server name and ID)
  - store per-user defaults for network / ssh key / firewall (needed when you have multiple of those) :contentReference[oaicite:3]{index=3}

### Commands (slash commands)
- `/create name location image app count`
  - Creates **1–10 VMs** (names become `NAME`, `NAME1`, `NAME2`, …).
  - Enforces Hetzner quota check when possible.
  - Uses a fixed default server type: `cx23`.
  - Only allows **x86-compatible images** (filters Hetzner images by architecture).
  - Optionally injects cloud-init for:
    - `coolify` (installs Docker + Coolify installer)
    - `wireguard` (installs wireguard + enables forwarding)
    - `none` (default)
  - After creation, waits ~20 seconds, then fetches server details and posts an embed with IPs, datacenter, image, status, etc. :contentReference[oaicite:4]{index=4}

- `/s server`
  - Fetches info (embed) for one of **your** servers by **name or ID** (looks it up from the SQLite mapping first). :contentReference[oaicite:5]{index=5}

- `/refresh`
  - Rebuilds the Hetzner cache (locations, server types, images, networks, ssh keys, firewalls) used for validation + autocomplete. :contentReference[oaicite:6]{index=6}

- `/images`
  - Lists all valid **x86** image names (the ones the bot will accept for `/create`). :contentReference[oaicite:7]{index=7}

- `/locations`
  - Lists Hetzner location names (e.g. `hel1`, `nbg1`). :contentReference[oaicite:8]{index=8}

- `/networks`, `/sshkeys`, `/firewalls`
  - Lists the discovered Hetzner resources (with IDs), so you can pick defaults. :contentReference[oaicite:9]{index=9}

- `/setdefaults network_id ssh_key_id firewall_id`
  - Saves your per-user default IDs in SQLite.
  - This matters if your Hetzner account has *multiple* networks/SSH keys/firewalls, because VM creation needs exactly one of each. :contentReference[oaicite:10]{index=10}

## How resource selection works (network / ssh key / firewall)
When creating a VM, the bot must choose:
- 1 network
- 1 ssh key
- 1 firewall

Selection rules:
1. If only one exists in the Hetzner account, it uses that automatically.
2. If multiple exist, it uses the user’s stored default (from `/setdefaults`).
3. If multiple exist and no default is set, VM creation fails and prints available IDs with instructions. :contentReference[oaicite:11]{index=11}

## Project layout / files
- `bot.py` (everything is in one file)
- `servers.db` (created automatically on first run, SQLite) :contentReference[oaicite:12]{index=12}

## Dependencies

Python 3.10+ recommended (uses modern typing, dataclasses, async patterns).

### Install
```bash
pip install -U discord.py python-dotenv hcloud

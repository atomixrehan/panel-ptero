"""Configuration from environment variables – for containers such as Pterodactyl, where nobody can type into a wizard.

With DISCORD_TOKEN set, the environment is the source of truth on EVERY start: change a variable in the Pterodactyl
"Startup" tab, restart, done. Pterodactyl's own SERVER_MEMORY / SERVER_PORT are used to size RAM and set the port."""
from __future__ import annotations

import os
from typing import Optional

from . import installer, props
from .config import SERVER_DIR, Config

TRUE = {"1", "true", "yes", "on", "y"}
PROPS_FILE = SERVER_DIR / "server.properties"
ID_VARS = {"OP_ROLE_ID": "op_role_id", "PANEL_CHANNEL_ID": "panel_channel_id", "CROSSCHAT_CHANNEL_ID": "crosschat_channel_id",
           "CONSOLE_CHANNEL_ID": "console_channel_id", "CONFIGURE_CHANNEL_ID": "configure_channel_id"}
BOT_OVERHEAD_MB = 220   # the bot (~70-100 MB) + JVM memory that lives outside the heap


def is_container() -> bool:
    """True inside a Pterodactyl (Wings) container, or when PANEL_HEADLESS=1."""
    return bool(os.environ.get("P_SERVER_UUID") or os.environ.get("PANEL_HEADLESS"))


def wants_env() -> bool:
    return bool(os.environ.get("DISCORD_TOKEN", "").strip())


def _flag(name: str) -> Optional[bool]:
    v = os.environ.get(name)
    return None if v is None or not v.strip() else v.strip().lower() in TRUE


def container_ram_mb() -> Optional[int]:
    """Java heap that fits the container limit (SERVER_MEMORY, MB): leaves room for the bot and the JVM's own overhead."""
    try:
        total = int(os.environ.get("SERVER_MEMORY", "0"))
    except ValueError:
        return None
    return max(512, int((total - BOT_OVERHEAD_MB) * 0.85)) if total > 0 else None


def _set_prop(key: str, value: str, changes: list, raw: bool = False) -> None:
    current = props.read(PROPS_FILE).get(key)
    if raw or current != value:
        before = PROPS_FILE.read_text(encoding="utf-8") if PROPS_FILE.exists() else ""
        props.write_key(PROPS_FILE, key, value, raw=raw)
        if (PROPS_FILE.read_text(encoding="utf-8") if PROPS_FILE.exists() else "") != before:
            changes.append(key)


def apply_env(cfg: Config) -> list:
    """Copy the environment into config.json / server.properties. Returns what changed. Exits with a clear message if required values are missing."""
    env, changes, missing = os.environ, [], []

    def put(key, value, label):
        if cfg[key] != value:
            cfg[key] = value
            changes.append(label)

    put("token", env["DISCORD_TOKEN"].strip(), "token")
    for var, key in ID_VARS.items():
        v = env.get(var, "").strip()
        if v.isdigit():
            put(key, int(v), var.lower())
        else:
            missing.append(var)
    if missing:
        raise SystemExit("Missing or invalid variables (numeric Discord IDs required): " + ", ".join(missing) +
                         "\nSet them in the Pterodactyl 'Startup' tab (or your environment) and start again.")
    put("eula", bool(_flag("EULA")), "eula")

    version = env.get("MC_VERSION", "latest").strip().lower()
    if version in ("", "latest"):
        if not cfg["mc_version"]:
            put("mc_version", installer.latest_stable_version(installer.paper_versions()), "mc_version")
    else:
        put("mc_version", version, "mc_version")

    preset = env.get("PLUGIN_PRESET", "recommended").strip().lower()
    plugins = list(installer.RECOMMENDED) if preset in ("", "recommended") else [] if preset == "none" else \
        [p.strip() for p in preset.split(",") if p.strip().replace("-", "").replace("_", "").isalnum()]
    if "PLUGIN_PRESET" in env or not cfg["setup_done"]:
        if sorted(plugins) != sorted(cfg["plugins"]):
            cfg["plugins"] = plugins
            cfg["install_done"] = False      # the next start installs whatever is missing
            changes.append("plugins")

    ram = env.get("RAM_MB", "").strip()
    new_ram = int(ram) if ram.isdigit() and int(ram) >= 512 else container_ram_mb()
    if new_ram:
        put("ram_mb", new_ram, "ram_mb")

    minutes = env.get("AUTOSHUT_MINUTES", "").strip()
    if minutes.isdigit():
        enabled, mins = int(minutes) > 0, max(1, int(minutes))
        if cfg["autoshut"]["enabled"] != enabled or (enabled and cfg["autoshut"]["minutes"] != mins):
            cfg["autoshut"] = {"enabled": enabled, "minutes": mins if enabled else cfg["autoshut"]["minutes"]}
            changes.append("autoshut")
    auto = _flag("AUTOSTART_ON_JOIN")
    if auto is not None:
        put("autostart", auto, "autostart")
    mode = env.get("COMMANDS", "").strip().lower()
    if mode in ("full", "minimal", "off"):
        put("commands", mode, "commands")

    if not cfg["setup_done"] or cfg["setup_steps"] != ["*"]:
        cfg["setup_done"], cfg["setup_steps"] = True, ["*"]
        changes.append("setup")
    if changes:
        cfg.save()

    # server.properties (only what was asked for; idempotent)
    prop_changes: list = []
    motd = env.get("MOTD", "").strip()
    if motd:
        want = props.motd_value(motd)
        raw_now = ""
        if PROPS_FILE.exists():
            for line in PROPS_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("motd="):
                    raw_now = line[5:]
        if raw_now != want:
            props.write_key(PROPS_FILE, "motd", want, raw=True)
            prop_changes.append("motd")
    maxp = env.get("MAX_PLAYERS", "").strip()
    if maxp.isdigit() and 1 <= int(maxp) <= 1000:
        _set_prop("max-players", maxp, prop_changes)
    port = env.get("SERVER_PORT", "").strip()
    if is_container() and port.isdigit():
        _set_prop("server-port", port, prop_changes)
    return changes + [f"server.properties:{k}" for k in prop_changes]


def sync_version(cfg: Config, log=print) -> None:
    """MC_VERSION raised in the Startup tab -> back up the world, then download the newer Paper. Never downgrades on its own."""
    have = installer.installed_version()
    want = cfg["mc_version"]
    if not have or have == want or not (SERVER_DIR / "server.jar").exists():
        return
    if installer.vkey(want) < installer.vkey(have):
        log(f"[panel] MC_VERSION={want} is OLDER than the installed Paper {have}; keeping {have} "
            "(downgrading can corrupt worlds – restore a backup and delete server.jar if you really want that).")
        cfg["mc_version"] = have
        cfg.save()
        return
    log(f"[panel] Updating Paper {have} -> {want}")
    from . import backups
    try:
        if backups.world_dirs():
            log(f"[panel] World backed up first: {backups.create_zip('pre-upgrade').name}")
        installer.download_server(want, log)
    except Exception as e:  # noqa: BLE001
        log(f"[panel] Version change failed ({e}); staying on {have}")
        cfg["mc_version"] = have
        cfg.save()

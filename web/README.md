# The Hub (web board)

A browser version of the Desk Hub display, called The Hub here, for a tablet (a Fire tablet in Fully Kiosk, for example) or any screen.

The Windows companion keeps collecting the data. It connects to this server the same way it connects to the ESP32 board, and the server shows everything on a web page. Taps on the page (media, shortcuts, Steam, alerts, speed test) go back to the companion.

## Install (Debian or Ubuntu)

    sudo sh install.sh

This installs a service that listens on port 80 and prints a token. It reads `/etc/deskhub-web.env` (`DESKHUB_TOKEN`, `DESKHUB_PORT`, `DESKHUB_CONTROLS=0` for view-only).

## Connect the companion

In the companion's `config.toml`, set the host and token to the server's:

    [hub]
    host = "SERVER-IP"
    token = "THE-TOKEN"

Restart the companion. The page shows "PC connected". (The companion talks to one hub at a time, so the ESP32 board and this server take turns.)

## Open the page

Go to `http://SERVER-IP/` and type the short page code (`DESKHUB_PIN` in `/etc/deskhub-web.env`, printed by the installer). The page remembers it on that device, so you only type it once.

For weather, set `DESKHUB_LAT`, `DESKHUB_LON` and optionally `DESKHUB_UNIT=c` in the same file, then `sudo systemctl restart deskhub-web`.

## More options

All of these go in `/etc/deskhub-web.env`, then `sudo systemctl restart deskhub-web`.

- `DESKHUB_CHECKS="pve1=10.0.0.5:8006,NAS=10.0.0.9:5001"`: hosts to watch. They show as Lab status and keep working when the PC is off.
- `DESKHUB_LAT`, `DESKHUB_LON`, `DESKHUB_UNIT`: weather place (the forecast card and header).
- `DESKHUB_CONTROLS=0`: view-only page.

The page also has a timer, a GitHub chart (when the companion has a GitHub token), and dims itself from 10 pm to 6 am.

## Two Claude logins

To show two Claude accounts side by side, give each its own Claude Code config folder and list them in the companion's `config.toml`:

    [claude]
    live_limits = true

    [[claude.accounts]]
    name = "Personal"
    config_dir = "~/.claude"

    [[claude.accounts]]
    name = "Work"
    config_dir = "~/.claude-work"

Sign the second login in once: set `CLAUDE_CONFIG_DIR` to its folder, run `claude`, then `/login`. Keep the `[[claude.accounts]]` blocks last in the `[claude]` section. Limits for any login other than `~/.claude` need `live_limits = true`; token counts work either way. The ESP32 board shows the first login.

## Set up a PC with one line

On a Windows PC, run this in PowerShell (use your server's address and page code). It downloads the companion, installs what it needs, writes `config.toml` with the right token and starts it:

    irm "http://SERVER-IP/setup.ps1?code=PAGE-CODE" | iex

The script comes from the server, so the token never has to be copied by hand. Anyone with the page code can fetch it, so keep the server on a trusted network.

## Read-only data feeds (NAS, doors, calls, alerts)

Put a JSON file at `/etc/deskhub-feeds.json` (or set `DESKHUB_FEEDS`) and restart the service. Each entry turns on one card:

    {
      "nas":    {"host": "10.0.0.9", "conf_dir": "/etc/deskhub-snmp"},
      "doors":  {"url": "http://door-server/hub-feed", "token": "..."},
      "calls":  {"url": "http://pbx/hub-feed", "token": "..."},
      "jarvis": {"url": "http://jarvis/hub-feed", "token": "..."}
    }

- **NAS** (Synology): read with SNMP, which only reads. Install `snmp` on the server, enable SNMPv3 on the NAS, and put the user in `conf_dir/snmp.conf` (`defVersion 3`, `defSecurityLevel authPriv`, `defSecurityName`, `defAuthType SHA`, `defAuthPassphrase`, `defPrivType AES`, `defPrivPassphrase`).
- **doors, calls, jarvis** (examples in `feeds/`): the server fetches the URL every 10 seconds with `Authorization: Bearer <token>`. The reply is JSON: `{"events": [{"t": epoch, "door", "who", "result": "granted|denied"}]}`, `{"calls": [{"t", "dir": "in|out|missed", "from", "to", "name", "secs"}]}` or `{"alerts": [{"t", "level": "info|warn|crit", "title", "detail", "resolved"}]}`. Only these fields are kept, trimmed to length.

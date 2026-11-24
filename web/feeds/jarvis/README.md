# JARVIS alerts feed for The Hub

A read-only feed that runs on the JARVIS host. It runs one fixed `SELECT` on the `alerts` table through the database container, as a database user that can only read that table, and serves the result as JSON behind a bearer token. It does not touch JARVIS's code, containers or configuration.

- `hubfeed.py`: the service (Python standard library only).
- `jarvis-hubfeed.service`: systemd unit.
- `hubfeed-setup.sh`: creates the `SELECT`-only database user, writes `/opt/hubfeed/conf.json` (token and database login, mode 600), installs and starts the service. Copy the three files to `/tmp` on the JARVIS host and run it as root.

On The Hub, add to `/etc/deskhub-feeds.json`:

    "jarvis": {"url": "http://JARVIS-HOST:8097/", "token": "<token from conf.json>"}

# FusionPBX call feed for The Hub

A read-only feed that runs on the FusionPBX host. It runs one fixed `SELECT` on the call records (`v_xml_cdr`) as a PostgreSQL login that can read only seven columns, and serves the latest external calls as JSON behind a bearer token. Calls between extensions are left out. It runs as its own unprivileged user.

- `pbxfeed.py`: the service (Python standard library and `psql`).
- `pbx-hubfeed.service`: systemd unit.
- `pbx-setup.sh`: creates the limited database login, writes `/opt/hubfeed/conf.json` (mode 600), installs and starts the service. Copy the three files to `/tmp` on the PBX and run it as root.

On The Hub, add to `/etc/deskhub-feeds.json`:

    "calls": {"url": "http://PBX-HOST:8097/", "token": "<token from conf.json>"}

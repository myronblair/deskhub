# Door events feed for The Hub (Portcullis)

A read-only feed that runs on the Portcullis host. It reads the stored door events (SQLite, opened read-only) and the door and card-holder names from the access system's store, and serves the latest events as JSON behind a bearer token. It never talks to the door controller and never writes anything.

It runs as the same user that owns those files, so it is sandboxed by systemd instead: the whole file system is read-only to it (`ProtectSystem=strict`, `ReadOnlyPaths`), it has no capabilities, and it can only open network sockets.

- `doorfeed.py`: the service (Python standard library only). Stored event times are read as the controller's local time (`tz` in `conf.json`, default America/Chicago).
- `door-hubfeed.service`: systemd unit with the sandbox settings.
- `door-setup.sh`: writes `/opt/hubfeed/conf.json` (token, mode 600), installs and starts the service. Copy the files to `/tmp` on the host and run it as root. Make sure the files use Unix line endings.

On The Hub, add to `/etc/deskhub-feeds.json`:

    "doors": {"url": "http://PORTCULLIS-HOST:8097/", "token": "<token from conf.json>"}

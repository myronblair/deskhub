# Desk Hub web board

A browser version of the Desk Hub display, for a tablet (a Fire tablet in Fully Kiosk, for example) or any screen.

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

    http://SERVER-IP/?token=THE-TOKEN

Add `&lat=..&lon=..` for weather (and `&unit=c` for Celsius).

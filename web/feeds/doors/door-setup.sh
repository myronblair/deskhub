set -e
install -d -m 755 /opt/hubfeed
install -m 644 /tmp/doorfeed.py /opt/hubfeed/doorfeed.py
install -m 644 /tmp/door-hubfeed.service /etc/systemd/system/door-hubfeed.service

if [ ! -f /opt/hubfeed/conf.json ]; then
  tok=$(head -c 40 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  TOK="$tok" python3 - <<'PY'
import json, os
open("/opt/hubfeed/conf.json", "w").write(json.dumps({"token": os.environ["TOK"], "port": 8097, "bind": "0.0.0.0", "tz": "America/Chicago"}))
PY
  chown uhppoted:uhppoted /opt/hubfeed/conf.json; chmod 600 /opt/hubfeed/conf.json
fi

echo "--- firewall:"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from 10.48.200.93 to any port 8097 proto tcp comment 'The Hub door feed' >/dev/null && echo "ufw active: opened 8097 for 10.48.200.93 only"
else
  echo "no active ufw; iptables INPUT policy: $(iptables -S INPUT 2>/dev/null | head -1)"
fi
systemctl daemon-reload
systemctl enable --now door-hubfeed >/dev/null 2>&1
sleep 2
systemctl is-active door-hubfeed
echo "--- sandbox settings in effect:"
systemctl show door-hubfeed -p ProtectSystem -p ReadOnlyPaths -p NoNewPrivileges -p CapabilityBoundingSet -p User

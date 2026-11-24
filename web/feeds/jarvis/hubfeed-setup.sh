set -e
install -d -m 700 /opt/hubfeed
install -m 644 /tmp/hubfeed.py /opt/hubfeed/hubfeed.py
install -m 644 /tmp/jarvis-hubfeed.service /etc/systemd/system/jarvis-hubfeed.service

if [ ! -f /opt/hubfeed/conf.json ]; then
  dbpw=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  tok=$(head -c 40 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  db=$(docker exec jarvis-db sh -c 'echo $MARIADB_DATABASE')
  # one user that can only read the alerts table, only from inside the database container
  docker exec -i jarvis-db sh -c 'mariadb -uroot -p"$(cat $MARIADB_ROOT_PASSWORD_FILE)"' <<SQL
CREATE USER IF NOT EXISTS 'hubfeed'@'localhost' IDENTIFIED BY '$dbpw';
ALTER USER 'hubfeed'@'localhost' IDENTIFIED BY '$dbpw';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'hubfeed'@'localhost';
GRANT SELECT ON \`$db\`.\`alerts\` TO 'hubfeed'@'localhost';
FLUSH PRIVILEGES;
SQL
  DB="$db" DBPW="$dbpw" TOK="$tok" python3 - <<'PY'
import json, os
conf = {"token": os.environ["TOK"], "db_user": "hubfeed", "db_password": os.environ["DBPW"],
        "db_name": os.environ["DB"], "port": 8097, "bind": "0.0.0.0"}
open("/opt/hubfeed/conf.json", "w").write(json.dumps(conf))
PY
  chmod 600 /opt/hubfeed/conf.json
fi

echo "--- grants:"
docker exec jarvis-db sh -c 'mariadb -uroot -p"$(cat $MARIADB_ROOT_PASSWORD_FILE)" -N -e "SHOW GRANTS FOR \"hubfeed\"@\"localhost\""'
echo "--- firewall:"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from 10.48.200.93 to any port 8097 proto tcp comment 'The Hub alerts feed' >/dev/null && echo "ufw active: opened 8097 for 10.48.200.93 only"
else
  echo "no active ufw"
fi
systemctl daemon-reload
systemctl enable --now jarvis-hubfeed >/dev/null 2>&1
sleep 2
systemctl is-active jarvis-hubfeed

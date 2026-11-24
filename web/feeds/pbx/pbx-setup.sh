set -e
id hubfeed >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hubfeed
install -d -m 750 -o root -g hubfeed /opt/hubfeed
install -m 644 /tmp/pbxfeed.py /opt/hubfeed/pbxfeed.py
install -m 644 /tmp/pbx-hubfeed.service /etc/systemd/system/pbx-hubfeed.service

if [ ! -f /opt/hubfeed/conf.json ]; then
  dbpw=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  tok=$(head -c 40 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  # one database login that can read only the few call-record columns the feed needs
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d fusionpbx <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hubfeed') THEN CREATE ROLE hubfeed LOGIN; END IF;
END \$\$;
ALTER ROLE hubfeed WITH LOGIN PASSWORD '$dbpw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE fusionpbx TO hubfeed;
GRANT USAGE ON SCHEMA public TO hubfeed;
REVOKE ALL ON v_xml_cdr FROM hubfeed;
GRANT SELECT (start_epoch, direction, missed_call, billsec, caller_id_number, caller_id_name, destination_number)
  ON v_xml_cdr TO hubfeed;
SQL
  DBPW="$dbpw" TOK="$tok" python3 - <<'PY'
import json, os
conf = {"token": os.environ["TOK"], "db_user": "hubfeed", "db_password": os.environ["DBPW"],
        "db_name": "fusionpbx", "port": 8097, "bind": "0.0.0.0"}
open("/opt/hubfeed/conf.json", "w").write(json.dumps(conf))
PY
  chown hubfeed:hubfeed /opt/hubfeed/conf.json; chmod 600 /opt/hubfeed/conf.json
fi

echo "--- column grants:"
sudo -u postgres psql -d fusionpbx -At -c "SELECT column_name FROM information_schema.column_privileges WHERE grantee='hubfeed' AND table_name='v_xml_cdr' AND privilege_type='SELECT' ORDER BY 1" | tr '\n' ' '
echo; echo "--- other table access (should be 0):"
sudo -u postgres psql -d fusionpbx -At -c "SELECT COUNT(*) FROM information_schema.table_privileges WHERE grantee='hubfeed'"
systemctl daemon-reload
systemctl enable --now pbx-hubfeed >/dev/null 2>&1
sleep 2
systemctl is-active pbx-hubfeed

#!/usr/bin/env bash
# Run on a fresh Ubuntu 24.04 EC2 instance, from inside the unzipped snaptrack folder:
#   sudo ./setup.sh <your-domain>      e.g.  sudo ./setup.sh 13-234-56-78.sslip.io
set -euo pipefail
DOMAIN="${1:?usage: sudo ./setup.sh your-domain.example}"
APP=/opt/snaptrack
export DEBIAN_FRONTEND=noninteractive

echo ">> timezone + swap (1 GB RAM instances need swap)"
timedatectl set-timezone "${TZ_NAME:-Asia/Kolkata}" || true
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo ">> packages"
apt-get update -y
apt-get install -y python3-venv python3-pip curl gpg rsync
if ! apt-get install -y caddy; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo ">> app files"
id snaptrack >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin snaptrack
mkdir -p "$APP"
rsync -a --exclude venv --exclude data --exclude .env --exclude __pycache__ ./ "$APP"/
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R snaptrack:snaptrack "$APP"
if [ ! -f /etc/snaptrack.env ]; then
  cp .env.example /etc/snaptrack.env
  echo ">> created /etc/snaptrack.env - EDIT IT (sudo nano /etc/snaptrack.env) then: sudo systemctl restart snaptrack"
fi
chmod 600 /etc/snaptrack.env

echo ">> systemd service"
cat > /etc/systemd/system/snaptrack.service <<UNIT
[Unit]
Description=SnapTrack
After=network-online.target
Wants=network-online.target
[Service]
User=snaptrack
WorkingDirectory=$APP
EnvironmentFile=/etc/snaptrack.env
Environment=SNAPTRACK_DATA=/var/lib/snaptrack
StateDirectory=snaptrack
ExecStart=$APP/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
[Install]
WantedBy=multi-user.target
UNIT

echo ">> HTTPS reverse proxy (Caddy gets a free Let's Encrypt certificate automatically)"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode gzip
	request_body {
		max_size 12MB
	}
	reverse_proxy 127.0.0.1:8000
}
CADDY

systemctl daemon-reload
systemctl enable --now snaptrack
systemctl restart caddy
echo
echo "Done. Open https://$DOMAIN  (first load can take ~30s while the certificate is issued)"
echo "Logs:  sudo journalctl -u snaptrack -f"

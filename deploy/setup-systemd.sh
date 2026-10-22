#!/usr/bin/env bash
# Run from any checkout. Installs the application into /opt/openrent.
set +x
set +v
set -euo pipefail
umask 077
ulimit -c 0

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if (( EUID != 0 )); then exec sudo -- /bin/bash "$repo/deploy/setup-systemd.sh"; fi
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
[[ -d /run/systemd/system ]] || { echo 'Run this on a Linux server with systemd.' >&2; exit 1; }
for tool in uv sops systemd-creds systemd-run; do command -v "$tool" >/dev/null; done
for file in schedule.sh criteria.txt pyproject.toml uv.lock .python-version .env; do test -f "$repo/$file"; done
grep -q '^sops_mac=' "$repo/.env"

if ! id openrent >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/openrent --shell /usr/sbin/nologin openrent
fi
[[ $(id -u openrent) != 0 && $(getent passwd openrent | cut -d: -f6-7) == /var/lib/openrent:/usr/sbin/nologin ]]
[[ $(id -Gn openrent) == openrent ]]
usermod --lock openrent
install -d -o root -g root -m 0700 /etc/openrent
credential_tmp=$(mktemp /etc/openrent/.age-key.XXXXXX)
trap 'unset age_identity; rm -f -- "$credential_tmp"' EXIT
unset age_identity
IFS= read -r -s -p 'Paste the AGE-SECRET-KEY-1… line and press Enter (hidden): ' age_identity </dev/tty
printf '\n' >/dev/tty
[[ $age_identity == AGE-SECRET-KEY-1* && $age_identity != *[[:space:]]* ]]
printf '%s\n' "$age_identity" | systemd-creds encrypt --with-key=auto --name=age-key - - >"$credential_tmp"
unset age_identity

# Install code and configuration; preserve the installed service's databases.
if systemctl cat openrent.service >/dev/null 2>&1; then systemctl stop openrent.service; fi
install -d -o root -g root -m 0755 /opt/openrent /opt/openrent/src
install -d -o openrent -g openrent -m 0700 /var/lib/openrent /opt/openrent/data
if [[ $repo != /opt/openrent ]]; then
  for file in schedule.sh criteria.txt pyproject.toml uv.lock .python-version; do
    install -o root -g root -m 0644 "$repo/$file" "/opt/openrent/$file"
  done
  install -o root -g openrent -m 0640 "$repo/.env" /opt/openrent/.env
  cp -R "$repo/src/." /opt/openrent/src/
fi
chown -R root:root /opt/openrent/src
chmod -R a+rX,go-w /opt/openrent/src
chgrp openrent /opt/openrent/.env
chmod 0640 /opt/openrent/.env

# Verify credential delivery and discard decrypted output without journaling it.
systemd-run --quiet --wait --pipe --collect \
  --property=User=openrent --property=Group=openrent --property=WorkingDirectory=/opt/openrent \
  --property="LoadCredentialEncrypted=age-key:$credential_tmp" \
  --property='Environment=SOPS_AGE_KEY_FILE=%d/age-key' \
  /usr/bin/env PATH="$PATH" sops decrypt --input-type dotenv --output-type dotenv .env >/dev/null
mv -f -- "$credential_tmp" /etc/openrent/age-key.cred
install -o root -g root -m 0644 "$repo/deploy/openrent-system.service" /etc/systemd/system/openrent.service
systemctl daemon-reload
systemctl enable openrent.service
systemctl restart openrent.service
systemctl --no-pager status openrent.service
printf '\nLogs: sudo journalctl -u openrent.service -f\n'

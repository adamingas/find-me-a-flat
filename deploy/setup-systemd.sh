#!/usr/bin/env bash
# Run from any checkout. Installs the application into /opt/openrent.
set +x
set +v
set -Eeuo pipefail
step='starting installer'
# Report location and phase, never the command: it might contain a secret.
installer_status=0
trap 'installer_status=$?; echo "ERROR: $step failed at line $LINENO (exit $installer_status)." >&2; exit "$installer_status"' ERR
fail() { echo "ERROR: $*" >&2; exit 1; }
progress() { step=$1; echo "==> $step"; }
progress 'Starting OpenRent installer'
umask 077
ulimit -c 0

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
echo "Checkout: $repo"
if (( EUID != 0 )); then
  progress 'Requesting sudo access'
  sudo -- /bin/bash "$repo/deploy/setup-systemd.sh"
  exit 0
fi
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
progress 'Checking systemd and dependencies'
[[ -d /run/systemd/system ]] || fail 'Run this on a Linux server booted with systemd.'
for tool in uv sops systemd-creds systemd-run systemctl useradd usermod getent install; do
  command -v "$tool" >/dev/null || fail "Missing command: $tool (searched PATH=$PATH)"
done
grep -q '^sops_mac=' "$repo/.env" || fail '.env must contain SOPS-encrypted dotenv data (sops_mac metadata missing).'

progress 'Preparing the openrent service account'
if ! id openrent >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/openrent --shell /usr/sbin/nologin openrent
fi
[[ $(id -u openrent) != 0 && $(getent passwd openrent | cut -d: -f6-7) == /var/lib/openrent:/usr/sbin/nologin ]] \
  || fail 'Existing openrent account must be non-root, with home /var/lib/openrent and shell /usr/sbin/nologin.'
[[ $(id -Gn openrent) == openrent ]] || fail 'The openrent account must belong only to the openrent group.'
usermod --lock openrent
install -d -o root -g root -m 0700 /etc/openrent
credential_tmp=$(mktemp /etc/openrent/.age-key.XXXXXX)
trap 'unset age_identity; rm -f -- "$credential_tmp"' EXIT
unset age_identity
progress 'Reading the Age key from the terminal (input hidden)'
IFS= read -r -s -p 'Paste the AGE-SECRET-KEY-1… line and press Enter (hidden): ' age_identity </dev/tty \
  || fail 'Could not read the Age key; run from an interactive terminal and press Enter after pasting.'
printf '\n' >/dev/tty
[[ $age_identity == AGE-SECRET-KEY-1* && $age_identity != *[[:space:]]* ]] \
  || fail 'Expected one AGE-SECRET-KEY-1… line without comments or spaces.'
progress 'Encrypting the Age key into a systemd credential'
printf '%s\n' "$age_identity" | systemd-creds encrypt --with-key=auto --name=age-key - - >"$credential_tmp"
unset age_identity

# Install code and configuration; preserve the installed service's databases.
if systemctl cat openrent.service >/dev/null 2>&1; then
  progress 'Stopping the existing OpenRent service'
  systemctl stop openrent.service
fi
progress 'Installing application files into /opt/openrent'
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
progress 'Verifying that the service account can decrypt .env'
systemd-run --quiet --wait --pipe --collect \
  --property=User=openrent --property=Group=openrent --property=WorkingDirectory=/opt/openrent \
  --property="LoadCredentialEncrypted=age-key:$credential_tmp" \
  --property='Environment=SOPS_AGE_KEY_FILE=%d/age-key' \
  /usr/bin/env PATH="$PATH" sops decrypt --input-type dotenv --output-type dotenv .env >/dev/null
mv -f -- "$credential_tmp" /etc/openrent/age-key.cred
progress 'Installing and enabling the systemd unit'
install -o root -g root -m 0644 "$repo/deploy/openrent-system.service" /etc/systemd/system/openrent.service
systemctl daemon-reload
systemctl enable openrent.service
progress 'Starting OpenRent and checking service status'
systemctl restart openrent.service
systemctl --no-pager status openrent.service
echo 'Installer completed. Check the logs below for application startup and runtime errors.'
printf '\nLogs: sudo journalctl -u openrent.service -f\n'

#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root" >&2
  exit 1
fi

app_root="${1:-/opt/smart-pdf}"
env_file="$app_root/backend/.env"
credential_file="/root/smartpdf-admin-credential.txt"
admin_user="$(sed -n 's/^SMART_PDF_ADMIN_USER=//p' "$env_file" | head -n 1)"

if [[ -z "$admin_user" ]]; then
  echo "SMART_PDF_ADMIN_USER is not configured" >&2
  exit 1
fi

new_password="$(openssl rand -hex 18)"
temporary_env="$(mktemp)"
awk -v password="$new_password" '
  BEGIN { replaced = 0 }
  /^SMART_PDF_ADMIN_PASSWORD=/ { print "SMART_PDF_ADMIN_PASSWORD=" password; replaced = 1; next }
  { print }
  END { if (!replaced) print "SMART_PDF_ADMIN_PASSWORD=" password }
' "$env_file" > "$temporary_env"
install -o root -g smartpdf -m 0640 "$temporary_env" "$env_file"
rm -f "$temporary_env"

ADMIN_USER="$admin_user" NEW_ADMIN_PASSWORD="$new_password" APP_ROOT="$app_root" \
  "$app_root/.venv/bin/python" <<'PY'
import os
import sqlite3
import sys
import time

app_root = os.environ["APP_ROOT"]
sys.path.insert(0, f"{app_root}/backend")
from auth import _hash_password

database_path = f"{app_root}/backend/data/ocr_history.db"
connection = sqlite3.connect(database_path)
cursor = connection.execute(
    "UPDATE users SET password_hash = ? WHERE username = ?",
    (_hash_password(os.environ["NEW_ADMIN_PASSWORD"]), os.environ["ADMIN_USER"]),
)
if cursor.rowcount == 0:
    connection.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (os.environ["ADMIN_USER"], _hash_password(os.environ["NEW_ADMIN_PASSWORD"]), time.time()),
    )
connection.commit()
connection.close()
PY

umask 077
{
  printf 'SMART_PDF_ADMIN_USER=%s\n' "$admin_user"
  printf 'SMART_PDF_ADMIN_PASSWORD=%s\n' "$new_password"
} > "$credential_file"
chmod 0600 "$credential_file"
unset new_password
echo "Admin password rotated; credential saved to $credential_file"

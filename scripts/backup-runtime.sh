#!/usr/bin/env bash
set -euo pipefail

backup_root="/var/backups/smart-pdf"
retention_days="${SMART_PDF_BACKUP_DAYS:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$backup_root/$stamp"

install -d -m 700 "$destination"
python3 - "$destination/ocr_history.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(
    "file:/opt/smart-pdf/backend/data/ocr_history.db?mode=ro",
    uri=True,
)
destination = sqlite3.connect(sys.argv[1])
source.backup(destination)
destination.close()
source.close()
PY

install -m 600 /opt/smart-pdf/backend/.env "$destination/backend.env"
install -m 600 /etc/systemd/system/smart-pdf.service "$destination/smart-pdf.service"
tar -C /opt/smart-pdf/backend -czf "$destination/uploads.tar.gz" uploads
chmod 600 "$destination"/*
find "$backup_root" -mindepth 1 -maxdepth 1 -type d -mtime +"$retention_days" -exec rm -rf -- {} +

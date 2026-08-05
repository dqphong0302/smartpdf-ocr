#!/usr/bin/env bash
# Smart PDF backup helper
# Usage: ./scripts/backup-smart-pdf.sh [retention_days]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RETENTION_DAYS="${1:-14}"

"$ROOT_DIR/../scripts/connect.sh" proxmox "pct exec 116 -- bash -lc '
  set -euo pipefail
  STAMP=\"$(date +%Y%m%d-%H%M%S)\"
  BACKUP_ROOT=\"/opt/smart-pdf/backups\"
  mkdir -p \"$BACKUP_ROOT\"
  cd /opt/smart-pdf/backend
  sqlite3 data/ocr_history.db \".backup /tmp/ocr_history-$STAMP.db\"
  tar -czf \"$BACKUP_ROOT/smart-pdf-backup-$STAMP.tar.gz\" data uploads /tmp/ocr_history-$STAMP.db
  rm -f \"/tmp/ocr_history-$STAMP.db\"
  find \"$BACKUP_ROOT\" -name \"smart-pdf-backup-*.tar.gz\" -mtime +$RETENTION_DAYS -delete
  echo \"✅ Smart PDF backup saved: $BACKUP_ROOT/smart-pdf-backup-$STAMP.tar.gz\"
'"

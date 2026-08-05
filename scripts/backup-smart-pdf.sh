#!/usr/bin/env bash
# Trigger and verify the server-side SmartPDF backup unit.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

"$ROOT_DIR/scripts/connect.sh" smart-pdf \
  'systemctl start smart-pdf-backup.service && systemctl status smart-pdf-backup.service --no-pager -l'

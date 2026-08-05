#!/usr/bin/env bash
set -euo pipefail

retention_days="${SMART_PDF_CLEANUP_DAYS:-14}"
base="/opt/smart-pdf/backend"

for directory in "$base/uploads" "$base/output" "$base/outputs" "$base/jobs" "$base/tmp"; do
  if [[ -d "$directory" ]]; then
    find "$directory" -type f -mtime +"$retention_days" -delete
    find "$directory" -depth -type d -empty -mtime +"$retention_days" -delete
  fi
done

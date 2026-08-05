#!/bin/bash
# Smart PDF — Dev Server (frontend + backend)
# Usage: ./dev.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  echo ""
  echo "⏹  Stopping servers..."
  [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null
  wait 2>/dev/null
  echo "✅ Done"
}
trap cleanup EXIT INT TERM

echo "🚀 Smart PDF — Dev Mode"
echo "──────────────────────────────"

# Backend
echo "📦 Starting backend (port 8000)..."
cd "$DIR/backend"
./venv/bin/python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
sleep 2

# Frontend
echo "🎨 Starting frontend (port 5173)..."
cd "$DIR/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "──────────────────────────────"
echo "✅ Backend:  http://localhost:8000"
echo "✅ Frontend: http://localhost:5173"
echo "──────────────────────────────"
echo "Press Ctrl+C to stop both servers"
echo ""

wait

#!/bin/bash
cd "$(dirname "$0")"
clear
echo "=============================="
echo "  EasyGo 訂購自動化 — 啟動中"
echo "=============================="
echo ""

# 確認有沒有安裝過
if [ ! -d "venv" ]; then
  echo "❌ 尚未安裝！請先雙擊 setup.command 完成安裝"
  read -p "按 Enter 關閉..."
  exit 1
fi

# 自動同步最新版本
if [ -d ".git" ]; then
  echo "⏳ 同步最新版本..."
  git pull origin main --quiet 2>/dev/null && echo "✅ 已是最新版本" || echo "⚠️  無法連線更新（使用本機版本）"
  echo ""
fi

# 停掉舊的伺服器（清除 port 5050 上舊的 process）
lsof -ti:5050 | xargs kill -9 2>/dev/null

# 啟動伺服器
venv/bin/python3 app.py &
sleep 2

echo ""
echo "=============================="
echo "  ✅ 啟動完成！"
echo "  http://localhost:5050"
echo "=============================="
echo ""

echo "http://localhost:5050" | pbcopy
echo "  (網址已複製到剪貼簿)"
echo ""

open "http://localhost:5050"

echo "  關閉此視窗即停止伺服器"
wait

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

# 補上舊版 .env 缺少的欄位（例如這次改版新增的 BE2_USERNAME / BE2_PASSWORD）
if [ -f ".env" ]; then
  NEED_BE2=0
  if ! grep -q "^BE2_USERNAME=" .env; then
    echo "BE2_USERNAME=" >> .env
    NEED_BE2=1
  fi
  if ! grep -q "^BE2_PASSWORD=" .env; then
    echo "BE2_PASSWORD=" >> .env
    NEED_BE2=1
  fi
  if [ "$NEED_BE2" = "1" ]; then
    echo "⚠️  .env 已補上 BE2_USERNAME / BE2_PASSWORD 兩行，請先打開 .env 填入你自己的 be2 帳號密碼再繼續"
  fi
fi

# 檢查必填欄位是否都有值，沒填就先不啟動，請使用者去填
if [ -f ".env" ]; then
  MISSING=""
  grep -q "^BE2_USERNAME=.\+" .env || MISSING="$MISSING BE2_USERNAME"
  grep -q "^BE2_PASSWORD=.\+" .env || MISSING="$MISSING BE2_PASSWORD"
  grep -q "^EASYGO_PASSWORD=.\+" .env || MISSING="$MISSING EASYGO_PASSWORD"
  if [ -n "$MISSING" ]; then
    echo ""
    echo "❌ .env 裡還有欄位沒填：$MISSING"
    echo "   請執行「open -e .env」打開檔案填好後，再重新雙擊 start.command"
    read -p "按 Enter 關閉..."
    exit 1
  fi
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

#!/bin/bash
cd "$(dirname "$0")"
clear
echo "=============================="
echo "  EasyGo 訂購自動化 — 安裝程式"
echo "=============================="
echo ""

# 確認 Python3
if ! command -v python3 &> /dev/null; then
  echo "❌ 找不到 Python3，請先至 https://www.python.org 下載安裝"
  read -p "按 Enter 關閉..."
  exit 1
fi
echo "✅ Python3 已安裝 ($(python3 --version))"

# 刪掉舊的壞掉的 venv
if [ -d "venv" ]; then
  echo "⏳ 清除舊的安裝環境..."
  rm -rf venv
fi

# 建立虛擬環境
echo "⏳ 建立虛擬環境..."
python3 -m venv venv
if [ ! -f "venv/bin/python3" ]; then
  echo "❌ 虛擬環境建立失敗，請截圖傳給 Winnie"
  read -p "按 Enter 關閉..."
  exit 1
fi
echo "✅ 虛擬環境建立完成"

# 安裝套件
echo "⏳ 安裝套件中（約 1-2 分鐘）..."
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet flask flask-cors playwright python-dotenv openpyxl
echo "✅ 套件安裝完成"

# 安裝瀏覽器
echo "⏳ 安裝自動化瀏覽器（約 2-3 分鐘）..."
venv/bin/playwright install chromium
echo "✅ 瀏覽器安裝完成"

# 建立 .env（若尚未存在）
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✅ 設定檔建立完成（.env）"
  echo "⚠️  請打開 .env，跟 Winnie 或主管索取 EASYGO_PASSWORD 填進去"
fi

echo ""
echo "=============================="
echo "  ✅ 安裝完成！"
echo "  之後只要雙擊 start.command 即可啟動"
echo "=============================="
read -p "按 Enter 關閉..."

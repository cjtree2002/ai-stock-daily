#!/bin/bash
# One-time setup script
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.aistocknews.daily.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.aistocknews.daily.plist"

echo "=== AI 美股日报 一键安装 ==="

# 1. Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Please install Python 3.9+."
  exit 1
fi

# 2. Install dependencies
echo "Installing Python dependencies..."
python3 -m pip install requests --quiet

# 3. Write .env if needed
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo ""
  echo "请输入你的 NewsAPI key（从 https://newsapi.org 免费注册获取）："
  read -r API_KEY
  echo "NEWS_API_KEY=$API_KEY" > "$SCRIPT_DIR/.env"
  echo "  已保存到 .env"
fi

# 4. Make scripts executable
chmod +x "$SCRIPT_DIR/run_daily.sh"
chmod +x "$SCRIPT_DIR/fetch_news.py"

# 5. Inject real path into plist and install
sed "s|SCRIPT_DIR|$SCRIPT_DIR|g" "$PLIST_SRC" > "$PLIST_DEST"
echo "LaunchAgent installed: $PLIST_DEST"

# 6. Load the agent
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
echo "LaunchAgent loaded (will run daily at 08:00)"

# 7. Test run now?
echo ""
echo "是否立即运行一次测试？(y/N)"
read -r CONFIRM
if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
  bash "$SCRIPT_DIR/run_daily.sh"
fi

echo ""
echo "✓ 安装完成！每天早上 8:00 自动生成日报并在浏览器打开。"
echo "  手动运行：bash $SCRIPT_DIR/run_daily.sh"
echo "  查看日志：tail -f $SCRIPT_DIR/logs/run.log"

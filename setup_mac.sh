#!/bin/bash
# ============================================================
# ワクスト自動更新スクリプト - Macセットアップ
# 使い方: bash setup_mac.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.wakust.autoupdate"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_DIR="$SCRIPT_DIR/logs"
PYTHON_SCRIPT="$SCRIPT_DIR/wakust_auto_update.py"

echo "======================================"
echo " ワクスト自動更新 セットアップ"
echo "======================================"

# --- Python確認 ---
echo ""
echo "▶ Pythonを確認中..."
if command -v python3 &>/dev/null; then
    PYTHON=$(command -v python3)
    echo "  ✅ Python3が見つかりました: $PYTHON"
else
    echo "  ❌ Python3が見つかりません"
    echo ""
    echo "  以下のURLからPythonをインストールしてください:"
    echo "  https://www.python.org/downloads/"
    echo ""
    echo "  インストール後、このスクリプトをもう一度実行してください。"
    exit 1
fi

# --- pipパッケージインストール ---
echo ""
echo "▶ 必要なパッケージをインストール中..."
$PYTHON -m pip install requests beautifulsoup4 schedule --quiet
echo "  ✅ インストール完了"

# --- ログフォルダ作成 ---
mkdir -p "$LOG_DIR"
echo "  ✅ ログフォルダ作成: $LOG_DIR"

# --- launchd plistを作成（毎日0:00に実行）---
echo ""
echo "▶ スケジュール設定中（毎日0:00）..."

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${PYTHON_SCRIPT}</string>
        <string>--once</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/wakust.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/wakust_error.log</string>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST

echo "  ✅ plist作成: $PLIST_PATH"

# --- launchdに登録 ---
# 既存のジョブをアンロード（エラーは無視）
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "  ✅ launchdに登録完了"

# --- 動作確認（今すぐ一度実行）---
echo ""
echo "▶ 動作確認のため今すぐ一度実行します..."
echo "--------------------------------------"
cd "$SCRIPT_DIR"
$PYTHON "$PYTHON_SCRIPT" --once
echo "--------------------------------------"

echo ""
echo "======================================"
echo " セットアップ完了！"
echo "======================================"
echo ""
echo "  毎日 0:00 に自動実行されます"
echo ""
echo "  ログの確認:"
echo "  tail -f $LOG_DIR/wakust.log"
echo ""
echo "  手動で今すぐ実行:"
echo "  python3 $PYTHON_SCRIPT --once"
echo ""
echo "  スケジュール停止:"
echo "  launchctl unload $PLIST_PATH"
echo ""
echo "  スケジュール再開:"
echo "  launchctl load $PLIST_PATH"
echo ""

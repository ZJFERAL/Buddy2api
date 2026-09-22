#!/bin/bash
# 验证码求解器依赖安装脚本
# 用于安装 happy-dom 等 npm 依赖

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR/..")"
CAPTCHA_NODE_PATH="$PROJECT_ROOT/providers/zcode/captcha_node"

echo "Installing captcha solver dependencies..."
echo "Working directory: $CAPTCHA_NODE_PATH"

cd "$CAPTCHA_NODE_PATH"

# 检查是否已存在 node_modules
if [ -d "node_modules" ]; then
    echo "⚠️  node_modules exists, checking dependencies..."
    if [ -d "node_modules/happy-dom" ]; then
        echo "✅ Dependencies already installed."
        exit 0
    fi
fi

# 使用 Node.js managed 版本执行 npm
NODE_BIN="/c/Users/zhaoj/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"

if ! command -v "$NODE_BIN" &> /dev/null; then
    echo "❌ Managed Node.js not found at $NODE_BIN"
    echo "Please install or configure Node.js first."
    exit 1
fi

# 安装依赖
echo "Installing happy-dom@^17.1.2 and other dependencies..."
"$NODE_BIN" -e "
const { execSync } = require('child_process');
try {
    execSync('npm install', {
        cwd: process.cwd(),
        stdio: 'inherit',
        env: { ...process.env, FORCE_COLOR: '1' }
    });
    console.log('\\n✅ Dependencies installed successfully!');
} catch (err) {
    console.error('\\n❌ Installation failed:', err.message);
    process.exit(1);
}
"

# 验证安装
if [ -d "node_modules/happy-dom" ]; then
    echo "✅ happy-dom verified in node_modules/"
else
    echo "❌ happy-dom not found after installation"
    exit 1
fi

echo ""
echo "🎉 Captcha solver setup complete!"
echo "You can now test with: providers/zcode/captcha_node/solver.js sceneId region prefix"

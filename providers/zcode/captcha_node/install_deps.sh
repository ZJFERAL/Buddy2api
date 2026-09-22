#!/bin/bash
# 安装验证码求解器依赖

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "📦 Installing npm dependencies for captcha solver..."

# 使用 npm（通过 git bash）
if command -v npm &> /dev/null; then
    echo "Using npm from PATH"
    npm install --no-progress --save-dev happy-dom@^17.1.2 jsdom@^26.1.0
else
    # 使用 managed node 的 npm
    NODE_BIN="/c/Users/zhaoj/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
    
    if [ ! -f "$NODE_BIN" ]; then
        echo "❌ Managed Node.js not found at $NODE_BIN"
        exit 1
    fi
    
    echo "Using managed Node.js: $NODE_BIN"
    
    # 通过 Node.js 执行 npm
    "$NODE_BIN" -e "
    const { execSync } = require('child_process');
    try {
        execSync('npm install --no-progress happy-dom@^17.1.2 jsdom@^26.1.0', {
            stdio: 'inherit',
            env: { ...process.env, FORCE_COLOR: '0' }
        });
        console.log('\\n✅ Dependencies installed!');
    } catch (err) {
        console.error('\\n❌ Error:', err.message);
        process.exit(1);
    }
    "
fi

# 验证安装
if [ -d "node_modules/happy-dom" ] && [ -d "node_modules/jsdom" ]; then
    echo "✅ All dependencies installed successfully!"
    ls -la node_modules/ | grep -E "(happy-dom|jsdom)"
else
    echo "⚠️ Some dependencies missing:"
    test -d "node_modules/happy-dom" && echo "  ✓ happy-dom" || echo "  ✗ happy-dom"
    test -d "node_modules/jsdom" && echo "  ✓ jsdom" || echo "  ✗ jsdom"
fi

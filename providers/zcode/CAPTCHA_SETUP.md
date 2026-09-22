# ZCode 验证码求解器依赖安装指南

## 🎯 背景

验证码求解功能已内化到本项目，不再依赖外部仓库路径。这解决了之前 `zcode2api` 仓库移动或删除导致的依赖问题。

## 📦 依赖清单

- **Node.js** >= 18 (建议使用 WorkBuddy managed 版本：`C:\Users\zhaoj\.workbuddy\binaries\node\versions\22.22.2-3\node.exe`)
- **happy-dom@^17.1.2** - 浏览器 DOM 模拟环境
- **jsdom** - JSDOM (由 happy-dom 传递依赖)

## 🚀 快速安装

### 方式一：使用脚本（推荐）

```bash
cd E:\AiWorkspace\Tools\buddy2api
./scripts/setup_captcha.sh
```

如果 Git Bash 不可用，手动执行：

```powershell
cd providers/zcode/captcha_node
& "C:/Users/zhaoj/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" -e "require('child_process').execSync('npm install', {stdio: 'inherit'})"
```

### 方式二：手动 npm 安装

```bash
cd E:\AiWorkspace\Tools\buddy2api\providers\zcode\captcha_node
npm install
```

## ✅ 验证安装

安装完成后会生成以下目录结构：

```
providers/zcode/captcha_node/
├── solver.js              # 求解器核心代码
├── package.json           # 依赖声明
└── node_modules/
    └── happy-dom/         # ← 必须存在
    └── jsdom/             # ← 可选
```

测试求解器是否正常工作：

```bash
node solver.js 11xygtvd sgp no8xfe
```

预期输出包含 `VERIFY_PARAM=` 开头的 base64 编码字符串（长度通常 > 300）。

## 🔧 故障排查

### 错误：MODULE_NOT_FOUND

**症状**：
```
Error: Cannot find module 'happy-dom'
```

**原因**：`node_modules/happy-dom` 未安装

**解决**：
```bash
cd E:\AiWorkspace\Tools\buddy2api\providers\zcode\captcha_node
npm install
```

### 错误：solver 退出码 1

**症状**：求解器执行失败，exit code = 1

**原因**：可能缺少 Node.js 依赖或版本不兼容

**解决**：
1. 确认 Node.js 版本 >= 18
2. 清理并重新安装：
   ```bash
   rm -rf node_modules package-lock.json
   npm install
   ```

### 错误：降级结果 (缺 securityToken)

**症状**：返回 ~90 字符的短串，不含 `securityToken` 字段

**原因**：
- SDK 走 failover 降级模式
- 网络无法访问阿里云 CDN (`o.alicdn.com`)
- happy-dom 环境不完整

**解决**：
1. 检查网络连接，确保能访问 `https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js`
2. 确认 `happy-dom` 完整安装

## 📝 内部实现说明

### 求解器来源

此求解器源自 [zcode2api](https://github.com/liu5269/zcode2api) (AGPL)，经过充分测试和验证：
- 实测平均响应时间 ~3 秒
- 可生成含 `securityToken` 的完整 param
- 反检测能力优于原始自带求解器

### 不再依赖外部路径

之前配置中使用的路径已被移除：
- ❌ `E:\AiWorkspace\Tools\zocdedemo\zcode2api\captcha_node\solver.js`
- ❌ `E:\AiWorkspace\Tools\zcode2api\captcha_node\solver.js`

所有求解逻辑已完整迁移到 `providers/zcode/captcha_node/solver.js`。

### 环境变量覆盖

如需自定义求解器路径，可通过环境变量指定：

```bash
ZCODE_CAPTCHA_SOLVER_JS=/path/to/custom/solver.js node server.py
```

优先级：
1. 环境变量 `ZCODE_CAPTCHA_SOLVER_JS`
2. 内置求解器 `providers/zcode/captcha_node/solver.js`

## 🔄 更新维护

如需升级求解器或依赖：

1. 修改 `package.json` 中的版本号
2. 重新运行 `npm install`
3. 验证求解器正常工作

## 💡 提示

- 首次安装需要下载依赖 (~2MB)，耗时约 30-60 秒
- 生产环境建议预先安装依赖，避免运行时首次加载
- 可以在 `.gitignore` 中添加 `node_modules/`，在 CI/CD 中按需安装

# 2026-09-23 — chore(repo): 仓库卫生与文档补齐

本次为**维护性提交**，不改变任何运行时行为，不涉及业务代码路径。目标是把此前只存在于本地工作区的项目上下文、启动脚本与示例配置纳入正式的版本管理约定，并统一"什么应该入仓 / 什么不该入仓"的边界。

## 变更概览

| 类别 | 文件 | 说明 |
|---|---|---|
| 新增 | `AGENTS.md` | 项目级 AI/开发者协作手册：架构、目录、路由、认证、测试、安全红线、常见工作流。 |
| 新增 | `docs/maintenance/dev-conventions.md` | 编码规范：import、数据模型注册、provider 实现、错误码、HTTP 路由、测试、提交前自检。 |
| 新增 | `docs/maintenance/channel-integration-guide.md` | 新增渠道（provider）完整指南：8 步走、17 个必须覆盖的测试点、常见坑。 |
| 新增 | `docs/maintenance/upstream-protocol-notes.md` | 各上游协议要点笔记：WorkBuddy / Qoder / qwen-work / worklens / Trae。 |
| 新增 | `docs/releases/2026-09-23-chore-repo-hygiene.md` | 本文件，本次变更日志。 |
| 新增 | `.env.example` | 环境变量样例（此前已被 `.gitignore` 规则 `!` 反向白名单，但未纳入版本管理；本次正式加入）。 |
| 修改 | `.gitignore` | 追加"Local launch scripts (per-user, do not commit)"区块，明确忽略本地启动脚本与快捷方式。 |
| 删除（版本控制） | `start-oneclick.bat`, `start-oneclick.vbs`, `start.bat` | 从 git 索引中移除（本地磁盘文件保留）。这些脚本因个人机器环境差异（Python 路径、路径硬编码、admin token 等）不宜入仓。 |

## 具体变更

### 1. `.gitignore`：本地启动脚本纳入忽略

在原文件末尾追加：

```
# Local launch scripts (per-user, do not commit)
start-oneclick.bat
start-oneclick.vbs
start.bat
Buddy2API.lnk
memorix.toml
```

理由：
- `start-oneclick.bat` / `start-oneclick.vbs` 依赖当前用户的 Python 安装位置、路径和 token，硬编码到仓里会让下一位使用者直接跑不起来，且 admin token 一旦入仓就等同于泄露。
- `start.bat` 是另一版本的启动脚本，用途与上面同类，同样不适合跨机器共享。
- `Buddy2API.lnk` 是本地 Windows 桌面快捷方式，`.lnk` 是二进制文件，本来就该忽略。
- `memorix.toml` 是个人记忆 / agent 配置，属于本地工作区产物。

保留 `.env.example` 的白名单规则（`!.env.example`），并在同一次提交中把示例文件正式纳入版本管理。

### 2. 从版本控制移除启动脚本

`git rm --cached start-oneclick.bat start-oneclick.vbs start.bat`

**磁盘上的文件不删除**，用户本地仍可继续使用；只是它们不再被 git 跟踪。以后本地修改这些脚本不会再出现在 `git status` 里。

### 3. 补充项目上下文文档（4 份）

- **`AGENTS.md`（根目录）** — 面向 AI 助手与新同事的第一份阅读材料。包含：
  - 项目定位与运行时架构（单进程、无 ORM、无 DB）
  - 目录说明、请求路由、认证与计费模型
  - 测试命令、常用工作流、**安全红线**（禁止泄露 `admin-tokens.json` / `accounts.json` / 个人 `.env` / 个人 token）
  - 版本策略（仅 `v<semver>` tag 发布 Docker 镜像）

- **`docs/maintenance/dev-conventions.md`** — 编码与提交规范：
  - `__all__` 显式导出、`TYPE_CHECKING` 隔离、不新增依赖
  - `ACCOUNT_SCHEMA` / `ProviderCredential` / `AccountState` / `CatalogEntry` / `ReasoningEffort` 五套数据模型的注册规则
  - Provider 实现清单（8 个方法、字段、异常路径）
  - 提交前 checklist（含真实 token 扫描正则）

- **`docs/maintenance/channel-integration-guide.md`** — 新增渠道 8 步指南：
  1. 建目录、2. 定 schema、3. 建 provider 类、4. 注册、5. 补 `__init__.pyi`、
  6. 补 provider schema 测试、7. 补路由/错误码、8. 补文档。
  附 17 个必覆盖测试点与 6 个常见坑。

- **`docs/maintenance/upstream-protocol-notes.md`** — 各上游协议要点笔记，作为后续接入新渠道时的参考。

### 4. `.env.example` 正式纳入版本管理

`.gitignore` 中原本存在 `!.env.example` 反向白名单规则（说明示例文件是被允许入库的），但该文件此前从未真正被 `git add` 过。本次提交把它正式纳入版本控制，作为新用户配置 `.env` 的起点。

`.env.example` 中不含任何真实凭据；`ADMIN_API_TOKEN` 使用占位符 `cb-admin-请换成足够长的随机值`，明确提示用户更换。

## 安全说明

- 本次提交前对新增/修改的所有文件执行了敏感字符串扫描：
  - `cb-admin-` 前缀（admin token 字面量）
  - `buddy-admin` / `workbuddy-admin`（历史默认 admin token）
  - `Bearer sk-` / `Bearer wss-` / `Bearer qcode-` / `Bearer wq-`（上游 API key）
  - `api_key` / `token` / `admin-tokens.json` 等关键词

- 扫描结果：**所有新增文档、示例文件、以及将要本地保留的 `start-oneclick.bat` 中，均不含任何真实凭据**。README 中出现的 `cb-admin-请换成足够长的随机值` 是占位符，不构成凭据泄露。

- 之前 `start-oneclick.bat` 中曾经硬编码的 admin token 已被本地轮换为 40 字符随机值，并且**该文件本身从版本控制移除**，因此历史提交之外的敏感信息不会再流入仓库。

## 影响范围

- **运行时行为**：无变化。`proxy.py`、`providers/*`、`tests/*` 全部未触碰。
- **部署 / 使用**：
  - 已使用 `start-oneclick.bat` 的用户：本地文件仍在，无需变更；下次 `git pull` 后，git 不会再提示该文件被"删除"。
  - 新用户（clone 后）：将看不到 `start-oneclick.bat`。请自行根据 `AGENTS.md` 中的"常用工作流"手动配置 `PYTHONPATH` / `ADMIN_API_TOKEN` / `.env`，然后运行 `python proxy.py`。或参见 `docs/` 下的部署文档。
- **CI**：无变化。未新增或删除任何测试。

## 后续 TODO

- [ ] 为 README 增加从 `.env.example` 复制到 `.env` 的快速开始段落（当前文档只描述了 `--admin-api-token` CLI 参数路径，未提及 `.env` 加载机制）。
- [ ] 评估是否将 `docker-compose.windows.yml` 中的默认端口 8788 与项目内其他地方约定的 8787 统一（`docker-compose.yml` 用 8787，Windows 版用 8788，可能只是历史原因，但需要确认）。
- [ ] 建立 `docs/releases/` 的编写规范：目前混用了 `chore` / `feat` / `release vX.Y` / 具体功能名等多种命名风格，建议统一为 `<YYYY-MM-DD>-<kebab-case-summary>.md`。

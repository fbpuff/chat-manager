# Chat Manager

Claude Code 对话管理器 — 浏览、搜索、重命名、删除 Claude Code 会话的 Web UI 工具。

## 功能

- 📋 浏览所有 Claude Code 对话会话
- 🔍 全文搜索对话内容
- ✏️ 重命名会话（与终端 `/rename` 双向同步）
- 📊 使用统计（Token 用量、模型分布）
- 📥 导出对话为 Markdown
- 🗑️ 批量删除会话

## 安装

### 方式 A：下载 EXE（无需 Python）

从 [Releases](https://github.com/fbpuff/chat-manager/releases) 下载 `Chat-Manager.exe`，双击运行。

> 浏览器可能会提示"此文件可能危险"→ 点击「保留」→「仍然运行」

### 方式 B：源码运行（需要 Python）

```bash
# 1. 克隆仓库
git clone https://github.com/fbpuff/chat-manager.git
# 或直接下载 ZIP 并解压到 ~/.claude/chat-manager/

# 2. 安装依赖
pip install flask watchdog

# 3. 启动
python chat-manager-web.py
# 双击 chat-manager.vbs 也可以静默启动
```

启动后访问 **http://127.0.0.1:9720**。

## Claude Code 集成

安装 skill 后可在 Claude Code 终端输入 `/chat-manager` 启动：

```bash
mkdir -p ~/.claude/skills/chat-manager
cp SKILL.md ~/.claude/skills/chat-manager/SKILL.md
```

## CLI 命令

```bash
python chat-manager.py list                  # 列出所有会话
python chat-manager.py view <session-id>     # 查看对话
python chat-manager.py search <keyword>      # 搜索关键词
python chat-manager.py stats                 # 使用统计
python chat-manager.py export <id> -o x.md   # 导出 Markdown
```

## 系统要求

- Windows 10/11（macOS/Linux 需修改启动脚本）
- Python 3.10+（仅源码方式）
- 端口 9720

## 许可证

MIT

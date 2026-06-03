#!/usr/bin/env python3
"""
Claude Code Chat History Manager — Web GUI
===========================================
Flask-powered local web interface for managing Claude Code transcripts.

Double-click chat-manager.bat or run:
    python chat-manager-web.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional, Iterator, Any
from dataclasses import dataclass

# ---- Import shared utilities from chat-manager.py -----------------------
# Determine the script directory — works both in normal Python and in
# PyInstaller --onefile bundles (where __file__ is inside a temp folder
# and bundled data is at sys._MEIPASS).
_SCRIPT_DIR = Path(__file__).resolve().parent
# PyInstaller support: also check the bundled data directory
_MEIPASS = Path(getattr(sys, '_MEIPASS', _SCRIPT_DIR))

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# Look for chat-manager.py in script dir first, then MEIPASS
_cm_path = _SCRIPT_DIR / "chat-manager.py"
if not _cm_path.exists():
    _cm_path = _MEIPASS / "chat-manager.py"

import importlib.util
_spec = importlib.util.spec_from_file_location("chat_manager", str(_cm_path))
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

# ---- Safe Python executable resolution ---------------------------------
# In a PyInstaller bundle, sys.executable is the .exe itself, NOT python.exe.
# Running "<bundle>.exe -m pip install ..." re-runs the bundled script instead
# of installing packages, creating a fork bomb. Always resolve the real Python.
def _get_python_exe() -> str:
    """Return the real Python interpreter, even inside a PyInstaller bundle."""
    # If NOT frozen (normal Python), sys.executable is fine
    if not getattr(sys, 'frozen', False):
        return sys.executable
    # In a PyInstaller bundle, search for the system Python
    for candidate in ['python', 'python3']:
        found = shutil.which(candidate)
        if found:
            return found
    # Last resort: try common install paths on Windows
    for base in [Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Python',
                 Path('C:\\Python313'), Path('C:\\Python312'), Path('C:\\Python311')]:
        for exe in ['python.exe', 'python3.exe']:
            p = base / exe
            if p.exists():
                return str(p)
    # Should never happen, but fall back to sys.executable
    return sys.executable

_PYTHON_EXE = _get_python_exe()

# ---- Flask bootstrap (auto-install if missing) -------------------------
try:
    from flask import Flask, jsonify, request, render_template_string, send_file
except ImportError:
    subprocess.check_call([_PYTHON_EXE, "-m", "pip", "install", "flask", "-q"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from flask import Flask, jsonify, request, render_template_string, send_file

# ---- Watchdog bootstrap (auto-install if missing) ----------------------
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    subprocess.check_call([_PYTHON_EXE, "-m", "pip", "install", "watchdog", "-q"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

# ---- App setup ---------------------------------------------------------
app = Flask(__name__)
_PORT = 9720
_BASE_DIR = _SCRIPT_DIR
_TITLES_FILE = _BASE_DIR / "titles.json"
_sessions_cache: Optional[dict] = None
_sessions_cache_time: float = 0.0

def _invalidate_sessions_cache():
    global _sessions_cache, _sessions_cache_time, _titles_cache
    _sessions_cache = None
    _sessions_cache_time = 0.0
    _titles_cache = None  # Also reload titles.json — terminal may have renamed


# ---- File system watcher for real-time updates -------------------------
# 使用防抖机制，避免短时间内多次缓存失效
_invalidate_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()

def _debounced_invalidate_cache():
    """防抖的缓存失效，1秒内只触发一次"""
    global _invalidate_timer
    with _timer_lock:
        _invalidate_timer = None
    _invalidate_sessions_cache()

def _schedule_invalidation():
    """安排延迟的缓存失效（防抖）"""
    global _invalidate_timer
    with _timer_lock:
        if _invalidate_timer:
            _invalidate_timer.cancel()
        _invalidate_timer = threading.Timer(1.0, _debounced_invalidate_cache)
        _invalidate_timer.daemon = True
        _invalidate_timer.start()

class ClaudeDataHandler(FileSystemEventHandler):
    """Watchdog handler to invalidate cache when Claude Code data changes."""
    def on_modified(self, event):
        # 忽略目录变化和临时文件
        if event.is_directory:
            return
        if event.src_path.endswith('.tmp'):
            return

        path = Path(event.src_path)
        # 只监听 history.jsonl 和 sessions/*.json，性能更好
        if path.name == 'history.jsonl' or (path.parent.name == 'sessions' and path.suffix == '.json'):
            _schedule_invalidation()

    def on_created(self, event):
        # 新文件创建时也触发缓存失效
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.name == 'history.jsonl' or (path.parent.name == 'sessions' and path.suffix == '.json'):
            _schedule_invalidation()

    def on_deleted(self, event):
        # 文件删除时也触发缓存失效
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.name == 'history.jsonl' or (path.parent.name == 'sessions' and path.suffix == '.json'):
            _schedule_invalidation()


_titles_cache: Optional[dict[str, str]] = None

def _load_custom_titles() -> dict[str, str]:
    """Load {session_id: custom_title} from titles.json (cached in memory)."""
    global _titles_cache
    if _titles_cache is not None:
        return _titles_cache
    if _TITLES_FILE.exists():
        try:
            _titles_cache = json.loads(_TITLES_FILE.read_text(encoding='utf-8'))
            return _titles_cache
        except (json.JSONDecodeError, OSError):
            pass
    _titles_cache = {}
    return _titles_cache

def _save_custom_titles(titles: dict[str, str]):
    global _titles_cache
    _titles_cache = titles
    _TITLES_FILE.write_text(json.dumps(titles, ensure_ascii=False, indent=2), encoding='utf-8')

def _resolve_title(sid: str, fallback: str, history_title: Optional[str] = None) -> str:
    """Resolve the best title from multiple sources.

    Priority (highest first):
      1. Transcript title (fallback=st.title) — includes /rename custom-title entries
      2. titles.json custom title (manually set in Web UI)
      3. history_title (display from history.jsonl) — initial session name

    titles.json is always synced to reflect the winning title."""
    titles = _load_custom_titles()
    custom = titles.get(sid)

    # 1. Transcript title (st.title) — most authoritative, includes /rename
    if fallback and fallback.strip():
        if not custom or custom.strip() != fallback.strip():
            titles[sid] = fallback
            _save_custom_titles(titles)
        return fallback

    # 2. Custom title from Web UI (titles.json)
    if custom:
        return custom

    # 3. History title from history.jsonl display field
    if history_title and history_title.strip():
        return history_title

    return '(untitled)'

# ---- HTML template (embedded, single-file) -----------------------------
# This keeps everything in two files: the .py and the .bat.
# The HTML is large but self-contained — no external CDN, no framework.

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Claude Code 对话管理器</title>
<style>
:root {
    --bg: #ffffff;
    --bg2: #f7f8fa;
    --bg3: #eceef2;
    --fg: #1a1a2e;
    --fg2: #4a4a6a;
    --fg3: #9898b0;
    --blue: #3b6df0;
    --green: #1ea85f;
    --red: #e04040;
    --yellow: #d4780a;
    --magenta: #7c3aed;
    --cyan: #0b7fd5;
    --border: #dde0e5;
    --shadow: 0 2px 8px rgba(0,0,0,0.06);
    --radius: 8px;
    --font: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    --mono: 'Cascadia Code', 'Consolas', 'Fira Code', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--fg);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
/* ---- Header ---- */
header {
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
}
header h1 { font-size: 1.1rem; font-weight: 600; color: var(--cyan); }
header .stats-bar { font-size: 0.82rem; color: var(--fg3); }
header .stats-bar span { margin: 0 10px; }
/* ---- Main layout ---- */
main {
    display: flex;
    flex: 1;
    overflow: hidden;
}
/* ---- Left panel: session list ---- */
.panel-left {
    width: 320px;
    min-width: 260px;
    background: var(--bg2);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.batch-bar {
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    background: var(--bg3);
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.82rem;
}
.batch-bar .select-all { cursor: pointer; display: flex; align-items: center; gap: 4px; }
.batch-bar .select-all input { margin: 0; }
.batch-bar .batch-del-btn {
    width: auto;
    padding: 4px 12px;
    font-size: 0.8rem;
    background: var(--red);
    color: #fff;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    margin-left: auto;
}
.batch-bar .batch-del-btn:hover { opacity: 0.85; }
.panel-left .search-box {
    padding: 10px;
    border-bottom: 1px solid var(--border);
}
.panel-left .search-box input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg3);
    color: var(--fg);
    font-size: 0.9rem;
    font-family: var(--font);
    outline: none;
}
.panel-left .search-box input:focus { border-color: var(--blue); }
.session-list {
    flex: 1;
    overflow-y: auto;
    padding: 4px 0;
}
.loading-msg { padding: 20px; text-align: center; color: var(--fg3); font-size: 0.9rem; }
.error-msg { padding: 20px; text-align: center; color: var(--red); font-size: 0.9rem; line-height: 1.6; }
.empty-msg { padding: 20px; text-align: center; color: var(--fg3); font-size: 0.9rem; }
.session-item {
    padding: 8px 10px;
    border-left: 3px solid transparent;
    transition: background 0.15s;
    display: flex;
    align-items: flex-start;
    gap: 8px;
}
.session-item .sess-check {
    margin-top: 6px;
    flex-shrink: 0;
    cursor: pointer;
}
.session-item .sess-info {
    cursor: pointer;
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.session-item:hover { background: var(--bg3); }
.session-item.active {
    background: var(--bg3);
    border-left-color: var(--blue);
}
.session-item .sess-title {
    font-size: 0.88rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.session-item .sess-meta {
    font-size: 0.75rem;
    color: var(--fg3);
    display: flex;
    gap: 10px;
}
.session-item .sess-meta .active-dot { color: var(--green); }
.session-item .sess-rename-btn {
    display: none;
    cursor: pointer;
    font-size: 0.85rem;
    color: var(--fg3);
    padding: 2px 6px;
    border-radius: 3px;
    flex-shrink: 0;
    opacity: 0.6;
    transition: opacity 0.15s, color 0.15s;
}
.session-item:hover .sess-rename-btn { display: inline; }
.session-item .sess-rename-btn:hover { opacity: 1; color: var(--blue); }
/* ---- Center panel: conversation ---- */
.panel-center {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
}
.conversation-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    flex-shrink: 0;
}
.conversation-header h2 { font-size: 1rem; color: var(--fg); margin-bottom: 4px; }
.conversation-header .conv-meta { font-size: 0.8rem; color: var(--fg3); display: flex; gap: 15px; flex-wrap: wrap; }
/* ---- Chat Bubble Styles (WeChat-like) ---- */
.conversation-view {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}
.chat-bubble {
    max-width: 75%;
    padding: 10px 15px;
    border-radius: 12px;
    line-height: 1.6;
    font-size: 0.9rem;
    white-space: pre-wrap;
    word-break: break-word;
    position: relative;
    animation: fadeIn 0.2s ease;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.chat-bubble.user {
    align-self: flex-end;
    background: #4a90f2;
    color: #fff;
    border-bottom-right-radius: 4px;
}
.chat-bubble.assistant {
    align-self: flex-start;
    background: #fff;
    color: var(--fg);
    border: 1px solid var(--border);
    border-bottom-left-radius: 4px;
}
.chat-bubble.thinking {
    align-self: center;
    font-size: 0.72rem;
    color: var(--fg3);
    cursor: pointer;
    user-select: none;
    padding: 2px 8px;
    border-radius: 10px;
    transition: background 0.15s;
}
.chat-bubble.thinking:hover { background: rgba(0,0,0,0.04); }
.chat-bubble.thinking .thinking-body {
    display: none;
    margin-top: 6px;
    padding: 10px 14px;
    background: var(--bg3);
    border-radius: 8px;
    text-align: left;
    font-style: italic;
    color: var(--fg2);
    font-size: 0.78rem;
    white-space: pre-wrap;
    max-width: 600px;
}
.chat-bubble.thinking.open .thinking-body { display: block; }
.chat-bubble.tool {
    align-self: center;
    font-size: 0.72rem;
    color: var(--fg3);
    padding: 1px 8px;
    border-radius: 8px;
    font-family: var(--font);
}
.chat-bubble.system {
    align-self: center;
    max-width: 90%;
    background: transparent;
    color: var(--fg3);
    font-size: 0.76rem;
    text-align: center;
    padding: 4px 10px;
}
.chat-avatar {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    flex-shrink: 0;
}
.msg-row {
    display: flex;
    align-items: flex-end;
    gap: 8px;
}
.msg-row.user-row { justify-content: flex-end; }
.msg-row.assistant-row { justify-content: flex-start; }
/* Editable title */
.editable-title {
    cursor: pointer;
    border-bottom: 1px dashed transparent;
    transition: border-color 0.15s;
}
.editable-title:hover { border-bottom-color: var(--blue); }
.editable-title input {
    font-size: 1rem;
    font-weight: 600;
    border: 1px solid var(--blue);
    border-radius: 4px;
    padding: 2px 8px;
    background: var(--bg);
    color: var(--fg);
    width: 400px;
    max-width: 100%;
    font-family: var(--font);
    outline: none;
}
.message .role {
    font-weight: 700;
    font-size: 0.78rem;
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.message.user .role { color: var(--blue); }
.message.assistant .role { color: var(--green); }
.message.system .role { color: var(--fg3); }
.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--fg3);
    text-align: center;
}
.empty-state .icon { font-size: 3rem; margin-bottom: 16px; }
.empty-state p { font-size: 0.9rem; }
/* ---- Right panel: actions ---- */
.panel-right {
    width: 280px;
    min-width: 220px;
    background: var(--bg2);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    padding: 15px;
    gap: 12px;
    flex-shrink: 0;
}
.panel-right h3 {
    font-size: 0.85rem;
    color: var(--fg2);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 2px;
}
button {
    width: 100%;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg3);
    color: var(--fg);
    font-size: 0.88rem;
    font-family: var(--font);
    cursor: pointer;
    transition: all 0.15s;
    text-align: left;
}
button:hover { background: var(--border); border-color: var(--fg3); }
button.danger { color: var(--red); border-color: rgba(247, 118, 142, 0.3); }
button.danger:hover { background: rgba(247, 118, 142, 0.12); }
button.primary { color: var(--green); border-color: rgba(158, 206, 106, 0.3); }
button.primary:hover { background: rgba(158, 206, 106, 0.1); }
.search-section input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg3);
    color: var(--fg);
    font-size: 0.88rem;
    font-family: var(--font);
    outline: none;
    margin-bottom: 8px;
}
.search-section input:focus { border-color: var(--blue); }
.search-results {
    max-height: 200px;
    overflow-y: auto;
    font-size: 0.82rem;
    border-top: 1px solid var(--border);
    margin-top: 8px;
    padding-top: 8px;
}
.search-hit {
    padding: 6px 0;
    cursor: pointer;
    border-bottom: 1px solid rgba(59, 66, 97, 0.5);
    color: var(--fg2);
}
.search-hit:hover { color: var(--cyan); }
.search-hit .hit-sid { font-size: 0.72rem; color: var(--fg3); }
.search-hit em { color: var(--yellow); font-style: normal; font-weight: 600; }
/* ---- Stats panel ---- */
.stats-panel {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 100;
}
.stats-panel.show { display: flex; }
.stats-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 30px;
    max-width: 500px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
}
.stats-card h2 { color: var(--cyan); margin-bottom: 20px; }
.stats-card table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.stats-card th, .stats-card td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
.stats-card th { color: var(--fg3); font-weight: 600; }
.stats-card .close-btn {
    margin-top: 20px;
    text-align: center;
}
.stats-card .close-btn button { width: auto; padding: 8px 30px; }
/* ---- Modal ---- */
.modal-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 100;
}
.modal-overlay.show { display: flex; }
.modal {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    max-width: 420px;
    width: 90%;
}
.modal h3 { margin-bottom: 12px; color: var(--red); }
.modal p { font-size: 0.9rem; color: var(--fg2); margin-bottom: 20px; }
.modal .btns { display: flex; gap: 10px; justify-content: flex-end; }
.modal .btns button { width: auto; }
/* ---- Toast ---- */
.toast {
    position: fixed;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%);
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 24px;
    font-size: 0.88rem;
    z-index: 200;
    animation: toastIn 0.3s ease, toastOut 0.3s ease 2.5s forwards;
}
@keyframes toastIn { from { opacity: 0; transform: translateX(-50%) translateY(10px); } }
@keyframes toastOut { to { opacity: 0; transform: translateX(-50%) translateY(-10px); } }
/* ---- Scrollbar ---- */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--fg3); }
/* ---- Responsive ---- */
@media (max-width: 900px) {
    main { flex-direction: column; }
    .panel-left { width: 100%; max-height: 35%; }
    .panel-right { width: 100%; flex-direction: row; flex-wrap: wrap; }
    .panel-right button { width: auto; flex: 1; min-width: 120px; }
}
</style>
</head>
<body>

<header>
    <h1>Claude Code 对话管理器</h1>
    <div class="stats-bar">
        <span id="total-sessions">--</span>
        <span id="total-messages">--</span>
        <span id="total-tokens">--</span>
        <button onclick="shutdownServer()" style="width:auto;padding:4px 12px;font-size:0.78rem;background:var(--bg3);border:1px solid var(--border);border-radius:4px;cursor:pointer;color:var(--fg3);margin-left:12px;">关闭服务器</button>
    </div>
</header>

<main>
    <!-- Left: Session List -->
    <aside class="panel-left">
        <div class="batch-bar" id="batch-bar" style="display:none">
            <label class="select-all"><input type="checkbox" id="select-all" onchange="toggleSelectAll()"> 全选</label>
            <button class="batch-del-btn" onclick="batchDelete()">删除选中 (<span id="selected-count">0</span>)</button>
        </div>
        <div class="search-box">
            <input type="text" id="filter-input" placeholder="筛选会话..." oninput="filterSessions()">
        </div>
        <div class="session-list" id="session-list"></div>
    </aside>

    <!-- Center: Conversation View -->
    <section class="panel-center">
        <div class="conversation-header" id="conv-header" style="display:none">
            <h2 id="conv-title"></h2>
            <div class="conv-meta">
                <span id="conv-date"></span>
                <span id="conv-project"></span>
                <span id="conv-model"></span>
                <span id="conv-size"></span>
                <span id="conv-msgs"></span>
            </div>
        </div>
        <div class="conversation-view" id="conv-view">
            <div class="empty-state">
                <div class="icon">&#128172;</div>
                <p>从左侧选择一个对话</p>
                <p style="font-size:0.78rem;margin-top:6px;">或使用右侧搜索功能</p>
            </div>
        </div>
    </section>

    <!-- Right: Actions -->
    <aside class="panel-right">
        <h3>操作</h3>
        <button onclick="refreshSessions()" class="primary">刷新列表</button>
        <button onclick="showStats()">使用统计</button>
        <button onclick="exportCurrent()">导出 Markdown</button>
        <button onclick="confirmDelete()" class="danger">删除此对话</button>

        <h3 style="margin-top:8px;">搜索</h3>
        <div class="search-section">
            <input type="text" id="global-search" placeholder="搜索所有对话..."
                   onkeydown="if(event.key==='Enter') globalSearch()">
            <button onclick="globalSearch()">搜索</button>
            <div class="search-results" id="search-results"></div>
        </div>
    </aside>
</main>

<!-- Stats Modal -->
<div class="stats-panel" id="stats-panel" onclick="if(event.target===this) closeStats()">
    <div class="stats-card" id="stats-card"></div>
</div>

<!-- Delete Confirmation Modal -->
<div class="modal-overlay" id="delete-modal">
    <div class="modal">
        <h3>删除会话</h3>
        <p id="delete-msg">确定要删除这个对话吗？此操作不可撤销。</p>
        <div class="btns">
            <button onclick="closeDeleteModal()">取消</button>
            <button class="danger" onclick="doDelete()" style="color:var(--red)">删除</button>
        </div>
    </div>
</div>

<script>
// ---- State ----
let sessions = [];
let currentSessionId = null;
let currentSessionMeta = null;
let selectedSids = new Set();
let sessionLoadFailed = false;
let renameInProgress = false;  // suppress auto-refresh during inline rename

// ---- Auto-refresh state ----
// lastSessionData & lastCacheTimestamp track whether the server-side cache
// was rebuilt (due to file watcher detecting changes), so the UI refreshes
// only when something actually changed — not on every poll.
let lastSessionData = null;
let lastCacheTimestamp = 0;

// ---- Cache Check Timer ----
function startCacheCheckTimer() {
    setInterval(async () => {
        if (renameInProgress) return;  // don't clobber inline rename UI

        if (lastSessionData !== null) {
            try {
                const response = await fetch('/api/sessions?v=' + Date.now());
                const data = await response.json();
                const newCacheTimestamp = data.cache_invalidation_time || 0;
                if (newCacheTimestamp !== lastCacheTimestamp) {
                    refreshSessions();
                }
            } catch (e) {
                // Ignore fetch errors
            }
        }
    }, 2000); // Check every 2 seconds
}

// ---- Init ----
async function refreshSessions() {
    sessionLoadFailed = false;
    const listEl = document.getElementById('session-list');
    listEl.innerHTML = '<div class="loading-msg">加载中…</div>';
    try {
        const r = await fetch('/api/sessions?v=' + Date.now());
        const data = await r.json();
        sessions = data.sessions;
        sessionLoadFailed = false;
        renderSessionList();
        updateHeaderStats(data);

        // Auto-update detail view title if the current session was renamed
        // (e.g. by terminal /rename) while the detail view is open
        if (currentSessionId && currentSessionMeta) {
            const updated = sessions.find(s => s.session_id === currentSessionId);
            if (updated && updated.title !== currentSessionMeta.title) {
                currentSessionMeta.title = updated.title;
                const titleEl = document.getElementById('conv-title');
                if (titleEl && titleEl.children.length === 0) return; // nothing to update
                if (titleEl) {
                    titleEl.textContent = '';
                    const sp = document.createElement('span');
                    sp.className = 'editable-title';
                    sp.textContent = updated.title || '(无标题)';
                    sp.title = '点击重命名';
                    sp.onclick = function() { startRename(this); };
                    titleEl.appendChild(sp);
                }
            }
        }

        // Store the current session data and cache timestamp for comparison
        lastSessionData = JSON.stringify(data.sessions.map(s => ({sid: s.session_id, title: s.title})));
        lastCacheTimestamp = data.cache_invalidation_time || 0;
    } catch (e) {
        sessionLoadFailed = true;
        listEl.innerHTML = '<div class="error-msg">⚠ 加载失败：' + e.message +
            '<br><button onclick="refreshSessions()" style="margin-top:8px;padding:4px 16px;cursor:pointer">重试</button></div>';
    }
}

function renderSessionList(filter = '') {
    const list = document.getElementById('session-list');
    const f = filter.toLowerCase();
    const filtered = sessions.filter(s =>
        !f || s.title.toLowerCase().includes(f) ||
         s.session_id.toLowerCase().includes(f) ||
         s.project.toLowerCase().includes(f)
    );

    if (filtered.length === 0) {
        if (sessionLoadFailed) return;
        list.innerHTML = '<div class="empty-msg">' + (filter ? '无匹配对话' : '暂无对话记录') + '</div>';
        return;
    }
    list.innerHTML = filtered.map(s => `
        <div class="session-item${s.session_id === currentSessionId ? ' active' : ''}">
            <input type="checkbox" class="sess-check" data-sid="${escHtml(s.session_id)}"
                   onclick="event.stopPropagation(); onCheckToggle()"
                   ${selectedSids.has(s.session_id) ? 'checked' : ''}>
            <div class="sess-info" onclick="selectSession('${escHtml(s.session_id)}')">
                <div class="sess-title">${escHtml(s.title)}</div>
                <div class="sess-meta">
                    <span>${s.date_short || s.human_date}</span>
                    <span>${s.size_str}</span>
                    <span>${s.msg_count} 条消息</span>
                    ${s.active ? '<span class="active-dot">活跃中</span>' : ''}
                </div>
            </div>
            <span class="sess-rename-btn" onclick="event.stopPropagation(); startSidebarRename('${escHtml(s.session_id)}', this)" title="重命名">&#9998;</span>
        </div>
    `).join('');}

function filterSessions() {
    renderSessionList(document.getElementById('filter-input').value);
}

// ---- Sidebar inline rename ----
function startSidebarRename(sid, btnEl) {
    if (btnEl._renaming) return;
    btnEl._renaming = true;
    renameInProgress = true;  // suppress auto-refresh while editing

    const item = btnEl.closest('.session-item');
    const titleDiv = item.querySelector('.sess-title');
    const oldTitle = titleDiv.textContent;

    const input = document.createElement('input');
    input.type = 'text';
    input.value = oldTitle === '(无标题)' ? '' : oldTitle;
    input.style.cssText = 'width:100%;font-size:0.85rem;font-weight:600;border:1px solid var(--blue);border-radius:3px;padding:1px 4px;background:var(--bg);color:var(--fg);font-family:var(--font);outline:none;box-sizing:border-box';
    titleDiv.replaceWith(input);
    input.focus();
    input.select();

    let saving = false;
    async function save() {
        if (saving) return;
        saving = true;
        input.onblur = null;
        const newTitle = input.value.trim() || oldTitle;
        const displayTitle = newTitle || '(无标题)';

        // Restore the title div with new text
        const newDiv = document.createElement('div');
        newDiv.className = 'sess-title';
        newDiv.textContent = displayTitle;
        if (input.parentNode) input.replaceWith(newDiv);
        btnEl._renaming = false;

        // Update sessions array
        if (newTitle && newTitle !== oldTitle) {
            for (let s of sessions) {
                if (s.session_id === sid) { s.title = newTitle; break; }
            }
            // Update detail view if this session is currently selected
            if (sid === currentSessionId && currentSessionMeta) {
                currentSessionMeta.title = newTitle;
                const titleEl = document.getElementById('conv-title');
                if (titleEl) {
                    titleEl.textContent = '';
                    const sp = document.createElement('span');
                    sp.className = 'editable-title';
                    sp.textContent = newTitle || '(无标题)';
                    sp.title = '点击重命名';
                    sp.onclick = function() { startRename(this); };
                    titleEl.appendChild(sp);
                }
            }
            // Persist to server
            try {
                await fetch('/api/session/' + sid + '/rename', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: newTitle})
                });
            } catch (e) { /* silent */ }
        }
        renameInProgress = false;
    }

    input.onblur = save;
    input.onkeydown = function(e) {
        if (e.key === 'Enter') { input.blur(); }
        if (e.key === 'Escape') { input.value = oldTitle; renameInProgress = false; input.blur(); }
    };
}

// ---- Batch selection ----
function onCheckToggle() {
    const checks = document.querySelectorAll('.sess-check:checked');
    selectedSids = new Set();
    checks.forEach(c => selectedSids.add(c.dataset.sid));
    updateBatchBar();
}

function toggleSelectAll() {
    const selectAll = document.getElementById('select-all');
    const checks = document.querySelectorAll('.sess-check');
    checks.forEach(c => {
        c.checked = selectAll.checked;
        if (selectAll.checked) selectedSids.add(c.dataset.sid);
        else selectedSids.delete(c.dataset.sid);
    });
    updateBatchBar();
}

function updateBatchBar() {
    const bar = document.getElementById('batch-bar');
    const count = selectedSids.size;
    document.getElementById('selected-count').textContent = count;
    bar.style.display = count > 0 ? 'flex' : 'none';
}

async function batchDelete() {
    const count = selectedSids.size;
    if (count === 0) { showToast('请先勾选要删除的会话'); return; }
    if (!confirm('确定删除选中的 ' + count + ' 个会话？此操作不可恢复。')) return;

    const sids = Array.from(selectedSids);
    let deleted = 0;
    for (const sid of sids) {
        try {
            const r = await fetch('/api/session/' + sid, { method: 'DELETE' });
            const data = await r.json();
            if (data.ok) deleted++;
        } catch (e) {}
    }
    showToast('已删除 ' + deleted + '/' + sids.length + ' 个会话');
    selectedSids.clear();
    updateBatchBar();
    document.getElementById('select-all').checked = false;
    if (sids.includes(currentSessionId)) {
        currentSessionId = null;
        currentSessionMeta = null;
        document.getElementById('conv-header').style.display = 'none';
        document.getElementById('conv-view').innerHTML =
            '<div class="empty-state"><div class="icon">&#128172;</div><p>从左侧选择一个对话</p><p style="font-size:0.78rem;margin-top:6px;">或使用右侧搜索功能</p></div>';
    }
    refreshSessions();
}

function updateHeaderStats(data) {
    document.getElementById('total-sessions').textContent = data.total + ' 个会话';
    document.getElementById('total-messages').textContent = data.total_msgs + ' 条消息';
    document.getElementById('total-tokens').textContent = data.total_tokens_str;
}

// ---- Session Selection ----
async function selectSession(sid) {
    currentSessionId = sid;
    renderSessionList(document.getElementById('filter-input').value);

    // Show loading
    document.getElementById('conv-header').style.display = 'none';
    document.getElementById('conv-view').innerHTML =
        '<div class="empty-state"><p>加载中...</p></div>';

    try {
        const r = await fetch('/api/session/' + sid);
        const data = await r.json();
        if (!r.ok || data.error) {
            document.getElementById('conv-view').innerHTML =
                '<div class="empty-state"><p style="color:var(--red)">加载失败：文件可能已被删除</p><p style="font-size:0.8rem;color:var(--fg3)">' + (data.error || '') + '</p></div>';
            return;
        }
        currentSessionMeta = data.meta;
        renderConversation(data);
    } catch (e) {
        document.getElementById('conv-view').innerHTML =
            '<div class="empty-state"><p style="color:var(--red)">加载失败：' + e.message + '</p></div>';
    }
}

function renderConversation(data) {
    const m = data.meta;
    document.getElementById('conv-header').style.display = 'block';
    const titleEl = document.getElementById('conv-title');
    titleEl.textContent = '';
    const titleSpan = document.createElement('span');
    titleSpan.className = 'editable-title';
    titleSpan.textContent = m.title || '(无标题)';
    titleSpan.title = '点击重命名';
    titleSpan.onclick = function() { startRename(this); };
    titleEl.appendChild(titleSpan);
    document.getElementById('conv-date').textContent = m.human_date;
    document.getElementById('conv-project').textContent = m.project;
    document.getElementById('conv-model').textContent = m.model;
    document.getElementById('conv-size').textContent = m.size_str;
    document.getElementById('conv-msgs').textContent = m.msg_count + ' 条消息';

    const view = document.getElementById('conv-view');
    if (!data.messages || data.messages.length === 0) {
        view.innerHTML = '<div class="empty-state"><p>此会话无消息</p></div>';
        return;
    }

    let parts = [];
    for (const msg of data.messages) {
        const mtype = msg.type || 'text';

        // System messages — centered dim text
        // Skip rename notifications (meta-commentary, not conversation)
        if (msg.role === 'system') {
            const txt = msg.text || '';
            if (txt.startsWith('The user named this session') ||
                txt.includes('named this session')) {
                continue;
            }
            parts.push(`<div class="chat-bubble system">${escHtml(txt)}</div>`);
            continue;
        }

        // Thinking — tiny dim indicator, click to expand
        if (mtype === 'thinking' || mtype === 'tool') {
            continue;
        }

        // Text messages — WeChat-style bubbles with avatar row
        const isUser = msg.role === 'user';
        if (isUser) {
            parts.push(`<div class="msg-row user-row">
                <div class="chat-bubble user">${escHtml(msg.text)}</div>
                <div class="chat-avatar" style="background:#4a90f2;color:#fff">${String.fromCodePoint(0x1F464)}</div>
            </div>`);
        } else {
            parts.push(`<div class="msg-row assistant-row">
                <div class="chat-avatar" style="background:#f0f0f0;color:#666">${String.fromCodePoint(0x1F916)}</div>
                <div class="chat-bubble assistant">${escHtml(msg.text)}</div>
            </div>`);
        }
    }
    view.innerHTML = parts.join('');
    view.scrollTop = view.scrollHeight;
}

// ---- Search ----
async function globalSearch() {
    const q = document.getElementById('global-search').value.trim();
    if (!q) return;
    const results = document.getElementById('search-results');
    results.innerHTML = '搜索中...';
    try {
        const r = await fetch('/api/search?q=' + encodeURIComponent(q));
        const data = await r.json();
        if (!data.results || data.results.length === 0) {
            results.innerHTML = '<div style="color:var(--fg3);padding:8px">未找到结果</div>';
            return;
        }
        results.innerHTML = data.results.map(res => `
            <div class="search-hit" onclick="selectSession('${res.session_id}')">
                <div class="hit-sid">${res.session_id.slice(0,8)}... &middot; ${res.hit_count} 处匹配</div>
                <div>${highlightMatches(res.samples[0], q)}</div>
            </div>
        `).join('');
    } catch (e) {
        results.innerHTML = '<div style="color:var(--red)">搜索错误：' + e.message + '</div>';
    }
}

function highlightMatches(text, q) {
    const re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    // Replace matches BEFORE HTML escaping, then escape the result
    return escHtml(text.replace(re, '\x00$1\x01'))
        .replace(/\x00/g, '<em>')
        .replace(/\x01/g, '</em>');
}

// ---- Stats ----
async function showStats() {
    const panel = document.getElementById('stats-panel');
    const card = document.getElementById('stats-card');
    panel.classList.add('show');
    card.innerHTML = '<p>加载中...</p>';
    try {
        const r = await fetch('/api/stats');
        const data = await r.json();
        card.innerHTML = buildStatsHtml(data);
    } catch (e) {
        card.innerHTML = '<p style="color:var(--red)">加载失败：' + e.message + '</p>';
    }
}

function buildStatsHtml(data) {
    let html = '<h2>使用统计</h2>';
    html += '<table>';
    html += `<tr><td>总会话数</td><td><strong>${data.total_sessions}</strong></td></tr>`;
    html += `<tr><td>总消息数</td><td><strong>${data.total_messages}</strong></td></tr>`;
    if (data.model_usage) {
        for (const [model, usage] of Object.entries(data.model_usage)) {
            html += `<tr><td colspan="2" style="color:var(--cyan);padding-top:12px">${escHtml(model)}</td></tr>`;
            html += `<tr><td style="padding-left:20px">输入 Token</td><td>${usage.input}</td></tr>`;
            html += `<tr><td style="padding-left:20px">输出 Token</td><td>${usage.output}</td></tr>`;
            html += `<tr><td style="padding-left:20px">缓存读取</td><td>${usage.cache_read}</td></tr>`;
        }
    }
    html += '</table>';
    if (data.daily) {
        html += '<h3 style="margin-top:16px;color:var(--fg2)">每日活动</h3>';
        html += '<table><tr><th>日期</th><th>会话数</th><th>消息数</th><th>工具调用</th></tr>';
        for (const d of data.daily) {
            html += `<tr><td>${d.date}</td><td>${d.sessions}</td><td>${d.messages}</td><td>${d.tool_calls}</td></tr>`;
        }
        html += '</table>';
    }
    html += '<div class="close-btn"><button onclick="closeStats()">关闭</button></div>';
    return html;
}

function closeStats() {
    document.getElementById('stats-panel').classList.remove('show');
}

// ---- Export ----
function exportCurrent() {
    if (!currentSessionId) { showToast('请先选择一个会话'); return; }
    window.open('/api/export/' + currentSessionId, '_blank');
}

// ---- Delete ----
function confirmDelete() {
    if (!currentSessionId) { showToast('请先选择一个会话'); return; }
    const title = currentSessionMeta ? currentSessionMeta.title : currentSessionId.slice(0,16);
    document.getElementById('delete-msg').textContent =
        '确定删除 "' + title + '"？此操作不可恢复。';
    document.getElementById('delete-modal').classList.add('show');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.remove('show');
}

async function doDelete() {
    if (!currentSessionId) return;
    try {
        const r = await fetch('/api/session/' + currentSessionId, { method: 'DELETE' });
        const data = await r.json();
        if (!r.ok) {
            showToast('删除失败：' + (data.error || '未知错误'));
            closeDeleteModal();
            return;
        }
        if (data.ok) {
            showToast('已删除');
            currentSessionId = null;
            currentSessionMeta = null;
            document.getElementById('conv-header').style.display = 'none';
            document.getElementById('conv-view').innerHTML =
                '<div class="empty-state"><div class="icon">&#128172;</div><p>请从左侧选择一个对话</p></div>';
            refreshSessions();
        } else {
            showToast('错误：' + (data.error || '未知'));
        }
    } catch (e) {
        showToast('删除失败：' + e.message);
    }
    closeDeleteModal();
}

// ---- Utils ----
function escHtml(s) {
    if (!s) return '';
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
}

// ---- Rename ----
function startRename(spanEl) {
    if (spanEl._renaming) return;
    spanEl._renaming = true;
    renameInProgress = true;  // suppress auto-refresh while editing
    const oldTitle = spanEl.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.value = oldTitle === '(无标题)' ? '' : oldTitle;
    input.style.width = '400px';
    input.style.maxWidth = '100%';
    input.style.fontSize = '1rem';
    input.style.fontWeight = '600';
    input.style.border = '1px solid var(--blue)';
    input.style.borderRadius = '4px';
    input.style.padding = '2px 8px';
    input.style.background = 'var(--bg)';
    input.style.color = 'var(--fg)';
    input.style.fontFamily = 'var(--font)';
    input.style.outline = 'none';
    spanEl.replaceWith(input);
    input.focus();
    input.select();

    let saving = false;
    async function save() {
        if (saving) return;
        saving = true;
        input.onblur = null;
        const newTitle = input.value.trim() || oldTitle;
        const displayTitle = newTitle || '(无标题)';
        // Update UI instantly
        const newSpan = document.createElement('span');
        newSpan.className = 'editable-title';
        newSpan.textContent = displayTitle;
        newSpan.title = '点击重命名';
        newSpan.onclick = function() { startRename(this); };
        if (input.parentNode) input.replaceWith(newSpan);
        else spanEl.replaceWith(newSpan);

        // Update local sessions array immediately (no server roundtrip needed)
        if (newTitle && newTitle !== oldTitle && currentSessionId) {
            for (let s of sessions) {
                if (s.session_id === currentSessionId) { s.title = newTitle; break; }
            }
            renderSessionList(document.getElementById('filter-input').value);
            // Also persist to server
            try {
                await fetch('/api/session/' + currentSessionId + '/rename', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: newTitle})
                });
            } catch (e) { /* silent — already updated locally */ }
        }
        renameInProgress = false;
    }

    input.onblur = save;
    input.onkeydown = function(e) {
        if (e.key === 'Enter') { input.blur(); }
        if (e.key === 'Escape') { input.value = oldTitle; renameInProgress = false; input.blur(); }
    };
}

function showToast(msg) {
    const old = document.getElementById('toast');
    if (old) old.remove();
    const t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.remove(); }, 3000);
}

// ---- Shutdown ----
let heartBeatTimer = null;

function shutdownServer() {
    if (confirm('确定关闭服务器？关闭后需重新双击 .vbs 启动。')) {
        navigator.sendBeacon('/api/shutdown');
        document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:var(--font);color:var(--fg3)"><p>服务器已关闭，可以关闭此页面。</p></div>';
    }
}

// Heartbeat: server auto-shuts down if no heartbeat for 30 seconds
// This handles browser close without needing beforeunload
function startHeartbeat() {
    if (heartBeatTimer) clearInterval(heartBeatTimer);
    heartBeatTimer = setInterval(function() {
        fetch('/api/heartbeat').catch(function() {});
    }, 10000);
    fetch('/api/heartbeat').catch(function() {});
}

// ---- Boot ----
startHeartbeat();
startCacheCheckTimer();
refreshSessions();
</script>
</body>
</html>
"""

# ---- API Routes ----------------------------------------------------------

# ---- Auto-shutdown via heartbeat ------------------------------------------
_shutdown_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()

def _reset_shutdown_timer():
    global _shutdown_timer
    with _timer_lock:
        if _shutdown_timer:
            _shutdown_timer.cancel()
        _shutdown_timer = threading.Timer(35.0, _do_shutdown)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()

def _do_shutdown():
    os._exit(0)


@app.route('/api/heartbeat')
def api_heartbeat():
    """Browser heartbeat — keeps server alive. Auto-shuts down after 35s idle."""
    _reset_shutdown_timer()
    return jsonify({'ok': True})


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """Shut down the server."""
    with _timer_lock:
        if _shutdown_timer:
            _shutdown_timer.cancel()
    try:
        os._exit(0)
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/')
def index():
    resp = app.make_response(render_template_string(INDEX_HTML))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/api/sessions')
def api_sessions():
    """Return all sessions with metadata — cached for 3 seconds for performance."""
    global _sessions_cache, _sessions_cache_time
    if _sessions_cache is not None and (time.time() - _sessions_cache_time) < 30.0:
        return jsonify(_sessions_cache)

    idx = cm.build_session_index()
    proj_map = cm.build_project_session_map()
    sid_to_proj = {}
    for proj_name, sids in proj_map.items():
        for sid in sids:
            sid_to_proj[sid] = proj_name
    active_sids = cm.get_active_session_ids()

    # Collect all known session IDs (history + disk)
    all_sids = set(idx.keys())
    for sids in proj_map.values():
        all_sids.update(sids)

    result = []
    for sid in all_sids:
        meta = idx.get(sid)
        tpath = cm.find_transcript(sid)
        proj = sid_to_proj.get(sid, meta.project if meta else 'unknown')

        if tpath:
            size_bytes = os.path.getsize(tpath)
            st = cm.quick_transcript_stats(tpath)
            raw_title = st.title or (meta.first_prompt if meta else '(untitled)')
            title = _resolve_title(sid, raw_title, meta.first_prompt if meta else None)
            model = st.model or '?'
            msg_count = st.user_messages + st.assistant_messages
            tokens = cm.format_tokens(st.input_tokens + st.output_tokens)
            size_str = cm.format_size(size_bytes)
            # Use file mtime for orphaned sessions without history entry
            if meta:
                ts_ms = meta.timestamp_ms
                human_date = meta.human_date
            else:
                mtime = os.path.getmtime(tpath)
                ts_ms = int(mtime * 1000)
                human_date = cm.format_timestamp(ts_ms)
        else:
            if not meta:
                continue  # No history entry AND no file — skip
            title = meta.first_prompt or '(deleted)'
            model = '?'
            msg_count = 0
            tokens = '0'
            size_str = chr(8212)  # em dash
            ts_ms = meta.timestamp_ms
            human_date = meta.human_date

        result.append({
            'session_id': sid,
            'title': title.replace('\n', ' ').strip() if title else '(untitled)',
            'human_date': human_date,
            'date_short': cm.format_date_short(ts_ms),
            'project': proj,
            'model': model,
            'msg_count': msg_count,
            'tokens_str': tokens,
            'size_str': size_str,
            'active': sid in active_sids,
            'timestamp_ms': ts_ms,
        })

    result.sort(key=lambda s: s['timestamp_ms'], reverse=True)
    total_msgs = sum(s['msg_count'] for s in result if isinstance(s['msg_count'], int))

    # Use a stable cache timestamp — only changes when the cache is rebuilt,
    # so the JS auto-refresh doesn't re-render the list every 2 seconds.
    _sessions_cache_time = time.time()

    _sessions_cache = {
        'sessions': result,
        'total': len(result),
        'total_msgs': total_msgs,
        'total_tokens_str': '—',
        'cache_invalidation_time': _sessions_cache_time,
    }
    resp = jsonify(_sessions_cache)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/api/session/<session_id>')
def api_session(session_id):
    """Return a session's full content."""
    tpath = cm.find_transcript(session_id)
    if not tpath:
        # Try fuzzy match
        matches = cm._fuzzy_find_sessions(session_id)
        if len(matches) == 1:
            tpath = cm.find_transcript(matches[0])
            session_id = matches[0]
        if not tpath:
            return jsonify({'error': 'Session not found'}), 404

    st = cm.quick_transcript_stats(tpath)
    idx = cm.build_session_index()
    meta = idx.get(session_id)
    proj = cm.find_project_for_session(session_id) or 'unknown'

    messages = []
    for entry in cm.read_transcript(tpath):
        typ = entry.get('type', '')
        msg = entry.get('message', {})

        if typ == 'user':
            # Skip tool results — they just repeat what the tool already showed
            content = msg.get('content', '')
            if isinstance(content, list):
                # Array content = tool result from a tool_use call
                continue
            if isinstance(content, str):
                # Skip tool results and internal system markup
                if '[Tool result' in content or '<local-command' in content or \
                   '<bash-stdout>' in content or '<bash-stderr>' in content or \
                   '<command-name>' in content:
                    continue
            text = cm.extract_text(entry, include_thinking=False)
            if text:
                # Strip known Claude Code internal markup only, not legit angle-bracket text (e.g. code)
                text = re.sub(r'<(?:local-command|command-name|command-message|command-args|bash-stdout|bash-stderr|system-reminder|antml:[^>]+)[^>]*>.*?</(?:local-command|command-name|command-message|command-args|bash-stdout|bash-stderr|system-reminder|antml:[^>]+)>', '', text, flags=re.DOTALL)
                text = re.sub(r'<[^>]+/>', '', text)  # self-closing tags
                text = text.strip()
                if text and not text.startswith('[Tool result'):
                    messages.append({'role': 'user', 'type': 'text', 'text': text})

        elif typ == 'assistant':
            blocks = cm.extract_assistant_blocks(entry)
            for block in blocks:
                bt = block.get('type', '')
                if bt == 'text':
                    block_text = cm.strip_ansi(block.get('text', ''))
                    if block_text.strip():
                        messages.append({'role': 'assistant', 'type': 'text', 'text': block_text})
                elif bt == 'thinking':
                    think_text = cm.strip_ansi(block.get('thinking', ''))
                    if think_text.strip():
                        messages.append({'role': 'assistant', 'type': 'thinking',
                                        'text': think_text[:200] + ('...' if len(think_text) > 200 else ''),
                                        'full_text': think_text})
                elif bt == 'tool_use':
                    name = block.get('name', '?')
                    inp = block.get('input', {})
                    # Use the human-readable description as the primary display
                    desc = inp.get('description', '') or block.get('description', '')
                    # Build a clean one-line summary
                    clean_input = {}
                    for k, v in inp.items():
                        if k == 'description': continue
                        if isinstance(v, str) and len(v) > 80:
                            clean_input[k] = v[:77] + '...'
                        elif not isinstance(v, (str, int, float, bool, type(None))):
                            clean_input[k] = str(type(v).__name__)
                        else:
                            clean_input[k] = v
                    inp_preview = json.dumps(clean_input, ensure_ascii=False)
                    if len(inp_preview) > 200:
                        inp_preview = inp_preview[:197] + '...'
                    messages.append({'role': 'assistant', 'type': 'tool',
                                    'text': f'{name}',
                                    'tool_name': name,
                                    'tool_input': desc or inp_preview,
                                    'tool_desc': desc})

        elif typ == 'system':
            text = cm.extract_text(entry, include_thinking=False)
            if text:
                messages.append({'role': 'system', 'type': 'system', 'text': text})

    return jsonify({
        'meta': {
            'title': _resolve_title(session_id, st.title or (meta.first_prompt if meta else '(untitled)'), meta.first_prompt if meta else None),
            'human_date': meta.human_date if meta else 'unknown',
            'project': proj,
            'model': st.model or '?',
            'size_str': cm.format_size(os.path.getsize(tpath)),
            'msg_count': st.user_messages + st.assistant_messages,
            'tokens_str': cm.format_tokens(st.input_tokens + st.output_tokens),
        },
        'messages': messages,
    })


@app.route('/api/search')
def api_search():
    """Search across all sessions."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'results': []})

    keyword = q.lower()
    idx = cm.build_session_index()
    results = []

    for sid, meta in idx.items():
        tpath = cm.find_transcript(sid)
        if not tpath:
            continue

        hits = []
        for entry in cm.read_transcript(tpath):
            if entry.get('type') not in ('user', 'assistant', 'ai-title'):
                continue
            text = cm.extract_text(entry, include_thinking=False)
            if not text:
                continue
            if keyword in text.lower():
                idx_pos = text.lower().find(keyword)
                start = max(0, idx_pos - 40)
                end = min(len(text), idx_pos + len(keyword) + 40)
                snippet = text[start:end]
                if start > 0:
                    snippet = '...' + snippet
                if end < len(text):
                    snippet = snippet + '...'
                hits.append(snippet)

        if hits:
            results.append({
                'session_id': sid,
                'title': meta.first_prompt,
                'hit_count': len(hits),
                'samples': hits[:5],
            })

    results.sort(key=lambda r: -r['hit_count'])
    return jsonify({'results': results[:20]})


@app.route('/api/stats')
def api_stats():
    """Return usage statistics."""
    cache_path = cm.get_stats_cache_path()
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding='utf-8'))
            return jsonify({
                'total_sessions': data.get('totalSessions', 0),
                'total_messages': data.get('totalMessages', 0),
                'model_usage': {
                    model: {
                        'input': cm.format_tokens(u.get('inputTokens', 0)),
                        'output': cm.format_tokens(u.get('outputTokens', 0)),
                        'cache_read': cm.format_tokens(u.get('cacheReadInputTokens', 0)),
                    }
                    for model, u in data.get('modelUsage', {}).items()
                },
                'daily': [
                    {
                        'date': d.get('date', ''),
                        'sessions': d.get('sessionCount', 0),
                        'messages': d.get('messageCount', 0),
                        'tool_calls': d.get('toolCallCount', 0),
                    }
                    for d in data.get('dailyActivity', [])
                ],
            })
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: compute from all transcripts
    total_sessions = 0
    total_user = 0
    total_assistant = 0
    total_input = 0
    total_output = 0
    total_cache = 0
    model_usage = {}
    proj_dir = cm.get_projects_dir()
    if proj_dir.exists():
        for pd in proj_dir.iterdir():
            if not pd.is_dir():
                continue
            for f in pd.iterdir():
                if f.suffix.lower() != '.jsonl' or f.name.startswith('agent-'):
                    continue
                total_sessions += 1
                st = cm.quick_transcript_stats(f)
                total_user += st.user_messages
                total_assistant += st.assistant_messages
                total_input += st.input_tokens
                total_output += st.output_tokens
                total_cache += st.cache_read_tokens
                if st.model:
                    if st.model not in model_usage:
                        model_usage[st.model] = {'input': 0, 'output': 0, 'cache_read': 0}
                    model_usage[st.model]['input'] += st.input_tokens
                    model_usage[st.model]['output'] += st.output_tokens
                    model_usage[st.model]['cache_read'] += st.cache_read_tokens

    # Format tokens for display (same structure as cached path)
    return jsonify({
        'total_sessions': total_sessions,
        'total_messages': total_user + total_assistant,
        'model_usage': {
            m: {
                'input': cm.format_tokens(u['input']),
                'output': cm.format_tokens(u['output']),
                'cache_read': cm.format_tokens(u['cache_read']),
            }
            for m, u in model_usage.items()
        },
        'daily': [],
    })


@app.route('/api/session/<session_id>/rename', methods=['PUT'])
def api_rename_session(session_id):
    """Rename a session — updates both our titles.json AND Claude Code's history.jsonl."""
    data = request.get_json(silent=True) or {}
    new_title = data.get('title', '').strip()
    if not new_title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400

    # 1. Save to our custom titles file
    titles = _load_custom_titles()
    titles[session_id] = new_title
    _save_custom_titles(titles)

    # 2. Update history.jsonl so Claude Code sees the new name too
    _update_history_display(session_id, new_title)
    _invalidate_sessions_cache()

    return jsonify({'ok': True, 'title': new_title})


def _update_history_display(session_id: str, new_title: str):
    """Update the 'display' field in history.jsonl for all entries of a session (atomic write)."""
    hist_path = cm.get_history_path()
    if not hist_path.exists():
        return
    try:
        lines = hist_path.read_text(encoding='utf-8').splitlines(keepends=True)
        tmp_path = hist_path.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    f.write(line)  # preserve blank lines
                    continue
                try:
                    entry = json.loads(stripped)
                    if entry.get('sessionId') == session_id:
                        entry['display'] = new_title
                        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                        continue
                except json.JSONDecodeError:
                    pass
                f.write(line)
        os.replace(tmp_path, hist_path)  # atomic on Windows & POSIX
        # Also write custom-title to the transcript (matches /rename behavior)
        tpath = cm.find_transcript(session_id)
        if tpath and tpath.exists():
            _update_transcript_title(tpath, new_title)
    except OSError:
        pass


def _update_transcript_title(tpath: Path, new_title: str):
    """Write a custom-title entry to the transcript — matches what /rename does
    in the terminal, so both sides stay in sync."""
    try:
        # Extract session ID from the transcript filename
        session_id = tpath.stem  # e.g. "d358ad2b-d855-4222-aa8d-27ae7b2a3161"
        new_entry = json.dumps({
            'type': 'custom-title',
            'customTitle': new_title,
            'sessionId': session_id,
        }, ensure_ascii=False)
        with open(tpath, 'a', encoding='utf-8') as f:
            f.write(new_entry + '\n')
    except OSError:
        pass


@app.route('/api/session/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """Delete a session transcript."""
    tpath = cm.find_transcript(session_id)
    if not tpath:
        matches = cm._fuzzy_find_sessions(session_id)
        if len(matches) == 1:
            session_id = matches[0]
            tpath = cm.find_transcript(session_id)
        if not tpath:
            # File not on disk — just prune history with the (possibly fuzzy-matched) ID
            cm._prune_history_entries([session_id])
            return jsonify({'ok': True, 'note': 'History entry removed (no file found)'})

    try:
        tpath.unlink()
        assoc_dir = tpath.with_suffix('')
        if assoc_dir.is_dir():
            shutil.rmtree(assoc_dir)
        cm._prune_history_entries([session_id])
        _invalidate_sessions_cache()
        return jsonify({'ok': True})
    except (OSError, PermissionError) as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/export/<session_id>')
def api_export_session(session_id):
    """Export a session as Markdown download."""
    tpath = cm.find_transcript(session_id)
    if not tpath:
        matches = cm._fuzzy_find_sessions(session_id)
        if len(matches) == 1:
            session_id = matches[0]
            tpath = cm.find_transcript(session_id)
        if not tpath:
            return jsonify({'error': 'Session not found'}), 404

    idx = cm.build_session_index()
    meta = idx.get(session_id)
    st = cm.quick_transcript_stats(tpath)
    proj = cm.find_project_for_session(session_id) or 'unknown'

    lines = []
    raw_title = st.title or (meta.first_prompt if meta else 'Claude Code Session')
    title = _resolve_title(session_id, raw_title, meta.first_prompt if meta else None)
    lines.append(f'# {title}')
    lines.append('')
    lines.append(f'- **Session:** `{session_id}`')
    lines.append(f'- **Date:** {meta.human_date if meta else "unknown"}')
    lines.append(f'- **Project:** `{proj}`')
    lines.append(f'- **Model:** {st.model}')
    lines.append('')
    lines.append('---')
    lines.append('')

    for entry in cm.read_transcript(tpath):
        typ = entry.get('type', '')
        if typ == 'ai-title':
            lines.append(f"## {entry.get('aiTitle', '')}")
            lines.append('')
            continue
        text = cm.extract_text(entry, include_thinking=False)
        if text is None:
            continue
        if typ == 'user':
            clean = re.sub(r'<[^>]+>', '', text)
            lines.append(f'**You:** {clean}')
            lines.append('')
        elif typ == 'assistant':
            lines.append(f'**Claude:** {text}')
            lines.append('')
        elif typ == 'system':
            lines.append(f'*{text}*')
            lines.append('')

    content = '\n'.join(lines)
    out_path = tpath.with_suffix('.md')
    out_path.write_text(content, encoding='utf-8')

    return send_file(out_path, mimetype='text/markdown',
                     download_name=f'{session_id[:8]}.md',
                     as_attachment=True)


# ---- Main ---------------------------------------------------------------

def _kill_existing():
    """Kill any process already listening on our port (prevents stale servers)."""
    try:
        import subprocess as sp
        result = sp.run(['netstat', '-ano'], capture_output=True, text=True)
        pids = set()
        for line in result.stdout.splitlines():
            if f':{_PORT}' in line and ('127.0.0.1' in line or '0.0.0.0' in line):
                parts = line.strip().split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            try:
                sp.run(['taskkill', '-f', '-pid', pid], capture_output=True, timeout=5)
            except Exception:
                pass
    except Exception:
        pass

def main():
    _kill_existing()
    _reset_shutdown_timer()  # Start the heartbeat watchdog

    # 设置 watchdog 观察者以实现实时更新
    handler = ClaudeDataHandler()
    observer = Observer()

    # 监听 history.jsonl 所在目录
    history_path = cm.get_history_path().parent
    if history_path.exists():
        observer.schedule(handler, str(history_path), recursive=False)

    # 监听 sessions 目录
    sessions_dir = Path(cm.get_history_path()).parent / 'sessions'
    if sessions_dir.exists():
        observer.schedule(handler, str(sessions_dir), recursive=False)

    # 注意：不再递归监听 projects 目录，避免性能问题
    # history.jsonl 已包含新增对话信息，足够检测新对话

    observer.start()

    # 注册优雅关闭
    def cleanup():
        observer.stop()
        observer.join()

    import atexit
    atexit.register(cleanup)

    print(f"\n  Claude Code 对话管理器 — Web 界面")
    print(f"  启动地址: http://127.0.0.1:{_PORT}")
    print(f"  轻量级实时监控已启用（监听 history.jsonl 和 sessions）")
    print(f"  关闭浏览器 35 秒后服务器自动退出，或点击「关闭服务器」按钮\n")
    webbrowser.open(f'http://127.0.0.1:{_PORT}')

    try:
        app.run(host='127.0.0.1', port=_PORT, debug=False)
    finally:
        observer.stop()
        observer.join()


if __name__ == '__main__':
    main()

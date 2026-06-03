#!/usr/bin/env python3
"""
Claude Code Chat History Manager
=================================
Manage your Claude Code conversation transcripts: list, search, view, delete, stats, export.

Usage: python chat-manager.py <command> [args]
       python chat-manager.py list
       python chat-manager.py view <session-id>
       python chat-manager.py search <keyword>
       python chat-manager.py stats
       python chat-manager.py delete <session-id> [--force]
       python chat-manager.py export <session-id> [--output file.md]
"""

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterator, Any

# ---------------------------------------------------------------------------
# Rich bootstrap — auto-install if missing
# ---------------------------------------------------------------------------

RICH_AVAILABLE = False
Console = None
Table = None
Panel = None
Text = None
Syntax = None
Group = None

def _try_import_rich() -> bool:
    global RICH_AVAILABLE, Console, Table, Panel, Text, Syntax, Group
    try:
        from rich.console import Console as _Console
        from rich.table import Table as _Table
        from rich.panel import Panel as _Panel
        from rich.text import Text as _Text
        from rich.syntax import Syntax as _Syntax
        from rich.console import Group as _Group
        Console = _Console
        Table = _Table
        Panel = _Panel
        Text = _Text
        Syntax = _Syntax
        Group = _Group
        RICH_AVAILABLE = True
        return True
    except ImportError:
        return False

def _install_and_import_rich():
    import subprocess
    python_exe = sys.executable
    try:
        subprocess.check_call([python_exe, "-m", "pip", "install", "rich", "-q"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass  # pip might not be available; fall back to plain text

if not _try_import_rich():
    _install_and_import_rich()
    _try_import_rich()

# ---------------------------------------------------------------------------
# Terminal encoding detection
# ---------------------------------------------------------------------------

def _is_windows_gbk() -> bool:
    """Check if stdout uses GBK encoding (common on Windows)."""
    enc = sys.stdout.encoding or ''
    return enc.lower() in ('gbk', 'gb2312', 'gb18030', 'cp936')

def _safe_chars(box: str = '=', fill: str = '#', empty: str = '.') -> tuple[str, str, str]:
    """Return safe characters for the current terminal encoding."""
    if _is_windows_gbk():
        return box, fill, empty
    return box, fill, empty

# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_claude_home() -> Path:
    """Return ~/.claude directory path."""
    return Path.home() / ".claude"

def get_projects_dir() -> Path:
    return get_claude_home() / "projects"

def get_history_path() -> Path:
    return get_claude_home() / "history.jsonl"

def get_sessions_dir() -> Path:
    return get_claude_home() / "sessions"

def get_stats_cache_path() -> Path:
    return get_claude_home() / "stats-cache.json"

# ---------------------------------------------------------------------------
# Session index from history.jsonl
# ---------------------------------------------------------------------------

@dataclass
class SessionMeta:
    session_id: str
    first_prompt: str       # first user prompt
    project: str            # working directory
    timestamp_ms: int       # epoch ms
    human_date: str         # formatted date

def format_timestamp(epoch_ms: int) -> str:
    """Convert epoch milliseconds to human-readable date string."""
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "unknown"

def format_date_short(epoch_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        local_dt = dt.astimezone()
        return local_dt.strftime("%m-%d %H:%M")
    except (ValueError, OSError):
        return "???"

def build_session_index() -> dict[str, SessionMeta]:
    """Parse history.jsonl and return {sessionId: SessionMeta}.
    Only keeps the first entry per sessionId."""
    index: dict[str, SessionMeta] = {}
    hist_path = get_history_path()
    if not hist_path.exists():
        return index
    try:
        with open(hist_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = entry.get('sessionId')
                if not sid or sid in index:
                    continue
                index[sid] = SessionMeta(
                    session_id=sid,
                    first_prompt=entry.get('display', '(no prompt)'),
                    project=entry.get('project', ''),
                    timestamp_ms=entry.get('timestamp', 0),
                    human_date=format_timestamp(entry.get('timestamp', 0)),
                )
    except (OSError, UnicodeDecodeError) as e:
        print(f"Warning: could not read history file: {e}", file=sys.stderr)
    return index

def build_project_session_map() -> dict[str, set[str]]:
    """Return {project_dir_name: set of session_ids} by scanning project directories."""
    mapping: dict[str, set[str]] = {}
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return mapping
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        sids = set()
        for f in proj_dir.iterdir():
            if f.suffix == '.jsonl' and not f.name.startswith('agent-'):
                sids.add(f.stem)
        if sids:
            mapping[proj_dir.name] = sids
    return mapping

def find_transcript(session_id: str) -> Optional[Path]:
    """Find the transcript .jsonl file for a given session ID across all project dirs."""
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return None
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None

def find_project_for_session(session_id: str) -> Optional[str]:
    """Return the project directory name that contains this session."""
    tpath = find_transcript(session_id)
    if tpath:
        return tpath.parent.name
    return None

# ---------------------------------------------------------------------------
# Transcript file reading
# ---------------------------------------------------------------------------

def read_transcript(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects line-by-line from a transcript JSONL file."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # Corrupt line; skip with a one-time warning
                    pass
    except (OSError, UnicodeDecodeError) as e:
        print(f"Warning: error reading {path}: {e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def truncate_str(s: str, max_len: int = 200) -> str:
    """Truncate a string to max_len chars, adding ellipsis if truncated."""
    s = s.replace('\n', ' ').replace('\r', '')
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + "..."

def extract_text(entry: dict, include_thinking: bool = False) -> Optional[str]:
    """Extract human-readable text from a transcript line.
    Returns None for entries with no displayable text."""
    typ = entry.get('type', '')
    msg = entry.get('message', {})

    if typ == 'user':
        content = msg.get('content', '')
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    bt = block.get('type', '')
                    if bt == 'tool_result':
                        inner = strip_ansi(str(block.get('content', '')))
                        parts.append(f"[Tool result: {truncate_str(inner)}]")
                    else:
                        parts.append(str(block))
                else:
                    parts.append(str(block))
            return ' '.join(parts)
        text = strip_ansi(str(content))
        # Filter out XML tags that are internal markup
        text = re.sub(r'<[^>]+>', '', text)
        return text.strip() or None

    elif typ == 'assistant':
        content = msg.get('content', [])
        parts = []
        for block in content:
            bt = block.get('type', '')
            if bt == 'text':
                parts.append(strip_ansi(block.get('text', '')))
            elif bt == 'thinking' and include_thinking:
                parts.append(f'[Thinking: {truncate_str(strip_ansi(block.get("thinking", "")), 300)}]')
            elif bt == 'tool_use':
                inp = block.get('input', {})
                inp_str = json.dumps(inp, ensure_ascii=False)
                if len(inp_str) > 120:
                    inp_str = inp_str[:117] + '...'
                parts.append(f'[Tool: {block.get("name", "?")}({inp_str})]')
        return ' '.join(parts) if parts else None

    elif typ == 'ai-title':
        title = entry.get('aiTitle', '')
        return f'=== {title} ===' if title else None

    elif typ == 'system':
        content = entry.get('content', '')
        subtype = entry.get('subtype', '')
        if subtype == 'turn_duration':
            dur = entry.get('durationMs', 0)
            if dur > 0:
                return f'[Turn duration: {dur}ms]'
        if content:
            return f'[System: {truncate_str(strip_ansi(str(content)), 200)}]'
        return None

    elif typ == 'attachment':
        attach = entry.get('attachment', {})
        if attach.get('type') == 'skill_listing':
            return None  # Skip skill listings in display
        return None

    return None

def extract_assistant_blocks(entry: dict) -> list[dict]:
    """Return the individual content blocks from an assistant message."""
    msg = entry.get('message', {})
    content = msg.get('content', [])
    return content

# ---------------------------------------------------------------------------
# Transcript stats helpers
# ---------------------------------------------------------------------------

@dataclass
class TranscriptStats:
    user_messages: int = 0
    assistant_messages: int = 0
    total_lines: int = 0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    title: str = ""

def quick_transcript_stats(path: Path) -> TranscriptStats:
    """Get stats for a transcript file without parsing everything."""
    stats = TranscriptStats()
    seen_usage_ids: set[str] = set()
    try:
        size = os.path.getsize(path)
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                stats.total_lines += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                typ = entry.get('type', '')
                if typ == 'user':
                    stats.user_messages += 1
                elif typ == 'assistant':
                    stats.assistant_messages += 1
                    msg = entry.get('message', {})
                    mid = msg.get('id', '')
                    # Only count unique message.usage per message.id
                    if mid and mid not in seen_usage_ids:
                        seen_usage_ids.add(mid)
                        usage = msg.get('usage', {})
                        stats.input_tokens += usage.get('input_tokens', 0)
                        stats.output_tokens += usage.get('output_tokens', 0)
                        stats.cache_read_tokens += usage.get('cache_read_input_tokens', 0)
                    if not stats.model:
                        stats.model = msg.get('model', '')
                elif typ == 'custom-title':
                    # /rename writes custom-title — always wins over ai-title
                    stats.title = entry.get('customTitle', '')
                elif typ == 'ai-title' and not stats.title:
                    # Use ai-title only if no custom-title has been seen yet
                    stats.title = entry.get('aiTitle', '')
    except (OSError, UnicodeDecodeError):
        pass
    return stats

def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes}B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}K"
    else:
        return f"{num_bytes / (1024 * 1024):.1f}M"

def format_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    elif n < 1_000_000:
        return f"{n/1000:.0f}K"
    else:
        return f"{n/1_000_000:.1f}M"

# ---------------------------------------------------------------------------
# Active session detection
# ---------------------------------------------------------------------------

def get_active_session_ids() -> set[str]:
    """Return set of session IDs that appear to be active (running)."""
    active: set[str] = set()
    sessions_dir = get_sessions_dir()
    if not sessions_dir.exists():
        return active
    for f in sessions_dir.iterdir():
        if f.suffix != '.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            if data.get('status') != 'idle':
                sid = data.get('sessionId')
                if sid:
                    active.add(sid)
        except (json.JSONDecodeError, OSError):
            pass
    return active

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def console_print(*args, **kwargs):
    """Unified print — uses rich Console if available."""
    if RICH_AVAILABLE:
        console = Console()
        console.print(*args, **kwargs)
    else:
        # Strip rich markup for plain print
        text = ' '.join(str(a) for a in args)
        print(strip_ansi(text))

def make_table(title: str, columns: list[str]) -> Any:
    """Create a table — rich.Table if available, else plain list placeholder."""
    if RICH_AVAILABLE:
        tbl = Table(title=title, border_style="dim blue", header_style="bold cyan")
        for col in columns:
            style = "dim" if col in ("Project", "Model", "Size") else None
            tbl.add_column(col, style=style, no_wrap=(col in ("Date", "Size", "Msgs")))
        return tbl
    return {"__plain__": True, "title": title, "columns": columns, "rows": []}

def add_table_row(table: Any, row: list[str]):
    """Add a row to the table."""
    if RICH_AVAILABLE:
        table.add_row(*row)
    else:
        table["rows"].append(row)

def render_table(table: Any):
    """Display the table."""
    if RICH_AVAILABLE:
        console = Console()
        console.print(table)
    else:
        # Plain text table
        cols = table["columns"]
        rows = table["rows"]
        if table["title"]:
            print(f"\n{table['title']}")
            print("-" * len(table["title"]))
        if not rows:
            print("  (no results)")
            return
        # Calculate column widths
        widths = [len(c) for c in cols]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))
        # Print header
        header = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
        print(header)
        print("-" * len(header))
        # Print rows
        for row in rows:
            print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))
        print()

# ---------------------------------------------------------------------------
# Command: list
# ---------------------------------------------------------------------------

def cmd_list(args):
    """List all conversation sessions."""
    index = build_session_index()
    project_map = build_project_session_map()

    # Build {sessionId -> projectName} from project_map
    sid_to_proj: dict[str, str] = {}
    for proj_name, sids in project_map.items():
        for sid in sids:
            sid_to_proj[sid] = proj_name

    # Get active sessions
    active_sids = get_active_session_ids()

    rows = []
    for sid, meta in index.items():
        # Filter by project
        proj_name = sid_to_proj.get(sid, meta.project or "unknown")
        if args.project:
            if args.project.lower() not in proj_name.lower():
                continue

        tpath = find_transcript(sid)
        if not tpath:
            # Session referenced in history but file missing
            rows.append((meta.first_prompt, meta.human_date, proj_name, "—", "—", "—", "—"))
            continue

        size_bytes = os.path.getsize(tpath)
        st = quick_transcript_stats(tpath)

        # Build display values
        title = st.title or meta.first_prompt or "(untitled)"
        title = truncate_str(title.replace('\n', ' '), 60)

        model = st.model or "?"
        # Simplify model names for display
        model = model.replace("deepseek-", "").replace("claude-", "")

        msgs = str(st.user_messages + st.assistant_messages)
        tokens_str = format_tokens(st.input_tokens + st.output_tokens)
        size_str = format_size(size_bytes)

        # Mark active sessions
        if sid in active_sids:
            date_str = f"> {meta.human_date}"
        else:
            date_str = meta.human_date

        rows.append((title, date_str, proj_name, size_str, msgs, model, tokens_str))

    # Sort
    if args.sort == 'size':
        rows.sort(key=lambda r: -_parse_size(r[3]))
    elif args.sort == 'messages':
        rows.sort(key=lambda r: -int(r[4]) if r[4].isdigit() else 0)
    else:  # date
        rows.sort(key=lambda r: r[1].replace('> ', ''), reverse=True)

    # Limit
    if args.n:
        rows = rows[:args.n]

    # Display
    tbl = make_table("Claude Code Conversations", ["Title", "Date", "Project", "Size", "Msgs", "Model", "Tokens"])
    for row in rows:
        add_table_row(tbl, list(row))
    render_table(tbl)
    print(f"\n({len(rows)} sessions shown)")

def _parse_size(s: str) -> int:
    """Parse size string like '12.5K' or '1.2M' to bytes."""
    if s == '—':
        return 0
    try:
        if s.endswith('K'):
            return int(float(s[:-1]) * 1024)
        elif s.endswith('M'):
            return int(float(s[:-1]) * 1024 * 1024)
        elif s.endswith('B'):
            return int(s[:-1])
        return int(s)
    except ValueError:
        return 0

# ---------------------------------------------------------------------------
# Command: view
# ---------------------------------------------------------------------------

def cmd_view(args):
    """Display a conversation in readable format."""
    tpath = find_transcript(args.session_id)
    if not tpath:
        # Try partial match
        matches = _fuzzy_find_sessions(args.session_id)
        if len(matches) == 1:
            tpath = find_transcript(matches[0])
            args.session_id = matches[0]
        elif len(matches) > 1:
            print(f"Multiple sessions match '{args.session_id}':")
            for m in matches:
                print(f"  {m}")
            return
        else:
            print(f"Session '{args.session_id}' not found.")
            return

    index = build_session_index()
    meta = index.get(args.session_id)

    # Build project map
    proj_name = find_project_for_session(args.session_id) or "unknown"

    # Gather stats
    st = quick_transcript_stats(tpath)

    # --- Header ---
    console_print(f"\n[bold cyan]{'='*70}[/]")
    title = st.title or (meta.first_prompt if meta else '(untitled)')
    console_print(f"[bold white]{title}[/]")
    console_print(f"[dim]Session: {args.session_id}[/]")
    console_print(f"[dim]Date:    {meta.human_date if meta else 'unknown'}[/]")
    console_print(f"[dim]Project: {proj_name}[/]")
    console_print(f"[dim]Model:   {st.model}[/]")
    console_print(f"[dim]Size:    {format_size(os.path.getsize(tpath))}[/]")
    console_print(f"[bold cyan]{'='*70}[/]\n")

    # --- Messages ---
    for entry in read_transcript(tpath):
        typ = entry.get('type', '')
        text = extract_text(entry, include_thinking=args.thinking)
        if text is None:
            # Show tool use if --full
            if args.full and typ == 'assistant':
                blocks = extract_assistant_blocks(entry)
                for block in blocks:
                    bt = block.get('type', '')
                    if bt == 'tool_use':
                        name = block.get('name', '?')
                        inp = block.get('input', {})
                        inp_short = json.dumps(inp, ensure_ascii=False)
                        if len(inp_short) > 200:
                            inp_short = inp_short[:197] + '...'
                        console_print(f"  [yellow][Tool: {name}][/] [dim]({inp_short})[/]")
            continue

        if typ == 'user':
            console_print(f"[bold blue]You:[/] [blue]{text}[/]")
        elif typ == 'assistant':
            console_print(f"[green]{text}[/]")
        elif typ == 'ai-title':
            console_print(f"\n[bold magenta]{text}[/]")
        elif typ == 'system':
            console_print(f"[dim italic]{text}[/]")
        else:
            if args.verbose:
                console_print(f"[dim]({typ}) {truncate_str(text, 100)}[/]")

    console_print(f"\n[dim]--- end of transcript ---[/]")

def _fuzzy_find_sessions(partial: str) -> list[str]:
    """Find session IDs that start with or contain the given partial string."""
    candidates: set[str] = set()
    # Check project dirs
    projects_dir = get_projects_dir()
    if projects_dir.exists():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.iterdir():
                if f.suffix == '.jsonl' and not f.name.startswith('agent-'):
                    candidates.add(f.stem)
    # Check history
    index = build_session_index()
    candidates.update(index.keys())

    # Exact match
    if partial in candidates:
        return [partial]
    # Starts with
    starts = [s for s in candidates if s.startswith(partial)]
    if starts:
        return starts
    # Contains
    contains = [s for s in candidates if partial in s]
    return contains

# ---------------------------------------------------------------------------
# Command: search
# ---------------------------------------------------------------------------

def cmd_search(args):
    """Search conversations for a keyword."""
    keyword = args.keyword if args.case_sensitive else args.keyword.lower()
    index = build_session_index()
    results: list[tuple[str, SessionMeta, list[str], int]] = []  # (sid, meta, sample_hits, total_hits)

    for sid, meta in index.items():
        if args.project:
            proj = find_project_for_session(sid) or meta.project
            if args.project.lower() not in proj.lower():
                continue

        tpath = find_transcript(sid)
        if not tpath:
            continue

        hits: list[str] = []
        for entry in read_transcript(tpath):
            if entry.get('type') not in ('user', 'assistant', 'ai-title'):
                continue
            text = extract_text(entry, include_thinking=False)
            if not text:
                continue
            search_text = text if args.case_sensitive else text.lower()
            if keyword in search_text:
                # Create context snippet
                idx = search_text.find(keyword)
                start = max(0, idx - args.context)
                end = min(len(text), idx + len(keyword) + args.context)
                snippet = text[start:end]
                if start > 0:
                    snippet = '...' + snippet
                if end < len(text):
                    snippet = snippet + '...'
                hits.append(snippet)

        if hits:
            results.append((sid, meta, hits[:args.max_hits], len(hits)))

    # Display results
    if not results:
        print(f"No matches found for '{args.keyword}'.")
        return

    if RICH_AVAILABLE:
        console_print(f"\nSearch results for [bold yellow]{args.keyword}[/]:\n")
    else:
        print(f"\nSearch results for '{args.keyword}':\n")

    for sid, meta, samples, total in results:
        title = truncate_str(meta.first_prompt.replace('\n', ' '), 70)
        marker = f"({total} hits)"
        console_print(f"[bold cyan]{sid[:8]}...[/] {marker} [dim]{title}[/]")
        for sample in samples:
            # Highlight the keyword using rich markup
            if RICH_AVAILABLE:
                # Simple highlight: wrap keyword occurrences in bold yellow
                kw = args.keyword
                lower_sample = sample.lower()
                lower_kw = kw.lower()
                parts = []
                pos = 0
                while True:
                    idx = lower_sample.find(lower_kw, pos)
                    if idx == -1:
                        parts.append(sample[pos:])
                        break
                    parts.append(sample[pos:idx])
                    parts.append(f"[bold yellow]{sample[idx:idx+len(kw)]}[/]")
                    pos = idx + len(kw)
                highlighted = ''.join(parts)
            else:
                highlighted = sample
            console_print(f"     {highlighted}")
        if total > len(samples):
            console_print(f"     [dim]... and {total - len(samples)} more[/]")
        print()

# ---------------------------------------------------------------------------
# Command: delete
# ---------------------------------------------------------------------------

def cmd_delete(args):
    """Delete conversation transcript files."""
    active_sids = get_active_session_ids()

    for sid in args.session_id:
        tpath = find_transcript(sid)
        if not tpath:
            # Try partial match
            matches = _fuzzy_find_sessions(sid)
            if len(matches) == 1:
                tpath = find_transcript(matches[0])
                sid = matches[0]
            else:
                print(f"Session '{sid}': not found (skipping)")
                continue

        # Warn if active
        if sid in active_sids:
            console_print(f"[bold red]!! WARNING:[/] Session [cyan]{sid[:8]}...[/] appears to be [bold]currently active[/]!")

        if args.dry_run:
            console_print(f"[dim][DRY RUN] Would delete: {tpath}[/]")
            # Also check for associated subdirectory
            assoc_dir = tpath.with_suffix('')
            if assoc_dir.is_dir():
                console_print(f"[dim][DRY RUN] Would also delete: {assoc_dir}[/]")
            continue

        if not args.force:
            meta = build_session_index().get(sid)
            title = truncate_str(meta.first_prompt, 50) if meta else "(unknown)"
            size = format_size(os.path.getsize(tpath))
            try:
                resp = input(f"Delete [cyan]{sid[:8]}...[/] '{title}' ({size})? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                return
            if resp.lower() != 'y':
                print("  Skipped.")
                continue

        # Delete the transcript file
        tpath.unlink()
        print(f"  Deleted: {tpath}")

        # Also delete associated subdirectory (contains subagent transcripts)
        assoc_dir = tpath.with_suffix('')
        if assoc_dir.is_dir():
            shutil.rmtree(assoc_dir)
            print(f"  Deleted directory: {assoc_dir}")

        # Prune history.jsonl if requested
        if args.prune_history:
            _prune_history_entries([sid])

# ---------------------------------------------------------------------------
# Command: stats
# ---------------------------------------------------------------------------

def cmd_stats(args):
    """Show usage statistics."""
    cache_path = get_stats_cache_path()
    if cache_path.exists():
        _display_cached_stats(cache_path, args.days)
    else:
        _display_computed_stats(args.days)

def _display_cached_stats(cache_path: Path, days: Optional[int]):
    """Display stats from the pre-computed stats-cache.json."""
    try:
        data = json.loads(cache_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading stats cache: {e}")
        return

    console_print(f"\n[bold cyan]==== Claude Code Usage Statistics ====[/]\n")

    # Summary
    console_print(f"[bold]Total Sessions:[/]  {data.get('totalSessions', 0)}")
    console_print(f"[bold]Total Messages:[/]  {data.get('totalMessages', 0)}")

    # Model usage
    model_usage = data.get('modelUsage', {})
    if model_usage:
        console_print(f"\n[bold]Model Usage:[/]")
        if RICH_AVAILABLE:
            tbl = Table(border_style="dim")
            tbl.add_column("Model", style="cyan")
            tbl.add_column("Input", justify="right")
            tbl.add_column("Output", justify="right")
            tbl.add_column("Cache Read", justify="right")
            for model, usage in model_usage.items():
                tbl.add_row(
                    model,
                    format_tokens(usage.get('inputTokens', 0)),
                    format_tokens(usage.get('outputTokens', 0)),
                    format_tokens(usage.get('cacheReadInputTokens', 0)),
                )
            console_print(tbl)
        else:
            print(f"  {'Model':<25} {'Input':>8} {'Output':>8} {'Cache Read':>10}")
            for model, usage in model_usage.items():
                print(f"  {model:<25} {format_tokens(usage.get('inputTokens',0)):>8} "
                      f"{format_tokens(usage.get('outputTokens',0)):>8} "
                      f"{format_tokens(usage.get('cacheReadInputTokens',0)):>10}")

    # Daily activity
    daily = data.get('dailyActivity', [])
    if daily:
        # Filter by days
        if days:
            daily = daily[-days:]
        console_print(f"\n[bold]Daily Activity:[/]")
        if RICH_AVAILABLE:
            tbl = Table(border_style="dim")
            tbl.add_column("Date", style="cyan")
            tbl.add_column("Sessions", justify="right")
            tbl.add_column("Messages", justify="right")
            tbl.add_column("Tool Calls", justify="right")
            for d in daily:
                tbl.add_row(
                    d.get('date', '?'),
                    str(d.get('sessionCount', 0)),
                    str(d.get('messageCount', 0)),
                    str(d.get('toolCallCount', 0)),
                )
            console_print(tbl)
        else:
            print(f"  {'Date':<12} {'Sessions':>8} {'Messages':>8} {'Tools':>8}")
            for d in daily:
                print(f"  {d.get('date','?'):<12} {d.get('sessionCount',0):>8} "
                      f"{d.get('messageCount',0):>8} {d.get('toolCallCount',0):>8}")

    # Longest session
    longest = data.get('longestSession', {})
    if longest:
        dur_sec = longest.get('duration', 0) / 1_000_000  # microseconds → seconds
        console_print(f"\n[bold]Longest Session:[/] {longest.get('sessionId','')[:8]}... "
                      f"— {longest.get('messageCount',0)} msgs, {dur_sec/60:.0f} min")

    # First session
    first = data.get('firstSessionDate', '')
    if first:
        console_print(f"[bold]First Session:[/]   {first[:10]}")

    # Hourly heatmap (simple text)
    hour_counts = data.get('hourCounts', {})
    if hour_counts:
        console_print(f"\n[bold]Activity by Hour (UTC):[/]")
        max_count = max(int(v) for v in hour_counts.values()) if hour_counts else 1
        for h in range(24):
            count = int(hour_counts.get(str(h), 0))
            bar_len = int(count / max_count * 30) if max_count > 0 else 0
            bar = '#' * bar_len + '.' * (30 - bar_len)
            print(f"  {h:02d}:00 {bar} {count}")

    console_print("")

def _display_computed_stats(days: Optional[int]):
    """Compute and display stats by scanning all transcripts (fallback)."""
    console_print(f"\n[bold cyan]==== Claude Code Usage Statistics (computed) ====[/]\n")

    total_sessions = 0
    total_user = 0
    total_assistant = 0
    total_input = 0
    total_output = 0
    total_cache = 0
    model_counts: dict[str, int] = {}

    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        print("No transcripts found.")
        return

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.iterdir():
            if f.suffix != '.jsonl' or f.name.startswith('agent-'):
                continue
            total_sessions += 1
            st = quick_transcript_stats(f)
            total_user += st.user_messages
            total_assistant += st.assistant_messages
            total_input += st.input_tokens
            total_output += st.output_tokens
            total_cache += st.cache_read_tokens
            if st.model:
                model_counts[st.model] = model_counts.get(st.model, 0) + 1

    console_print(f"[bold]Total Sessions:[/]     {total_sessions}")
    console_print(f"[bold]User Messages:[/]      {total_user}")
    console_print(f"[bold]Assistant Messages:[/] {total_assistant}")
    console_print(f"[bold]Input Tokens:[/]       {format_tokens(total_input)}")
    console_print(f"[bold]Output Tokens:[/]      {format_tokens(total_output)}")
    console_print(f"[bold]Cache Read:[/]         {format_tokens(total_cache)}")

    if model_counts:
        console_print(f"\n[bold]Models Used:[/]")
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            print(f"  {model}: {count} sessions")
    console_print("")

# ---------------------------------------------------------------------------
# Command: export
# ---------------------------------------------------------------------------

def cmd_export(args):
    """Export a conversation to Markdown."""
    tpath = find_transcript(args.session_id)
    if not tpath:
        matches = _fuzzy_find_sessions(args.session_id)
        if len(matches) == 1:
            tpath = find_transcript(matches[0])
            args.session_id = matches[0]
        else:
            print(f"Session '{args.session_id}' not found.")
            return

    index = build_session_index()
    meta = index.get(args.session_id)
    st = quick_transcript_stats(tpath)
    proj = find_project_for_session(args.session_id) or "unknown"

    lines: list[str] = []
    title = st.title or (meta.first_prompt if meta else "Claude Code Session")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- **Session:** `{args.session_id}`")
    lines.append(f"- **Date:** {meta.human_date if meta else 'unknown'}")
    lines.append(f"- **Project:** `{proj}`")
    lines.append(f"- **Model:** {st.model}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for entry in read_transcript(tpath):
        typ = entry.get('type', '')

        if typ == 'ai-title':
            lines.append(f"## {entry.get('aiTitle', '')}")
            lines.append("")
            continue

        text = extract_text(entry, include_thinking=args.thinking)
        if text is None and not (args.thinking and typ == 'assistant'):
            continue

        if typ == 'user':
            # Clean up XML markup from text
            clean = re.sub(r'<[^>]+>', '', text or '')
            lines.append(f"**🧑 User:** {clean}")
            lines.append("")
        elif typ == 'assistant':
            if text:
                lines.append(f"**🤖 Claude:** {text}")
                lines.append("")
            if args.thinking:
                blocks = extract_assistant_blocks(entry)
                for block in blocks:
                    bt = block.get('type', '')
                    if bt == 'thinking':
                        lines.append("<details>")
                        lines.append("<summary>💭 Thinking</summary>")
                        lines.append("")
                        lines.append(block.get('thinking', ''))
                        lines.append("")
                        lines.append("</details>")
                        lines.append("")
                    elif bt == 'tool_use':
                        lines.append("<details>")
                        lines.append(f"<summary>🔧 Tool: {block.get('name', '?')}</summary>")
                        lines.append("")
                        lines.append("```json")
                        lines.append(json.dumps(block.get('input', {}), ensure_ascii=False, indent=2))
                        lines.append("```")
                        lines.append("")
                        lines.append("</details>")
                        lines.append("")
        elif typ == 'system':
            if text:
                lines.append(f"*{text}*")
                lines.append("")

    output_path = args.output or (tpath.with_suffix('.md').name)
    content = '\n'.join(lines)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Exported to: {output_path} ({len(content)} bytes)")

# ---------------------------------------------------------------------------
# History pruning helper
# ---------------------------------------------------------------------------

def _prune_history_entries(session_ids: list[str]):
    """Remove entries from history.jsonl matching given session IDs (atomic write)."""
    hist_path = get_history_path()
    if not hist_path.exists():
        return
    sids = set(session_ids)
    try:
        lines = hist_path.read_text(encoding='utf-8').splitlines(keepends=True)
        tmp_path = hist_path.with_suffix('.tmp')
        removed = 0
        with open(tmp_path, 'w', encoding='utf-8') as f:
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    f.write(line)
                    continue
                try:
                    entry = json.loads(stripped)
                    if entry.get('sessionId') in sids:
                        removed += 1
                        continue
                except json.JSONDecodeError:
                    pass
                f.write(line)
        os.replace(tmp_path, hist_path)  # atomic on Windows & POSIX
        if removed:
            print(f"  Pruned {removed} entries from history.jsonl")
    except OSError as e:
        print(f"  Warning: could not prune history: {e}")

# ---------------------------------------------------------------------------
# JSON output helper
# ---------------------------------------------------------------------------

def output_json(data: Any):
    """Print data as JSON to stdout."""
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Chat History Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python chat-manager.py list
              python chat-manager.py list -p materials
              python chat-manager.py view abc12345-...
              python chat-manager.py search "error"
              python chat-manager.py stats
              python chat-manager.py delete abc12345-... --force
              python chat-manager.py export abc12345-... -o session.md
        """),
    )
    parser.add_argument('--json', action='store_true', help='Machine-readable JSON output')

    sub = parser.add_subparsers(dest='command', help='Available commands')

    # --- list ---
    p_list = sub.add_parser('list', help='List all conversation sessions')
    p_list.add_argument('-p', '--project', help='Filter by project name (substring match)')
    p_list.add_argument('-n', type=int, help='Limit number of results')
    p_list.add_argument('--sort', choices=['date', 'size', 'messages'], default='date', help='Sort order')

    # --- view ---
    p_view = sub.add_parser('view', help='View a conversation')
    p_view.add_argument('session_id', help='Session ID (UUID, or first few chars)')
    p_view.add_argument('--full', action='store_true', help='Show tool use blocks')
    p_view.add_argument('--thinking', action='store_true', help='Show thinking blocks')
    p_view.add_argument('--verbose', action='store_true', help='Show all entry types')

    # --- search ---
    p_search = sub.add_parser('search', help='Search conversations for a keyword')
    p_search.add_argument('keyword', help='Search keyword')
    p_search.add_argument('--context', '-c', type=int, default=60, help='Characters of context around match')
    p_search.add_argument('--case-sensitive', '-s', action='store_true', help='Case-sensitive search')
    p_search.add_argument('--max-hits', type=int, default=3, help='Max hit samples per session')
    p_search.add_argument('-p', '--project', help='Filter by project name')

    # --- delete ---
    p_delete = sub.add_parser('delete', help='Delete conversation transcript(s)')
    p_delete.add_argument('session_id', nargs='+', help='Session ID(s) to delete')
    p_delete.add_argument('--force', '-f', action='store_true', help='Delete without confirmation')
    p_delete.add_argument('--dry-run', action='store_true', help='Show what would be deleted without deleting')
    p_delete.add_argument('--prune-history', action='store_true', help='Also remove entries from history.jsonl')

    # --- stats ---
    p_stats = sub.add_parser('stats', help='Show usage statistics')
    p_stats.add_argument('--days', type=int, help='Limit daily stats to last N days')

    # --- export ---
    p_export = sub.add_parser('export', help='Export a conversation to Markdown')
    p_export.add_argument('session_id', help='Session ID to export')
    p_export.add_argument('--output', '-o', help='Output file path (default: <session-id>.md)')
    p_export.add_argument('--thinking', action='store_true', help='Include thinking blocks')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Dispatch
    if args.command == 'list':
        cmd_list(args)
    elif args.command == 'view':
        cmd_view(args)
    elif args.command == 'search':
        cmd_search(args)
    elif args.command == 'delete':
        cmd_delete(args)
    elif args.command == 'stats':
        cmd_stats(args)
    elif args.command == 'export':
        cmd_export(args)

if __name__ == '__main__':
    main()

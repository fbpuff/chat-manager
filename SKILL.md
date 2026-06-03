---
name: chat-manager
description: Launch the Claude Code Chat Manager web UI to browse, search, rename, and delete conversation sessions.
---

# Chat Manager

Launch the local chat manager web app for browsing Claude Code conversation sessions.

## Instructions

When the user invokes this skill:

1. Kill any existing process on port 9720:
   ```
   Get-Process -Id (Get-NetTCPConnection -LocalPort 9720 -ErrorAction SilentlyContinue).OwningProcess -ErrorAction SilentlyContinue | Stop-Process -Force
   ```

2. Launch the server in the background:
   ```
   Start-Process -WindowStyle Hidden -FilePath "pythonw.exe" -ArgumentList "$env:USERPROFILE\.claude\chat-manager\chat-manager-web.py"
   ```

3. Wait 1 second, then open the browser:
   ```
   Start-Process "http://127.0.0.1:9720"
   ```

4. Tell the user: "Chat Manager 已启动 → http://127.0.0.1:9720"

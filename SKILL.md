---
name: chat-manager
description: Launch the Claude Code Chat Manager web UI to browse, search, rename, and delete conversation sessions.
---

# Chat Manager

Launch the local chat manager web app for browsing Claude Code conversation sessions.

## Instructions

When the user invokes this skill:

1. Ensure the server is running and open it (only ONE browser instance will open):
   ```powershell
   if (Get-NetTCPConnection -LocalPort 9720 -ErrorAction SilentlyContinue) { Start-Process "http://127.0.0.1:9720" } else { Start-Process -WindowStyle Hidden -FilePath "pythonw.exe" -ArgumentList "C:\Users\fbpuf\.claude\chat-manager\chat-manager-web.py" }
   ```

2. Tell the user: "Chat Manager 已启动 → http://127.0.0.1:9720"

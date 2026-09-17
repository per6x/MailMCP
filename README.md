# Apple Mail MCP

A minimal local MCP server with one tool: `save_email_draft`. It composes an
email in Apple Mail, adds local file attachments, and saves the message as a
draft. It never sends mail.

## Requirements

- macOS with Mail configured
- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)

## Run

```sh
uv run server.py
```

Configure an MCP host to launch the server over stdio. For example:

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/MailMCP",
        "run",
        "server.py"
      ]
    }
  }
}
```

The first tool call may cause macOS to ask whether the Python process may
control Mail. Allow it under **System Settings → Privacy & Security →
Automation**. Attachment paths must point to existing files; `~` is supported.


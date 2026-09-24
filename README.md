# Apple Mail MCP

Local MCP for Apple Mail on macOS. Search and cached reads use local storage;
changes use Apple Events. No UI automation.

## Install

Requires macOS, a configured Mail account, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and your chosen client.

Choose one command below. Run it from the folder where you want to keep MailMCP.

### Codex

```sh
git clone https://github.com/per6x/MailMCP.git && cd MailMCP && uv sync --locked && codex mcp add mail -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

### [Claude Code](https://code.claude.com/docs/en/mcp)

```sh
git clone https://github.com/per6x/MailMCP.git && cd MailMCP && uv sync --locked && claude mcp add --scope user --transport stdio mail -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

Already cloned? Run only the `codex mcp add ...` or `claude mcp add ...` part from
the MailMCP folder after `uv sync --locked`. Restart your client after adding it.

### Claude Desktop, Cursor, and other MCP clients

```sh
git clone https://github.com/per6x/MailMCP.git && cd MailMCP && uv sync --locked
```

Merge this into your client's MCP configuration, replacing both paths:

```json
{
  "mcpServers": {
    "mail": {
      "type": "stdio",
      "command": "/absolute/path/to/MailMCP/.venv/bin/python",
      "args": ["/absolute/path/to/MailMCP/server.py"]
    }
  }
}
```

[Claude Desktop](https://modelcontextprotocol.io/docs/develop/connect-local-servers): **Settings → Developer → Edit Config**.
[Cursor](https://cursor.com/docs/mcp): `~/.cursor/mcp.json`. Restart the client after saving.

### macOS permissions

Allow **Automation → Mail** when prompted. Local storage reads may also need
**Full Disk Access** for the app running the server.

## Use

Ask your client to search mail, read a message, save a draft with attachments,
or send an email. Sending requires an explicit instruction; reuse the same
`request_id` when retrying a send.

[Capabilities and current limitations](docs/implementation-status.md).

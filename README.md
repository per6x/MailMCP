# Apple Mail MCP

Local MCP for Apple Mail on macOS. Search and cached reads use local storage;
changes use Apple Events. No UI automation.

## Install

Requires macOS, a configured Mail account, and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/per6x/MailMCP.git
cd MailMCP
uv sync --locked
```

Add to your MCP client's configuration (replace the path):

```json
{
  "mcpServers": {
    "mail": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/MailMCP", "run", "server.py"]
    }
  }
}
```

Allow **Automation → Mail** when prompted. Local storage reads may also need
**Full Disk Access** for the app running the server.

## Use

Ask your client to search mail, read a message, save a draft with attachments,
or send an email. Sending requires an explicit instruction; reuse the same
`request_id` when retrying a send.

[Capabilities and current limitations](docs/implementation-status.md).

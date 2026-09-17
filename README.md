# Apple Mail MCP

A minimal local MCP server for Apple Mail. It never sends mail.

- `save_email_draft`: compose and save a draft with local attachments.
- `list_email_drafts`: list saved drafts and their IDs.
- `read_email_draft`: read a draft's body, recipients, and attachment names.
- `edit_email_draft`: change subject, body, recipients, or attachments.

Editing preserves omitted fields; empty lists clear recipients or attachments.
Close the compose window before editing. Mail's saved messages are read-only
through scripting, so editing saves a replacement before deleting the original.
List drafts again to obtain the replacement's new ID. Edits use plain text and
do not preserve rich-text formatting. Omitted bodies use Mail's readable text.
If an edit fails, check Drafts before retrying: a replacement may already exist.

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

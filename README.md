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

Draft creation and editing verify each attachment's filename, size, and SHA-256
hash against the decoded bytes in Mail's saved MIME source. Files are added one
at a time, with bounded polling and re-saving while Mail loads them, and checked
again after the compose window closes. Duplicate filenames are supported.
Saving and verification share a 60-second deadline; a delay alone never counts
as success. Native Mail previews may still display PDFs inline.

Mail uses different IDs for compose windows and saved messages. During creation,
a temporary `MailMCP pending <UUID>` marker in both subject and body identifies
the initial save; subsequent checks use its Message-ID. The requested subject
and body replace the marker before attachments are added. If interrupted early,
the marked draft may remain. Errors identify the draft when available; inspect
it before retrying. Failed verification never deletes an original being edited.
Exported files remain available until replacement verification finishes.

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

## Tests

```sh
uv run python -m unittest -v
```

The regression suite models delayed Mail saves at the script boundary and checks
the real MIME verifier. It does not open or control Mail. Live validation requires
disposable drafts with two different PDFs: create, close/reopen, edit while
preserving attachments, replace them, and clear them. Compare decoded bytes after
each save and after exported temporary files have been removed. Never send test
drafts.

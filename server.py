from pathlib import Path
import subprocess
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


mcp = MCPServer("Apple Mail")


MAIL_SCRIPT = r'''
on run argv
    set argumentIndex to 1

    set messageSubject to item argumentIndex of argv
    set argumentIndex to argumentIndex + 1
    set messageBody to item argumentIndex of argv
    set argumentIndex to argumentIndex + 1

    set toAddresses to {}
    set toCount to (item argumentIndex of argv) as integer
    set argumentIndex to argumentIndex + 1
    repeat toCount times
        set end of toAddresses to item argumentIndex of argv
        set argumentIndex to argumentIndex + 1
    end repeat

    set ccAddresses to {}
    set ccCount to (item argumentIndex of argv) as integer
    set argumentIndex to argumentIndex + 1
    repeat ccCount times
        set end of ccAddresses to item argumentIndex of argv
        set argumentIndex to argumentIndex + 1
    end repeat

    set bccAddresses to {}
    set bccCount to (item argumentIndex of argv) as integer
    set argumentIndex to argumentIndex + 1
    repeat bccCount times
        set end of bccAddresses to item argumentIndex of argv
        set argumentIndex to argumentIndex + 1
    end repeat

    set attachmentPaths to {}
    set attachmentCount to (item argumentIndex of argv) as integer
    set argumentIndex to argumentIndex + 1
    repeat attachmentCount times
        set end of attachmentPaths to item argumentIndex of argv
        set argumentIndex to argumentIndex + 1
    end repeat

    tell application "Mail"
        set draftMessage to make new outgoing message with properties {subject:messageSubject, content:messageBody, visible:false}

        tell draftMessage
            repeat with recipientAddress in toAddresses
                make new to recipient at end of to recipients with properties {address:(contents of recipientAddress)}
            end repeat
            repeat with recipientAddress in ccAddresses
                make new cc recipient at end of cc recipients with properties {address:(contents of recipientAddress)}
            end repeat
            repeat with recipientAddress in bccAddresses
                make new bcc recipient at end of bcc recipients with properties {address:(contents of recipientAddress)}
            end repeat
            repeat with attachmentPath in attachmentPaths
                set attachmentFile to POSIX file (contents of attachmentPath)
                make new attachment with properties {file name:attachmentFile} at after last paragraph
            end repeat
            save
        end tell
    end tell

    return "Draft saved in Apple Mail."
end run
'''


def _append_group(arguments: list[str], values: list[str]) -> None:
    arguments.append(str(len(values)))
    arguments.extend(values)


@mcp.tool()
def save_email_draft(
    to: list[str],
    subject: str,
    body: str,
    attachments: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> str:
    """Compose an email with optional Cc/Bcc recipients and save it as an Apple Mail draft.

    Attachment paths may be absolute or start with ``~``. The draft is never sent.
    """
    if sys.platform != "darwin":
        raise ToolError("Apple Mail is only available on macOS.")

    resolved_attachments: list[str] = []
    for attachment in attachments:
        path = Path(attachment).expanduser().resolve()
        if not path.is_file():
            raise ToolError(f"Attachment is not a file: {attachment}")
        resolved_attachments.append(str(path))

    arguments = [subject, body]
    _append_group(arguments, to)
    _append_group(arguments, cc or [])
    _append_group(arguments, bcc or [])
    _append_group(arguments, resolved_attachments)

    try:
        result = subprocess.run(
            ["osascript", "-", *arguments],
            input=MAIL_SCRIPT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError(
            "Apple Mail did not respond. Check the macOS Automation permission prompt."
        ) from error

    if result.returncode != 0:
        message = result.stderr.strip() or "Apple Mail could not save the draft."
        raise ToolError(message)

    return result.stdout.strip() or "Draft saved in Apple Mail."


if __name__ == "__main__":
    mcp.run()


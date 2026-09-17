from pathlib import Path
import json
import subprocess
import sys
import tempfile

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

    set messageSender to item argumentIndex of argv
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
        set draftMessage to make new outgoing message with properties {subject:messageSubject, visible:true}

        tell draftMessage
            if messageSender is not "" then set sender to messageSender
            set content to messageBody
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
                tell content
                    make new attachment with properties {file name:attachmentFile} at after last paragraph
                end tell
            end repeat
            save
            close saving yes
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
    sender: str = "",
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

    arguments = [subject, body, sender]
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


DRAFT_SCRIPT = r'''
function run(argv) {
    const mail = Application("Mail");
    const drafts = mail.draftsMailbox.messages;
    if (argv[0] === "list") {
        return JSON.stringify(drafts().slice(0, Number(argv[1])).map(m => ({
            id: m.id(), subject: m.subject(), sender: m.sender()
        })));
    }
    const matches = drafts.whose({id: Number(argv[1])})();
    if (matches.length !== 1) throw new Error("Draft not found; list drafts again.");
    const draft = matches[0];
    if (argv[0] === "read") return JSON.stringify({
        id: draft.id(), subject: draft.subject(), sender: draft.sender(),
        body: draft.content(),
        to: draft.toRecipients().map(r => r.address()),
        cc: draft.ccRecipients().map(r => r.address()),
        bcc: draft.bccRecipients().map(r => r.address()),
        attachments: draft.mailAttachments().map(a => ({name: a.name()}))
    });
    if (argv[0] === "export") {
        const paths = draft.mailAttachments().map((a, index) => {
            if (!a.downloaded()) throw new Error("Attachment is not downloaded; open it in Mail first.");
            const name = a.name().split("/").pop();
            const path = argv[2] + "/" + index + "/" + name;
            mail.save(a, {in: Path(path)});
            return path;
        });
        return JSON.stringify(paths);
    }
    if (argv[0] === "delete") {
        mail.delete(draft);
        return JSON.stringify({deleted: true});
    }
    throw new Error("Unknown draft operation");
}
'''


def _draft_operation(operation: str, value: int, *extra: str) -> dict | list:
    if sys.platform != "darwin":
        raise ToolError("Apple Mail is only available on macOS.")
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", operation, str(value), *extra],
            input=DRAFT_SCRIPT, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError("Mail did not respond. Check Mail before retrying.") from error
    if result.returncode:
        raise ToolError(result.stderr.strip())
    return json.loads(result.stdout)


@mcp.tool()
def list_email_drafts(limit: int = 50) -> list[dict]:
    """List saved Apple Mail drafts with IDs, subjects, and senders (maximum 500)."""
    if not 1 <= limit <= 500:
        raise ToolError("limit must be between 1 and 500.")
    return _draft_operation("list", limit)


@mcp.tool()
def read_email_draft(draft_id: int) -> dict:
    """Read a saved draft's text, recipients, sender, and attachment names.

    Obtain draft_id from list_email_drafts. Attachment contents are not returned.
    """
    return _draft_operation("read", draft_id)


@mcp.tool()
def edit_email_draft(
    draft_id: int,
    subject: str | None = None,
    body: str | None = None,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
) -> str:
    """Replace a saved draft with an edited plain-text draft; never send it.

    Omitted fields are preserved. Empty lists clear recipients or attachments.
    attachments replaces the complete attachment list with local file paths.
    Close the draft's compose window first. The replacement has a new ID:
    call list_email_drafts afterward. Rich-text formatting is not preserved;
    Mail's readable text is used when body is omitted.
    """
    if all(value is None for value in (subject, body, to, cc, bcc, attachments)):
        raise ToolError("Provide at least one field to change.")
    message = read_email_draft(draft_id)
    if body is None:
        body = message["body"].replace("\ufffc", "")

    with tempfile.TemporaryDirectory(prefix="mailmcp-") as directory:
        if attachments is None:
            for index, attachment in enumerate(message["attachments"]):
                folder = Path(directory) / str(index)
                folder.mkdir()
                if Path(attachment["name"]).name in ("", ".", ".."):
                    raise ToolError("Cannot preserve attachment filename; supply attachments explicitly.")
            attachments = _draft_operation("export", draft_id, directory)
        save_email_draft(
            to=message["to"] if to is None else to,
            subject=message["subject"] if subject is None else subject,
            body=body, attachments=attachments,
            cc=message["cc"] if cc is None else cc,
            bcc=message["bcc"] if bcc is None else bcc,
            sender=message["sender"],
        )
    try:
        _draft_operation("delete", draft_id)
    except ToolError as error:
        raise ToolError(
            "Replacement draft saved, but the original could not be removed. "
            "Both may exist; list drafts before retrying. " + str(error)
        ) from error
    return "Edited draft saved. Use list_email_drafts to get its new ID."


if __name__ == "__main__":
    mcp.run()

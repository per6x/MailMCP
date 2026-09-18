from pathlib import Path
from collections import Counter
from email import policy
from email.parser import Parser
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

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
        -- A private marker bridges Mail's unrelated compose and saved-message IDs.
        set draftMarker to item argumentIndex of argv
        set draftMessage to make new outgoing message with properties {subject:draftMarker, visible:true}

        tell draftMessage
            if messageSender is not "" then set sender to messageSender
            set content to draftMarker
            repeat with recipientAddress in toAddresses
                make new to recipient at end of to recipients with properties {address:(contents of recipientAddress)}
            end repeat
            repeat with recipientAddress in ccAddresses
                make new cc recipient at end of cc recipients with properties {address:(contents of recipientAddress)}
            end repeat
            repeat with recipientAddress in bccAddresses
                make new bcc recipient at end of bcc recipients with properties {address:(contents of recipientAddress)}
            end repeat
            save
            return id
        end tell
    end tell

end run
'''


COMPOSE_SCRIPT = r'''
on run argv
    set operation to item 1 of argv
    set composeID to (item 2 of argv) as integer
    tell application "Mail"
        set draftMessage to outgoing message id composeID
        tell draftMessage
            if operation is "prepare" then
                set subject to item 3 of argv
                set content to item 4 of argv
            else if operation is "attach" then
                set attachmentFile to (POSIX file (item 3 of argv)) as alias
                tell content
                    make new attachment with properties {file name:attachmentFile} at after last paragraph
                end tell
            else if operation is "close" then
                close saving yes
                return ""
            else if operation is not "save" then
                error "Unknown compose operation"
            end if
            save
        end tell
    end tell
    return ""
end run
'''


SAVE_TIMEOUT = 60.0
POLL_INTERVAL = 0.25


def _run_script(script: str, arguments: list[str], deadline: float,
                *, javascript: bool = False) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError("Attachment verification timed out.")
    command = ["osascript"] + (["-l", "JavaScript"] if javascript else [])
    try:
        result = subprocess.run(
            [*command, "-", *arguments], input=script, capture_output=True,
            text=True, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError("Mail did not respond before the save deadline.") from error
    if result.returncode:
        raise ToolError(result.stderr.strip() or "Apple Mail operation failed.")
    return result.stdout.strip()


def _fingerprint(name: str, data: bytes) -> tuple[str, int, str]:
    return unicodedata.normalize("NFC", name), len(data), hashlib.sha256(data).hexdigest()


def _mime_attachments(source: str) -> Counter:
    message = Parser(policy=policy.default).parsestr(source)
    return Counter(
        _fingerprint(part.get_filename() or "", part.get_payload(decode=True) or b"")
        for part in message.walk()
        if part.get_filename() is not None or part.get_content_disposition() == "attachment"
    )


def _wait_for_saved(expected: Counter, deadline: float, *, marker: str = "",
                    message_id: str = "", subject: str | None = None,
                    compose_id: str = "") -> dict:
    detail = "Saved draft is not yet available."
    while time.monotonic() < deadline:
        snapshot = json.loads(_run_script(
            DRAFT_SCRIPT, ["snapshot", marker, message_id], deadline, javascript=True,
        ))
        if snapshot:
            source = snapshot["source"]
            actual = _mime_attachments(source)
            saved_subject = str(Parser(policy=policy.default).parsestr(source)["Subject"] or "")
            if source and actual == expected and (subject is None or saved_subject == subject):
                return snapshot
            missing = list((expected - actual).elements())
            unexpected = list((actual - expected).elements())
            detail = (
                f"Draft {snapshot['id']}: missing or mismatched files "
                f"{[item[0] for item in missing]}; unexpected or mismatched files "
                f"{[item[0] for item in unexpected]}."
            )
        if compose_id:
            # Attachment loading can complete after the preceding save. Save
            # the same compose object again; never insert the attachment twice.
            _run_script(COMPOSE_SCRIPT, ["save", compose_id], deadline)
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
    raise ToolError("Attachment verification timed out. " + detail)


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
    fingerprints = []
    for attachment in attachments:
        path = Path(attachment).expanduser().resolve()
        if not path.is_file():
            raise ToolError(f"Attachment is not a file: {attachment}")
        resolved_attachments.append(str(path))
        try:
            fingerprints.append(_fingerprint(path.name, path.read_bytes()))
        except OSError as error:
            raise ToolError(f"Cannot read attachment: {attachment}") from error

    arguments = [subject, body, sender]
    _append_group(arguments, to)
    _append_group(arguments, cc or [])
    _append_group(arguments, bcc or [])
    _append_group(arguments, resolved_attachments)
    marker = "MailMCP pending " + str(uuid.uuid4())
    arguments.append(marker)
    deadline = time.monotonic() + SAVE_TIMEOUT
    identity = marker
    try:
        compose_id = _run_script(MAIL_SCRIPT, arguments, deadline)
        initial = _wait_for_saved(Counter(), deadline, marker=marker)
        message_id = initial["message_id"]
        identity = f"draft {initial['id']} (Message-ID {message_id})"
        _run_script(COMPOSE_SCRIPT, ["prepare", compose_id, subject, body], deadline)
        expected = Counter()
        for path, fingerprint in zip(resolved_attachments, fingerprints):
            _run_script(COMPOSE_SCRIPT, ["attach", compose_id, path], deadline)
            expected[fingerprint] += 1
            _wait_for_saved(expected, deadline, message_id=message_id, subject=subject,
                            compose_id=compose_id)
        # Empty attachment lists still need a persisted-draft check.
        _wait_for_saved(expected, deadline, message_id=message_id, subject=subject,
                        compose_id=compose_id)
        _run_script(COMPOSE_SCRIPT, ["close", compose_id], deadline)
        _wait_for_saved(expected, deadline, message_id=message_id, subject=subject)
    except ToolError as error:
        raise ToolError(
            f"Could not verify {identity}. A partial draft may exist; check Drafts "
            "before retrying. Any original draft has been retained. " + str(error)
        ) from error
    return "Draft saved in Apple Mail."


DRAFT_SCRIPT = r'''
function run(argv) {
    const mail = Application("Mail");
    const drafts = mail.draftsMailbox.messages;
    if (argv[0] === "snapshot") {
        const marker = argv[1], messageID = argv[2];
        // Bootstrap with a unique marker in BOTH subject and body. Thereafter
        // use the RFC Message-ID; Mail can change its numeric ID on each save.
        const matches = messageID
            ? drafts.whose({messageId: messageID})()
            : drafts.whose({subject: marker})().filter(d => d.content().trim() === marker);
        if (!matches.length) return "null";
        if (matches.length !== 1) throw new Error("Saved draft identity is ambiguous; check Drafts.");
        const d = matches[0], id = d.messageId();
        if (!id) return "null";
        return JSON.stringify({id: d.id(), message_id: id, subject: d.subject(), source: d.source()});
    }
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

# Apple Mail MCP implementation and verification

Objective: full Apple Mail functionality through a local MCP, using current
interfaces, without clicks, keystrokes, Accessibility, or visible compose windows.
SQLite reads and direct Apple Events commands are allowed. Database writes are
not used; Mail owns persistence and account synchronization.

This is a completion checklist, not a claim of completion. The goal stays active
until each implemented feature has evidence appropriate to its side effects.

| Capability | Current state | Required evidence |
| --- | --- | --- |
| Indexed mailbox enumeration, filtered metadata search | Implemented | SQLite fixtures and live read-only queries passed |
| Hidden draft creation/editing with attachment integrity | Implemented | Fixtures and live create/edit with two attachments passed; duplicate PDF names and all recipient groups passed; live preserve/replace/clear passed with 13 hidden-compose checks |
| Accounts and mailbox hierarchy, synchronization | Implemented; read paths verified | Native account and hierarchy reads passed. Check/sync commands still need controlled live verification |
| Full message bodies, headers, raw MIME, attachment listing/export | Implemented and verified | Live readable text/raw MIME, Unicode attachment metadata, byte-exact export; fixture tests cover file races/symlinks |
| Read/unread, flags, junk, copy/move/delete/archive | Implemented; most live paths verified | Set/clear read and flags passed. Copy/move preserved readable body and attachment hashes; destination IDs and delete readback passed. Junk/highlight and provider Archive/Trash behavior still need live checks |
| Mailbox creation/rename/deletion and import | Partial; platform gap confirmed | Top-level local create/rename passed. Nested creation and local mailbox deletion fail with -10000; deletion also fails through equivalent AppleScript. Import wrapper implemented with validation tests; live import and a non-GUI deletion alternative remain pending |
| Native replies, reply-all, forwarding | Implemented and live verified | Saved MIME threading, recipients, readable body, and original attachment hashes passed; no response emails sent |
| Reply/forward sending | Implemented; offline tests passed | Shared durable journal; native preparation verification was interrupted by a Mail hang and remains pending |
| Redirect drafts | Implemented; native probe passed | Resent recipients, body and attachments observed in a hidden draft; complete wrapper workflow still needs live verification |
| Sending existing saved drafts | Pending | Native API accepts outgoing compose objects, not saved-message objects; no additional sending authorized |
| Explicit sending of new messages | Implemented and verified | Durable request journal with restart/crash/timeout and attachment snapshot tests; preparation-only live test; one separately authorized email accepted and received |
| Signatures and mail rules | Implemented and live verified, with limitation | Signature lifecycle and disabled-rule creation, condition/action edits, rename, and deletion passed. Removing individual conditions fails in Mail; shortening condition lists rejected before mutation |
| MCP metadata, schemas, resources/prompts, host integration | Implemented; tests passing | Structured output, effect annotations, capabilities/message resources, review prompt, actual stdio clients in auto and legacy modes; errors validated before Mail is invoked |
| Packaging, setup, capability limitations | Verified for current implemented scope | Locked offline install, 64-test suite, whitespace/compile checks, current README and capabilities resource |
| Full-scope final audit | Pending | Remaining redirect/saved-response send and mailbox platform gaps must be resolved or explicitly bounded |

“Full” means useful coverage of Mail's supported mail operations, not simulation
of app menus. Unsupported platform features (for example private/deprecated HTML
composition fields) must be identified rather than presented as working tools.
Sending test mail is not implied by permission to implement a mail server.

## Current evidence and retained test artifacts

On 2026-09-24: 64 default tests pass, including actual current/legacy MCP stdio clients. The native integration check passed account
reads, body/headers/MIME, attachment listing/export, read/flag updates, preserving
unread state during reads, copy/move body and attachment integrity, and deletion
readback. Saved messages from these checks were removed; none were sent during those checks.

Two empty local mailboxes created during initial testing remain because native
deletion fails. They were reused for subsequent tests, which verified they were
empty again afterward:

- `MailMCP native verification 06dbc2e1-4745-4549-ba48-cdbb22bbe2ed A`
- `MailMCP native verification 06dbc2e1-4745-4549-ba48-cdbb22bbe2ed renamed`

Do not use UI automation to clean these up. Do not claim mailbox deletion is
implemented, or delete Mail's database/files to hide the unsupported operation.
Investigate a supported programmatic alternative as part of completing the goal.

## Authorized outbound verification, 2026-09-24

One email was explicitly authorized by the user to an address they supplied.
The initial preparation stopped **before** calling send because provider sync
retained several saved versions under one draft UUID. The journal records
`failed_before_send`; no send command occurred for that request. The verifier now
selects exactly one version matching the requested state and rejects multiple
matching versions, covered by a regression test.

After that failure was confirmed, a fresh journaled request sent the one authorized
message. Mail returned acceptance, and the same Message-ID was observed in Sent
and the receiving Inbox. Recipient and readable body matched. Both attachment
hashes matched the original input in Sent. The receiving provider normalized text
attachment line endings from LF to CRLF (62 to 64 bytes); their text matched after
normalizing newlines. Sent plain MIME also acquired quote markers while the HTML
and native readable body retained the expected text. This is evidence of delivery
with transport formatting changes, not a byte-identical MIME delivery claim.

The failed preparation's three saved versions were removed. The delivered message
remains in Sent/Inbox. Private recipient/test evidence is kept under ignored
`.verification/`, not in tracked project documentation. No further email sends
are authorized by this test permission.

Preparation-only live checks used a simulated transmission callback and verified
To/Cc/Bcc, leading spaces, Unicode, two PDF fixtures with duplicate names, hidden
compose state, and replay without another draft. Response checks created and
removed unsent native reply, reply-all, and forwarding drafts of the controlled
test message. Signature and rule fixtures were removed after live verification.

The persistent send journal lives outside Mail's data directory. It stores request
fingerprints and outcome metadata and must survive restarts to prevent duplicate
sends. It never edits Mail's database.

Attachment-clear regression: Mail inserts U+2028 around PDF attachments and saves
it as a normal newline when rebuilding plain text. Readable-text verification now
normalizes Unicode line/paragraph separators to newlines. A regression test first
failed and then passed; the complete live create/edit/preserve/replace/clear check
passed afterward, observing hidden compose state 13 times and removing its drafts.

## Additional implementation and current limits

Local cached reads and attachment export now use the index plus .emlx files,
without launching Mail. Identity, path, size, concurrent changes, and partial
MIME are checked. The controlled received message's cached body matched the native
source, with both detached attachments correctly marked absent from inline MIME.
Cache exports share the native export path's atomic no-clobber file writer.

Response sending shares the proven request journal. Offline tests verify that
failed threading/envelope checks cannot reach transmission, retries do not recreate
responses, and attachment bytes are snapshotted. Live preparation was interrupted
by a Mail scripting compose-factory hang; Mail was restarted with user permission
and read-only status recovered. No additional test email was sent. Redirect and
mailbox-import wrappers are not yet fully live verified. The goal is not complete.

Leading-paragraph bug: Mail 16 on the tested macOS version inserts an empty
URL-share paragraph before scripted content. Initial-content, subsequent-content,
plain-format and rich-text character/paragraph changes did not remove it. The
native HTML-content setter, despite its deprecated dictionary description, accepts
escaped body HTML and a CSS rule hiding that generated paragraph. A controlled
hidden draft's native readable content then started at the requested first line.
The body verifier now preserves leading newlines and rejects unrequested blank
lines; it ignores the generated HTML paragraph only when the hiding CSS is present.
Recipient clients may process HTML differently; post-transport rendering of the
workaround has not been verified with another live send.

Attachment insertion was found to rebuild HTML and drop the hiding style. The
composer therefore reapplies the style to saved HTML after attachment insertion,
retaining its attachment objects and Content-IDs, and then verifies body and all
attachment hashes again. The live preparation-only check passed with leading
spaces, Unicode, To/Cc/Bcc, two different PDFs sharing a filename, and replay
without another draft. No additional email was sent for this formatting fix.

The separate saved-and-closed draft check also passed: native readable content
started at the requested first line and both attachments remained present. The
recipient-free fixture was removed afterward.

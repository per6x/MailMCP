# Apple Mail storage and background integration

Researched 2026-09-24. Scope: programmatic mail access without clicks,
keystrokes, Accessibility automation, or visible compose windows. The user
clarified that ordinary programming interfaces, including Apple Events, are
acceptable. No messages were sent or changed for this research.

## Recommendation

Use a read-only adapter for Mail's local index and downloaded message files,
with Mail's declared Apple Events interface for mutations and synchronization.
For drafts, use hidden outgoing messages and verify the saved MIME contents.
This is an implementation recommendation, conditional on proving that the
installed Mail version saves these messages without displaying a window.
The scripting dictionary documents hidden composition, but is not proof of
actual runtime behavior.

Reverse engineering the local format is useful for reads. A fully local writer
remains an experimental project: reconstructing SQLite rows is insufficient
evidence that Mail will preserve the change or synchronize it correctly. No
published Apple API reviewed here describes an external transaction that updates
the index, message files, and remote mailbox as one operation. This is a finding
about the reviewed interfaces, not proof that such an internal mechanism cannot
be reverse engineered.

## Confirmed Apple interfaces

Apple defines Apple Events as interprocess messages carrying commands and data.
AppleScript/JXA can use those interfaces without simulated user interactions.
Apple documents GUI scripting separately as clicks, keystrokes, and interaction
with controls through System Events. [How Mac scripting works](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/HowMacScriptingWorks.html),
[Automating the user interface](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html).

The installed first-party dictionary was inspected on macOS 26.6.2 build 25G83,
Mail 16.0: `/System/Applications/Mail.app/Contents/Resources/Mail.sdef`.

- `outgoing message` declares a writable `visible` Boolean whose default is
  false, and handlers for `save`, `close`, and `send`.
- It exposes subject, sender, content, and recipients. Its Cocoa class is
  `ComposeBackEnd_Scripting`, identifying a potential subject for further local
  inspection, not a stable public framework API.
- Saved `message` subject, content, and raw source are read-only; mailbox,
  read status, flagged status, flag index, and junk status are writable.
- `synchronize` explicitly targets an IMAP account. `check for new mail` is also
  declared. These allow Mail to own its synchronization state.

Consequently, saved-draft editing cannot be assumed to mean changing a saved
message's content property. Hidden replacement drafts need separate persistence,
attachment-integrity, and original-preservation checks. The dictionary alone
does not prove these workflows succeed.

MailKit's public surface consists of content blocking, actions while downloading
messages, compose-session participation, and message security. Its compose
handler receives sessions created by the user and validates recipients/adds
headers/approves delivery. The documented surface does not offer arbitrary
mailbox enumeration or a standalone draft creation API. Therefore MailKit is not
a replacement mail-store backend for this MCP. [MailKit](https://developer.apple.com/documentation/mailkit),
[Compose session handler](https://developer.apple.com/documentation/mailkit/mecomposesessionhandler).

App Intents' Mail schemas describe how an app exposes *its own* email features
to Siri and Apple Intelligence. They should not be mistaken for a documented
client API into Apple Mail's database. [Mail domain](https://developer.apple.com/documentation/appintents/app-schema-domain-mail).

## Local storage investigation and evidence still needed

Apple supports both server mailboxes and On My Mac storage; Drafts location is
configurable for IMAP accounts. Server drafts and local drafts must therefore be
distinguished explicitly. Apple also warns that deleting a mailbox through
Finder may not appear in Mail, directly demonstrating that external filesystem
changes need not produce matching application state. [Mailbox behavior settings](https://support.apple.com/en-ng/guide/mail/cpmlprefacctmbox/mac),
[Creating, deleting, and rebuilding mailboxes](https://support.apple.com/en-ie/guide/mail/mlhlp1021/mac).

For local reads, inspect the actual schema and verify index-to-file mapping,
account/mailbox identity, flags, MIME boundaries, attachment availability, and
missing-cache behavior using fixtures derived from observed structure. Keep
version detection and unsupported-schema errors explicit. A readable local index
cannot establish that every remote message body has been downloaded.

Use SQLite `mode=ro`; do not use `immutable=1` against an actively changing Mail
database, because it disables locking and change detection. For a consistent
snapshot use SQLite's backup API. Copying only a database file while overlooking
its WAL can omit committed changes. A SQLite snapshot also does not make the
separate message-file tree atomic; the adapter must handle missing/changing
files. [SQLite URI options](https://sqlite.org/uri.html),
[Backup API](https://sqlite.org/backup.html),
[Write-ahead logging](https://sqlite.org/wal.html).

Before shipping direct local writes, experimental evidence would have to show:

1. Every affected table, trigger, file, identifier, and change-tracking record.
2. Correct concurrent behavior with Mail running and with pending remote changes.
3. Persistence after Mail restart, account synchronization, and index rebuild.
4. Attachment bytes and draft identity surviving reopen and another client.
5. Recovery from a failure between filesystem and database updates.

These are proposed acceptance criteria, not claims that they have been met.

## If Mail must never run

Use the account's protocol/API for server-backed data, optionally supplemented
by local reads. This can create drafts without launching Apple Mail; their
appearance in Mail depends on its subsequent sync and configured Drafts mailbox.
It cannot update On My Mac-only mailboxes through the remote provider.

- IMAP specifies mailbox management, search/fetch, flags, copy/move, deletion,
  and APPEND, including the `\Draft` flag. Sending requires a separate submission
  protocol. Draft replacement needs capability-aware handling and verification;
  ordinary APPEND alone does not update the existing message. [RFC 9051](https://www.rfc-editor.org/rfc/rfc9051.html).
- Apple publishes iCloud IMAP/SMTP settings and requires an app-specific password
  for this manual client configuration. [iCloud server settings](https://support.apple.com/en-ie/102525).
- Gmail exposes MIME-based draft create/get/update/delete operations; replacing
  the MIME message changes the message ID while retaining the draft container
  ID. [Gmail drafts](https://developers.google.com/workspace/gmail/api/guides/drafts).
- Microsoft Graph v1.0 creates drafts using JSON or MIME and permits attachment
  content; sending is a separate operation. [Graph create message](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0).

Provider integration needs its own authorized credentials and account mapping.
Those integrations are alternatives when background Mail operation cannot meet
the requirement; they are not evidence that direct local DB writes are complete.

## Local verification, 2026-09-24

Live recipient-free test drafts confirmed hidden saving and editing with two
attachments through Apple Events. The check observed `visible == false` after
each compose mutation, verified the body, and compared decoded attachment hashes
after removing the input files. Saved test drafts were deleted afterward; no mail
was sent. Reproduction: `scripts/verify_background_drafts.py --run`.

The initial experiment exposed an existing verifier bug: both the numeric ID and
RFC Message-ID changed on every save. Header comparison showed Mail's
`X-Universally-Unique-Identifier` remained stable across preparing content, adding
an attachment, resaving, and closing the compose object. The implementation now
uses that UUID and fails explicitly if it is absent or malformed. A later
provider-sync test retained stale versions under the same UUID; verification now
requires exactly one saved version matching the requested state, rejecting
multiple matching versions.
This header is an observed storage detail, not a documented compatibility promise.

The read-only adapter also successfully queried the installed V10 index for two
mailboxes, two messages, and one metadata lookup in approximately 8 milliseconds.
This is a local smoke check, not a general search-performance benchmark.

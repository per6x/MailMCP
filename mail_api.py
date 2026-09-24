"""Validated, bounded calls to Mail's native scripting interface (no GUI scripting)."""

from functools import wraps
from email import policy
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata
from threading import RLock
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from mail_files import atomic_export, ExportError


MAIL_LOCK = RLock()
SCRIPT = Path(__file__).with_suffix('.js').read_text()


class MailAPIError(Exception):
    pass


class MailboxRef(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    account_id: str = Field(description="Account ID from list_accounts, or 'local' for On My Mac.", min_length=1)
    path: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1, max_length=30,
        description='Exact path components from list_account_mailboxes. Do not split names containing slashes.')


SystemMailbox = Literal['inbox', 'drafts', 'sent', 'trash', 'junk', 'outbox']
HighlightColor = Literal['none', 'blue', 'gray', 'green', 'orange', 'purple', 'red', 'yellow']


def serialized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with MAIL_LOCK:
            return fn(*args, **kwargs)
    return wrapped


def call(operation: str, **parameters) -> dict:
    if sys.platform != 'darwin':
        raise MailAPIError('Apple Mail is only available on macOS.')
    request = {'operation': operation, **parameters}
    request = {key: value.model_dump() if isinstance(value, BaseModel) else value
               for key, value in request.items()}
    with MAIL_LOCK:
        try:
            result = subprocess.run(['osascript', '-l', 'JavaScript', '-', json.dumps(request)],
                                    input=SCRIPT, text=True, capture_output=True, timeout=60, check=False)
        except subprocess.TimeoutExpired as error:
            raise MailAPIError('Mail timed out. A change may already have applied; inspect its state before retrying.') from error
        except OSError as error:
            raise MailAPIError('Cannot start the macOS Apple Events bridge: ' + str(error)) from error
    if result.returncode:
        detail = result.stderr.strip()
        if '-1743' in detail:
            detail = 'Allow the MCP host to control Mail in System Settings > Privacy & Security > Automation.'
        raise MailAPIError(detail or 'Mail rejected the operation.')
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise MailAPIError('Mail returned an invalid response; inspect state before retrying a change.') from error
    if not isinstance(value, dict):
        raise MailAPIError('Mail returned an unexpected response shape.')
    source = value.pop('_mime_source', None)
    if source:
        _fill_mime_types(value.get('attachments', [value]), source)
    return value


def _fill_mime_types(attachments: list[dict], source: str) -> None:
    """Use saved MIME for broken native type getters; preserve ambiguous cases."""
    types: dict[str, set[str]] = {}
    for part in Parser(policy=policy.default).parsestr(source).walk():
        name = part.get_filename()
        if name is not None:
            key = unicodedata.normalize('NFC', name)
            types.setdefault(key, set()).add(part.get_content_type())
    for attachment in attachments:
        if attachment.get('mime_type'):
            continue
        candidates = types.get(unicodedata.normalize('NFC', attachment['name']), set())
        if len(candidates) == 1:
            attachment['mime_type'] = next(iter(candidates))


def scope(message_id: int, mailbox: MailboxRef | None, system_mailbox: str | None = None) -> dict:
    if isinstance(message_id, bool) or message_id < 1:
        raise MailAPIError('message_id must be a positive integer.')
    if mailbox is not None and system_mailbox is not None:
        raise MailAPIError('Choose mailbox or system_mailbox, not both.')
    return dict(message_id=message_id, mailbox=mailbox, system_mailbox=system_mailbox)


def mailbox_name(name: str) -> None:
    if not name.strip() or any(c in name for c in ('\x00', '\r', '\n', '/')):
        raise MailAPIError('Mailbox name must be nonempty and contain no slash, NUL, or newline.')


def page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 500 or isinstance(offset, bool) or not 0 <= offset <= 1_000_000:
        raise MailAPIError('limit must be 1..500 and offset must be 0..1000000.')


@serialized
def export_attachment(message_id: int, attachment_id: str, destination: str,
                      mailbox: MailboxRef | None, system_mailbox: str | None,
                      overwrite: bool = False) -> dict:
    selector = scope(message_id, mailbox, system_mailbox)
    if not attachment_id:
        raise MailAPIError('attachment_id is required.')
    try:
        return atomic_export(destination,lambda staged: call('export_attachment',**selector,
            attachment_id=attachment_id,path=str(staged)),overwrite)
    except ExportError as error:
        raise MailAPIError(str(error)) from error

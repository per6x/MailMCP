"""Explicit outbound delivery with a durable, fail-closed retry journal.

Mail owns delivery. This module records acceptance only, never claims that a
recipient received the message, and never repeats an uncertain send command.
"""

from contextlib import contextmanager
from email.utils import getaddresses
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from mail_api import serialized


class SendJournal:
    def __init__(self, directory: Path | None = None):
        self.directory = directory

    @contextmanager
    def connection(self, *, create: bool = True):
        directory = self.directory or Path(os.environ.get(
            'APPLE_MAIL_MCP_STATE_DIR', str(Path.home() / 'Library/Application Support/AppleMailMCP')))
        connection = None
        try:
            directory = directory.expanduser().resolve()
            path = directory / 'send-journal.sqlite3'
            if create:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                except FileExistsError:
                    if path.is_symlink() or not path.is_file():
                        raise ToolError('Send journal path is not a regular file.')
                connection = sqlite3.connect(path, timeout=10)
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('CREATE TABLE IF NOT EXISTS sends ('
                                   'request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, '
                                   'status TEXT NOT NULL, result TEXT NOT NULL, updated_at REAL NOT NULL)')
                connection.commit()
            else:
                if not path.is_file() or path.is_symlink():
                    raise ToolError('No send journal exists; request_id is unknown.')
                connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10)
            yield connection
        except (OSError, sqlite3.Error) as error:
            raise ToolError('Cannot access the send journal; no automatic resend is safe: ' + str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def validate_key(request_id: str) -> None:
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
            raise ToolError('request_id must contain 1..128 letters, digits, dots, underscores, colons, or hyphens.')

    def reserve(self, request_id: str, fingerprint: str) -> dict | None:
        self.validate_key(request_id)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint,result FROM sends WHERE request_id=?', (request_id,)).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ToolError('request_id was already used for different email content. It cannot be reused.')
                return json.loads(row[1])
            result = {'request_id': request_id, 'status': 'preparing', 'delivery_confirmed': False}
            db.execute('INSERT INTO sends VALUES (?,?,?,?,?)',
                       (request_id, fingerprint, 'preparing', json.dumps(result), time.time()))
            db.commit()
        return None

    def transition(self, request_id: str, previous: str, result: dict) -> None:
        with self.connection() as db:
            cursor = db.execute('UPDATE sends SET status=?,result=?,updated_at=? WHERE request_id=? AND status=?',
                                (result['status'], json.dumps(result), time.time(), request_id, previous))
            if cursor.rowcount != 1:
                raise ToolError('Send state changed unexpectedly. Inspect get_send_status; do not resend.')
            db.commit()

    def get(self, request_id: str) -> dict:
        self.validate_key(request_id)
        with self.connection(create=False) as db:
            row = db.execute('SELECT result FROM sends WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise ToolError('Unknown send request_id.')
        return json.loads(row[0])


def validate_envelope(to: list[str], cc: list[str], bcc: list[str], subject: str, sender: str) -> None:
    if not (to or cc or bcc):
        raise ToolError('At least one To, Cc, or Bcc recipient is required for sending.')
    for value in [subject, sender, *to, *cc, *bcc]:
        if any(c in value for c in ('\r', '\n', '\x00')):
            raise ToolError('Subjects and addresses cannot contain CR, LF, or NUL.')
    for value in [*to, *cc, *bcc, *([sender] if sender else [])]:
        parsed = getaddresses([value])
        if len(parsed) != 1 or not parsed[0][1] or '@' not in parsed[0][1]:
            raise ToolError('Each recipient/sender must be one explicit email address, optionally with a display name.')
        local, _, domain = parsed[0][1].rpartition('@')
        if not local or not domain or any(c.isspace() for c in domain):
            raise ToolError('Invalid email address.')


@contextmanager
def snapshot_attachments(attachments: list[str] | None):
    """Bind request fingerprints and Mail preparation to the same immutable bytes."""
    with tempfile.TemporaryDirectory(prefix='mailmcp-send-') as directory:
        staged, files = [], []
        try:
            for index, source in enumerate(attachments or []):
                path = Path(source).expanduser().resolve()
                if not path.is_file():
                    raise ToolError(f'Attachment is not a file: {source}')
                destination = Path(directory) / str(index) / path.name
                destination.parent.mkdir()
                shutil.copyfile(path, destination)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                staged.append(str(destination))
                files.append({'name': path.name, 'sha256': digest})
        except OSError as error:
            raise ToolError('Cannot snapshot attachment: ' + str(error)) from error
        yield staged, files


class Outbound:
    def __init__(self, create_draft: Callable, send_compose: Callable, journal: SendJournal | None = None):
        self.create_draft = create_draft
        self.send_compose = send_compose
        self.journal = journal or SendJournal()

    @serialized
    def send(self, request_id: str, to: list[str], subject: str, body: str,
             attachments: list[str] | None = None, cc: list[str] | None = None,
             bcc: list[str] | None = None, sender: str = '') -> dict[str, Any]:
        self.journal.validate_key(request_id)
        cc, bcc = cc or [], bcc or []
        validate_envelope(to, cc, bcc, subject, sender)
        if '\x00' in body:
            raise ToolError('Email body cannot contain NUL.')
        with snapshot_attachments(attachments) as (staged, files):
            request = dict(to=to, cc=cc, bcc=bcc, subject=subject, body=body, sender=sender, attachments=files)
            return self.submit(request_id, request, lambda: self.create_draft(
                to=to, cc=cc, bcc=bcc, subject=subject, body=body,
                sender=sender, attachments=staged, close=False))

    @serialized
    def submit(self, request_id: str, request: dict, prepare: Callable[[], dict]) -> dict[str, Any]:
        """Journal one verified preparation/send; all composition modes share this boundary.

        prepare must return a verified, hidden, still-open outgoing message. The
        callback is never called on replay. No native read/mutation is needed to
        return a prior response, even if its source message has since moved.
        """
        self.journal.validate_key(request_id)
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        previous = self.journal.reserve(request_id, fingerprint)
        if previous is not None:
            return {**previous, 'replayed': True}
        try:
            draft = prepare()
        except Exception as error:
            result = {'request_id': request_id, 'status': 'failed_before_send',
                      'delivery_confirmed': False, 'error': str(error)}
            self.journal.transition(request_id, 'preparing', result)
            raise ToolError('No send was attempted. A partial draft may exist. ' + str(error)) from error
        base = {'request_id': request_id, 'delivery_confirmed': False,
                'draft': {key: value for key, value in draft.items() if key not in ('compose_id', 'sent')}}
        # This commit MUST precede the single irreversible send event. A crash
        # from this point onward leaves an in-flight request that cannot replay.
        inflight = {**base, 'status': 'in_flight'}
        self.journal.transition(request_id, 'preparing', inflight)
        try:
            accepted = self.send_compose(draft)
            result = {**base, 'status': 'accepted_by_mail' if accepted is True else 'unknown'}
        except Exception as error:
            result = {**base, 'status': 'unknown', 'error': str(error)}
        self.journal.transition(request_id, 'in_flight', result)
        return result


def register(mcp, create_draft: Callable, send_compose: Callable) -> Outbound:
    service = Outbound(create_draft, send_compose)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                        idempotent_hint=True, open_world_hint=True))
    def send_email(request_id: str, to: list[str], subject: str, body: str,
                   attachments: list[str] | None = None, cc: list[str] | None = None,
                   bcc: list[str] | None = None, sender: str = '') -> dict[str, Any]:
        """Compose from text and send through Mail, only after the user explicitly asks to send.

        This transmits mail; saving/preparing a draft is NOT authorization to send.
        Use a unique request_id (such as a UUID) per intended message and reuse that
        same ID on retries. The persisted journal prevents repeating a send after
        timeout/crash. Content and attachments are verified in a hidden saved draft
        before sending. accepted_by_mail means accepted, not delivered; unknown or
        in_flight requires inspecting Mail's Outbox/Sent before any new request.
        Never create a new request_id merely to work around an uncertain result.
        """
        return service.send(request_id, to, subject, body, attachments, cc, bcc, sender)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def get_send_status(request_id: str) -> dict[str, Any]:
        """Read the persisted result of a send request. Does not retry or claim recipient delivery."""
        return service.journal.get(request_id)

    return service

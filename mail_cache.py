"""Read downloaded .emlx MIME without launching Mail or modifying its store.

Mail's cache is private and not a transactional snapshot with SQLite. Verify the
indexed identity, reject ambiguous paths and changing files, and report detached
MIME data explicitly instead of treating partial .emlx files as complete mail.
"""
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import os
from pathlib import Path
import plistlib
import re
import stat
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from mail_index import MailIndex, MailIndexError
from mail_mime import readable_bodies
from mail_files import atomic_export, ExportError


def _is_attachment(part):
    return (part.get_filename() is not None or part.get_content_disposition()=='attachment'
            or (not part.is_multipart() and part.get_content_maintype() not in ('text','multipart')))


def _parts(part,path=()):
    if len(path)>100:raise MailIndexError('Cached MIME nesting exceeds the supported limit.')
    yield part,'.'.join(str(n) for n in path) or '1'
    if part.is_multipart() and not _is_attachment(part):
        for i,child in enumerate(part.get_payload(),1):
            yield from _parts(child,(*path,i))


class MailCache:
    def __init__(self, index: MailIndex | None=None):
        self.index=index or MailIndex()

    def _record(self, message_id: int) -> dict:
        metadata=self.index.get(message_id)
        with self.index._connect() as db:
            columns={r['name'] for r in db.execute('PRAGMA table_info(messages)')}
            if 'remote_id' not in columns:
                raise MailIndexError('This index lacks the remote_id needed to verify cached message identity.')
            row=db.execute('SELECT remote_id,mailbox FROM messages WHERE ROWID=?',(message_id,)).fetchone()
        if row is None or row['mailbox']!=metadata['mailbox_id']:
            raise MailIndexError('Message moved during lookup; search again.')
        return {**metadata,'_remote_id':row['remote_id']}

    def _mailbox_path(self, url: str) -> Path:
        root=self.index._path().parent.parent.resolve()
        parsed=urlsplit(url)
        if parsed.scheme not in ('imap','ews','local','pop') or not re.fullmatch(r'[A-Fa-f0-9-]{36}',parsed.netloc):
            raise MailIndexError('Unsupported mailbox URL layout for local cache reads; use read_email.')
        parts=[unquote(part) for part in parsed.path.split('/') if part]
        if not parts or any(p in ('.','..') or '/' in p or '\x00' in p for p in parts):
            raise MailIndexError('Unsupported mailbox path components in the local cache.')
        path=root/parsed.netloc
        for part in parts:path=path/(part+'.mbox')
        resolved=path.resolve()
        if not resolved.is_relative_to(root):
            raise MailIndexError('Mailbox cache path escapes the Mail version directory.')
        if not resolved.is_dir():
            raise MailIndexError('Mailbox is not downloaded or its local layout is unsupported; use read_email.')
        return resolved

    @staticmethod
    def _find(mailbox: Path,message_id: int) -> Path:
        names={f'{message_id}.emlx',f'{message_id}.partial.emlx'}
        candidates=[];visited=0;deadline=time.monotonic()+5
        def failed(error):raise error
        for directory,folders,files in os.walk(mailbox,followlinks=False,onerror=failed):
            visited+=len(folders)+len(files)
            if visited>250_000 or time.monotonic()>deadline:
                raise MailIndexError('Cache lookup exceeded its scan limit; use read_email.')
            # Child mailboxes own distinct messages and must not satisfy this scope.
            folders[:]=[name for name in folders if not name.endswith('.mbox') and not (Path(directory)/name).is_symlink()]
            if Path(directory).name!='Messages':continue
            candidates.extend(Path(directory)/name for name in files if name in names)
            if len(candidates)>1:
                raise MailIndexError('Multiple cached files match this message; wait for Mail synchronization.')
        if not candidates:
            raise MailIndexError('Message body is not cached locally; synchronize/download it in Mail first.')
        return candidates[0]

    @staticmethod
    def _load(path: Path,max_bytes: int) -> tuple[bytes,dict]:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise MailIndexError('Cached message is not a regular file.')
            line=stream.readline(32)
            if not re.fullmatch(rb'[0-9]+[ \t]*\r?\n',line):
                raise MailIndexError('Unsupported emlx length header.')
            length=int(line)
            if not 1<=length<=max_bytes:
                raise MailIndexError('Cached MIME exceeds the configured byte limit or has an invalid length.')
            source=stream.read(length)
            if len(source)!=length:
                raise MailIndexError('Cached message is truncated; wait for synchronization.')
            trailer=stream.read(1_048_577)
            if len(trailer)>1_048_576:
                raise MailIndexError('Unexpectedly large emlx metadata trailer.')
            after=os.fstat(stream.fileno())
        signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if signature(before)!=signature(after) or signature(after)!=signature(path.stat(follow_symlinks=False)):
            raise MailIndexError('Cached message changed while reading; try again after synchronization.')
        try:
            metadata=plistlib.loads(trailer.strip())
        except (ValueError,plistlib.InvalidFileException) as error:
            raise MailIndexError('Unsupported emlx metadata trailer.') from error
        if not isinstance(metadata,dict):raise MailIndexError('Invalid emlx metadata object.')
        return source,metadata

    def read(self,message_id: int,max_body_chars: int=100_000,max_mime_bytes: int=50_000_000) -> dict:
        if isinstance(message_id,bool) or message_id<1:
            raise MailIndexError('message_id must be a positive integer.')
        if not 1<=max_body_chars<=2_000_000 or not 1<=max_mime_bytes<=50_000_000:
            raise MailIndexError('Invalid cached body/MIME limit.')
        try:
            record=self._record(message_id)
            path=self._find(self._mailbox_path(record['mailbox_url']),message_id)
            source,trailer=self._load(path,max_mime_bytes)
            message=BytesParser(policy=policy.default).parsebytes(source)
            if record['_remote_id'] is not None and str(trailer.get('remote-id'))!=str(record['_remote_id']):
                raise MailIndexError('Cached remote identity does not match the index; synchronize and search again.')
            nfc=lambda s:unicodedata.normalize('NFC',str(s or ''))
            if nfc(message.get('Subject'))!=nfc(record['subject']):
                raise MailIndexError('Cached subject does not match the index; synchronize and search again.')
            senders={address.casefold() for _,address in getaddresses(message.get_all('From',[]))}
            if record['sender'] and record['sender'].casefold() not in senders:
                raise MailIndexError('Cached sender does not match the index; synchronize and search again.')
            body=readable_bodies(message)[-1]
            attachments=[];missing_body=False;missing_parts=False
            for part,part_id in _parts(message):
                detached=part.get('X-Apple-Content-Length') is not None
                missing_parts |= detached
                is_attachment=_is_attachment(part)
                if is_attachment:
                    data=part.get_payload(decode=True) if not part.is_multipart() else None
                    cached=not detached and data is not None
                    attachments.append({'index':len(attachments),'name':part.get_filename(),
                        'mime_type':part.get_content_type(),'part_id':part_id,'cached_in_mime':cached,
                        'size':len(data) if cached else None,'sha256':hashlib.sha256(data).hexdigest() if cached else None})
                elif detached:missing_body=True
            # A record can move while SQLite and the filesystem are read separately.
            after=self._record(message_id)
            for key in ('mailbox_id','mailbox_url','_remote_id','subject','sender','date_received'):
                if record[key]!=after[key]:
                    raise MailIndexError('Indexed identity changed during cache read; search again.')
            return {**{k:v for k,v in record.items() if not k.startswith('_')},
                'backend':'mail_cache','message_id':str(message.get('Message-ID','')).strip('<>'),
                'body':body[:max_body_chars],'body_truncated':len(body)>max_body_chars,
                'body_complete':not missing_body,'source_complete':not missing_parts and '.partial.' not in path.name,
                'headers':[{'name':key,'value':str(value)} for key,value in message.items()],
                'cache_mime_sha256':hashlib.sha256(source).hexdigest(),
                'attachments':attachments,'cache_path':str(path),
                'limitations':['Detached attachments may exist in separate Mail cache files; use save_cached_attachment or native save_email_attachment.'] if missing_parts else []}
        except OSError as error:
            raise MailIndexError('Cannot read local Mail cache: '+str(error)) from error
        except (ValueError,LookupError,TypeError) as error:
            raise MailIndexError('Cannot decode cached MIME: '+str(error)) from error


    def export_attachment(self,message_id: int,attachment_index: int,destination: str,
                          overwrite: bool=False,max_attachment_bytes: int=50_000_000) -> dict:
        if isinstance(attachment_index,bool) or attachment_index<0:
            raise MailIndexError('attachment_index must be zero or greater.')
        if not 1<=max_attachment_bytes<=500_000_000:
            raise MailIndexError('max_attachment_bytes must be 1..500000000.')
        def produce(staged):
            record=self.read(message_id)
            if attachment_index>=len(record['attachments']):
                raise MailIndexError('Attachment index not found; read_cached_email again.')
            descriptor=record['attachments'][attachment_index]
            path=Path(record['cache_path'])
            source,_=self._load(path,50_000_000)
            if hashlib.sha256(source).hexdigest()!=record['cache_mime_sha256']:
                raise MailIndexError('Cached MIME changed before attachment export; read it again.')
            message=BytesParser(policy=policy.default).parsebytes(source)
            part=next(p for p,part_id in _parts(message) if part_id==descriptor['part_id'] and _is_attachment(p))
            if descriptor['cached_in_mime']:
                data=part.get_payload(decode=True)
                origin='emlx_mime'
                if len(data)>max_attachment_bytes:raise MailIndexError('Attachment exceeds configured byte limit.')
            else:
                name=descriptor['name']
                if not name or Path(name).name!=name or name in ('.','..') or '\x00' in name:
                    raise MailIndexError('Detached attachment has no safe filename; use the native export tool.')
                folder=path.parent.parent/'Attachments'/str(message_id)/descriptor['part_id']
                attached=folder/name
                if not attached.resolve().is_relative_to(path.parent.parent.resolve()):
                    raise MailIndexError('Detached attachment path escapes its cache directory.')
                fd=os.open(attached,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as stream:
                    before=os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):raise MailIndexError('Detached attachment is not a regular file.')
                    if before.st_size>max_attachment_bytes:raise MailIndexError('Attachment exceeds configured byte limit.')
                    data=stream.read(max_attachment_bytes+1)
                    after=os.fstat(stream.fileno())
                if len(data)>max_attachment_bytes:raise MailIndexError('Attachment exceeds configured byte limit.')
                signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                if signature(before)!=signature(after) or signature(after)!=signature(attached.stat(follow_symlinks=False)):
                    raise MailIndexError('Detached attachment changed while reading; retry after synchronization.')
                origin='detached_cache'
            # Validate the message/part association again before writing a destination.
            current=self.read(message_id)
            if current['cache_mime_sha256']!=record['cache_mime_sha256'] or current['cache_path']!=record['cache_path']:
                raise MailIndexError('Message changed during attachment export; read_cached_email again.')
            staged.write_bytes(data)
            return {**descriptor,'source':origin,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        try:
            return atomic_export(destination,produce,overwrite)
        except (ExportError,OSError) as error:
            raise MailIndexError('Cannot export cached attachment: '+str(error)) from error

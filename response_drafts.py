"""Native reply/forward drafts with verified plain-text quotes and attachments."""
from collections import Counter
from email import policy
from email.parser import Parser
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import uuid

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import mail_api
from mail_api import MailboxRef, SystemMailbox, serialized
from mail_tools import read_email, tool_errors, ID
from outbound import snapshot_attachments, validate_envelope


def _header_ids(value: str) -> list[str]:
    return re.findall(r'<([^>]+)>',value) or value.split()


@tool_errors
@serialized
def create_response(backend, mode: str, message_id: int, body: str,
                    reply_all: bool=False, to: list[str] | None=None, cc: list[str] | None=None,
                    bcc: list[str] | None=None, attachments: list[str] | None=None,
                    mailbox: MailboxRef | None=None, system_mailbox: SystemMailbox | None=None,
                    include_quote: bool=True, *, close: bool=True) -> dict[str,Any]:
    if mode not in ("reply", "forward"):
        raise ToolError("Unknown response mode.")
    if "\x00" in body:
        raise ToolError("Email body cannot contain NUL.")
    selector=mail_api.scope(message_id,mailbox,system_mailbox)
    original=read_email(message_id,mailbox,system_mailbox,include_source=True,max_source_chars=50_000_000)
    if mode=='forward' and any(not a['downloaded'] for a in original['attachments']):
        raise ToolError('Download/synchronize the original attachments before forwarding.')
    # Resolve added file paths before creating the native response.
    extra_files=[]
    for value in attachments or []:
        path=Path(value).expanduser().resolve()
        if not path.is_file(): raise ToolError(f'Attachment is not a file: {value}')
        extra_files.append(str(path))
    marker='MailMCP response pending '+str(uuid.uuid4())
    deadline=time.monotonic()+backend.SAVE_TIMEOUT
    identity=marker
    try:
        response=mail_api.call('compose_response',**selector,mode=mode,reply_all=reply_all,marker=marker)
        compose_id=str(response['compose_id'])
        if response['original_message_id'].strip('<>') != original['message_id'].strip('<>'):
            raise ToolError('Source message identity changed while composing; draft retained.')
        required=backend._mime_attachments(original['source']) if mode=='forward' else None
        initial=backend._wait_for_saved(required,deadline,marker=marker,subject_only=True,
                                         allow_extra_attachments=True,compose_id=compose_id)
        stable_id=backend._draft_uuid(initial['source'])
        identity=f'draft {initial["id"]} (Mail UUID {stable_id})'
        native=backend.read_email_draft(initial['id'])
        native_source=Parser(policy=policy.default).parsestr(initial['source'])
        combined_body=body
        if include_quote and native['body'].strip():
            combined_body += '\n\n'+native['body'].replace('\ufffc','')
        recipient_fields={'to':native['to'] if to is None else to,
                          'cc':native['cc'] if cc is None else cc,
                          'bcc':native['bcc'] if bcc is None else bcc}
        fields={**recipient_fields,'sender':native['sender'],'body':combined_body}
        with tempfile.TemporaryDirectory(prefix='mailmcp-response-') as directory:
            for index,a in enumerate(native['attachments']):
                if Path(a['name']).name in ('','.','..'): raise ToolError('Unsafe attachment filename in native response.')
                (Path(directory)/str(index)).mkdir()
            preserved=backend._draft_operation('export',initial['id'],directory) if native['attachments'] else []
            exported=Counter(backend._fingerprint(Path(p).name,Path(p).read_bytes()) for p in preserved)
            if exported!=backend._mime_attachments(initial['source']):
                raise ToolError('Exported response attachments do not match the saved MIME.')
            # Keep the native reply object so Mail retains its threading headers.
            backend._run_script(backend.COMPOSE_SCRIPT,['prepare',compose_id,response['subject'],combined_body,backend._body_html(combined_body)],deadline)
            mail_api.call('compose_recipients',compose_id=int(compose_id),**recipient_fields)
            backend._wait_for_saved(Counter(),deadline,draft_uuid=stable_id,subject=response['subject'],compose_id=compose_id)
            expected=Counter()
            for path in [*preserved,*extra_files]:
                file=Path(path)
                fingerprint=backend._fingerprint(file.name,file.read_bytes())
                backend._run_script(backend.COMPOSE_SCRIPT,['attach',compose_id,path],deadline)
                expected[fingerprint]+=1
                backend._wait_for_saved(expected,deadline,draft_uuid=stable_id,subject=response['subject'],compose_id=compose_id)
            backend._restore_body_style(expected,deadline,compose_id,stable_id,response['subject'])
            backend._wait_for_saved(expected,deadline,draft_uuid=stable_id,subject=response['subject'],compose_id=compose_id,fields=fields)
            if close:
                backend._run_script(backend.COMPOSE_SCRIPT,['close',compose_id],deadline)
            final=backend._wait_for_saved(expected,deadline,draft_uuid=stable_id,subject=response['subject'],fields=fields)
        headers=Parser(policy=policy.default).parsestr(final['source'])
        if mode=='reply':
            original_rfc=original['message_id'].strip('<>')
            if original_rfc not in _header_ids(str(headers.get('In-Reply-To',''))):
                raise ToolError('Mail did not persist the expected In-Reply-To header; inspect the retained draft.')
            for key in ('In-Reply-To','References'):
                if _header_ids(str(headers.get(key,'')))!=_header_ids(str(native_source.get(key,''))):
                    raise ToolError('Native reply threading changed during editing; draft retained.')
        result = {'id':final['id'],'draft_uuid':stable_id,'message_id':final['message_id'],
                'subject':response['subject'],**recipient_fields,'sender':native['sender'],'sent':False,
                'quoted_text_format':'plain_text','original_message_id':original['message_id']}
        if not close:
            result['compose_id'] = int(compose_id)
        return result
    except (ToolError,mail_api.MailAPIError,OSError) as error:
        raise ToolError(f'Could not verify {identity}. A partial response draft may exist; inspect Drafts before retrying. No send was attempted. '+str(error)) from error


def verify_redirect_headers(backend, original: str, saved: str) -> None:
    """Mail's saved redirect must preserve authorship and original reply routing."""
    old=Parser(policy=policy.default).parsestr(original)
    new=Parser(policy=policy.default).parsestr(saved)
    for key in ('From','To','Cc','Reply-To'):
        if backend._address_set(old.get_all(key,[])) != backend._address_set(new.get_all(key,[])):
            raise ToolError(f'Redirect changed original {key}; draft retained.')
    for key in ('In-Reply-To','References'):
        if _header_ids(str(old.get(key,''))) != _header_ids(str(new.get(key,''))):
            raise ToolError(f'Redirect changed original {key}; draft retained.')
    original_id=str(old.get('Message-ID','')).strip('<>')
    retained=_header_ids(str(new.get('Message-ID','')))+_header_ids(str(new.get('Resent-Message-ID','')))
    if not original_id or original_id not in retained:
        raise ToolError('Redirect no longer identifies the original message; draft retained.')
    if not new.get('Resent-From') or not new.get('Resent-Date'):
        raise ToolError('Mail did not persist redirect metadata; draft retained.')


@tool_errors
@serialized
def create_redirect(backend,message_id: int,to: list[str],cc: list[str] | None=None,
                    bcc: list[str] | None=None,mailbox: MailboxRef | None=None,
                    system_mailbox: SystemMailbox | None=None, *, close: bool=True) -> dict[str,Any]:
    validate_envelope(to,cc or [],bcc or [],'', '')
    selector=mail_api.scope(message_id,mailbox,system_mailbox)
    original=read_email(message_id,mailbox,system_mailbox,include_source=True,
                        max_body_chars=2_000_000,max_source_chars=50_000_000)
    if original['body_truncated']:
        raise ToolError('Original body exceeds verification limit; redirect not created.')
    if any(not a['downloaded'] for a in original['attachments']):
        raise ToolError('Download/synchronize original attachments before redirecting.')
    marker='MailMCP redirect pending '+str(uuid.uuid4())
    identity=marker
    deadline=time.monotonic()+backend.SAVE_TIMEOUT
    try:
        response=mail_api.call('compose_response',**selector,mode='redirect',reply_all=False,marker=marker)
        compose_id=str(response['compose_id'])
        if response['original_message_id'].strip('<>') != original['message_id'].strip('<>'):
            raise ToolError('Original identity changed while composing the redirect.')
        expected=backend._mime_attachments(original['source'])
        initial=backend._wait_for_saved(expected,deadline,marker=marker,subject_only=True,compose_id=compose_id)
        stable_id=backend._draft_uuid(initial['source'])
        identity=f'draft {initial["id"]} (Mail UUID {stable_id})'
        mail_api.call('compose_recipients',compose_id=int(compose_id),to=to,cc=cc or [],bcc=bcc or [])
        backend._run_script(backend.COMPOSE_SCRIPT,['subject',compose_id,original['subject']],deadline)
        fields={'body':original['body'],'to':to,'cc':cc or [],'bcc':bcc or [],'recipient_header_prefix':'Resent-'}
        final=backend._wait_for_saved(expected,deadline,draft_uuid=stable_id,subject=original['subject'],
                                      compose_id=compose_id,fields=fields)
        verify_redirect_headers(backend,original['source'],final['source'])
        if close:
            backend._run_script(backend.COMPOSE_SCRIPT,['close',compose_id],deadline)
            final=backend._wait_for_saved(expected,deadline,draft_uuid=stable_id,subject=original['subject'],fields=fields)
            verify_redirect_headers(backend,original['source'],final['source'])
        headers=Parser(policy=policy.default).parsestr(final['source'])
        sender=str(headers['Resent-From'])
        result={'id':final['id'],'draft_uuid':stable_id,'message_id':final['message_id'],
                'original_message_id':original['message_id'],'subject':original['subject'],
                'to':to,'cc':cc or [],'bcc':bcc or [],'sender':sender,'sent':False}
        if not close:result['compose_id']=int(compose_id)
        return result
    except (ToolError,mail_api.MailAPIError,OSError) as error:
        raise ToolError(f'Could not verify {identity}. A partial redirect may exist. No send was attempted. '+str(error)) from error


@tool_errors
@serialized
def send_response(backend, request_id: str, mode: str, message_id: int, body: str,
                  reply_all: bool=False, to: list[str] | None=None, cc: list[str] | None=None,
                  bcc: list[str] | None=None, attachments: list[str] | None=None,
                  mailbox: MailboxRef | None=None, system_mailbox: SystemMailbox | None=None,
                  include_quote: bool=True) -> dict[str,Any]:
    backend.outbound.journal.validate_key(request_id)
    selector=mail_api.scope(message_id,mailbox,system_mailbox)
    if mode not in ('reply','forward'):
        raise ToolError('Unknown response mode.')
    if '\x00' in body:
        raise ToolError('Email body cannot contain NUL.')
    if mode=='forward':
        validate_envelope(to or [],cc or [],bcc or [],'', '')
    with snapshot_attachments(attachments) as (staged, files):
        request={'kind':mode, 'selector':{**selector,'mailbox':mailbox.model_dump() if mailbox else None},
                 'body':body,'reply_all':reply_all,'to':to,'cc':cc,'bcc':bcc,
                 'include_quote':include_quote,'attachments':files}
        def prepare():
            draft=create_response(backend,mode,message_id,body,reply_all=reply_all,to=to,cc=cc,bcc=bcc,
                attachments=staged,mailbox=mailbox,system_mailbox=system_mailbox,include_quote=include_quote,close=False)
            validate_envelope(draft['to'],draft['cc'],draft['bcc'],draft['subject'],draft['sender'])
            return draft
        return backend.outbound.submit(request_id,request,prepare)


def register(mcp,backend):
    annotation=ToolAnnotations(read_only_hint=False,destructive_hint=False,idempotent_hint=False,open_world_hint=True)

    @mcp.tool(annotations=annotation)
    def create_reply_draft(message_id: ID,body: str,reply_all: bool=False,
                           attachments: list[str] | None=None,mailbox: MailboxRef | None=None,
                           system_mailbox: SystemMailbox | None=None,include_quote: bool=True) -> dict[str,Any]:
        """Create a hidden native reply/reply-all draft; verify recipients, body, attachments, and threading.

        Never sends. Mail selects reply recipients using its account rules; returned
        To/Cc/Bcc permit review. Quoted content is plain text. Extra attachments are
        local paths. Original attachments follow Mail's normal reply behavior.
        """
        return create_response(backend,'reply',message_id,body,reply_all=reply_all,attachments=attachments,
                               mailbox=mailbox,system_mailbox=system_mailbox,include_quote=include_quote)

    @mcp.tool(annotations=annotation)
    def create_forward_draft(message_id: ID,to: list[str],body: str='',cc: list[str] | None=None,
                             bcc: list[str] | None=None,attachments: list[str] | None=None,
                             mailbox: MailboxRef | None=None,system_mailbox: SystemMailbox | None=None,
                             include_quote: bool=True) -> dict[str,Any]:
        """Create a hidden forwarding draft with verified original and additional attachments. Never sends.

        Original attachments must be downloaded. Body prefixes Mail's native quoted
        text (converted to plain text); include_quote=false omits that text. The
        supplied To/Cc/Bcc replace native recipients. Returned draft IDs are saved IDs.
        """
        return create_response(backend,'forward',message_id,body,to=to,cc=cc or [],bcc=bcc or [],
                               attachments=attachments,mailbox=mailbox,system_mailbox=system_mailbox,
                               include_quote=include_quote)

    send_annotation=ToolAnnotations(read_only_hint=False,destructive_hint=True,idempotent_hint=True,open_world_hint=True)

    @mcp.tool(annotations=send_annotation)
    def send_reply_email(request_id: str,message_id: ID,body: str,reply_all: bool=False,
                         attachments: list[str] | None=None,mailbox: MailboxRef | None=None,
                         system_mailbox: SystemMailbox | None=None,include_quote: bool=True) -> dict[str,Any]:
        """Send a native threaded reply/reply-all only on explicit user instruction to send.

        Mail chooses reply recipients. Use create_reply_draft first if review is
        needed. Reuse request_id on retries; unknown/in_flight must be inspected,
        never bypassed with a new ID. Accepted by Mail does not confirm delivery.
        Preparation verifies threading, body, recipients, and attachment bytes.
        """
        return send_response(backend,request_id,'reply',message_id,body,reply_all=reply_all,
            attachments=attachments,mailbox=mailbox,system_mailbox=system_mailbox,include_quote=include_quote)

    @mcp.tool(annotations=send_annotation)
    def send_forward_email(request_id: str,message_id: ID,to: list[str],body: str='',
                           cc: list[str] | None=None,bcc: list[str] | None=None,
                           attachments: list[str] | None=None,mailbox: MailboxRef | None=None,
                           system_mailbox: SystemMailbox | None=None,include_quote: bool=True) -> dict[str,Any]:
        """Send a native forward only on explicit user instruction to send.

        Includes verified original attachments and optional local additions.
        Quotes are plain text. Reuse request_id on retries; uncertain sends are
        never automatically repeated. Accepted by Mail does not confirm delivery.
        """
        return send_response(backend,request_id,'forward',message_id,body,to=to,cc=cc or [],bcc=bcc or [],
            attachments=attachments,mailbox=mailbox,system_mailbox=system_mailbox,include_quote=include_quote)

    @mcp.tool(annotations=annotation)
    def create_redirect_draft(message_id: ID,to: list[str],cc: list[str] | None=None,
                               bcc: list[str] | None=None,mailbox: MailboxRef | None=None,
                               system_mailbox: SystemMailbox | None=None) -> dict[str,Any]:
        """Create a hidden native redirect draft without sending.

        Preserves the original readable body, attachments, authorship, and reply
        routing. New recipients are stored in Resent-To/Cc/Bcc. Original message
        content is not edited. Mail may regenerate MIME and draft Message-IDs.
        """
        return create_redirect(backend,message_id,to,cc,bcc,mailbox,system_mailbox)

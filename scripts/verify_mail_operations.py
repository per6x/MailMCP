"""Opt-in integration check using two EXISTING empty local test mailboxes and an unsent draft.

No messages are sent. Only this run's uniquely named messages are removed.
Pass --mailbox-a and --mailbox-b explicitly. Mail's tested native API cannot
delete mailboxes, so this check reuses dedicated test folders instead of making
new ones. The folders themselves remain after the check.
"""
import argparse
from pathlib import Path
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run',action='store_true')
parser.add_argument('--mailbox-a',required=True)
parser.add_argument('--mailbox-b',required=True)
options=parser.parse_args()
if not options.run:
    parser.error('--run is required to create disposable test objects in Mail')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import hashlib
import json
import tempfile
import uuid
import unicodedata

import mail_tools as tools
from mail_api import MailboxRef
import server

prefix='MailMCP native verification '+str(uuid.uuid4())
created_messages=[]

def report(step):
    print(json.dumps({'passed':step}),flush=True)

try:
    assert tools.list_accounts()['accounts']
    listed=tools.list_account_mailboxes(limit=2)
    assert len(listed['mailboxes']) <= 2
    report('account and native mailbox reads')
    assert options.mailbox_a != options.mailbox_b
    a=MailboxRef(account_id='local',path=[options.mailbox_a])
    b=MailboxRef(account_id='local',path=[options.mailbox_b])
    local=tools.list_account_mailboxes(account_id='local',limit=500)['mailboxes']
    for box in (a,b):
        assert next(m for m in local if m['path']==box.path)['message_count']==0, 'Use empty dedicated test folders'
    with tempfile.TemporaryDirectory(prefix='mailmcp-native-') as directory:
        path=Path(directory)/'attachment å.txt'
        data=b'Disposable attachment, not a real email.\n'
        path.write_bytes(data)
        server.save_email_draft([],prefix,'Body for native API verification.',[str(path)])
        matches=[m for m in server.list_email_drafts(500) if m['subject']==prefix]
        assert len(matches)==1
        original_id=matches[0]['id']
        msg=tools.read_email(original_id,system_mailbox='drafts',include_source=True)
        assert 'Body for native API verification.' in msg['body']
        assert server._mime_attachments(msg['source'])[server._fingerprint(path.name,data)]==1
        assert unicodedata.normalize('NFC',msg['attachments'][0]['name'])==unicodedata.normalize('NFC',path.name)
        assert msg['attachments'][0]['mime_type']=='text/plain'
        attachments=tools.list_email_attachments(original_id,system_mailbox='drafts')['attachments']
        result=tools.save_email_attachment(original_id,attachments[0]['id'],str(Path(directory)/'export.txt'),system_mailbox='drafts')
        assert result['sha256']==hashlib.sha256(data).hexdigest()
        assert Path(result['path']).read_bytes()==data
        report('read body, headers, MIME, attachment list and byte-exact export')
        state=tools.update_email(original_id,system_mailbox='drafts',read=False,flag_index=2)
        assert state['read'] is False and state['flag_index']==2
        readback=tools.read_email(original_id,system_mailbox='drafts',max_body_chars=5)
        assert readback['body_truncated'] and len(readback['body'])==5
        assert readback['read'] is False
        state=tools.update_email(original_id,system_mailbox='drafts',read=True,flag_index=-1)
        assert state['read'] is True and state['flagged'] is False
        report('set/clear flags and read status; read does not mark read')
        copied=tools.copy_email(original_id,a,system_mailbox='drafts')
        assert copied['status']=='verified',copied
        copy_id=copied['id']
        created_messages.append((copy_id,a))
        copy=tools.read_email(copy_id,mailbox=a,include_source=True)
        assert copy['subject']==prefix and 'Body for native API verification.' in copy['body']
        assert server._mime_attachments(copy['source'])==server._mime_attachments(msg['source'])
        moved=tools.move_email(copy_id,b,mailbox=a)
        assert moved['status']=='verified',moved
        created_messages[-1]=(moved['id'],b)
        move=tools.read_email(moved['id'],mailbox=b,include_source=True)
        assert move['subject']==prefix and 'Body for native API verification.' in move['body']
        assert server._mime_attachments(move['source'])==server._mime_attachments(msg['source'])
        report('copy and move with verified destination IDs')
        assert tools.delete_email(moved['id'],mailbox=b)['deleted']
        created_messages.clear()
        # Native folder count verifies the deletion, without following provider Trash.
        native=tools.list_account_mailboxes(account_id='local',limit=500)['mailboxes']
        assert next(m for m in native if m['path']==b.path)['message_count']==0
        report('delete disposable message')
finally:
    # Exact random subject and tracked message IDs only; no user data selected.
    for draft in server.list_email_drafts(500):
        if draft['subject']==prefix:
            server._draft_operation('delete',draft['id'])
    for message_id,mailbox in created_messages:
        tools.delete_email(message_id,mailbox=mailbox)
    print('Disposable saved messages cleaned up; the two test folders remain.',flush=True)

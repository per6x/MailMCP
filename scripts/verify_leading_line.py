"""Verify a closed hidden draft has no added leading line and retains two attachments. Never sends."""
import argparse
from pathlib import Path
import sys
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run',action='store_true')
if not parser.parse_args().run:parser.error('--run is required to create a disposable recipient-free draft')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,tempfile,time,uuid
import server
subject='MailMCP leading-line verification '+str(uuid.uuid4())
body='First line, without a blank paragraph above it.\nSecond line: åäö.'
try:
    with tempfile.TemporaryDirectory() as directory:
        files=[]
        for i in range(2):
            path=Path(directory)/f'fixture-{i}.pdf'
            path.write_bytes(f'%PDF-1.4\n% fixture {i}\n%%EOF\n'.encode());files.append(str(path))
        draft=server.create_email_draft([],subject,body,files)
    saved=server.read_email_draft(draft['id'])
    assert saved['body'].startswith('First line,'),repr(saved['body'])
    assert len(saved['attachments'])==2
    print(json.dumps({'closed_draft_leading_line':'passed','attachments':2,'mail_sent':False}),flush=True)
finally:
    for item in server.list_email_drafts(500):
        if item['subject']==subject:server._draft_operation('delete',item['id'])

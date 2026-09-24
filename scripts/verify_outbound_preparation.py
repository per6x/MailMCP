"""Verify real hidden Mail preparation with the transmission callback replaced.

No send command is executed. The journal is temporary; the prepared draft is
closed and deleted. All recipient addresses use the reserved .invalid domain.
"""
import argparse
from pathlib import Path
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run',action='store_true')
if not parser.parse_args().run: parser.error('--run is required to create the disposable draft')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import json
import tempfile
import time
import uuid
from collections import Counter

import server
from outbound import Outbound,SendJournal

created=[]
subject='MailMCP outbound preparation '+str(uuid.uuid4())
body='  Leading spaces.\nUnicode: åäö.\nSecond paragraph.\n'

def create(**kw):
    result=server._create_email_draft(**kw)
    created.append(result)
    return result

def simulated_transmission(draft):
    visible=server._run_script('function run(a){return String(Application("Mail").outgoingMessages.byId(Number(a[0])).visible());}',
                               [str(draft['compose_id'])],time.monotonic()+10,javascript=True)
    assert visible=='false'
    snapshot=server._wait_for_saved(expected,time.monotonic()+15,draft_uuid=draft['draft_uuid'],subject=subject)
    assert server._fields_match(snapshot['source'],dict(body=body,to=['to@example.invalid'],cc=['cc@example.invalid'],bcc=['bcc@example.invalid']))
    # Deliberately return acceptance WITHOUT invoking Mail's send command.
    return True

try:
    with tempfile.TemporaryDirectory(prefix='mailmcp-outbound-check-') as directory:
        files=[];expected=Counter()
        for i in range(2):
            folder=Path(directory)/str(i);folder.mkdir()
            path=folder/'same.pdf'
            content=('%PDF-1.4\n% test fixture '+str(i)+'\n%%EOF\n').encode()
            path.write_bytes(content);files.append(str(path))
            expected[server._fingerprint(path.name,content)]+=1
        service=Outbound(create,simulated_transmission,SendJournal(Path(directory)/'journal'))
        kwargs=dict(request_id='preparation-only',to=['to@example.invalid'],cc=['cc@example.invalid'],
                    bcc=['bcc@example.invalid'],subject=subject,body=body,attachments=files)
        result=service.send(**kwargs)
        assert result['status']=='accepted_by_mail',result
        assert service.send(**kwargs)['replayed']
        assert len(created)==1
        print(json.dumps({'preparation':'passed','transmission':'simulated_only','mail_sent':False,
                          'body_and_to_cc_bcc':'verified','duplicate_attachment_filenames':'verified','replay':'verified'}),flush=True)
finally:
    for draft in created:
        server._run_script(server.COMPOSE_SCRIPT,['close',str(draft['compose_id'])],time.monotonic()+15)
        snapshot=json.loads(server._run_script(server.DRAFT_SCRIPT,['snapshot','','',draft['draft_uuid'],'','candidates'],time.monotonic()+15,javascript=True))
        for item in (snapshot if isinstance(snapshot,list) else [snapshot] if snapshot else []):
            server._draft_operation('delete',item['id'])
    # A preparation error can precede the structured result; only this exact
    # random subject belongs to the fixture and may be removed here.
    for draft in server.list_email_drafts(500):
        if draft['subject']==subject: server._draft_operation('delete',draft['id'])
    print('Saved preparation-only test draft cleaned up.',flush=True)

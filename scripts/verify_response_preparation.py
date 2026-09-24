"""Opt-in reply/forward send preparation and redirect-draft check. Never transmits mail."""
import argparse
from pathlib import Path
import sys
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run',action='store_true')
parser.add_argument('--message-id',type=int,required=True)
parser.add_argument('--system-mailbox',choices=['inbox','sent','drafts'],required=True)
args=parser.parse_args()
if not args.run:parser.error('--run is required to create disposable drafts')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,tempfile,time
from unittest.mock import patch
import server
from outbound import Outbound,SendJournal
from response_drafts import send_response,create_redirect

identities=set();compose_ids=[];transmissions=[]
original_wait=server._wait_for_saved

def track(*a,**kw):
    result=original_wait(*a,**kw)
    identities.add(server._draft_uuid(result['source']))
    return result

def simulated_send(draft):
    compose_ids.append(str(draft['compose_id']))
    visible=server._run_script('function run(a){return String(Application("Mail").outgoingMessages.byId(Number(a[0])).visible());}',
                               [str(draft['compose_id'])],time.monotonic()+10,javascript=True)
    assert visible=='false','Native response became visible'
    transmissions.append(draft)
    return True

try:
    with tempfile.TemporaryDirectory(prefix='mailmcp-response-verify-') as directory:
        service=Outbound(server._create_email_draft,simulated_send,SendJournal(Path(directory)/'journal'))
        with patch.object(server,'outbound',service),patch.object(server,'_wait_for_saved',side_effect=track):
            for mode,all_recipients in [('reply',False),('reply',True),('forward',False)]:
                key=mode+str(all_recipients)
                params=dict(backend=server,request_id=key,mode=mode,message_id=args.message_id,
                    body='Disposable response preparation. Never transmitted.',reply_all=all_recipients,
                    system_mailbox=args.system_mailbox,to=['nobody@example.invalid'] if mode=='forward' else None)
                count=len(transmissions)
                result=send_response(**params)
                assert result['status']=='accepted_by_mail',result
                assert send_response(**params)['replayed']
                assert len(transmissions)==count+1
                print(json.dumps({'mode':mode,'reply_all':all_recipients,'preparation':'passed','replay':'passed','transmission':'simulated_only'}),flush=True)
            result=create_redirect(server,args.message_id,['to@example.invalid'],cc=['cc@example.invalid'],
                                   bcc=['bcc@example.invalid'],system_mailbox=args.system_mailbox)
            assert result['sent'] is False
            print(json.dumps({'mode':'redirect','draft_verification':'passed'}),flush=True)
finally:
    for compose_id in compose_ids:
        server._run_script(server.COMPOSE_SCRIPT,['close',compose_id],time.monotonic()+30)
    for identity in identities:
        raw=server._run_script(server.DRAFT_SCRIPT,['snapshot','','',identity,'','candidates'],time.monotonic()+60,javascript=True)
        items=json.loads(raw) or []
        if isinstance(items,dict):items=[items]
        for item in items:server._draft_operation('delete',item['id'])
    print(json.dumps({'test_draft_identities_cleaned':len(identities),'mail_sent':False}),flush=True)

"""Opt-in native reply/reply-all/forward verification using an explicit test message. Never sends."""
import argparse
from pathlib import Path
import sys
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run',action='store_true')
parser.add_argument('--message-id',type=int,required=True)
parser.add_argument('--system-mailbox',choices=['inbox','sent','drafts'],required=True)
args=parser.parse_args()
if not args.run: parser.error('--run is required to create disposable drafts')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,time,uuid
from unittest.mock import patch
import server
from response_drafts import create_response
identities=set()
original_wait=server._wait_for_saved

def track(*a,**kw):
    result=original_wait(*a,**kw)
    identities.add(server._draft_uuid(result['source']))
    return result

try:
    with patch.object(server,'_wait_for_saved',side_effect=track):
        for mode,reply_all in [('reply',False),('reply',True),('forward',False)]:
            result=create_response(server,mode,args.message_id,'Disposable response verification '+str(uuid.uuid4()),
                reply_all=reply_all,to=['nobody@example.invalid'] if mode=='forward' else None,
                system_mailbox=args.system_mailbox)
            assert result['sent'] is False
            print(json.dumps({'mode':mode,'reply_all':reply_all,'draft_verified':True}),flush=True)
finally:
    for identity in identities:
        raw=server._run_script(server.DRAFT_SCRIPT,['snapshot','','',identity,'','candidates'],time.monotonic()+60,javascript=True)
        items=json.loads(raw) or []
        if isinstance(items,dict):items=[items]
        for item in items:
            server._draft_operation('delete',item['id'])
    print(json.dumps({'test_draft_identities_cleaned':len(identities),'mail_sent':False}),flush=True)

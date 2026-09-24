"""Opt-in signature and disabled-rule lifecycle checks. No messages are sent."""
import argparse
from pathlib import Path
import sys
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run',action='store_true')
if not parser.parse_args().run: parser.error('--run is required to create disposable Mail settings')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import json
import uuid
import mail_settings as settings

name='MailMCP settings verification '+str(uuid.uuid4())
signature_names=[name,name+' renamed']
rule_names=[name,name+' renamed']
errors=[]
try:
    result=settings.create_signature(name,'First signature åäö')
    assert result['content'].rstrip()=='First signature åäö'
    result=settings.update_signature(name,new_name=name+' renamed',content='Second signature')
    assert result['name']==name+' renamed' and result['content'].rstrip()=='Second signature'
    assert settings.delete_signature(name+' renamed')['deleted']
    print(json.dumps({'signature_lifecycle':'passed'}),flush=True)
    conditions=[settings.RuleCondition(field='subject header',qualifier='equal to value',expression=name)]
    actions=settings.RuleActions(mark_read=True,flag_index=2)
    rule=settings.create_mail_rule(name,conditions,actions,enabled=False)
    assert rule['enabled'] is False
    assert rule['conditions'][0]['expression']==name
    assert rule['actions']['mark_read'] is True and rule['actions']['flag_index']==2
    rule=settings.update_mail_rule(name,new_name=name+' renamed',
            conditions=[settings.RuleCondition(field='from header',expression='nobody@example.invalid')],
            actions=settings.RuleActions(mark_read=False,flag_index=-1),match_all=False)
    assert rule['enabled'] is False and rule['match_all'] is False
    assert rule['conditions'][0]['field']=='from header'
    assert rule['actions']['mark_read'] is False and rule['actions']['flag_index']==-1
    assert settings.read_mail_rule(name+' renamed')['name']==name+' renamed'
    assert settings.delete_mail_rule(name+' renamed')['deleted']
    print(json.dumps({'disabled_rule_lifecycle':'passed','mail_sent':False}),flush=True)
finally:
    for item in settings.list_signatures(limit=500)['signatures']:
        if item['name'] in signature_names:
            try:
                if not settings.delete_signature(item['name'])['deleted']:errors.append('Signature deletion not observed')
            except Exception as error:errors.append(str(error))
    for item in settings.list_mail_rules(limit=500)['rules']:
        if item['name'] in rule_names:
            try:
                if not settings.delete_mail_rule(item['name'])['deleted']:errors.append('Rule deletion not observed')
            except Exception as error:errors.append(str(error))
    if errors: print(json.dumps({'cleanup_errors':errors,'test_name':name}),flush=True)
    else: print('Disposable settings removed.',flush=True)
    assert not errors, 'Settings cleanup incomplete'

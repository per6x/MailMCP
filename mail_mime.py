"""Readable projection of Mail-generated MIME for composition verification.

This parses HTML as data. It does not render web content, load resources, or run
scripts. Mail can save PDF drafts with an empty plain alternative and the actual
text only in HTML; every nonempty body alternative is checked.
"""
from email.message import EmailMessage
from html.parser import HTMLParser
import unicodedata
import re


class _MailText(HTMLParser):
    blocks={'div','p','blockquote','li','tr','section','article','h1','h2','h3','h4','h5','h6','pre'}
    ignored={'head','script','style','object'}

    def __init__(self, hide_share_header=False):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.ignore=0
        self.pre=0
        self.hide_share_header=hide_share_header
        self.hidden_depth=0

    def newline(self):
        if self.parts and not self.parts[-1].endswith('\n'): self.parts.append('\n')

    def handle_starttag(self,tag,attrs):
        if self.hidden_depth:
            if tag not in {'br','img','input','hr','meta','link','source','wbr','embed','area','base','param','col'}:
                self.hidden_depth+=1
            return
        if self.hide_share_header and tag=='div' and 'Apple-Mail-URLShareUserContentTopClass' in dict(attrs).get('class','').split():
            self.hidden_depth=1
            return
        if tag in self.ignored: self.ignore+=1
        if self.ignore: return
        if tag=='pre': self.pre+=1
        if tag in self.blocks: self.newline()
        if tag=='br': self.parts.append('\n')

    def handle_endtag(self,tag):
        if self.hidden_depth:
            self.hidden_depth-=1
            return
        if tag in self.ignored:
            self.ignore=max(0,self.ignore-1)
            return
        if self.ignore: return
        if tag in self.blocks: self.newline()
        if tag=='pre': self.pre=max(0,self.pre-1)

    def handle_data(self,data):
        if self.ignore: return
        if not self.pre and not data.strip() and ('\n' in data or '\r' in data): return
        # Mail represents runs of ordinary input spaces as Apple-converted-space
        # spans containing NBSP; their readable meaning is the original spacing.
        self.parts.append(data.replace('\xa0',' '))


def normalized_text(value: str) -> str:
    return unicodedata.normalize('NFC',value.replace('\r\n','\n').replace('\r','\n').replace('\ufffc','').replace('\u2028','\n').replace('\u2029','\n')).rstrip('\n')


def readable_bodies(message: EmailMessage) -> list[str]:
    bodies=[]
    for kind in ('plain','html'):
        part=message.get_body(preferencelist=(kind,))
        if part is None: continue
        content=part.get_content()
        if kind=='html':
            # Only suppress Mail's generated header when our explicit CSS rule
            # actually hides it. Unstyled leading empty paragraphs remain visible.
            rule='div.Apple-Mail-URLShareUserContentTopClass { display: none !important; }'
            hide=bool(re.search(r'<style\b[^>]*>\s*'+re.escape(rule)+r'\s*</style>',content,re.IGNORECASE))
            parser=_MailText(hide_share_header=hide); parser.feed(content); parser.close()
            content=''.join(parser.parts)
        if normalized_text(content): bodies.append(content)
    return bodies or ['']

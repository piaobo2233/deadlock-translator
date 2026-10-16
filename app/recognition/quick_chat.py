"""Recognize official Chinese canned messages; never blacklist typed English phrases."""
import json
from pathlib import Path
import re


def normalized(value):
    return re.sub(r'[\s，。！？!?…：;；"“”‘’\'、,.]+','',value)


class QuickChat:
    def __init__(self):
        self.exact=set()
        self.templates=[]
        try:
            data=json.loads(Path(__file__).with_name('quick_chat.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            # Generic Chinese verification remains active if an older package
            # lacks the optional catalogue. Never disable the recognition chain.
            return
        names='(?:'+'|'.join(re.escape(normalized(n)) for n in data['names'])+')'
        for row in data['messages']:
            value=normalized(row['chinese'])
            if '{' not in value:
                self.exact.add(value)
            elif set(re.findall(r'\{[^}]+\}',value)) <= {'{s:param_1}'}:
                parts=value.split('{s:param_1}')
                self.templates.append(re.compile(names.join(re.escape(p) for p in parts)))

    def matches(self,text):
        if not re.search(r'[\u3400-\u9fff]',text):
            return False
        value=normalized(text)
        return value in self.exact or any(p.fullmatch(value) for p in self.templates)

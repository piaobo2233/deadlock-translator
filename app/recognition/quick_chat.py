"""Recognize official Chinese canned messages; never blacklist typed English phrases."""
import re
from app.game_data.service import catalogue


def normalized(value):
    return re.sub(r'[\s，。！？!?…：;；"“”‘’\'、,.]+','',value)


class QuickChat:
    def __init__(self, data=None):
        self.exact=set()
        self.templates=[]
        self._prepared = data is None
        try:
            data = catalogue('quick_chat') if data is None else data
            if (not isinstance(data['names'], list) or any(not isinstance(n, str) for n in data['names'])
                    or not isinstance(data['messages'], list)
                    or any(not isinstance(row, dict) or not isinstance(row.get('chinese'), str)
                           for row in data['messages'])):
                raise ValueError('Invalid quick chat catalogue')
        except (OSError,ValueError,KeyError,TypeError):
            # Generic Chinese verification remains active if an older package
            # lacks the optional catalogue. Never disable the recognition chain.
            return
        names='(?:'+'|'.join(re.escape(normalized(n)) for n in data['names'] if normalized(n))+')'
        for row in data['messages']:
            value=normalized(row['chinese'])
            if '{' not in value:
                self.exact.add(value)
            elif data['names'] and set(re.findall(r'\{[^}]+\}',value)) <= {'{s:param_1}'}:
                parts=value.split('{s:param_1}')
                self.templates.append(re.compile(names.join(re.escape(p) for p in parts)))

    def matches(self,text):
        if self._prepared and not catalogue('quick_chat'):
            return False
        if not re.search(r'[\u3400-\u9fff]',text):
            return False
        value=normalized(text)
        return value in self.exact or any(p.fullmatch(value) for p in self.templates)

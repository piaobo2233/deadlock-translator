"""Small official-name dictionary; sentences receive references, never word splicing."""
import re
from app.game_data.service import catalogue

# These names also have ordinary meanings. Let sentence context decide.
AMBIGUOUS=frozenset(('seven','lash','mirage','warden','grey','ivy','inferno','haze','paradox','pocket','dynamo','vindicta'))
def records():
    try:
        rows = catalogue('terms')['terms']
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get('english'), str)
                or not row['english'].strip() or not isinstance(row.get('chinese'), str)
                or not row['chinese'].strip() for row in rows):
            raise ValueError('Invalid game term records')
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        # Optional game data must never prevent importing the built-in dictionary.
        return []


def _clear():
    from app.game_data.service import _activated
    _activated.cache_clear()


records.cache_clear = _clear

def exact_terms():
    return {row['english'].casefold():row['chinese'] for row in records()
            if row['english'].casefold() not in AMBIGUOUS and len(row['english'])>2}

def hint(sentence):
    matches=[row for row in records() if re.search(r'(?<![\w])'+re.escape(row['english'])+r'(?![\w])',sentence,re.I)]
    if not matches:return ''
    matches=sorted(matches,key=lambda row:len(row['english']),reverse=True)[:10]
    return '\n游戏官方术语参考（结合句意使用，普通词或数字用法不要强行替换）：\n'+ '\n'.join(
        row['english']+' = '+row['chinese'] for row in matches)

"""Small official-name dictionary; sentences receive references, never word splicing."""
import json,re
from functools import lru_cache
from pathlib import Path

# These names also have ordinary meanings. Let sentence context decide.
AMBIGUOUS=frozenset(('seven','lash','mirage','warden','grey','ivy','inferno','haze','paradox','pocket','dynamo','vindicta'))
@lru_cache(maxsize=1)
def records():
    return json.loads(Path(__file__).with_name('game_terms.json').read_text(encoding='utf-8'))['terms']

def exact_terms():
    return {row['english'].casefold():row['chinese'] for row in records()
            if row['english'].casefold() not in AMBIGUOUS and len(row['english'])>2}

def hint(sentence):
    matches=[row for row in records() if re.search(r'(?<![\w])'+re.escape(row['english'])+r'(?![\w])',sentence,re.I)]
    if not matches:return ''
    matches=sorted(matches,key=lambda row:len(row['english']),reverse=True)[:10]
    return '\n游戏官方术语参考（结合句意使用，普通词或数字用法不要强行替换）：\n'+ '\n'.join(
        row['english']+' = '+row['chinese'] for row in matches)

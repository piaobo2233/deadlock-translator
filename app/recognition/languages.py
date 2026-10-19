"""Body-only admission for the eight receive languages and mixed messages."""
import re
import unicodedata

RECEIVE_LANGUAGES = ('eng', 'spa', 'por', 'fra', 'deu', 'jpn', 'kor', 'rus')
LATIN_WORDS = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
FOREIGN_COMMON = frozenset(('hola gracias ayuda vamos juntos espera necesito atacar defender '
    'izquierda derecha ahora medio bien equipo por favor sí si no está están dónde qué '
    'olá obrigado obrigada você vocês precisamos juntos espere esquerda direita não sim '
    'bonjour merci salut aide attendez ensemble gauche droite maintenant besoin oui nous vous '
    'hallo danke bitte hilfe zusammen warten links rechts jetzt brauche nicht ja nein wir ihr '
    'el la los las un una de del al con para pero porque donde como estoy estamos puedes puede '
    'ven venir cuidado atacar defiende torre urna tengo tenemos enemigo enemigos '
    'o os as um uma é eu ele ela nós eles elas que do da dos das em com para mas porque '
    'preciso ajuda cuidado atacar defenda torre inimigo inimigos '
    'je tu il elle on ils elles le la les un une des du de au aux avec pas pour mais parce '
    'peux peut suis sommes aller venez attention attaquer défendez tour ennemi ennemis '
    'ich du er sie es die der das ein eine und oder aber mit für auf zu im am ist sind '
    'kann können habe haben heilung vorsicht angreifen verteidigen turm gegner').split())


def contains_han(text):
    return any('\u3400' <= c <= '\u9fff' or '\uf900' <= c <= '\ufaff'
               or '\U00020000' <= c <= '\U000323af' for c in text)


def latin_words(text):
    return [w for w in LATIN_WORDS.findall(text)
            if all(c in "'’" or 'LATIN' in unicodedata.name(c, '') for c in w)]


def native_chinese(text):
    # Han-only Japanese is ambiguous: keep it unchanged without inventing a language.
    return contains_han(text) and not foreign_chat(text)


def script_model(text):
    if re.search(r'[\u3041-\u3096\u30a1-\u30fa]', text):
        return 'jpn'
    if re.search(r'[\uac00-\ud7a3\u3131-\u318e]', text):
        return 'kor'
    if any('CYRILLIC' in unicodedata.name(c, '') for c in text):
        return 'rus'
    return ''


def foreign_chat(text):
    """Complement English admission without an English vocabulary requirement.

    Multiword Latin chat uses morphology; a lone foreign word needs known usage,
    just like English short messages. Geometry/source/three real frames still gate it.
    """
    letters = ''.join(c for c in text if c.isalpha())
    if not letters or (len(letters) > 3 and max(letters.casefold().count(c)
            for c in set(letters.casefold())) / len(letters) > .6):
        return False
    kana = sum('\u3041' <= c <= '\u3096' or '\u30a1' <= c <= '\u30fa' for c in text)
    hangul = sum('\uac00' <= c <= '\ud7a3' or '\u3131' <= c <= '\u318e' for c in text)
    cyrillic = sum('CYRILLIC' in unicodedata.name(c, '') for c in letters)
    if kana >= 2 or (kana and contains_han(text)) or hangul >= 2 or cyrillic >= 2:
        return True
    words = latin_words(text)
    if contains_han(text):
        # A position letter or game/hero name in Chinese is not a foreign sentence.
        meaningful = set('push wait help need come go back attack defend please thanks sorry together'.split())
        return (any(len(w) >= 2 and (w.casefold() in meaningful or w.casefold() in FOREIGN_COMMON) for w in words)
                or (len([w for w in words if len(w) >= 2]) >= 2 and any(w.islower() for w in words)))
    if not words or any(c.isalpha() and 'LATIN' not in unicodedata.name(c, '') for c in text):
        return False
    foreign = any(w.casefold() in FOREIGN_COMMON or any(ord(c) > 127 for c in w) for w in words)
    if not foreign:
        return False
    if len(words) == 1:
        return words[0].casefold() in FOREIGN_COMMON
    letters = sum(len(w) for w in words)
    return letters >= len(text.strip()) * .5 and all(
        len(w) < 4 or max(w.casefold().count(c) for c in set(w.casefold())) / len(w) <= .6
        for w in words)

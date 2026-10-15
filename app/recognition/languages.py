"""Receive-language policy. Han in a chat body always opts out of translation."""
import re
import unicodedata

RECEIVE_LANGUAGES = ('eng', 'spa', 'por', 'fra', 'deu')
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


def foreign_chat(text):
    """Complement English admission without an English vocabulary requirement.

    Multiword Latin chat uses morphology; a lone foreign word needs known usage,
    just like English short messages. Geometry/source/three real frames still gate it.
    """
    if contains_han(text):
        return False
    words = latin_words(text)
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

"""Internal display policy; legacy manual knobs never enter these calculations."""
import math


def lifetime(message):
    # Source validity always ends display first; this only bounds retained history.
    return min(120.,max(30.,20.+len(message.translated)*.18))


def widths(source,screen,font_size,text_length):
    sw,sh=screen
    preferred=min(round(sw*.32),max(font_size*14,round(font_size*math.sqrt(max(1,text_length))*2)))
    minimum=max(80,font_size*4)
    maximum=max(minimum,min(sw-16,font_size*44,round(sw*.45)))
    choices=[preferred,maximum,max(minimum,preferred//2)]
    if source and len(source)>=4:
        choices.extend((sw-source[0]-source[2]-24,source[2],sw-max(4,source[0])-16))
    return tuple(dict.fromkeys(max(minimum,min(maximum,round(v))) for v in choices))

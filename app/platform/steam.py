"""Discover Steam libraries without importing the desktop entry point."""
import os
from pathlib import Path
import re


def steam_libraries():
    libraries = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            libraries.append(os.path.normpath(winreg.QueryValueEx(key, 'SteamPath')[0]))
    except (ImportError, OSError):
        pass
    for base in list(libraries) + [r'C:\Program Files (x86)\Steam']:
        try:
            text = (Path(base) / 'steamapps/libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        libraries.extend(os.path.normpath(match.group(1).replace('\\\\', '\\'))
                         for match in re.finditer(r'"path"\s*"([^"]+)"', text))
    return list(dict.fromkeys(libraries))

"""Private cache generation, source changes and failure preservation using synthetic game data."""
import copy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from app.game_data import catalogues, service, valve
from app.recognition.quick_chat import QuickChat
from app.storage import store


def compiled_message():
    # Independent minimal KV3 fixture: {m_strMessageToken: #fixture_message}.
    strings = b'm_strMessageToken\0#fixture_message\0'
    first = strings + b'\0' * (-len(strings) % 4) + struct.pack('<I', 2)
    second = struct.pack('<iii', 1, 0, 1) + bytes((9, 6)) + b'\x00\xdd\xee\xff'
    def literal(body):
        remaining = len(body) - 15
        return bytes((0xF0,)) + bytes((255,)) * (remaining // 255) + bytes((remaining % 255,)) + body
    a, b = literal(first), literal(second)
    header = bytearray(120)
    for offset, value in {0: 0x4B563305, 20: 1, 28: len(strings), 32: 1, 40: 2,
                          72: len(first), 76: len(a), 80: len(second), 84: len(b),
                          96: 2, 108: 1}.items():
        struct.pack_into('<I', header, offset, value)
    data = bytes(header) + a + b
    return struct.pack('<IHHII', 28 + len(data), 12, 1, 8, 1) + b'DATA' + struct.pack('<II', 8, len(data)) + data


def fake_game(root):
    directory = root / 'steamapps/common/Deadlock/game/citadel'
    directory.mkdir(parents=True)
    (root / 'steamapps/appmanifest_1422450.acf').write_text('"buildid" "123"', encoding='utf-8')
    for group in catalogues.GROUPS:
        folder = directory / 'resource/localization' / group; folder.mkdir(parents=True)
        for language, value in (('english', 'Fixture Beacon'), ('schinese', '测试信标')):
            body = '"fixture_name" "' + value + '"\n'
            if group == 'citadel_main' and language == 'schinese':
                body += '"fixture_message" "测试集合"\n'
            (folder / (group + '_' + language + '.txt')).write_text(body, encoding='utf-8')
    payload = compiled_message()
    tree = b'vdata_c\0scripts\0'
    body = b''
    for relative in catalogues.DEFINITIONS:
        tree += Path(relative).stem.encode() + b'\0'
        tree += struct.pack('<IHHIIH', zlib.crc32(payload), 0, 0, len(body), len(payload), 0xFFFF)
        body += payload
    tree += b'\0\0\0'
    (directory / 'pak01_dir.vpk').write_bytes(struct.pack('<III', 0x55AA1234, 2, len(tree)) + bytes(16) + tree)
    (directory / 'pak01_000.vpk').write_bytes(body)
    return directory


class GameResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = fake_game(self.root / 'installation')
        self.app = self.root / 'assistant'
        service._activated.cache_clear()
        self.addCleanup(service._activated.cache_clear)

    def test_vpk_kv3_roles_and_crc(self):
        bodies = valve.resources(self.game, catalogues.DEFINITIONS)
        self.assertEqual(valve.message_tokens(valve.kv3(next(iter(bodies.values())))), {'fixture_message'})
        archive = self.game / 'pak01_000.vpk'; archive.write_bytes(b'broken' + archive.read_bytes()[6:])
        with self.assertRaisesRegex(ValueError, 'CRC'):
            valve.resources(self.game, catalogues.DEFINITIONS)

    def test_parser_rejects_unknown_encoding_and_malformed_streams(self):
        for payload in (b'', bytes(128), compiled_message()[:-1]):
            with self.assertRaises((ValueError, struct.error, IndexError)):
                valve.kv3(payload)
        with self.assertRaises(ValueError):
            valve.lz4(b'\x00\x00\x00', 10)
        tree = {'m_strCommsWheelLabelToken': '#menu_only', 'nested': [
            {'m_strMessageToken': '#label_but_actual_message'}, {'m_strSelfMessageToken': '#self'}]}
        self.assertEqual(valve.message_tokens(tree), {'label_but_actual_message', 'self'})

    def test_prepares_both_catalogues_and_preserves_personal_data(self):
        personal = Path(store.data_path(self.app, 'glossary.json'))
        personal.parent.mkdir(parents=True); personal.write_bytes(b'personal fixture')
        before = personal.read_bytes()
        result = service.prepare(self.app, self.game)
        self.assertEqual(result['snapshot']['build_id'], '123')
        self.assertEqual(result['quick_chat']['messages'][0]['chinese'], '测试集合')
        self.assertEqual(result['terms']['terms'][0]['chinese'], '测试信标')
        self.assertEqual(personal.read_bytes(), before)
        self.assertIsNotNone(service.inspect(self.app)[0])
        self.assertTrue(QuickChat(result['quick_chat']).matches('测试集合！'))
        self.assertFalse(QuickChat(result['quick_chat']).matches('fixture_message'))

    def test_activation_requires_restart_and_update_disables_old_cache(self):
        self.assertEqual(service.catalogue('terms', self.app), {})
        service.prepare(self.app, self.game)
        self.assertEqual(service.catalogue('terms', self.app), {})
        service._activated.cache_clear()
        self.assertTrue(service.catalogue('terms', self.app))
        path = self.root / 'installation/steamapps/appmanifest_1422450.acf'
        path.write_text('"buildid" "124"', encoding='utf-8')
        self.assertEqual(service.catalogue('terms', self.app), {})
        self.assertIsNone(service.inspect(self.app)[0])
        self.assertTrue(service.cache_path(self.app).is_file())

    def test_archive_or_source_change_invalidates_even_if_build_unchanged(self):
        service.prepare(self.app, self.game)
        self.assertTrue(service.catalogue('quick_chat', self.app))
        archive = self.game / 'pak01_000.vpk'
        archive.write_bytes(archive.read_bytes() + b'changed')
        self.assertEqual(service.catalogue('quick_chat', self.app), {})
        self.assertIsNone(service.inspect(self.app)[0])

    def test_failed_generation_or_save_retains_previous_cache(self):
        service.prepare(self.app, self.game)
        before = service.cache_path(self.app).read_bytes()
        with patch.object(service, 'build', side_effect=ValueError('changed game')):
            with self.assertRaises(ValueError): service.prepare(self.app, self.game)
        with patch.object(service.store, 'atomic_write_json', return_value=False):
            with self.assertRaises(OSError): service.prepare(self.app, self.game)
        self.assertEqual(service.cache_path(self.app).read_bytes(), before)

    def test_changed_snapshot_during_generation_is_not_committed(self):
        before, selected = catalogues.snapshot(self.game)
        after = copy.deepcopy(before); after['build_id'] = '124'
        with patch.object(catalogues, 'snapshot', side_effect=[(before, selected), (after, selected)]):
            with self.assertRaisesRegex(ValueError, '生成期间'):
                service.prepare(self.app, self.game)
        self.assertFalse(service.cache_path(self.app).exists())

    def test_malformed_or_missing_cache_is_optional(self):
        path = service.cache_path(self.app); path.parent.mkdir(parents=True)
        for body in ('{', '[]', '{}', '{"schema_version":1,"generator_version":1}'):
            path.write_text(body, encoding='utf-8')
            service._activated.cache_clear()
            self.assertEqual(service.catalogue('terms', self.app), {})
            self.assertEqual(service.catalogue('quick_chat', self.app), {})
        for data in ({}, {'names': [None], 'messages': []}, {'names': [], 'messages': [None]}):
            self.assertFalse(QuickChat(data).matches('测试集合'))

    def test_no_steam_directory_and_manual_parent_selection(self):
        with patch('app.platform.steam.steam_libraries', return_value=[]):
            self.assertIsNone(service.discover({}))
        self.assertEqual(service.game_directory(self.game.parent.parent), self.game)


if __name__ == '__main__':
    unittest.main()

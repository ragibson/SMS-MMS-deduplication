import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'dedupe_texts.py'
MIB = 1024 * 1024
PROFILE = '''
import resource, runpy, sys
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print('Peak RSS:', rss if sys.platform == 'darwin' else rss * 1024)
'''


class TestStreaming(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.input = Path(scratch.name) / 'backup.xml'
        self.output = self.input.with_name('deduplicated.xml')
        self.log = self.input.with_name('deduplication.log')

    def arguments(self):
        return [str(SCRIPT), '-i', str(self.input), '-o', str(self.output), '-l', str(self.log)]

    def profile(self):
        result = subprocess.run([sys.executable, '-c', PROFILE, *self.arguments()],
                                cwd=SCRIPT.parent, capture_output=True, text=True, check=True)
        rss = int(re.search(r'Peak RSS: (\d+)', result.stdout)[1])
        print(f'{self.id()}: {self.input.stat().st_size / MIB:.1f} MiB input, {rss / MIB:.1f} MiB peak RSS',
              flush=True)
        return rss

    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'requires Unix peak RSS')
    def test_peak_memory_does_not_scale_with_input_size(self):
        peaks = []
        for repeat in (1024, 120_000):
            with self.input.open('wb') as file:
                file.write(b'<smses>')
                for index in range(repeat):
                    # The non-key attributes vary; only one message should be retained.
                    file.write(b'<sms _id="%d" body="%s"/>' % (index, b'x' * 1024))
                file.write(b'</smses>')
            peaks.append(self.profile())
            self.assertIn(b'count="1"', self.output.read_bytes())
            self.assertIn(b'<sms _id="0"', self.output.read_bytes())
            with self.log.open('rb') as log:
                self.assertEqual(sum(line.startswith(b'Removing sms:') for line in log), repeat - 1)
        self.assertLess(peaks[1] - peaks[0], 16 * MIB)

    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'requires Unix peak RSS')
    def test_peak_memory_does_not_scale_with_message_payload_size(self):
        peaks = []
        block = b'YWJjMTIz' * 8192
        endings = (b'YQ==', b'YQ==', b'Yg==')
        for repeat in (1, 1280):
            expected = []
            with self.input.open('wb') as file:
                file.write(b'<smses><mms date="1"><parts><part text="picture"/></parts></mms>')
                for ending in endings:
                    file.write(b'<mms date="1"><parts><part text="picture" data="')
                    digest = hashlib.sha256()
                    for _ in range(repeat):
                        file.write(block)
                        digest.update(block)
                    file.write(ending + b'"/></parts></mms>')
                    digest.update(ending)
                    expected.append(digest.digest())
                file.write(b'</smses>')
            peaks.append(self.profile())
            with self.output.open('rb') as file:
                for expected_hash in (expected[0], expected[2]):
                    opening = file.readline(256)
                    while b'data="' not in opening:
                        self.assertTrue(opening, 'missing attachment')
                        opening = file.readline(256)
                    file.seek(-len(opening.split(b'data="', 1)[1]), os.SEEK_CUR)
                    digest = hashlib.sha256()
                    for _ in range(repeat):
                        digest.update(file.read(len(block)))
                    digest.update(file.read(len(endings[0])))
                    self.assertEqual(digest.digest(), expected_hash)
                    self.assertEqual(file.readline(), b'"/></parts></mms>\n')
            self.assertEqual(self.log.read_bytes().count(b'Removing mms:'), 2)
            self.assertIn(f'<LENGTH {len(block) * repeat + len(endings[0])} OMISSION>'.encode(), self.log.read_bytes())
        self.assertLess(peaks[1] - peaks[0], 16 * MIB)

    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'requires Unix peak RSS')
    def test_peak_memory_does_not_scale_with_parts_per_message(self):
        peaks = []
        for width in (128, 80_000):
            with self.input.open('wb') as file:
                file.write(b'<smses>')
                for _ in range(2):
                    file.write(b'<mms date="1"><parts>')
                    for index in range(width):
                        file.write(b'<part text="%s-%06d"/>' % (b'x' * 64, index))
                    file.write(b'</parts></mms>')
                file.write(b'</smses>')
            peaks.append(self.profile())
            with self.output.open('rb') as file:
                self.assertIn(b'count="1"', file.read(256))
            with self.log.open('rb') as file:
                self.assertEqual(file.readline(), b'Removing mms:\n')
            self.assertGreater(self.output.stat().st_size, width * 64)
        self.assertLess(peaks[1] - peaks[0], 16 * MIB)

    def test_verify_duplicates_uses_exact_properties_when_hashes_collide(self):
        with patch.object(sys, 'path', [str(SCRIPT.parent), *sys.path]):
            import dedupe_texts

        self.input.write_bytes(b'<smses><sms body="one"/><sms body="two"/><sms body="one"/></smses>')
        constant_digest = Mock()
        constant_digest.digest.return_value = bytes(32)
        with patch('message_properties.sha256', return_value=constant_digest):
            with patch.object(sys, 'argv', self.arguments()):
                dedupe_texts.main()
            self.assertIn(b'count="1"', self.output.read_bytes())
            with patch.object(sys, 'argv', [*self.arguments(), '--verify-duplicates']):
                dedupe_texts.main()
            self.assertIn(b'count="2"', self.output.read_bytes())
            self.assertIn(b'<sms body="two"/>', self.output.read_bytes())

    def test_large_encoded_body_matches_small_body_after_whitespace_normalization(self):
        # Put a character reference and multibyte UTF-8 characters across read boundaries.
        body = b' ' * 65_533 + b'&#x1f600;' + b' ' * 65_526 + 'é'.encode() + b'  world'
        self.input.write_bytes(b'<smses><sms body="' + body + b'"/><sms body="&#128512; '
                               + 'é'.encode() + b' world"/></smses>')
        for flags in ([], ['--verify-duplicates']):
            self.output.unlink(missing_ok=True)
            subprocess.run([sys.executable, *self.arguments(), '--ignore-whitespace-differences', *flags], check=True)
            self.assertIn(b'count="1"', self.output.read_bytes())
            self.assertIn(body, self.output.read_bytes())

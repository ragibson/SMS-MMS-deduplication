import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch

import dedupe_texts


class TestStreamingMemory(unittest.TestCase):
    def write_messages(self, filepath, count, body):
        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(f'<smses count="{count}">')
            for _ in range(count):
                file.write(f'<sms date="1" address="+15550000" body="{body}" />')
            file.write('</smses>')

    def run_and_measure(self, filepath):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'output.xml')
            log = os.path.join(directory, 'output.log')
            result = subprocess.run(
                ['/usr/bin/time', '-f', '%M', sys.executable, 'dedupe_texts.py', '-i', filepath,
                 '-o', output, '-l', log],
                check=True, capture_output=True, text=True,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            return int(result.stderr.strip().splitlines()[-1])

    def test_duplicate_count_does_not_increase_peak_memory(self):
        """Repeated input entries retain one compact key, rather than one XML element each."""
        with tempfile.TemporaryDirectory() as directory:
            small = os.path.join(directory, 'small.xml')
            large = os.path.join(directory, 'large.xml')
            self.write_messages(small, 100, 'message')
            self.write_messages(large, 20_000, 'message')
            self.assertLess(self.run_and_measure(large) - self.run_and_measure(small), 12 * 1024)

    def test_repeated_large_payloads_do_not_accumulate_in_memory(self):
        """Large MMS-like values are released per entry, rather than retained per duplicate."""
        with tempfile.TemporaryDirectory() as directory:
            small = os.path.join(directory, 'small.xml')
            large = os.path.join(directory, 'large.xml')
            payload = 'x' * 256_000
            self.write_messages(small, 2, payload)
            self.write_messages(large, 100, payload)
            self.assertLess(self.run_and_measure(large) - self.run_and_measure(small), 16 * 1024)

    def test_verify_duplicates_avoids_hash_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'input.xml')
            self.write_messages(source, 1, 'first')
            # A second independently written document makes collision behavior explicit.
            other = os.path.join(directory, 'other.xml')
            self.write_messages(other, 1, 'second')
            output, log = os.path.join(directory, 'out.xml'), os.path.join(directory, 'out.log')
            default_args = Namespace(default_country_code='+1', ignore_date_milliseconds=False,
                                     ignore_whitespace_differences=False, aggressive=False,
                                     verify_duplicates=False, fix_utf16=False)

            class ConstantDigest:
                def digest(self):
                    return b'collision-summary'

            with patch('dedupe_texts.hashlib.blake2b', return_value=ConstantDigest()):
                _, default_unique = dedupe_texts.stream_deduplicate([source, other], output, log, default_args)
            self.assertEqual(sum(default_unique.values()), 1)

            args = Namespace(default_country_code='+1', ignore_date_milliseconds=False,
                             ignore_whitespace_differences=False, aggressive=False,
                             verify_duplicates=True, fix_utf16=False)
            with patch('dedupe_texts.hashlib.blake2b', side_effect=AssertionError('verification must not hash')):
                counts, unique = dedupe_texts.stream_deduplicate([source, other], output, log, args)
            self.assertEqual(sum(counts.values()), 2)
            self.assertEqual(sum(unique.values()), 2)


if __name__ == '__main__':
    unittest.main()

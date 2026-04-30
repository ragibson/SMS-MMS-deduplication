import os
import subprocess
import sys
import tempfile
import unittest

from shared_testing_functions import clean_up_test_output, TEST_OUTPUT_XML, TEST_LOG_FILE

DEDUPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _measure_memory(input_path, flags=None):
    output_path = os.path.join(os.getcwd(), TEST_OUTPUT_XML)
    log_path = os.path.join(os.getcwd(), TEST_LOG_FILE)
    args = ["-i", input_path, "-o", output_path, "-l", log_path]
    if flags:
        args.extend(flags.split())

    script = f'''
import resource, sys
sys.path.insert(0, {repr(DEDUPE_DIR)})
from dedupe_texts import main
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
main({repr(args)})
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(after - before)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Dedupe subprocess failed:\n{result.stderr}")
    lines = result.stdout.strip().splitlines()
    return int(lines[-1])


class TestMemoryScaling(unittest.TestCase):
    def test_memory_does_not_scale_with_input_size(self):
        unique = 50
        small_duplicates = 100
        large_duplicates = 10000

        def make_xml(duplicates):
            count = unique * (1 + duplicates)
            lines = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>',
                f'<smses count="{count}" type="full">'
            ]
            template = '    <sms protocol="0" address="+11111111111" date="{date}" type="1" subject="null" body="test {i}"/>'
            for i in range(unique):
                lines.append(template.format(date=1600000000000 + i, i=i))
                for _ in range(duplicates):
                    lines.append(template.format(date=1600000000000 + i, i=i))
            lines.append('</smses>')
            return '\n'.join(lines)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(make_xml(small_duplicates))
            small_path = f.name

        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(make_xml(large_duplicates))
            large_path = f.name

        try:
            small_peak = _measure_memory(small_path)
            clean_up_test_output()
            large_peak = _measure_memory(large_path)
            clean_up_test_output()

            # Large input is 100x larger but memory should not scale proportionally.
            self.assertLess(
                large_peak,
                small_peak * 4 + 30000,
                f"Memory scaled with input size: small={small_peak}KB, large={large_peak}KB"
            )
        finally:
            for p in (small_path, large_path, TEST_OUTPUT_XML, TEST_LOG_FILE):
                if os.path.exists(p):
                    os.unlink(p)

    def test_memory_does_not_scale_with_message_size(self):
        unique = 10
        duplicates = 10

        def make_xml(body):
            count = unique * (1 + duplicates)
            lines = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>',
                f'<smses count="{count}" type="full">'
            ]
            safe_body = body.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            template = '    <sms protocol="0" address="+11111111111" date="{date}" type="1" subject="null" body="{body}"/>'
            for i in range(unique):
                lines.append(template.format(date=1600000000000 + i, body=safe_body))
                for _ in range(duplicates):
                    lines.append(template.format(date=1600000000000 + i, body=safe_body))
            lines.append('</smses>')
            return '\n'.join(lines)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(make_xml('test'))
            small_path = f.name

        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(make_xml('A' * 5_000_000))
            large_path = f.name

        try:
            small_peak = _measure_memory(small_path)
            clean_up_test_output()
            large_peak = _measure_memory(large_path)
            clean_up_test_output()

            # Large messages are 5 MB each but bookkeeping memory should stay bounded.
            self.assertLess(
                large_peak,
                small_peak * 5 + 30000,
                f"Memory scaled with message size: small={small_peak}KB, large={large_peak}KB"
            )
        finally:
            for p in (small_path, large_path, TEST_OUTPUT_XML, TEST_LOG_FILE):
                if os.path.exists(p):
                    os.unlink(p)

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

import os
import re
import subprocess
import unittest

from shared_testing_functions import clean_up_test_output

TEST_INPUT_PATH = os.path.join("tests", "test_cases", "large_input_gitignore.xml")
MEM_STATS_FILE = os.path.join("tests", "test_cases", "memory_stats_gitignore.txt")


class TestMemoryUsage(unittest.TestCase):
    def create_larger_input_file(self):
        unique_texts = set(f"{i:100}" for i in range(100_000))

        lines = []
        lines.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>")
        lines.append('<smses count="400000" type="full">')
        for _ in range(4):
            for text in unique_texts:
                lines.append(f'    <sms protocol="0" address="+11111111111" date="1600000000000" type="1" '
                             f'subject="null" body="{text}"/>')
        lines.append('</smses>')

        with open(TEST_INPUT_PATH, 'w', encoding='utf-8') as file:
            file.writelines(lines)
        return os.path.getsize(TEST_INPUT_PATH)  # return final size in bytes

    def check_memory_usage(self):
        # prepare a large input
        input_file_size_bytes = self.create_larger_input_file()

        # using GNU time to capture peak resident set size
        cmd = [
            "/usr/bin/time", "-v", "-o", MEM_STATS_FILE,
            "python3", "dedupe_texts.py",
            "-i", TEST_INPUT_PATH,
            "-o", "test_deduplicated.xml",
            "-l", "test_deduplication.log",
        ]

        subprocess.run(cmd, check=True)

        # parse peak RSS line
        with open(MEM_STATS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        m = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", content)
        peak_kb = int(m.group(1))
        assert peak_kb > 0

        # no more than the input file size plus ~25 MB overhead
        assert peak_kb < input_file_size_bytes / 1024 + 25_000

    def test_memory_usage(self):
        self.create_larger_input_file()
        self.check_memory_usage()

    def tearDown(self):
        clean_up_test_output()
        if os.path.exists(TEST_INPUT_PATH):
            os.unlink(TEST_INPUT_PATH)
        if os.path.exists(MEM_STATS_FILE):
            os.unlink(MEM_STATS_FILE)


if __name__ == '__main__':
    unittest.main()

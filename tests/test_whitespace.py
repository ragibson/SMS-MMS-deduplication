import os
import unittest

from shared_testing_functions import run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output


class TestCrossMessageProtocol(unittest.TestCase):
    def test_whitespace_disabled(self, filename='test_cases/whitespace_duplicates.xml'):
        _ = read_message_count(filename)
        run_deduplication(filename)
        self.assertTrue(not os.path.exists(TEST_OUTPUT_XML))

    def test_whitespace_enabled(self, filename='test_cases/whitespace_duplicates.xml'):
        """
        Check that user can specify deduplication of messages that only differ by whitespace.

        This checks leading/trailing spaces as well as whitespace type (here, CRLF vs. LF and multiple spaces).
        """
        original_count = read_message_count(filename)
        run_deduplication(filename, flags="--ignore-whitespace-differences")
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(original_count // 5, deduplicated_count)  # 5 -> 1 SMS and 5 -> 1 MMS

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

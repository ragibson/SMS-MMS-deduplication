import os
import unittest

from shared_testing_functions import run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output


class TestTimestamps(unittest.TestCase):
    def test_similar_timestamps_disabled(self, filename='test_cases/similar_timestamps.xml'):
        _ = read_message_count(filename)
        run_deduplication(filename)
        self.assertTrue(not os.path.exists(TEST_OUTPUT_XML))

    def test_similar_timestamps_enabled(self, filename='test_cases/similar_timestamps.xml'):
        """Check that user can specify deduplication of identical messages in same-second buckets."""
        original_count = read_message_count(filename)
        run_deduplication(filename, flags="--ignore-date-milliseconds")
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(original_count // 2, deduplicated_count)  # 4 -> 2 SMS and 4 -> 2 MMS

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

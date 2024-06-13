import os
import unittest

from shared_testing_functions import run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output


class TestCrossMessageProtocol(unittest.TestCase):
    def test_cross_sms_mms_disabled(self, filename='test_cases/cross_sms_mms_duplicate.xml'):
        _ = read_message_count(filename)
        run_deduplication(filename)
        self.assertTrue(not os.path.exists(TEST_OUTPUT_XML))

    def test_cross_sms_mms_enabled(self, filename='test_cases/cross_sms_mms_duplicate.xml'):
        """Check that duplicates can be identified across SMS/MMS/RCS protocols if aggressively deduplicating."""
        original_count = read_message_count(filename)
        run_deduplication(filename, flags="--aggressive")
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(original_count // 2, deduplicated_count)  # just a single duplicate

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

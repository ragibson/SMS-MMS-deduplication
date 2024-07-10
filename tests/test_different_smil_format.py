import unittest

from shared_testing_functions import run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output


class TestDifferentSMILFormat(unittest.TestCase):
    def test_different_smil_format_ignored(self, filename='test_cases/different_smil_format.xml'):
        """Check that duplicates can be identified across SMS/MMS/RCS protocols if aggressively deduplicating."""
        original_count = read_message_count(filename)
        run_deduplication(filename)
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(original_count // 6, deduplicated_count)  # all messages are duplicates

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

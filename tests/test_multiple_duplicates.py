import unittest

from shared_testing_functions import run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output


class TestMultipleDuplicates(unittest.TestCase):
    def test_multiple_duplicates(self, filename='test_cases/multiple_duplicates.xml'):
        """Check that deduplication can remove multiple messages all mapped to a single message."""
        original_count = read_message_count(filename)
        run_deduplication(filename)
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertTrue(original_count - 5, deduplicated_count)  # 4 -> 1 SMS and 3 -> 1 MMS

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

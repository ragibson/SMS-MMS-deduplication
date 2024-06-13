import os
import unittest

from shared_testing_functions import run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output


class TestCountryCodes(unittest.TestCase):
    def test_alternate_country_code_disabled(self, filename='test_cases/alternate_country_code.xml'):
        _ = read_message_count(filename)
        run_deduplication(filename)
        self.assertTrue(not os.path.exists(TEST_OUTPUT_XML))

    def test_alternate_country_code_enabled(self, filename='test_cases/alternate_country_code.xml'):
        """Check that user can specify +2 country code to identify corresponding duplicates."""
        original_count = read_message_count(filename)
        run_deduplication(filename, flags="--default-country-code +2")
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(original_count // 2, deduplicated_count)  # one SMS and one MMS duplicate

    def test_missing_country_code(self, filename='test_cases/missing_country_code.xml'):
        """Check that missing +1 country code is correctly identified as a duplicate."""
        original_count = read_message_count(filename)
        run_deduplication(filename)
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(original_count // 2, deduplicated_count)  # one SMS and one MMS duplicate

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

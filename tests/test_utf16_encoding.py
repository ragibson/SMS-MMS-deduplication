import unittest

from shared_testing_functions import (run_deduplication, read_message_count, TEST_OUTPUT_XML, clean_up_test_output,
                                      check_all_elements_unedited)


class TestUTF16Encoding(unittest.TestCase):
    def test_utf16_surrogate_pairs(self, filename='test_cases/utf16_encoding.xml',
                                   corrected_filename='test_cases/utf16_encoding_corrected.xml'):
        """Invalid XMLs with surrogate pairs should still run with --fix-utf16."""
        original_count = read_message_count(filename)
        run_deduplication(filename, flags="--fix-utf16")
        deduplicated_count = read_message_count(TEST_OUTPUT_XML)
        self.assertTrue(original_count - 1, deduplicated_count)  # 2 -> 1 SMS

        # we check against the corrected version of this input XML
        self.assertTrue(check_all_elements_unedited(corrected_filename, TEST_OUTPUT_XML))

    def tearDown(self):
        clean_up_test_output()


if __name__ == '__main__':
    unittest.main()

import unittest

from shared_testing_functions import (
    run_deduplication_multi,
    read_message_count,
    TEST_OUTPUT_XML,
    clean_up_test_output,
)


class TestMultiFile(unittest.TestCase):
    def test_combine_and_dedupe_two_files(self):
        """
        Combine two files that together contain duplicates across files and ensure total count and dedupe.
        test_cases/multi_file_part1.xml has 3 messages, part2 has 3 with 2 duplicates across.
        After dedupe, expect 4 unique messages.
        """
        original_1 = read_message_count("test_cases/multi_file_part1.xml")
        original_2 = read_message_count("test_cases/multi_file_part2.xml")
        self.assertEqual(original_1, 3)
        self.assertEqual(original_2, 3)

        run_deduplication_multi(
            ["test_cases/multi_file_part1.xml", "test_cases/multi_file_part2.xml"]
        )
        deduped_total = read_message_count(TEST_OUTPUT_XML)
        self.assertEqual(deduped_total, 4)

    def test_works_with_flags(self):
        """Ensure flags still apply when combining, e.g., whitespace ignore across files."""
        run_deduplication_multi(
            [
                "test_cases/multi_file_whitespace_part1.xml",
                "test_cases/multi_file_whitespace_part2.xml",
            ],
            flags="--ignore-whitespace-differences",
        )
        deduped_total = read_message_count(TEST_OUTPUT_XML)
        # Each part has 1 sms and 1 mms, second file whitespace variants; result should be 2 unique total
        self.assertEqual(deduped_total, 2)

    def tearDown(self):
        clean_up_test_output()


if __name__ == "__main__":
    unittest.main()

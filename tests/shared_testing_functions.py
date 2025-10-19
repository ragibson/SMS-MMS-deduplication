import os
from argparse import Namespace

from lxml.etree import XMLParser, parse
from dedupe_texts import (
    read_input_xml,
    combine_input_xmls,
    deduplicate_messages_in_tree,
    rewrite_tree_ids_and_count,
    write_output_xml,
)

TEST_OUTPUT_XML = "test_deduplicated.xml"
TEST_LOG_FILE = "test_deduplication.log"
TEST_OUTPUT_DIRECTORY = "tests" if os.path.basename(os.getcwd()) != "tests" else ""


def read_message_count(filepath):
    if filepath not in (TEST_OUTPUT_XML, TEST_LOG_FILE):
        filepath = os.path.join(TEST_OUTPUT_DIRECTORY, filepath)
    tree = parse(filepath, parser=XMLParser(encoding="UTF-8"))

    # make sure the message count in the XML file is accurate
    xml_count = int(tree.getroot().attrib["count"])
    child_count = len([x for x in tree.getroot()])
    if xml_count != child_count:
        raise ValueError(f"XML '{filepath}' has incorrect count in <smses ...>!")

    return xml_count


def clean_up_test_output(output_log_files=(TEST_OUTPUT_XML, TEST_LOG_FILE)):
    for fp in output_log_files:
        # Remove from CWD
        if os.path.exists(fp):
            os.unlink(fp)
        # Also remove from tests/ directory if present from prior CLI-style runs
        tests_fp = os.path.join(TEST_OUTPUT_DIRECTORY or "tests", fp)
        if os.path.exists(tests_fp):
            os.unlink(tests_fp)


def _parse_flags(flags: str) -> Namespace:
    # Defaults consistent with CLI
    args = {
        "default_country_code": "+1",
        "ignore_date_milliseconds": False,
        "ignore_whitespace_differences": False,
        "aggressive": False,
    }
    tokens = flags.split()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--default-country-code":
            if i + 1 < len(tokens):
                args["default_country_code"] = tokens[i + 1]
                i += 2
                continue
        elif t == "--ignore-date-milliseconds":
            args["ignore_date_milliseconds"] = True
        elif t == "--ignore-whitespace-differences":
            args["ignore_whitespace_differences"] = True
        elif t == "--aggressive":
            args["aggressive"] = True
        i += 1
    return Namespace(**args)


def run_deduplication(filepath, flags=""):
    clean_up_test_output()  # ensure a clean slate

    args = _parse_flags(flags)
    input_fp = os.path.join(TEST_OUTPUT_DIRECTORY, filepath)

    # Read input and run deduplication
    input_tree = read_input_xml(input_fp)
    with open(TEST_LOG_FILE, "w", encoding="utf-8") as log_file:
        output_tree, input_counts, output_counts = deduplicate_messages_in_tree(
            input_tree, log_file, args
        )

    # Rewrite counts and write output if duplicates found
    rewrite_tree_ids_and_count(
        output_tree, sum(count for count in output_counts.values())
    )
    if input_counts != output_counts:
        write_output_xml(output_tree, TEST_OUTPUT_XML)


def run_deduplication_multi(filepaths, flags=""):
    clean_up_test_output()

    args = _parse_flags(flags)
    inputs = [os.path.join(TEST_OUTPUT_DIRECTORY, fp) for fp in filepaths]

    input_tree = combine_input_xmls(inputs)
    with open(TEST_LOG_FILE, "w", encoding="utf-8") as log_file:
        output_tree, input_counts, output_counts = deduplicate_messages_in_tree(
            input_tree, log_file, args
        )

    rewrite_tree_ids_and_count(
        output_tree, sum(count for count in output_counts.values())
    )
    if input_counts != output_counts:
        write_output_xml(output_tree, TEST_OUTPUT_XML)

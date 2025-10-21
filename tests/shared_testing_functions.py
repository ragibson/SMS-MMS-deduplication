import os

from lxml.etree import XMLParser, parse

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
        if os.path.exists(fp):
            os.unlink(fp)


def run_deduplication(filepaths, flags=''):
    """
    Runs the deduplication script on the given filepaths and flags.

    A single filepath can be provided as a string, or multiple filepaths as a list of strings.
    """
    if isinstance(filepaths, str):
        filepaths = [filepaths]

    clean_up_test_output()  # sanity check that any files generated are actually from this run

    script_location = "dedupe_texts.py"
    if os.path.basename(os.getcwd()) == "tests":
        script_location = os.path.join("..", script_location)

    # this is gross, but probably okay for such a simple tool
    os.system(f"python3 {script_location} "
              + "".join(f" -i {os.path.join(TEST_OUTPUT_DIRECTORY, fp)}" for fp in filepaths)
              + f" -o {TEST_OUTPUT_XML} -l {TEST_LOG_FILE} {flags}")

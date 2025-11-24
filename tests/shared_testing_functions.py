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


def read_input_xml(filepath):
    p = XMLParser(encoding='UTF-8')  # don't need to care about huge_tree in the tests
    with open(filepath, 'rb') as file:
        return parse(file, parser=p)


def check_all_elements_unedited(input_filepaths, output_filepath):
    """Verify that all output elements are taken directly from the input XML."""
    if isinstance(input_filepaths, str):
        input_filepaths = [input_filepaths]

    # the input filepath is quoted as from tests/, but pytest executes from the root directory
    input_filepaths = [os.path.join(TEST_OUTPUT_DIRECTORY, fp) for fp in input_filepaths]

    for fp in (*input_filepaths, output_filepath):
        if not os.path.exists(fp):
            raise ValueError(f"File missing: '{fp}' from {os.getcwd()}")

    input_trees = [read_input_xml(fp) for fp in input_filepaths]
    output_tree = read_input_xml(output_filepath)

    input_elements = [(child.tag,) + tuple(sorted(child.items()))
                      for tree in input_trees for child in tree.getroot().iterchildren()]
    output_elements = [(child.tag,) + tuple(sorted(child.items())) for child in output_tree.getroot().iterchildren()]

    for child in output_tree.getroot().iterchildren():
        key = (child.tag,) + tuple(sorted(child.items()))
        if key not in input_elements:
            raise ValueError(f"Somehow missing? {key}")

    return all(element in input_elements for element in output_elements)


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

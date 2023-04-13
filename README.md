# SMS-MMS-deduplication

This is a simple tool to remove duplicate text messages from XML backups of
the "SMS Backup & Restore" format.

It also supports removal of more complicated duplicates than other tools while
taking extreme care not to identify any false positives.

For example, we handle instances where

* One message contains a data attachment, but the other does not
* The internal ordering of phone numbers is inconsistent between messages
* The internal [SMIL data](https://en.wikipedia.org/wiki/Synchronized_Multimedia_Integration_Language)
  format varies, but the message content and data are otherwise identical
* The internal storage fields are inconsistently omitted or `null`

These conflicts tend to occur when using multiple backup agents over time or
simultaneously. E.g., accidentally recovering data from Google's backups
***and*** Samsung's backups or simply changing manufacturers or carriers.

## Usage

The usage of this tool is extremely simple and can handle files of several
gigabytes in a few seconds.

```
Usage: python3 dedupe_texts.py input_file [output_file [log_file]]
```

### Console Output

This will produce output of the following form.

```
Reading 'example-input.xml'... Done in 8.1 s.
Preparing log file 'deduplication-results.log'.
Searching for duplicates... Done in 5.9 s.
Deduplication Summary:
    Message Type    |   Original Count   |      Removed       | Deduplicated Count 
        mms         |       24893        |       10325        |       14568        
        sms         |       19828        |         0          |       19828        
Writing 'example-output.xml'... Done in 3.8 s
```

or

```
Reading 'example-input.xml'... Done in 8.1 s.
Preparing log file 'deduplication-results.log'.
Searching for duplicates... Done in 5.8 s.
Deduplication Summary:
    Message Type    |   Original Count   |      Removed       | Deduplicated Count 
        mms         |       14341        |         0          |       14341        
        sms         |       19676        |         0          |       19676        
No duplicate messages found. Skipping writing of output file.
```

### Log File Output

The log file contains sections of the following form for each removed message.

```
Removing mms:
    date: 1680729606000
 address: <REDACTED #1> | <REDACTED #2>
    text: look at this amazing picture!
  m_type: 128
    type: 137 | 151

In favor of keeping mms:
    date: 1680729606000
 address: <REDACTED #1> | <REDACTED #2>
    text: look at this amazing picture!
  m_type: 128
    type: 137 | 151
    data: <LENGTH 539706 OMISSION>
```

## Related Work

This tool is somewhat inspired by
[legacy work by radj](https://github.com/radj/AndroidSMSBackupRestoreCleaner),
but is significantly simpler, much more up to date, requires far fewer
dependencies/setup, and supports MMS messages.
# SMS-MMS-deduplication

This is a simple tool to remove duplicate text messages from XML backups of
the "SMS Backup & Restore" format.

It also supports removal of more complicated duplicates than other tools while
taking extreme care not to identify any false positives.

For example, we handle instances where

* One message contains a data attachment (e.g., images sent via text message),
  but the other does not
* The internal ordering of phone numbers is inconsistent between messages
* The internal [SMIL data](https://en.wikipedia.org/wiki/Synchronized_Multimedia_Integration_Language)
  format varies, but the message content and data are otherwise identical
* The internal storage fields are inconsistently omitted or `null`

These conflicts tend to occur when using multiple backup agents over time or
simultaneously. E.g., accidentally recovering data from Google's backups
*and* Samsung's backups or simply changing manufacturers or carriers.

If you intend to use this to remove duplicated messages on your device (rather
than in your backup location), please read ["An important warning about
deduplicating messages *on a device* in practice"](#ImportantWarning).

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

<a name = "ImportantWarning"></a>

## An important warning about deduplicating messages *on a device* in practice

Note that

* SMS Backup & Restore avoids restoring duplicates by default, and
* Most messaging clients/apps actually hide deleted conversations before they
  are deleted internally (they continue the deletion work in the background)

Thus, if you flag conversations for deletion and then start restoring from
backup (without verifying the message deletion has completed internally),
***you may lose messages!***

In these cases, the backup restoration essentially detects duplicates of
messages that were mid-deletion and only completes a partial restore. E.g.,

* With duplicates where some messages have data attachments and others do not,
  you may lose images, shared contacts, etc. from text messages
* Some messaging clients may continue the deletion after the backup is
  restored, in which case you will simply lose entire messages or conversations

With this in mind, to deduplicate messages on a device itself, you should

1) Perform the backup and deduplicate it (retain both versions, just in case)
2) Confirm you have not received any new messages in the meantime (consider
   using airplane mode)
3) Mass-delete your text messages
4) Wait a few minutes (the time required depends on your phone's processing
   speed, the number of messages, etc.)
5) Clear your messaging client's data (`App Info > Storage > Clear Data`) to
   force a refresh of the text message view
6) Confirm that no messages appear in the view. Otherwise, return to step #2
7) Restore from the deduplicated backup and verify that all messages were
   restored before removing the original (non-deduplicated) backup file

For step #7, consider keeping the restoration's duplicate check enabled. If it
detects *any* duplicates when restoring to a phone that appears to have zero
existing text messages, that should be a *major* warning that something has
gone wrong.

Moreover, if you create a new backup afterward, it should not be much smaller
than the one you restored from!

## Related Work

This tool is somewhat inspired by
[legacy work by radj](https://github.com/radj/AndroidSMSBackupRestoreCleaner),
but is significantly simpler, much more up to date, requires far fewer
dependencies/setup, and supports MMS messages.
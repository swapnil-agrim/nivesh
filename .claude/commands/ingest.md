---
description: Ingest CAS statements from the inbox and show where holdings reconcile
argument-hint: "(no arguments)"
---
Ingest the statements in the inbox. $ARGUMENTS

The handler is `nivesh ingest`. It reads the PDFs in the data directory inbox, stores the
holdings, and prints a summary of where the consolidated book comes from and which ISINs differ
between the broker feed and the depository statement. HDFC holdings come from the separate
`nivesh sync` command after `nivesh login`. Interactive sessions cannot run shell commands, so
tell the owner to run the line in a terminal. The statement unlock details come from the keychain and
are never passed as arguments.

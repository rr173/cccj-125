      * ACCT daily account feed -- layout version 2
      * BALANCE widened to S9(9)V99, CURRENCY added, filler resized
      * record length: 10 + 20 + 6 + 3 + 8 + 4 + 2 = 53 bytes
       01  ACCT-RECORD.
           05  ACCT-ID            PIC X(10).
           05  ACCT-NAME          PIC X(20).
           05  BALANCE            PIC S9(9)V99 COMP-3.
           05  CURRENCY           PIC X(3).
           05  LAST-TXN-DATE      PIC 9(8).
           05  LEGACY-CODE        PIC X(4).
           05  LEGACY-CODE-NUM    REDEFINES LEGACY-CODE PIC 9(4).
           05  FILLER             PIC X(2).

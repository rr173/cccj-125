      * ACCT daily account feed -- layout version 1
      * revision 2: TRACE-CODE added, filler shrunk (still 50 bytes)
      * record length: 10 + 20 + 5 + 8 + 4 + 3 = 50 bytes
       01  ACCT-RECORD.
           05  ACCT-ID            PIC X(10).
           05  ACCT-NAME          PIC X(20).
           05  BALANCE            PIC S9(7)V99 COMP-3.
           05  LAST-TXN-DATE      PIC 9(8).
           05  TRACE-CODE         PIC X(2).
           05  LEGACY-CODE        PIC X(4).
           05  LEGACY-CODE-NUM    REDEFINES LEGACY-CODE PIC 9(4).
           05  FILLER             PIC X(1).

      * ACCT daily account feed -- layout version 2 (51 bytes)
       01  ACCT-RECORD.
           05  ACCT-ID         PIC X(10).
           05  ACCT-NAME       PIC X(20).
           05  BALANCE         PIC S9(6)V999 COMP-3.
           05  BALANCE-TEXT    REDEFINES BALANCE PIC X(5).
           05  CURRENCY        PIC X(3).
           05  LAST-TXN-DATE   PIC 9(8).
           05  LEGACY-CODE     PIC X(4).
           05  LEGACY-CODE-NUM REDEFINES LEGACY-CODE PIC 9(4).
           05  FILLER          PIC X(1).

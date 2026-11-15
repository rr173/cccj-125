      * ACCT daily account feed -- layout version 1 (50 bytes)
       01  ACCT-RECORD.
           05  ACCT-ID         PIC X(10).
           05  ACCT-NAME       PIC X(20).
           05  BALANCE         PIC S9(7)V99 COMP-3.
           05  LAST-TXN-DATE   PIC 9(8).
           05  LEGACY-CODE     PIC X(4).
           05  LEGACY-CODE-NUM REDEFINES LEGACY-CODE PIC 9(4).
           05  OLD-STATUS      PIC X(2).
           05  FILLER          PIC X(1).

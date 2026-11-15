      * ACCT v2 draft with a duplicate group declaration
       01  ACCT-RECORD.
           05  ACCT-ID   PIC X(10).
           05  GRP-A.
               10  CODE  PIC X(2).
           05  GRP-A.
               10  CODE  PIC 9(2).
           05  GRP-B.
               10  CODE  PIC X(4).

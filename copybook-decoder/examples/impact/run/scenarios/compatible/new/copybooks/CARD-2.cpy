      * CARD feed -- layout version 2 (13 bytes)
       01  CARD-RECORD.
           05  CARD-ID   PIC X(6).
           05  AMOUNT    PIC S9(5)V99 COMP-3.
           05  AMT-TEXT  REDEFINES AMOUNT PIC X(4).
           05  REGION    PIC X(3).

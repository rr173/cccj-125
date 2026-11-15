       01  SUMMARY-RECORD.
           05  SUMMARY-ID      PIC X(6).
           05  BATCH-GROUP OCCURS 3 TIMES.
               10  BATCH-CODE   PIC X(2).
               10  BATCH-AMT    PIC S9(9)V99 COMP-3.
           05  FILLER          PIC X(3).
           05  REMARK          PIC X(10).
           05  REMARK-NUM      REDEFINES REMARK PIC 9(10).
           05  ZONED-AMT       PIC S9(5)V99.

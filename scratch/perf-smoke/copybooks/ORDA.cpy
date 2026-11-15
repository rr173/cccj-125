       01  ORDER-RECORD.
           05  ORDER-ID          PIC X(8).
           05  CUSTOMER-NAME     PIC X(12).
           05  ITEM-GROUP OCCURS 2 TIMES.
               10  ITEM-CODE      PIC X(4).
               10  ITEM-AMOUNT    PIC S9(5)V99 COMP-3.
               10  ITEM-NOTE      REDEFINES ITEM-AMOUNT PIC X(4).
           05  FILLER             PIC X(2).
           05  TOTAL              PIC S9(7)V99 COMP-3.
           05  TOTAL-TEXT         REDEFINES TOTAL PIC X(5).
           05  FLAG               PIC 9(1).

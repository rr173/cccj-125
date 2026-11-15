      * Batch handoff exchange layout -- five record types in one fixed width
      * All five record types share one 73-byte physical record; fields not
      * applicable to a type are sent as spaces (DISPLAY) or zero (COMP-3).
      * Record length:
      *   REC-TYPE 6 + FILE-ID 12 + BATCH-ID 10 + SEQ 6 + CATEGORY 3
      *   + QTY S9(9)V999 COMP-3 (7) + DETAIL-COUNT 7
      *   + TOTAL-AAA 7 + TOTAL-BBB 7 + BATCH-COUNT 5 + FILLER 3 = 73
       01  HANDOFF-RECORD.
           05  REC-TYPE            PIC X(6).
           05  FILE-ID             PIC X(12).
           05  BATCH-ID            PIC X(10).
           05  SEQ-NUM             PIC 9(6).
           05  CATEGORY            PIC X(3).
           05  QUANTITY            PIC S9(9)V999 COMP-3.
           05  DETAIL-COUNT        PIC 9(7).
           05  TOTAL-AAA           PIC S9(9)V999 COMP-3.
           05  TOTAL-BBB           PIC S9(9)V999 COMP-3.
           05  BATCH-COUNT         PIC 9(5).
           05  FILLER              PIC X(3).

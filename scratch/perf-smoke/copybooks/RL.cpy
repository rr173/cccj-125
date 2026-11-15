       01  RL-RECORD.
           05  BASE-FIELD   PIC X(6).
           05  SHORT-VIEW   REDEFINES BASE-FIELD PIC 9(4).
           05  LONG-VIEW    REDEFINES BASE-FIELD PIC X(8).
           05  TAIL-FIELD   PIC X(2).
           05  FILLER       PIC X(2).

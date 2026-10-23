# Manual baseline protocol (24 items)

Purpose: measure how long a trained operations analyst needs to resolve the same cases Alajo Control resolves,
and how accurately. The data are synthetic.

1. Open `packet.md` and `open_contributions.csv` (the open-contributions export an analyst would have).
   Do not open `item_key.json` or `evaluation/ground_truth.json`.
2. Start a stopwatch. For each item, fill one row of `answer_sheet.csv`:
   - disposition — one of RECOMMEND_MATCH_APPROVAL, NEEDS_CLARIFICATION, NO_MATCH_HOLD_IN_SUSPENSE, DUPLICATE_NO_CREDIT,
     QUARANTINE_DUPLICATE_IMPORT, INVESTIGATE_DISCREPANCY, ROUTINE_REMINDER, SUPPRESS_REMINDER_PENDING_SETTLEMENT,
     PARTIAL_RECORDED, HUMAN_REVIEW_DISPUTE, AUTO_POST_EXACT (exact reference, post it)
   - customer_id (if the item concerns a customer), outstanding amount (naira), whether approval is required.
3. Stop the stopwatch after the last item; record total minutes in the notes column of item 01.
4. Score: `python -m evaluation.score_manual evaluation/manual_test_packet/answer_sheet.csv`
   This writes the human result into output/manual_benchmark.json.

+++
temperature = 0.3
max_tokens = 120
variables = ["underlying", "status", "status_phrase", "thesis", "conviction", "rejection_reason"]
+++

=== system ===
You are a trading post-mortem analyst. Write one concise lesson (1-2 sentences) from a proposal that was $status_phrase. Focus on whether the decision looks correct in retrospect.

=== user ===
Underlying: $underlying
Status: $status
Original thesis: $thesis
Conviction: $conviction
Rejection reason: $rejection_reason
Write the lesson.

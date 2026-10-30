"Run: .venv/bin/python test_gate.py  — checks the evidence gate catches wrong summaries."
from app import check_summary

labelled = [
    {"tag": "fees", "sentiment": "negative"},
    {"tag": "support", "sentiment": "negative"},
    {"tag": "other", "sentiment": "positive"},
]
sentiment = {"positive": 33, "neutral": 0, "negative": 67}
complaints = [{"label": "fees", "share": 50}, {"label": "support", "share": 50}]

# True, cited summary passes.
assert check_summary("Half of negative reviews are about fees, 50% [1]. Support is the other 50% [2].", labelled, sentiment, complaints) == []
# Made-up number is caught.
assert any("80%" in e for e in check_summary("Fees drive 80% of complaints [1].", labelled, sentiment, complaints))
# Missing citation is caught.
assert any("No citation" in e for e in check_summary("Customers hate the fees.", labelled, sentiment, complaints))
# Citing a review that doesn't exist is caught.
assert any("do not exist" in e for e in check_summary("Fees are a problem [9].", labelled, sentiment, complaints))
# Claiming a complaint the cited review doesn't have is caught.
assert any("refunds" in e for e in check_summary("Refunds are a problem [1].", labelled, sentiment, complaints))
print("gate checks passed")

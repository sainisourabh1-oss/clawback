import csv

from clawback.aggregate import Subscription
from clawback.export import to_csv


def test_csv_neutralizes_spreadsheet_formula_prefixes(tmp_path):
    path = tmp_path / "results.csv"
    sub = Subscription(
        vendor="=HYPERLINK(\"https://bad.example\")",
        domain="+SUM(1,1)", amount=12.0, currency="USD", cycle="month",
        monthly_cost=12.0, identity_key="plan|month", hits=1,
        best_confidence=0.8, last_subject="receipt",
    )
    to_csv([sub], path)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["vendor"].startswith("'=")
    assert rows[0]["domain"].startswith("'+")

from __future__ import annotations

from scripts.bench import BenchRow, format_table, run_benchmark


def test_run_benchmark_returns_one_row_per_concurrency() -> None:
    rows = run_benchmark(pages=20, delay_ms=0, concurrencies=[1, 5])

    assert [row.concurrency for row in rows] == [1, 5]
    assert all(row.wall_seconds >= 0 for row in rows)
    assert all(row.pages_per_second > 0 for row in rows)


def test_format_table_renders_header_and_rows() -> None:
    rows = [
        BenchRow(concurrency=1, wall_seconds=30.30, pages_per_second=9.9),
        BenchRow(concurrency=10, wall_seconds=5.95, pages_per_second=50.4),
    ]

    table = format_table(rows, pages=300, delay_ms=50, links_per_page=50)
    lines = table.splitlines()

    assert lines[0].startswith("pages=300 delay_ms=50 links_per_page=50 ")
    assert lines[2] == "| concurrency | wall seconds | pages per second |"
    assert lines[3] == "| --- | --- | --- |"
    assert lines[4] == "| 1 | 30.30 | 9.9 |"
    assert lines[5] == "| 10 | 5.95 | 50.4 |"

#!/usr/bin/env python3
"""Export per-entity DDR_Bench SQLite rows as JSON/JSONL data packages."""

from __future__ import annotations

import json
import csv
import sqlite3
from pathlib import Path
from typing import Any


def sqlite_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params)]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["section", "metric", "fiscal_year", "fact_name", "unit", "value", "details"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def package_paths(cik: str, package_dir: Path) -> list[Path]:
    return [
        package_dir / f"company_{cik}_schema.json",
        package_dir / f"company_{cik}_metadata.json",
        package_dir / f"company_{cik}_filings.jsonl",
        package_dir / f"company_{cik}_financial_facts.jsonl",
        package_dir / f"company_{cik}_summary.json",
        package_dir / f"company_{cik}_summary.csv",
    ]


CORE_FACT_NAMES = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "CostOfRevenue",
    "CostsAndExpenses",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "EarningsPerShareBasic",
    "EarningsPerShareDiluted",
    "Assets",
    "AssetsCurrent",
    "CashAndCashEquivalentsAtCarryingValue",
    "Liabilities",
    "LiabilitiesCurrent",
    "LongTermDebt",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "StockholdersEquity",
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInInvestingActivities",
    "NetCashProvidedByUsedInFinancingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsOfDividends",
    "PaymentsForRepurchaseOfCommonStock",
    "ResearchAndDevelopmentExpense",
    "SellingGeneralAndAdministrativeExpense",
    "IncomeTaxExpenseBenefit",
    "InterestExpenseNonOperating",
    "Goodwill",
    "InventoryNet",
]


def build_summary(conn: sqlite3.Connection, cik: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row_counts = {
        table: conn.execute(f"select count(*) from {table} where cik=?", (cik,)).fetchone()[0]
        for table in ["companies", "company_addresses", "company_tickers", "filings", "financial_facts"]
    }
    filing_forms = sqlite_rows(
        conn,
        """
        select form, count(*) as count, min(filing_date) as first_filing_date, max(filing_date) as last_filing_date
        from filings where cik=? group by form order by count desc, form
        """,
        (cik,),
    )
    filing_years = sqlite_rows(
        conn,
        """
        select substr(filing_date, 1, 4) as filing_year, count(*) as count
        from filings where cik=? and filing_date != ''
        group by filing_year order by filing_year
        """,
        (cik,),
    )
    category_counts = sqlite_rows(
        conn,
        """
        select fact_category, count(*) as count
        from financial_facts where cik=?
        group by fact_category order by count desc
        """,
        (cik,),
    )
    top_fact_names = sqlite_rows(
        conn,
        """
        select fact_name, fact_category, unit, count(*) as count,
               min(fiscal_year) as first_fiscal_year, max(fiscal_year) as last_fiscal_year
        from financial_facts where cik=?
        group by fact_name, fact_category, unit
        order by count desc, fact_name
        limit 200
        """,
        (cik,),
    )
    fiscal_year_coverage = sqlite_rows(
        conn,
        """
        select fiscal_year, form_type, fiscal_period, count(*) as fact_count,
               count(distinct fact_name) as distinct_fact_names
        from financial_facts
        where cik=? and fiscal_year is not null
        group by fiscal_year, form_type, fiscal_period
        order by fiscal_year, form_type, fiscal_period
        """,
        (cik,),
    )
    placeholders = ",".join("?" for _ in CORE_FACT_NAMES)
    core_fy_facts = sqlite_rows(
        conn,
        f"""
        select fact_name, fiscal_year, unit, fact_value, fact_category, form_type, filed_date,
               end_date, dimension_segment, dimension_geography
        from financial_facts
        where cik=? and fiscal_period='FY' and form_type='10-K'
          and fact_name in ({placeholders})
        order by fact_name, fiscal_year, filed_date, id
        """,
        (cik, *CORE_FACT_NAMES),
    )

    deduped_core: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in core_fy_facts:
        if row.get("dimension_segment") or row.get("dimension_geography"):
            continue
        key = (str(row.get("fact_name")), int(row.get("fiscal_year") or 0), str(row.get("unit") or ""))
        deduped_core[key] = row

    yoy_changes: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in deduped_core.values():
        grouped.setdefault((str(row.get("fact_name")), str(row.get("unit") or "")), []).append(row)
    for (fact_name, unit), rows in grouped.items():
        rows = sorted(rows, key=lambda item: int(item.get("fiscal_year") or 0))
        previous: dict[str, Any] | None = None
        for row in rows:
            value = row.get("fact_value")
            if previous is not None and value is not None and previous.get("fact_value") not in (None, 0):
                prev_value = float(previous["fact_value"])
                cur_value = float(value)
                change = cur_value - prev_value
                pct_change = change / abs(prev_value)
                yoy_changes.append({
                    "fact_name": fact_name,
                    "unit": unit,
                    "from_fiscal_year": previous.get("fiscal_year"),
                    "to_fiscal_year": row.get("fiscal_year"),
                    "from_value": previous.get("fact_value"),
                    "to_value": value,
                    "change": change,
                    "pct_change": pct_change,
                })
            previous = row

    largest_abs_values = sqlite_rows(
        conn,
        """
        select fact_name, fact_category, fiscal_year, fiscal_period, form_type, unit, fact_value,
               dimension_segment, dimension_geography
        from financial_facts
        where cik=? and fact_value is not null and fiscal_year is not null
        order by abs(fact_value) desc
        limit 100
        """,
        (cik,),
    )
    largest_yoy_changes = sorted(yoy_changes, key=lambda item: abs(float(item["change"])), reverse=True)[:100]

    summary = {
        "cik": cik,
        "row_counts": row_counts,
        "filing_forms": filing_forms,
        "filing_years": filing_years,
        "financial_fact_category_counts": category_counts,
        "top_fact_names": top_fact_names,
        "fiscal_year_coverage": fiscal_year_coverage,
        "core_fy_facts": sorted(deduped_core.values(), key=lambda item: (str(item.get("fact_name")), int(item.get("fiscal_year") or 0))),
        "largest_yoy_changes": largest_yoy_changes,
        "largest_abs_values": largest_abs_values,
    }

    csv_rows: list[dict[str, Any]] = []
    for table, count in row_counts.items():
        csv_rows.append({"section": "row_counts", "metric": table, "value": count})
    for item in filing_forms:
        csv_rows.append({
            "section": "filing_forms",
            "metric": item.get("form"),
            "value": item.get("count"),
            "details": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        })
    for item in category_counts:
        csv_rows.append({
            "section": "fact_category_counts",
            "metric": item.get("fact_category"),
            "value": item.get("count"),
        })
    for item in top_fact_names[:100]:
        csv_rows.append({
            "section": "top_fact_names",
            "metric": item.get("fact_category"),
            "fact_name": item.get("fact_name"),
            "unit": item.get("unit"),
            "value": item.get("count"),
            "details": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        })
    for item in summary["core_fy_facts"]:
        csv_rows.append({
            "section": "core_fy_facts",
            "metric": "FY 10-K",
            "fiscal_year": item.get("fiscal_year"),
            "fact_name": item.get("fact_name"),
            "unit": item.get("unit"),
            "value": item.get("fact_value"),
            "details": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        })
    for item in largest_yoy_changes:
        csv_rows.append({
            "section": "largest_yoy_changes",
            "metric": f"{item.get('from_fiscal_year')}->{item.get('to_fiscal_year')}",
            "fiscal_year": item.get("to_fiscal_year"),
            "fact_name": item.get("fact_name"),
            "unit": item.get("unit"),
            "value": item.get("change"),
            "details": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        })
    return summary, csv_rows


def export_10k_company_package(db_path: str | Path, cik: str, package_dir: Path) -> list[Path]:
    """Export schema, metadata, filings, financial facts, and summary files for one CIK."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    package_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = [
        row["name"]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        )
    ]
    schema: dict[str, Any] = {"source_database": str(db_path), "tables": {}}
    for table in tables:
        schema["tables"][table] = {
            "columns": [dict(row) for row in conn.execute(f"pragma table_info({table})")],
            "comment": sqlite_rows(conn, "select comment from table_comments where table_name=?", (table,)),
            "documentation": sqlite_rows(conn, "select documentation from table_documentation where table_name=?", (table,)),
            "column_comments": sqlite_rows(conn, "select column_name, comment from column_comments where table_name=?", (table,)),
            "column_documentation": sqlite_rows(conn, "select column_name, documentation from column_documentation where table_name=?", (table,)),
        }

    metadata = {
        "cik": cik,
        "companies": sqlite_rows(conn, "select * from companies where cik=?", (cik,)),
        "company_addresses": sqlite_rows(conn, "select * from company_addresses where cik=?", (cik,)),
        "company_tickers": sqlite_rows(conn, "select * from company_tickers where cik=?", (cik,)),
        "row_counts": {},
    }
    for table in ["companies", "company_addresses", "company_tickers", "filings", "financial_facts"]:
        metadata["row_counts"][table] = conn.execute(f"select count(*) from {table} where cik=?", (cik,)).fetchone()[0]

    filings = sqlite_rows(conn, "select * from filings where cik=? order by filing_date, id", (cik,))
    financial_facts = sqlite_rows(
        conn,
        """
        select * from financial_facts
        where cik=?
        order by fiscal_year, fiscal_period, fact_name, end_date, id
        """,
        (cik,),
    )
    if not metadata["companies"]:
        raise ValueError(f"No company row found for CIK {cik}")
    if not financial_facts:
        raise ValueError(f"No financial_facts rows found for CIK {cik}")

    summary, summary_csv_rows = build_summary(conn, cik)
    paths = package_paths(cik, package_dir)
    write_json(paths[0], schema)
    write_json(paths[1], metadata)
    write_jsonl(paths[2], filings)
    write_jsonl(paths[3], financial_facts)
    write_json(paths[4], summary)
    write_summary_csv(paths[5], summary_csv_rows)
    return paths

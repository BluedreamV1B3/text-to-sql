import argparse
import os
import sys
import time
from pathlib import Path

import pyodbc


CONN_STR = (
    os.getenv("TEXT2SQL_BIOMED_CONN_STR")
    or r"DRIVER={ODBC Driver 17 for SQL Server};"
    r"SERVER=.\SQLEXPRESS;"
    r"DATABASE=biomed;"
    r"Trusted_Connection=yes;"
    r"Encrypt=yes;TrustServerCertificate=yes;"
)

DEFAULT_CSV_DIR = Path(
    os.getenv("TEXT2SQL_BIOMED_CSV_DIR", "data/biomed_csv_files/done")
)
CSV_FILES = {
    "AlzheimerDisease_CombinedGeneData_UUID": DEFAULT_CSV_DIR
    / "AlzheimerDisease_CombinedGeneData_UUID.csv",
    "DrugTargets_IndicationsAndTherapeuticUses": DEFAULT_CSV_DIR
    / "DrugTargets_IndicationsAndTherapeuticUses.csv",
}

TABLE_DDL = {
    "AlzheimerDisease_CombinedGeneData_UUID": """
CREATE TABLE dbo.AlzheimerDisease_CombinedGeneData_UUID (
    UUID varchar(max) NULL,
    SNP varchar(max) NULL,
    A1 varchar(max) NULL,
    A2 varchar(max) NULL,
    freq float NULL,
    b float NULL,
    se float NULL,
    p float NULL,
    chr_37 bigint NULL,
    bp_37 bigint NULL,
    chr_38 bigint NULL,
    bp_38 bigint NULL,
    nearestGene varchar(max) NULL
);
""",
    "DrugTargets_IndicationsAndTherapeuticUses": """
CREATE TABLE dbo.DrugTargets_IndicationsAndTherapeuticUses (
    UUID varchar(max) NULL,
    chemblId varchar(max) NULL,
    drugName varchar(max) NULL,
    tradeName varchar(max) NULL,
    drugType varchar(max) NULL,
    actionType varchar(max) NULL,
    targetType varchar(max) NULL,
    target varchar(max) NULL,
    approvedSymbol varchar(max) NULL,
    approvedName varchar(max) NULL,
    yearOfFirstApproval bigint NULL,
    usan_year float NULL,
    patent_no varchar(max) NULL,
    max_phase_for_ind float NULL,
    mesh_id varchar(max) NULL,
    mesh_heading varchar(max) NULL,
    efo_id varchar(max) NULL,
    efo_term varchar(max) NULL,
    tradeNames_list varchar(max) NULL,
    tradename_count bigint NULL,
    syns_list varchar(max) NULL,
    synonyms_count bigint NULL,
    ct varchar(max) NULL
);
""",
}

EXPECTED_ROWS = {
    "AlzheimerDisease_CombinedGeneData_UUID": 21_143_200,
    "DrugTargets_IndicationsAndTherapeuticUses": 1_195_695,
}


def sql_literal(path: Path) -> str:
    return str(path).replace("'", "''")


def execute(cur, sql: str) -> None:
    cur.execute(sql)
    while cur.nextset():
        pass


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{table}")
    return cur.fetchone()[0] is not None


def row_count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT_BIG(*) FROM dbo.{table}")
    return int(cur.fetchone()[0])


def bulk_insert_sql(table: str, csv_path: Path, firstrow: int = 2, lastrow: int | None = None) -> str:
    lastrow_clause = f",\n    LASTROW = {lastrow}" if lastrow is not None else ""
    return f"""
BULK INSERT dbo.{table}
FROM '{sql_literal(csv_path)}'
WITH (
    FORMAT = 'CSV',
    FIRSTROW = {firstrow}{lastrow_clause},
    FIELDQUOTE = '"',
    TABLOCK,
    BATCHSIZE = 100000,
    ROWTERMINATOR = '0x0a'
);
"""


def print_db_file_usage(cur) -> None:
    cur.execute(
        """
SELECT name, type_desc,
       CAST(size * 8.0 / 1024 AS decimal(18,2)) AS size_mb,
       CAST(FILEPROPERTY(name, 'SpaceUsed') * 8.0 / 1024 AS decimal(18,2)) AS used_mb
FROM sys.database_files
ORDER BY type_desc, name;
"""
    )
    for row in cur.fetchall():
        print(tuple(row))


def test_bulk_insert(cur, table: str) -> None:
    test_table = f"_bulk_test_{table}"
    execute(cur, f"DROP TABLE IF EXISTS dbo.{test_table};")
    execute(cur, TABLE_DDL[table].replace(f"dbo.{table}", f"dbo.{test_table}"))
    try:
        execute(cur, bulk_insert_sql(test_table, CSV_FILES[table], firstrow=2, lastrow=1001))
        count = row_count(cur, test_table)
        if count != 1000:
            raise RuntimeError(f"{test_table} loaded {count} rows; expected 1000")
        print(f"Bulk insert smoke test passed for {table}: {count} rows")
    finally:
        execute(cur, f"DROP TABLE IF EXISTS dbo.{test_table};")


def import_table(cur, table: str, force: bool = False) -> None:
    csv_path = CSV_FILES[table]
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    if table_exists(cur, table):
        count = row_count(cur, table)
        if count == EXPECTED_ROWS[table] and not force:
            print(f"{table} already exists with expected row count {count}; skipping")
            return
        if not force:
            raise RuntimeError(
                f"dbo.{table} already exists with {count} rows. "
                "Use --force to drop and reload it."
            )
        print(f"Dropping existing dbo.{table} with {count} rows")
        execute(cur, f"DROP TABLE dbo.{table};")

    print(f"Creating dbo.{table}")
    execute(cur, TABLE_DDL[table])
    print(f"Bulk importing {csv_path}")
    t0 = time.monotonic()
    execute(cur, bulk_insert_sql(table, csv_path))
    elapsed = time.monotonic() - t0
    count = row_count(cur, table)
    print(f"Imported dbo.{table}: {count} rows in {elapsed:.1f}s")
    if count != EXPECTED_ROWS[table]:
        raise RuntimeError(f"dbo.{table} row count {count} != expected {EXPECTED_ROWS[table]}")


def verify_queries(cur) -> None:
    checks = [
        (
            "AlzheimerDisease_CombinedGeneData_UUID",
            "SELECT TOP (1) UUID, SNP, p FROM dbo.AlzheimerDisease_CombinedGeneData_UUID WHERE SNP = 'rs6497896' ORDER BY p;",
        ),
        (
            "DrugTargets_IndicationsAndTherapeuticUses",
            "SELECT TOP (1) UUID, drugName, mesh_heading FROM dbo.DrugTargets_IndicationsAndTherapeuticUses WHERE LOWER(drugType) = 'small molecule';",
        ),
    ]
    for table, sql in checks:
        cur.execute(sql)
        row = cur.fetchone()
        print(f"Verification query for {table}: {tuple(row) if row else 'no rows returned'}")


def main() -> None:
    global CONN_STR, CSV_FILES

    parser = argparse.ArgumentParser()
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=DEFAULT_CSV_DIR,
        help="Directory containing the two Biomed CSV files.",
    )
    parser.add_argument(
        "--connection-string",
        default=CONN_STR,
        help="ODBC connection string for the biomed SQL Server database.",
    )
    args = parser.parse_args()

    CONN_STR = args.connection_string
    CSV_FILES = {
        "AlzheimerDisease_CombinedGeneData_UUID": args.csv_dir
        / "AlzheimerDisease_CombinedGeneData_UUID.csv",
        "DrugTargets_IndicationsAndTherapeuticUses": args.csv_dir
        / "DrugTargets_IndicationsAndTherapeuticUses.csv",
    }

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    with pyodbc.connect(CONN_STR, timeout=30, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_SNAME(), USER_NAME()")
        print("Connected:", tuple(cur.fetchone()))
        print("Database file usage before:")
        print_db_file_usage(cur)

        for table in CSV_FILES:
            test_bulk_insert(cur, table)

        if args.test_only:
            print("Test-only mode complete.")
            return

        for table in CSV_FILES:
            import_table(cur, table, force=args.force)

        print("Database file usage after:")
        print_db_file_usage(cur)
        verify_queries(cur)


if __name__ == "__main__":
    main()

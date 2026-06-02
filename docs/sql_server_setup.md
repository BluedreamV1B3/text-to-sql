# SQL Server Setup

The evaluation runner expects Microsoft SQL Server and the ODBC Driver 17 for SQL Server.

Default connection targets are defined in `evaluation/eval_runner.py`:

- `Insurance` database via Windows auth
- `biomed` database via SQL auth login `biomed_eval`
- `kaggleDBQA` database via per-dataset SQL logins

The KaggleDBQA datasets use SQL Server schemas/logins such as:

- `geo_eval`
- `gmc_eval`
- `sms_eval`
- `baseball_eval`
- `hiphop_eval`
- `soccer_eval`

The default password in the local scripts is `Eval@2024!`; change this in your SQL Server setup or update the connection strings before running in a different environment.

## Biomed Large Table Import

Two large Biomed CSV files are not tracked in git:

- `AlzheimerDisease_CombinedGeneData_UUID.csv`
- `DrugTargets_IndicationsAndTherapeuticUses.csv`

Place them under `data/biomed_csv_files/done` or pass a path explicitly:

```powershell
python evaluation/import_biomed_missing_tables.py --csv-dir C:\path\to\biomed_csv_files\done
```

The import script bulk-loads them into the `biomed` database as:

- `dbo.AlzheimerDisease_CombinedGeneData_UUID`
- `dbo.DrugTargets_IndicationsAndTherapeuticUses`

Run `--test-only` first to verify CSV parsing before a full import:

```powershell
python evaluation/import_biomed_missing_tables.py --csv-dir C:\path\to\biomed_csv_files\done --test-only
```

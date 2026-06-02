# Repository Layout Notes

This repository keeps a practical split between generation and evaluation while sharing one benchmark schema/question source.

## Shared Dataset Definitions

The shared folders are:

- `BiomedSQL/`
- `Insurance/`
- `KaggleDBQA/`

The KaggleDBQA raw database files are excluded. The repository keeps schemas, questions, examples, metadata, and small key files needed for prompt construction.

## Generation

`generation/` contains LLM adapters and workbook generation scripts. Provider API keys are read from environment variables.

## Evaluation

`evaluation/` contains SQL Server execution, metric calculation, result summarization, and targeted rerun helpers.

## Benchmark Workbooks

`benchmark/workbooks/` contains the lightweight workbook inputs for the four current batches.

## Excluded Artifacts

The following are intentionally not versioned:

- raw CSV data
- SQL Server database files
- SQL runtime JSON outputs
- full local result folders
- local `.env` files
- notebooks that contain historical API-key outputs

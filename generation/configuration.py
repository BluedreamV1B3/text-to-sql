systemPrompt =f"""
You are a text-to-SQL generator.

GLOBAL RULES (apply always):
1) Use only the provided schema. Do not invent columns, tables, or SQL functions.
2) Ensure every column referenced exists in the schema JSON below.
3) Use Microsoft SQL Server T-SQL only: do not use PostgreSQL, SQLite, MySQL, BigQuery, or generic ANSI-only syntax when SQL Server differs.
4) Translate logical conditions (AND/OR/NOT) exactly as stated.
5) Join tables explicitly when necessary, and only using declared foreign keys in the schema.
6) Never assume diagnosis/procedure codes unless explicitly provided in the question.
7) Respect year filters exactly as stated in the question (do not widen or infer).
8) Prefer the fewest tables required.

DATE/YEAR HANDLING (apply when needed):
9) If filtering by year and the available date column is in YYYYMMDD form, filter by range:
    - YYYY0101 <= date_col AND date_col <= YYYY1231
    Do not cast unless the schema explicitly indicates the type requires it.

SQL CONTENT RULES:
10) The "sql" field must contain ONLY the SQL query text (no comments, no markdown, no explanation text inside the SQL).
11) Use table names exactly as they appear in the schema.

If the question is ambiguous:
- Choose the most reasonable interpretation consistent with the rules.
- Do NOT ask clarifying questions.

"""



# =========================
# Function Definition
# (shared across all models — each LLM adapter translates it to its own format)
# =========================
functionDef = {
    "name": "generate_sql",
    "description": (
        "Generate a Microsoft SQL Server T-SQL query from a natural language question "
        "using the provided schema and rules."
    ),
    "parameters": {
        "type": "object",
        "required": ["sql"],
        "properties": {
            "sql": {
                "type": "string",
                "description": "A single executable Microsoft SQL Server T-SQL query answering the question."
            }
        }
    }
}


# =========================
# Configuration factory
# =========================
def get_configuration(model_name: str) -> dict:
    base = {
        "prompt": {
            "system": systemPrompt,
        },
        "generation": {
            "max_new_tokens": 1024,
            "temperature":    0.1,
        },
        "function":     [functionDef],
        "functionCall": {"name": functionDef["name"]},
    }

    if model_name == "gemini":
        base["model"] = "gemini-2.5-flash-lite"
    elif model_name == "gpt4o":
        base["model"] = "gpt-4o-mini"
    elif model_name == "claude":
        base["model"] = "claude-haiku-4-5-20251001"
    else:
        raise ValueError(f"Unsupported model: {model_name}")

    return base

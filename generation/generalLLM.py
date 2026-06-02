from abc import ABC, abstractmethod


class GeneralLLM(ABC):
    def __init__(self, configuration):
        self.configuration = configuration
        self.debug = False

    def getSystemPrompt(self) -> str:
        return self.configuration["prompt"]["system"]

    def _get_function_def(self) -> dict:
        """Return the single function definition from config."""
        return self.configuration["function"][0]

    def _get_function_name(self) -> str:
        return self.configuration["functionCall"]["name"]

    def _normalise_result(self, tool_input: dict, user_prompt: str) -> dict:
        """
        Shared post-processing: clean up the SQL string returned by any model's
        tool call and return a consistent dict.
        """
        sql_raw     = tool_input.get("sql", "") or ""
        explanation = tool_input.get("explanation", "") or ""
        question    = tool_input.get("question", user_prompt) or user_prompt

        sql_clean = " ".join(sql_raw.split())
        if sql_clean:
            sql_clean = sql_clean.rstrip(";") + ";"

        if self.debug:
            print(f"\n  [tool result] sql      : {sql_clean[:120]}")
            print(f"  [tool result] explanation: {explanation[:80]}")

        return {
            "question":    question,
            "sql":         sql_clean,
            "explanation": explanation,
        }

    @abstractmethod
    def getResponse(self, userPrompt: str) -> str:
        """Return the raw text response from the model (used internally if needed)."""
        pass

    @abstractmethod
    def getResponseFunction(self, userPrompt: str) -> dict:
        """
        Call the model using function/tool calling and return a dict with keys:
            question, sql, explanation
        """
        pass
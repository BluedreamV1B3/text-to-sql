import os
import json
from openai import OpenAI
from tenacity import retry, wait_random_exponential, stop_after_attempt
from generalLLM import GeneralLLM


class LLM(GeneralLLM):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # -------------------------------------------------
    # Translate shared functionDef → OpenAI tool format
    # OpenAI wraps it under {"type": "function", "function": {...}}
    # -------------------------------------------------
    def _build_openai_tool(self) -> dict:
        fn = self.configuration["function"][0]
        return {
            "type": "function",
            "function": {
                "name":        fn["name"],
                "description": fn["description"],
                "parameters":  fn["parameters"],
            }
        }

    # -------------------------------------------------
    # Generation (plain text — kept for compatibility)
    # -------------------------------------------------
    @retry(wait=wait_random_exponential(min=1, max=10),
           stop=stop_after_attempt(5))
    def getResponse(self, userPrompt):
        gen_cfg = self.configuration.get("generation", {})
        response = self.client.chat.completions.create(
            model=self.configuration["model"],
            messages=[
                {"role": "system", "content": self.getSystemPrompt()},
                {"role": "user",   "content": userPrompt},
            ],
            temperature=gen_cfg.get("temperature", 0.1),
            max_tokens=gen_cfg.get("max_new_tokens", 1024),
        )
        return response.choices[0].message.content

    # -------------------------------------------------
    # Structured output via function calling
    # -------------------------------------------------
    @retry(wait=wait_random_exponential(min=1, max=10),
           stop=stop_after_attempt(5))
    def getResponseFunction(self, userPrompt):
        gen_cfg   = self.configuration.get("generation", {})
        tool      = self._build_openai_tool()
        func_name = self.configuration["functionCall"]["name"]

        response = self.client.chat.completions.create(
            model=self.configuration["model"],
            messages=[
                {"role": "system", "content": self.getSystemPrompt()},
                {"role": "user",   "content": userPrompt},
            ],
            temperature=gen_cfg.get("temperature", 0.1),
            max_tokens=gen_cfg.get("max_new_tokens", 1024),
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": func_name}},
        )

        if self.debug:
            print("\n===== RAW OPENAI RESPONSE =====")
            print(response)
            print("================================\n")

        tool_call  = response.choices[0].message.tool_calls[0]
        tool_input = json.loads(tool_call.function.arguments)

        sql_raw = tool_input.get("sql", "") or ""

        return {
            "question":    tool_input.get("question", userPrompt),
            "sql":         sql_raw,
            "explanation": tool_input.get("explanation", ""),
        }

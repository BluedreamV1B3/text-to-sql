import os
import anthropic
from tenacity import retry, wait_random_exponential, stop_after_attempt
from generalLLM import GeneralLLM


class LLM(GeneralLLM):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            timeout=float(os.getenv("CLAUDE_REQUEST_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("CLAUDE_SDK_MAX_RETRIES", "0")),
        )

    # -------------------------------------------------
    # Translate shared functionDef → Claude tool format
    # Claude uses "input_schema" instead of "parameters"
    # -------------------------------------------------
    def _build_claude_tool(self) -> dict:
        fn = self.configuration["function"][0]
        return {
            "name":         fn["name"],
            "description":  fn["description"],
            "input_schema": fn["parameters"],
        }

    # -------------------------------------------------
    # Generation (plain text — kept for compatibility)
    # -------------------------------------------------
    @retry(wait=wait_random_exponential(min=1, max=10),
           stop=stop_after_attempt(5))
    def getResponse(self, userPrompt):
        gen_cfg = self.configuration.get("generation", {})
        response = self.client.messages.create(
            model=self.configuration["model"],
            max_tokens=gen_cfg.get("max_new_tokens", 1024),
            temperature=gen_cfg.get("temperature", 0.1),
            system=self.getSystemPrompt(),
            messages=[{"role": "user", "content": userPrompt}]
        )
        return response.content[0].text

    # -------------------------------------------------
    # Structured output via tool use
    # -------------------------------------------------
    def getResponseFunction(self, userPrompt):
        gen_cfg   = self.configuration.get("generation", {})
        tool      = self._build_claude_tool()
        func_name = self.configuration["functionCall"]["name"]

        response = self.client.messages.create(
            model=self.configuration["model"],
            max_tokens=gen_cfg.get("max_new_tokens", 1024),
            temperature=gen_cfg.get("temperature", 0.1),
            system=self.getSystemPrompt(),
            tools=[tool],
            tool_choice={"type": "tool", "name": func_name},
            messages=[{"role": "user", "content": userPrompt}]
        )

        if self.debug:
            print("\n===== RAW CLAUDE RESPONSE =====")
            for block in response.content:
                print(block)
            print("================================\n")

        # tool_choice forced — content[0] is always a tool_use block
        tool_input  = response.content[0].input
        sql_raw = tool_input.get("sql", "") or ""

        return {
            "question":    tool_input.get("question", userPrompt),
            "sql":         sql_raw,
            "explanation": tool_input.get("explanation", ""),
        }

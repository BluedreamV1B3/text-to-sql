import os
from google import genai
from google.genai import types
from tenacity import retry, wait_random_exponential, stop_after_attempt
from generalLLM import GeneralLLM


class LLM(GeneralLLM):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.client = genai.Client(
            api_key=os.getenv("GOOGLE_API_KEY")
        )

    # -------------------------------------------------
    # Translate shared functionDef → Gemini tool format
    # Gemini uses types.Tool with function_declarations
    # -------------------------------------------------
    def _to_gemini_schema(self, property_def: dict) -> types.Schema:
        type_name = str(property_def.get("type", "string")).upper()
        gemini_type = getattr(types.Type, type_name, types.Type.STRING)
        return types.Schema(
            type=gemini_type,
            description=property_def.get("description", ""),
        )

    def _build_gemini_tool(self) -> types.Tool:
        fn     = self.configuration["function"][0]
        props  = fn["parameters"]["properties"]
        schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                name: self._to_gemini_schema(prop_def)
                for name, prop_def in props.items()
            },
            required=fn["parameters"]["required"],
        )
        func_decl = types.FunctionDeclaration(
            name=fn["name"],
            description=fn["description"],
            parameters=schema,
        )
        return types.Tool(function_declarations=[func_decl])

    # -------------------------------------------------
    # Generation (plain text — kept for compatibility)
    # -------------------------------------------------
    @retry(wait=wait_random_exponential(min=1, max=10),
           stop=stop_after_attempt(3))
    def getResponse(self, userPrompt):
        gen_cfg     = self.configuration.get("generation", {})
        full_prompt = f"{self.getSystemPrompt()}\n\nUSER QUESTION:\n{userPrompt}"

        try:
            response = self.client.models.generate_content(
                model=self.configuration["model"],
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=gen_cfg.get("temperature", 0.1),
                    max_output_tokens=gen_cfg.get("max_new_tokens", 1024),
                    response_mime_type="application/json",
                )
            )
            return response.text
        except Exception as e:
            print(f"Gemini API Error: {type(e).__name__}: {str(e)}")
            raise

    # -------------------------------------------------
    # Structured output via function calling
    # -------------------------------------------------
    @retry(wait=wait_random_exponential(min=1, max=10),
           stop=stop_after_attempt(3))
    def getResponseFunction(self, userPrompt):
        gen_cfg   = self.configuration.get("generation", {})
        tool      = self._build_gemini_tool()
        func_name = self.configuration["functionCall"]["name"]

        # Force Gemini to always call the specific function
        tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[func_name],
            )
        )

        try:
            response = self.client.models.generate_content(
                model=self.configuration["model"],
                contents=f"{self.getSystemPrompt()}\n\nUSER QUESTION:\n{userPrompt}",
                config=types.GenerateContentConfig(
                    temperature=gen_cfg.get("temperature", 0.1),
                    max_output_tokens=gen_cfg.get("max_new_tokens", 1024),
                    tools=[tool],
                    tool_config=tool_config,
                )
            )
        except Exception as e:
            print(f"Gemini API Error: {type(e).__name__}: {str(e)}")
            raise

        if self.debug:
            print("\n===== RAW GEMINI RESPONSE =====")
            print(response)
            print("================================\n")

        # Extract function call arguments from the response
        func_call  = response.candidates[0].content.parts[0].function_call
        tool_input = dict(func_call.args)

        sql_raw = tool_input.get("sql", "") or ""

        return {
            "question":    tool_input.get("question", userPrompt),
            "sql":         sql_raw,
            "explanation": tool_input.get("explanation", ""),
        }

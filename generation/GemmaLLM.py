import json
import torch
from tenacity import retry, wait_random_exponential, stop_after_attempt
from transformers import pipeline
from generalLLM import GeneralLLM




class LLM(GeneralLLM):
    """
    Gemma-3 Text-to-SQL LLM wrapper
    """

    def __init__(self, configuration, pipe=None):
        super().__init__(configuration)

        if pipe is None:
            self.pipe = pipeline(
                "text-generation",                  # text-only pipeline
                model=configuration["model"],
                device="cpu",                       # force CPU for stability
                dtype=torch.float32                 # safe on CPU
            )
        else:
            self.pipe = pipe

    # --------------------------------------------------
    # Shared Robust JSON Extractor
    # --------------------------------------------------
    def _extract_first_json(self, text: str):
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False

        for i in range(start, len(text)):
            c = text[i]

            if escape:
                escape = False
                continue

            if c == '\\':
                escape = True
                continue

            if c == '"':
                in_string = not in_string

            if not in_string:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            return None
        return None

    # --------------------------------------------------
    # Generation
    # --------------------------------------------------
    @retry(wait=wait_random_exponential(min=1, max=10), stop=stop_after_attempt(5))
    def getResponse(self, userPrompt):

        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": self.getSystemPrompt()}]
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": userPrompt}]
            }
        ]

        gen_cfg = self.configuration.get("generation", {})
        do_sample = gen_cfg.get("do_sample", False)

        pipe_kwargs = {
            "text": messages,
            "max_new_tokens": gen_cfg.get("max_new_tokens", 1024),
            "do_sample": do_sample
        }

        if do_sample:
            pipe_kwargs["temperature"] = gen_cfg.get("temperature", 0.1)

        output = self.pipe(**pipe_kwargs)
        generated_text = output[0]["generated_text"][-1]["content"]

        if self.debug:
            print("\n===== RAW GEMMA OUTPUT =====")
            print(generated_text)
            print("============================\n")

        return generated_text

    # --------------------------------------------------
    # Structured Output
    # --------------------------------------------------
    def getResponseFunction(self, userPrompt):

        raw = self.getResponse(userPrompt)
        parsed = self._extract_first_json(raw)

        if not parsed or not isinstance(parsed, dict):
            return {
                "question": userPrompt,
                "sql": "",
                "explanation": "FAILED_TO_PARSE_JSON"
            }

        required = ["question", "sql", "explanation"]
        if not all(k in parsed for k in required):
            return {
                "question": userPrompt,
                "sql": "",
                "explanation": f"INVALID_JSON_STRUCTURE: {list(parsed.keys())}"
            }

        sql = parsed["sql"].strip().rstrip(";") + ";"

        return {
            "question": parsed["question"],
            "sql": sql,
            "explanation": parsed["explanation"]
        }

_instances = {}

def create_llm(model_name, configuration):
    if model_name not in _instances:
        if model_name == "gpt4o":
            from OpenAILLM import LLM
            _instances[model_name] = LLM(configuration)
        elif model_name == "claude":
            from ClaudeLLM import LLM
            _instances[model_name] = LLM(configuration)
        elif model_name == "gemini":
            from GeminiLLM import LLM
            _instances[model_name] = LLM(configuration)
        else:
            raise ValueError(f"Unsupported model: {model_name}")
    return _instances[model_name]
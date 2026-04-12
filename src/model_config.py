import os
import openai
import anthropic
from openai import OpenAI


def prompt_model(model_name, prompt, temperature=0.8):
    print(f"Using model: {model_name}; temperature: {temperature}")
    if "claude" in model_name.lower():
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
        if not api_key:
            raise ValueError("Set ANTHROPIC_API_KEY or CLAUDE_API_KEY environment variable for Claude models.")
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model=model_name,
            messages=[
                {"role": "user", "content": prompt},
            ],
            max_tokens=4000,
            temperature=temperature,
        )
        print(response)
        return response.content[0].text
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY environment variable for OpenAI models.")

        kwargs = {"api_key": api_key}
        base_url = os.environ.get("MODEL_SERVING_URL")
        if base_url:
            kwargs["base_url"] = base_url

        client = openai.OpenAI(**kwargs)

        response = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        print(response)
        return response.choices[0].message.content.strip()


# Alias for backward compatibility (used by localization.py)
generate_text = prompt_model

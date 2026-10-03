"""
mini_test.py — the smallest possible test of Qwen3-VL-4B running locally.

What this does, in order:
  1. Takes one page image (you'll give it a PNG/JPG path)
  2. Sends it to the model running on your machine via Ollama
  3. Prints back whatever the model says about that image

Nothing else. No PDF handling, no JSON validation, no batching.
Once this works and you understand it, we build the rest back up.
"""

import ollama

MODEL_NAME = "qwen3-vl:4b-instruct"   # must match what "ollama list" shows
IMAGE_PATH = "test_page.png"          # put any image here to start -- even a
                                       # screenshot of a table works fine

# This is the instruction the model follows. Start simple -- once you see
# it responding sensibly, THIS is the line you'll iterate on.
PROMPT = "Describe what you see on this page. If there is a table, describe its rows and columns."


response = ollama.chat(
    model=MODEL_NAME,
    messages=[
        {
            "role": "user",
            "content": PROMPT,
            "images": [IMAGE_PATH],
        }
    ],
)

print(response["message"]["content"])
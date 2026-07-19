import base64
import json
import urllib.request
from pathlib import Path

# Create a dummy text file to act as our document
scratch_dir = Path(__file__).resolve().parent
test_file = scratch_dir / "test_api_doc.txt"

with open(test_file, "w") as f:
    f.write("This is a confidential company document. The secret passcode for the vault is: OMEGA_PROTOCOL_99.")

# Encode it to base64
with open(test_file, "rb") as f:
    b64_data = base64.b64encode(f.read()).decode("utf-8")

# Construct the data URI
mime_type = "text/plain"
data_uri = f"data:{mime_type};base64,{b64_data}"

# Prepare the standard OpenAI API payload using the new multimodal format
payload = {
    "model": "gpt-4o",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read the attached document. What is the secret passcode for the vault?"},
                {"type": "file_url", "file_url": {"url": data_uri}}
            ]
        }
    ]
}

url = "http://127.0.0.1:8001/v1/chat/completions"
headers = {"Content-Type": "application/json"}

print(f"Sending API request with {mime_type} document...")
req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)

try:
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode('utf-8'))
        bot_reply = result['choices'][0]['message']['content']
        print("\n--- ChatGPT API Response ---")
        print(bot_reply)
        print("----------------------------")
except Exception as e:
    print(f"Error: {e}")

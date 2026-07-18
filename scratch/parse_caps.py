import json

log_data = open(r'C:\Users\abhis\.gemini\antigravity\brain\79f96371-058e-4372-8f0d-3f421f49b353\.system_generated\tasks\task-2042.log', 'r').read()
start = log_data.find('Successfully fetched models:\n') + 29
end = log_data.find('\n\n\nLog:')
data = json.loads(log_data[start:end].strip())

print('Model Properties:')
for m in data.get('models', []):
    print(f"\n--- {m['slug']} ---")
    print(f"Max Tokens (Context): {m.get('max_tokens')}")
    print(f"Capabilities: {list(m.get('capabilities', {}).keys())}")
    print(f"Reasoning Type: {m.get('reasoning_type')}")
    print(f"Thinking Efforts: {m.get('thinking_efforts')}")
    print(f"Configurable Thinking: {m.get('configurable_thinking_effort')}")

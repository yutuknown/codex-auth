import json
import os
import sys
import urllib.request

API_URL = "http://127.0.0.1:8000/v1/chat/completions"

def call_api(prompt):
    data = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}]
    }
    req = urllib.request.Request(API_URL, data=json.dumps(data).encode('utf-8'), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode('utf-8'))
        return result['choices'][0]['message']['content']

def extract_json(content):
    # Find the bounds of the JSON array
    start = content.find('[')
    end = content.rfind(']')
    if start != -1 and end != -1:
        try:
            return json.loads(content[start:end+1])
        except:
            pass
            
    raise ValueError(f"Could not extract JSON from response.\nContent: {content}")

def extract_code(content):
    lines = content.strip().split('\n')
    # Remove ChatGPT UI buttons from the top of the code block if present
    if len(lines) > 0 and lines[0].strip() in ["Python", "JSON", "JavaScript", "HTML", "CSS", "Bash", "Shell", "TypeScript", "React", "Run", "Copy code", "python", "json"]:
        lines.pop(0)
    if len(lines) > 0 and lines[0].strip() in ["Run", "Copy code"]:
        lines.pop(0)
        
    return '\n'.join(lines).strip()

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/auto_coder.py 'Project description'")
        sys.exit(1)
        
    project_idea = sys.argv[1]
    
    # 1. Architecture Phase
    print(f"[*] Brainstorming architecture for: {project_idea}")
    arch_prompt = f"""
I want to build this project: "{project_idea}"
Provide the file and folder architecture required to build this project.
Output ONLY a JSON array of objects. Each object should represent a file to be created, with the keys:
- "path": The file path (e.g., "src/main.py" or "index.html")
- "description": A short description of what this file does.

DO NOT output any conversational text. Output ONLY the raw JSON array.
"""
    try:
        arch_response = call_api(arch_prompt)
        files = extract_json(arch_response)
        print(f"[*] Architecture received. Planning to create {len(files)} files.")
    except Exception as e:
        print(f"[!] Failed to generate architecture: {e}")
        sys.exit(1)
    
    # 2. Implementation Phase
    sandbox_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sandbox")
    os.makedirs(sandbox_dir, exist_ok=True)
    print(f"[*] Target sandbox directory: {sandbox_dir}")
    
    for f in files:
        filepath = f.get('path')
        desc = f.get('description')
        if not filepath:
            continue
            
        print(f"\n[*] Generating code for {filepath}...")
        
        code_prompt = f"""
We are building a project with the following concept: "{project_idea}"
Write the complete code for the file `{filepath}`.
This file's purpose is: {desc}

Output ONLY the raw code inside a markdown block. Do not include any explanations, greetings, or conversational text. Just the code block.
"""
        try:
            code_response = call_api(code_prompt)
            code = extract_code(code_response)
            
            # 3. File I/O
            full_path = os.path.join(sandbox_dir, filepath)
            
            # Prevent path traversal outside sandbox
            if not os.path.abspath(full_path).startswith(os.path.abspath(sandbox_dir)):
                print(f"    [!] Skipping {filepath}: Path traversal detected.")
                continue
                
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            with open(full_path, "w", encoding="utf-8") as out:
                out.write(code)
                
            print(f"    [+] Saved {filepath} to sandbox.")
        except Exception as e:
            print(f"    [!] Failed to generate {filepath}: {e}")
            
    print("\n[*] Project generation complete! Check the sandbox folder.")

if __name__ == "__main__":
    main()

# Custom Rules

### Safe Codex Configuration Management
When writing scripts or tools that manage Codex configuration files (e.g., `auth.json`), **never** default to directly updating the root `~/.codex` directory. Instead, the tool should create and write to a `.codex` folder located inside the local repository (e.g., using `pathlib.Path(__file__).resolve().parent.parent / ".codex"` or similar repository-relative path resolution). This ensures that testing the tool does not accidentally overwrite the user's active global Codex credentials.

### ChatGPT Session Management
When building or modifying the Stealth API (`src/api.py`), **do not** start a new ChatGPT session for every single API request. Creating a new chat for every request severely clogs the user's ChatGPT history sidebar.
Instead, map the stateless OpenAI API `messages` array to the stateful ChatGPT UI:
- If the `messages` array contains only 1 message (or only a system prompt + 1 user prompt), start a **new chat**.
- If the `messages` array contains multiple messages, assume it is a continuation of the current session. Do **not** navigate to a new chat. Extract only the **last** user message and send it in the currently active chat.
This prevents chat history spam while maintaining OpenAI API compatibility.

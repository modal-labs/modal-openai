"""Generated OpenAI Agents API configuration."""

import modal

# Modal Secret containing OPENAI_API_KEY and OPENAI_EXECUTOR_API_KEY
OPENAI_SECRET_NAME = "openai-agents-test-agent"

# OpenAI Agent ID (create via API or CLI)
OPENAI_AGENT_ID = "agent_auto"

# Additional Modal Secrets for the sandbox
WORKER_SECRET_NAMES = ()

# Sandbox configuration
pool = {
    "name": "test-agent",
    "agent_id": OPENAI_AGENT_ID,
    "image": modal.Image.debian_slim(python_version="3.14")
        .apt_install("git", "nodejs", "npm", "ripgrep")
        .run_commands("npm install -g @openai/codex@alpha"),
    # Uncomment to add GPU:
    # "gpu": "A10G",
    # "cpu": 4,
    # "memory": 16384,
}

"""Sample pool: set OPENAI_AGENT_ID to a real preview agent before deploying."""

import os

import modal

from modal_agents.pool import Pool

pool = Pool(
    name="test-agent",
    agent_id=os.environ["OPENAI_AGENT_ID"],
    image=(
        modal.Image.debian_slim()
        .apt_install("git", "nodejs", "npm", "ripgrep")
        # Pin a tested executor version before production use.
        .run_commands("npm install -g @openai/codex@alpha", "mkdir -p /workspace")
    ),
    cpu=2,
    memory=4096,
    timeout=1800,
    # gpu="A10G",
    # worker_secret_names=("my-data-secret",),
)

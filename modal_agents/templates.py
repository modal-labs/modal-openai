"""Source templates for generated pool files."""

POOL_CONFIG = '''"""Editable sandbox configuration for one OpenAI agent."""

import modal

from modal_agents.pool import Pool

pool = Pool(
    name=$pool_name,
    agent_id=$agent_id,
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
'''

WEBHOOK_HANDLER = '''"""Deploy this pool's signed webhook and sandbox reconciliation worker."""

from pathlib import Path

from modal_agents.pool import load_pool
from modal_agents.server import build_app

root = Path(__file__).resolve().parents[1]
pool = load_pool(root / "agents" / ($pool_name + ".py"))
app = build_app(pool, root / "agents" / ($pool_name + "_executor.sh"))
'''

EXECUTOR_WRAPPER = """#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT_ID="${1:?Usage: executor.sh ENVIRONMENT_ID}"
: "${CODEX_API_KEY:?Must set CODEX_API_KEY to the restricted executor key}"
WORKSPACE="${MODAL_AGENTS_WORKSPACE:-/workspace}"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

exec codex exec-server \\
    --remote https://api.openai.com/v1/agents/api \\
    --environment-id "$ENVIRONMENT_ID"
"""

"""Deploy this pool's signed webhook and sandbox reconciliation worker."""

from pathlib import Path

from modal_agents.pool import load_pool
from modal_agents.server import build_app

root = Path(__file__).resolve().parents[1]
pool = load_pool(root / "agents" / ("test-agent" + ".py"))
app = build_app(pool, root / "agents" / ("test-agent" + "_executor.sh"))

import sys
from pathlib import Path

# Add the project root to the python path so it can find run_ingest and pipeline modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_ingest import IngestHandler

# Vercel's Python runtime requires a class named "handler" that inherits from BaseHTTPRequestHandler.
# We just subclass your existing IngestHandler and hardcode the mode to "mock".
class handler(IngestHandler):
    mode = "mock"

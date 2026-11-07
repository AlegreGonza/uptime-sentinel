import os
import sys
from pathlib import Path

os.environ.setdefault("CHECKER_FUNCTION", "uptime-checker-test")
os.environ.setdefault("TARGETS_TABLE", "uptime-targets-test")
os.environ.setdefault("RESULTS_TABLE", "uptime-checks-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ALERTS_TOPIC", "arn:aws:sns:us-east-1:000000000000:test")

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
"""Run the local overlay service.

    python service_run.py

is equivalent to:

    uvicorn detective.service:app --host 127.0.0.1 --port 8756

See detective/service.py for the full /scan + /events/{job_id} contract and
event vocabulary. Loads a local .env (if python-dotenv is installed and a
.env file exists) so a real scan can pick up explicitly configured search or
reasoning providers. detective/service.py itself never touches dotenv, so
importing it directly from tests stays hermetic regardless of what is in .env.

No em dashes (house rule).
"""

from __future__ import annotations

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import uvicorn

from detective.service import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("detective.service:app", host=HOST, port=PORT, reload=False, log_level="info")

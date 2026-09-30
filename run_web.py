"""Start the dashboard. Usage: python run_web.py  ->  http://127.0.0.1:8000"""
import uvicorn

from app.config import env
from app.single_instance import ensure_single_instance

if __name__ == "__main__":
    ensure_single_instance("The dashboard", 8766)
    host = env("WEB_HOST", "127.0.0.1")
    port = int(env("WEB_PORT", "8000"))
    print(f"Dashboard: http://{host}:{port}")
    uvicorn.run("app.web:app", host=host, port=port, reload=False)

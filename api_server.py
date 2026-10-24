from __future__ import annotations

import uvicorn

from backend.app import app


if __name__ == '__main__':
    uvicorn.run('api_server:app', host='127.0.0.1', port=8000, reload=True)

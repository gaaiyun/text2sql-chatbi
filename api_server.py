"""兼容入口：保留 v1 的 `uvicorn api_server:app` 启动方式（n8n 集成文档仍在使用）。

新部署建议使用 `python -m text2sql serve`。
"""

import os

from text2sql.api.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))

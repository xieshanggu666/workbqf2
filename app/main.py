import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.api.router import router
from app.core.database import Base, engine
from app.core.config import PORT

# 启动时确保数据表存在（init_db.py 会调用 create_all，这里同样兜底）
Base.metadata.create_all(bind=engine)

app = FastAPI(title="流域洪水预报与水库群联合调度系统")
app.include_router(router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
STATIC_DIR = os.path.abspath(STATIC_DIR)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
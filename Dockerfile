# text2sql：FastAPI 接口（默认）或 Streamlit 界面，同一个镜像。
#   docker compose up          # 接口 :8000，界面 :8501
# 不配置数据库与模型时使用镜像内生成的合成演示库，只启用语义层路径。

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md LICENSE streamlit_app.py api_server.py ./
COPY text2sql ./text2sql
COPY app_pages ./app_pages
COPY .streamlit/config.toml ./.streamlit/config.toml

# 构建时生成演示库，容器启动不再等待；以非 root 用户运行
RUN python -m text2sql build-demo \
    && useradd --create-home --uid 10001 app \
    && chown -R app /app/data
USER app

EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python", "-m", "text2sql", "serve", "--host", "0.0.0.0", "--port", "8000"]

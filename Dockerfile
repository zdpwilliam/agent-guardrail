# 单镜像双角色：网关（含控制台）与商城共用一份代码，compose 里起两个服务。
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY src ./src
COPY shop ./shop
COPY policies ./policies
COPY eval ./eval
COPY Makefile ./
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "guardrail.main:app", "--host", "0.0.0.0", "--port", "8000"]

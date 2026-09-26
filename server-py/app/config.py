# server-py/app/config.py
# 统一配置入口：所有环境变量从这里读取，业务代码不直接用 os.environ
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 【密钥统一管理】真实密钥只放**仓库根目录**的 .env（已被 .gitignore 忽略）：
#   1) 先读当前工作目录的 .env（兼容 server-py/.env 的老用法，本地文件优先）
#   2) 再读仓库根目录的 .env 作为兜底，且不覆盖已存在的变量
# 这样 docker-compose（读根目录 .env）和直接跑 uvicorn 用的是同一份配置，
# 不会出现"容器里能跑、本地跑不起来"或者"key 抄了两份、改了一处另一处没改"。
load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


class _AppConfig:
    port = int(os.getenv("PORT", "3000"))
    env = os.getenv("NODE_ENV", "development")
    allowed_origins = (os.getenv("ALLOWED_ORIGINS") or "http://localhost:5173").split(",")


class _AiConfig:
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    zhipu_key = os.getenv("ZHIPU_API_KEY")
    primary_model = os.getenv("PRIMARY_MODEL", "deepseek-chat")
    embed_model = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    base_url = "https://api.deepseek.com/v1"
    embed_base_url = os.getenv("EMBED_BASE_URL", "https://api.siliconflow.cn/v1")


class _ChromaConfig:
    url = os.getenv("CHROMA_URL", "http://localhost:8000")


class _CacheConfig:
    ttl = int(os.getenv("CACHE_TTL", "1800000"))  # 30 分钟（毫秒）


class Config:
    app = _AppConfig()
    ai = _AiConfig()
    chroma = _ChromaConfig()
    cache = _CacheConfig()


config = Config()


def validate_config():
    if not config.ai.deepseek_key:
        print("❌ 缺少 DEEPSEEK_API_KEY，请在 .env 文件中配置", file=sys.stderr)
        sys.exit(1)
    print("✓ 配置校验通过")

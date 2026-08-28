"""加载配置：从环境变量/ .env 读取模型调用配置"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# __file__：当前文件config.py自己的路径
# resolve 转成绝对路径
# parents 往上数n级
# 这里相当于从config回到src再回到hy3项目根目录
# 不管在哪里运行都能定位到这个位置
# 等价操作：
# os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# load_dotenv是把 `.env` 文件转为环境变量的工具
# 运行之后，os.getenv才能取到
# .env 文件 ──load_dotenv──▶ 环境变量 ──os.getenv──▶ Settings 对象 ──▶ Hy3Client
load_dotenv(PROJECT_ROOT / ".env")

# dataclass 样板类，自动装载__init__等function
@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    model_name: str
    reasoning_effort: str
    temperature: float
    top_p: float
    timeout_seconds: int
    max_retries: int

def load_settings() -> Settings:
    """读取配置，缺少必填项时给出可操作的报错"""
    
    base_url = os.getenv("HY3_BASE_URL","")
    api_key = os.getenv("HY3_API_KEY","")
    
    if not base_url or not api_key:
        raise RuntimeError(
            "HY3_BASE_URL / HY3_API_KEY 未配置："
            "请复制 .env.example 为 .env 并填写真实值"
        )
    
    return Settings(
        base_url=base_url,
        api_key=api_key,
        model_name=os.getenv("HY3_MODEL_NAME","hy3"),
        reasoning_effort=os.getenv("HY3_REASONING_EFFORT","high"),
        temperature=float(os.getenv("HY3_TEMPERATURE","0.9")),
        top_p=float(os.getenv("HY3_TOP_P","1.0")),
        timeout_seconds=int(os.getenv("HY3_TIMEOUT_SECONDS","300")),
        max_retries=int(os.getenv("HY3_MAX_RETRIES","3"))
    )
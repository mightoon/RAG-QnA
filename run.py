"""
RAG 智能问答平台 — 启动入口

用法：
    python run.py                          # 使用默认配置 customer/customer_config.yaml
    python run.py --config path/to/cfg.yaml
    python run.py --host 0.0.0.0 --port 8000 --workers 1
    python run.py --noconnection           # 演示模式：不连接任何外部服务
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG IQA Platform")
    parser.add_argument("--config", "-c", default="customer/customer_config.yaml",
                        help="配置文件路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--noconnection", "-n", action="store_true",
                        help="演示模式：不连接 LLM/向量库/ES/MySQL/Redis 等"
                             "外部服务，全部使用本地 Mock 实现，界面可正常"
                             "访问（数据不持久化）")
    args = parser.parse_args()

    import os

    import uvicorn
    from rag.api.app import create_app
    from rag.config.loader import load_config

    config = load_config(args.config)
    config.noconnection = args.noconnection
    app = create_app(config)

    if args.reload:
        # reload 模式经工厂函数启动，配置路径经环境变量传递
        os.environ["RAG_CONFIG"] = args.config
        if args.noconnection:
            os.environ["RAG_NOCONNECTION"] = "1"

    uvicorn.run(
        app if not args.reload else "rag.api.app:create_app_from_env",
        host=args.host,
        port=args.port,
        workers=args.workers if not args.reload else 1,
        reload=args.reload,
        factory=bool(args.reload),
        log_config=None,
    )


if __name__ == "__main__":
    main()

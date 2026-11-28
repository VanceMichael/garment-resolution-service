"""本地启动入口。

    python3 -m service.server --port 8000 --state aftercare.sqlite3 --materials data/catalog.json
    python3 -m service.server --port 8000 --seed

`--seed` 与 `--materials` 等价，默认导入 data/catalog.json；导入可重复执行。
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):  # 支持 python3 service/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from service import materials  # type: ignore
    from service.http_api import build_server  # type: ignore
    from service.store import Store  # type: ignore
else:
    from . import materials
    from .http_api import build_server
    from .store import Store

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="服装批次争议售后办理 HTTP 服务（合成资料）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state", default=str(ROOT / "aftercare.sqlite3"), help="SQLite 状态文件；重启用同一文件继续查询")
    parser.add_argument("--materials", default=str(ROOT / "data" / "catalog.json"), help="合成资料文件")
    parser.add_argument("--seed", action="store_true", help="启动时导入合成资料（可重复）")
    parser.add_argument("--partner-url", default=None, help="协作方回放器地址，如 http://127.0.0.1:8765")
    parser.add_argument("--no-seed", action="store_true", help="不导入资料")
    args = parser.parse_args(argv)

    store = Store(args.state)
    store.initialize()
    if not args.no_seed and Path(args.materials).exists():
        result = materials.import_catalog(store, args.materials, actor_id="system:seed")
        print(json.dumps({"seeded": result}, ensure_ascii=False), file=sys.stderr, flush=True)
    server = build_server(store, host=args.host, port=args.port, partner_url=args.partner_url,
                          materials_path=args.materials)
    print(json.dumps({"host": args.host, "port": server.server_address[1], "state": args.state,
                      "partner_url": args.partner_url, "synthetic": True}, ensure_ascii=False), flush=True)

    def stop(signum, frame):
        # shutdown() 等待 serve_forever 退出；必须由其他线程调用，否则主线程在信号处理中互锁。
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

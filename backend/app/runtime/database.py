"""图 Worker 的有界 MySQL I/O；与监督/通知进程分别保有连接容量。"""

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def graph_session_factory(url):
    if make_url(url).get_backend_name() != "mysql":
        raise ValueError("图运行需要 MySQL，不支持内存数据库替代事务语义")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=3,
        max_overflow=1,
        pool_timeout=3,
        connect_args={"connect_timeout": 3, "read_timeout": 5, "write_timeout": 5},
    )
    return sessionmaker(engine, expire_on_commit=False)

"""使用 P0 已验证的隔离数据库生命周期执行 P1 测试，不调用真实模型。"""

import sys

from scripts.run_p0_tests import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(["tests/graph", "--junitxml=../artifacts/p1/graph.xml"])
    raise SystemExit(main())

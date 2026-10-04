import os

# 测试不依赖 postgres：在导入 app 之前把引导引擎指到本地 sqlite。
# 各用例通过 dependency_overrides 使用自己的内存库（StaticPool）。
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SEED_ON_EMPTY", "false")

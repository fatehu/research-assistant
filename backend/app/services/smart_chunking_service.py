"""
智能分块策略服务 - 向后兼容 shim

本文件是旧的 smart_chunking_service.py 的替代品。
所有逻辑已拆分到 app.services.smart_chunking 包中：

  smart_chunking/
  ├── types.py               # 枚举、数据类、异常  (~130 行)
  ├── academic_detector.py    # 学术结构检测        (~110 行)
  ├── text_preprocessor.py    # 分句、OCR 降噪      (~180 行)
  ├── semantic_chunker.py     # V2 语义分块器       (~200 行)
  ├── hierarchical_chunker.py # 层级分块器          (~300 行)
  ├── service.py              # 主服务 & 工厂       (~350 行)
  └── __init__.py             # 公共导出

所有 ``from app.services.smart_chunking_service import X`` 的代码
无需任何修改即可继续工作。
"""

# 从包中导入并重导出所有公共名称
from app.services.smart_chunking import *  # noqa: F401, F403

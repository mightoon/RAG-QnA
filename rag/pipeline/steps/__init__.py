"""
Pipeline 步骤实现（import 副作用注册到 StepRegistry）

入库：ingest_parse / ingest_chunk / ingest_write
查询：query_understand / query_retrieve / query_generate
"""
from . import ingest_parse          # noqa: F401
from . import ingest_chunk          # noqa: F401
from . import ingest_write          # noqa: F401
from . import query_understand      # noqa: F401
from . import query_retrieve        # noqa: F401
from . import query_self_eval       # noqa: F401
from . import query_generate        # noqa: F401
